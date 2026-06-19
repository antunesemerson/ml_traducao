from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_semantic_short_label_pair_checkpoint import AGENT_KEY, PRIMARY_FAMILY


RULE_VERSION = "issue_semantic_short_label_pair_blocked_context_audit_queue_v1"
QUEUE_STRATEGY = "semantic_short_label_pair_blocked_context_audit"
DEFAULT_BLOCK_REASON = "ui_only_v10_missing_explicit_ui_signal"
DEFAULT_LIMIT = 160
DECISION_OPTIONS = [
    "safe_short_label",
    "needs_domain_context",
    "needs_repair",
    "needs_new_microagent",
    "manual_exception",
]


def latest_guarded_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No semantic guarded expansion dry-run found.")
    return int(row["id"])


def latest_bridge_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No short-label pure no-token bridge proposal run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_pair_blocked_context_audit_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def top_package(relative_path: str | None) -> str:
    value = str(relative_path or "unknown").replace("\\", "/")
    return value.split("/", 1)[0] if "/" in value else "root"


def priority_score(row: dict[str, Any]) -> float:
    text = str(row.get("text_sample") or "")
    key = str(row.get("source_key") or "")
    score = 10.0
    score += min(float(row.get("char_count") or len(text)) / 80.0, 3.0)
    if key.isupper():
        score += 1.0
    if any(part in key.lower() for part in ("title", "name", "label", "modifier", "attribute", "status", "history")):
        score += 1.0
    if any(marker in text for marker in ("[", "]", "$", "#")):
        score += 0.5
    return score


def fetch_runs(conn, *, dry_run_id: int, bridge_run_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    dry_run = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    bridge_run = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs
        WHERE id = ?
        """,
        (bridge_run_id,),
    ).fetchone()
    if dry_run is None:
        raise RuntimeError(f"Dry-run not found: {dry_run_id}")
    if bridge_run is None:
        raise RuntimeError(f"Bridge proposal run not found: {bridge_run_id}")
    return dict(dry_run), dict(bridge_run)


def fetch_candidates(
    conn,
    *,
    dry_run: dict[str, Any],
    bridge_run: dict[str, Any],
    block_reason: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = ?
                AND queued.queue_bucket LIKE 'blocked_context:%'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.segment_id = item.segment_id
                AND decision.agent_key = ?
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
          )
        """
    )
    params: list[Any] = [
        int(dry_run["ledger_run_id"]),
        PRIMARY_FAMILY,
        int(bridge_run["id"]),
        int(dry_run["id"]),
        block_reason,
    ]
    if not include_existing:
        params.extend([AGENT_KEY, AGENT_KEY])
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            ledger.id AS ledger_item_id,
            ledger.issue_kind,
            source.english_text,
            source.spanish_text,
            COALESCE(confirm.confirmed_text, output.portuguese_text, item.text_sample, '') AS confirmed_text
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_items item
        JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = ?
         AND ledger.segment_id = item.segment_id
         AND ledger.issue_family = ?
        JOIN ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items bridge
          ON bridge.run_id = ?
         AND bridge.segment_id = item.segment_id
         AND bridge.bridge_status = 'blocked'
        JOIN source_segments source
          ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirm
          ON confirm.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.dry_run_allowed = 0
          AND item.classifier_decision = 'needs_domain_context'
          AND item.block_reason = ?
          {existing_filter}
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_stratified(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(rows, key=lambda item: (-priority_score(item), item["relative_path"], item["source_line_number"] or 0)):
        row["package"] = top_package(row.get("relative_path"))
        row["priority_score"] = priority_score(row)
        row["queue_bucket"] = f"blocked_context:{row['block_reason']}"
        row["suggested_decision"] = "audit_blocked_context_guard"
        buckets[row["package"]].append(row)

    selected: list[dict[str, Any]] = []
    packages = deque(sorted(buckets, key=lambda key: (-len(buckets[key]), key)))
    seen_segments: set[int] = set()
    while packages and len(selected) < limit:
        package = packages.popleft()
        bucket = buckets[package]
        while bucket:
            row = bucket.popleft()
            segment_id = int(row["segment_id"])
            if segment_id in seen_segments:
                continue
            selected.append(row)
            seen_segments.add(segment_id)
            break
        if bucket:
            packages.append(package)
    return selected


def insert_queue_run(
    conn,
    *,
    dry_run: dict[str, Any],
    bridge_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    cursor = conn.execute(
        """
        INSERT INTO ml_issue_review_queue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            issue_family,
            queue_strategy,
            limit_count,
            per_bucket,
            selected_count,
            open_count,
            reviewed_count,
            bucket_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            int(dry_run["ledger_run_id"]),
            AGENT_KEY,
            PRIMARY_FAMILY,
            QUEUE_STRATEGY,
            limit,
            limit,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, *, queue_run_id: int, dry_run: dict[str, Any], bridge_run: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        evidence = {
            "dry_run_id": int(dry_run["id"]),
            "dry_run_item_id": int(row["id"]),
            "bridge_proposal_run_id": int(bridge_run["id"]),
            "opportunity_run_id": int(dry_run["opportunity_run_id"]),
            "profile": row["profile"],
            "classifier_decision": row["classifier_decision"],
            "classifier_reason": row["classifier_reason"],
            "block_reason": row["block_reason"],
            "queue_strategy": QUEUE_STRATEGY,
            "char_count": int(row.get("char_count") or 0),
            "token_count": int(row.get("token_count") or 0),
            "package": row["package"],
            "purpose": "Audit whether this blocked reason can be relaxed or needs a new semantic/domain microagent.",
        }
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_review_queue_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                issue_kind,
                agent_key,
                queue_bucket,
                priority_score,
                review_status,
                suggested_decision,
                evidence_text,
                evidence_json,
                english_text,
                spanish_text,
                confirmed_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_run_id,
                int(dry_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                PRIMARY_FAMILY,
                row.get("issue_kind") or "semantic_short_label_context_blocked",
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                row.get("text_sample") or "",
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            ),
        )
        row["queue_item_id"] = int(cursor.lastrowid)


def trim(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    dry_run: dict[str, Any],
    bridge_run: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    block_reason: str,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    candidate_packages = Counter(top_package(row.get("relative_path")) for row in candidates)
    selected_packages = Counter(row["package"] for row in selected)
    fields = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "profile",
        "block_reason",
        "package",
        "char_count",
        "token_count",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("text_sample") or ""
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("text_sample") or ""
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row["segment_id"],
                "decision": "pending",
                "corrected_text": "",
                "notes": "",
                "decision_options": DECISION_OPTIONS,
                "review_hint": (
                    "Decide if the block_reason is overly conservative. Use safe_short_label only when confirmed/output text is natural PT-BR, "
                    "matches English meaning, preserves CK3 tokens and does not need domain context."
                ),
                "block_reason": row.get("block_reason"),
                "evidence_text": row.get("text_sample") or "",
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "confirmed_text": row.get("confirmed_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Semantic + short-label blocked context audit queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Guarded dry-run id: {dry_run['id']}",
        f"Bridge proposal run id: {bridge_run['id']}",
        f"Ledger run id: {dry_run['ledger_run_id']}",
        f"Agent key: {AGENT_KEY}",
        f"Queue strategy: {QUEUE_STRATEGY}",
        f"Target block reason: {block_reason}",
        "",
        "Summary:",
        f"- candidates after filters: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        "",
        "Candidate packages:",
        *[f"- {key}: {value:,}" for key, value in candidate_packages.most_common(20)],
        "",
        "Selected packages:",
        *[f"- {key}: {value:,}" for key, value in selected_packages.most_common(20)],
        "",
        "Samples:",
    ]
    for row in selected[:50]:
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number')} | "
            f"{row['source_key']} | {row['block_reason']} | score={row['priority_score']:.2f} | "
            f"{trim(row.get('text_sample'), 160)}"
        )
    lines.extend(
        [
            "",
            "Decision options:",
            *[f"- {option}" for option in DECISION_OPTIONS],
            "",
            "Safety note:",
            "- Queue only: no source/output file reads, no confirmation updates, no model promotion and no output writes.",
            "- This queue audits blocked candidates, so it must feed evidence before any guard relaxation.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    dry_run_id: int | None = None,
    bridge_run_id: int | None = None,
    block_reason: str = DEFAULT_BLOCK_REASON,
    limit: int = DEFAULT_LIMIT,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_dry_run_id = dry_run_id or latest_guarded_dry_run_id(conn)
        selected_bridge_run_id = bridge_run_id or latest_bridge_run_id(conn)
        dry_run, bridge_run = fetch_runs(conn, dry_run_id=selected_dry_run_id, bridge_run_id=selected_bridge_run_id)
        candidates = fetch_candidates(
            conn,
            dry_run=dry_run,
            bridge_run=bridge_run,
            block_reason=block_reason,
            include_existing=include_existing,
        )
        selected = select_stratified(candidates, limit=limit)
        paths = report_paths(settings)
        queue_run_id = insert_queue_run(
            conn,
            dry_run=dry_run,
            bridge_run=bridge_run,
            selected=selected,
            paths=paths,
            limit=limit,
        )
        insert_items(conn, queue_run_id=queue_run_id, dry_run=dry_run, bridge_run=bridge_run, rows=selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        dry_run=dry_run,
        bridge_run=bridge_run,
        candidates=candidates,
        selected=selected,
        block_reason=block_reason,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_semantic_short_label_pair_blocked_context_audit_queue] Queue generated")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Queue run id: {queue_run_id}")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Dry-run id: {selected_dry_run_id}")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Bridge proposal run id: {selected_bridge_run_id}")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Candidates after filters: {len(candidates):,}")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Selected: {len(selected):,}")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Report: {txt_path}")
    print(f"[issue_semantic_short_label_pair_blocked_context_audit_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "dry_run_id": selected_dry_run_id,
        "bridge_run_id": selected_bridge_run_id,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an audit queue for blocked semantic + short-label context decisions.")
    parser.add_argument("--dry-run-id", type=int)
    parser.add_argument("--bridge-run-id", type=int)
    parser.add_argument("--block-reason", default=DEFAULT_BLOCK_REASON)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        dry_run_id=args.dry_run_id,
        bridge_run_id=args.bridge_run_id,
        block_reason=args.block_reason,
        limit=args.limit,
        include_existing=args.include_existing,
    )
