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


RULE_VERSION = "issue_semantic_short_label_pair_guarded_expansion_audit_queue_v1"
QUEUE_STRATEGY = "semantic_short_label_pair_guarded_expansion_audit"
DEFAULT_PER_PROFILE = 100
PROFILE_ORDER = [
    "pair_no_token_short_label",
    "pair_low_token_short_text",
    "pair_medium_token_short_ui",
]
DECISION_OPTIONS = [
    "safe_short_label",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_pair_guarded_expansion_audit_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def latest_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE finished_at IS NOT NULL
          AND new_allowed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No guarded expansion dry-run found.")
    return int(row["id"])


def fetch_dry_run(conn, *, dry_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded expansion dry-run not found: {dry_run_id}")
    return dict(row)


def top_package(relative_path: str | None) -> str:
    value = relative_path or "unknown"
    return value.split("/", 1)[0] if "/" in value else value


def priority_score(row: dict[str, Any]) -> float:
    profile_weight = {
        "pair_medium_token_short_ui": 10.0,
        "pair_low_token_short_text": 8.0,
        "pair_no_token_short_label": 6.0,
    }.get(str(row.get("profile") or ""), 1.0)
    token_weight = float(row.get("token_count") or 0) * 0.35
    length_weight = min(float(row.get("char_count") or 0) / 120.0, 2.0)
    format_weight = 0.8 if any(marker in str(row.get("text_sample") or "") for marker in ("#", "$", "[", "]")) else 0.0
    return profile_weight + token_weight + length_weight + format_weight


def fetch_candidates(conn, *, dry_run: dict[str, Any], include_existing: bool) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = ?
                AND queued.queue_bucket LIKE 'expansion_audit:%'
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
    params: list[Any] = [int(dry_run["id"])]
    if not include_existing:
        params.extend([AGENT_KEY, AGENT_KEY])
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            ledger.id AS ledger_item_id,
            ledger.issue_kind,
            source.english_text,
            source.spanish_text
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_items item
        JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = ?
         AND ledger.segment_id = item.segment_id
         AND ledger.issue_family = ?
        JOIN source_segments source
          ON source.id = item.segment_id
        WHERE item.run_id = ?
          AND item.dry_run_allowed = 1
          AND item.already_checkpointed = 0
          {existing_filter}
        ORDER BY item.profile, item.relative_path, item.source_line_number, item.source_key
        """,
        (int(dry_run["ledger_run_id"]), PRIMARY_FAMILY, *params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_stratified(rows: list[dict[str, Any]], *, per_profile: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(lambda: defaultdict(deque))
    for row in sorted(rows, key=lambda item: (-priority_score(item), item["relative_path"], item["source_line_number"] or 0, item["source_key"])):
        grouped[str(row["profile"])][top_package(row["relative_path"])].append(row)

    selected: list[dict[str, Any]] = []
    selected_segments: set[int] = set()
    for profile in PROFILE_ORDER:
        package_buckets = grouped.get(profile) or {}
        packages = deque(sorted(package_buckets, key=lambda key: (-len(package_buckets[key]), key)))
        picked = 0
        while packages and picked < per_profile:
            package = packages.popleft()
            bucket = package_buckets[package]
            while bucket:
                row = bucket.popleft()
                segment_id = int(row["segment_id"])
                if segment_id in selected_segments:
                    continue
                row["queue_bucket"] = f"expansion_audit:{profile}"
                row["package"] = package
                row["priority_score"] = priority_score(row)
                row["suggested_decision"] = "audit_guarded_semantic_short_label_expansion"
                selected.append(row)
                selected_segments.add(segment_id)
                picked += 1
                break
            if bucket:
                packages.append(package)
    return selected


def insert_queue_run(
    conn,
    *,
    dry_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    per_profile: int,
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
            per_profile * len(PROFILE_ORDER),
            per_profile,
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


def insert_queue_items(conn, *, queue_run_id: int, dry_run: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        evidence = {
            "dry_run_id": int(dry_run["id"]),
            "dry_run_item_id": int(row["id"]),
            "opportunity_run_id": int(dry_run["opportunity_run_id"]),
            "profile": row["profile"],
            "classifier_decision": row["classifier_decision"],
            "classifier_reason": row["classifier_reason"],
            "paired_families": list(("semantic_review_router", "short_label_style_microagent")),
            "queue_strategy": QUEUE_STRATEGY,
            "char_count": int(row.get("char_count") or 0),
            "token_count": int(row.get("token_count") or 0),
            "package": row["package"],
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
                row.get("issue_kind") or "needs_human_or_semantic_conflict",
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                row.get("text_sample") or "",
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("text_sample"),
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
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    per_profile: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    candidate_profiles = Counter(row["profile"] for row in candidates)
    selected_profiles = Counter(row["profile"] for row in selected)
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
        "package",
        "char_count",
        "token_count",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
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
                    "Audit whether the guarded expansion is truly safe. Use safe_short_label only when PT-BR is natural, "
                    "tokens are intact, and English/Spanish context does not reveal semantic loss."
                ),
                "evidence_text": row.get("text_sample") or "",
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Semantic + short-label guarded expansion audit queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Dry-run id: {dry_run['id']}",
        f"Ledger run id: {dry_run['ledger_run_id']}",
        f"Agent key: {AGENT_KEY}",
        f"Queue strategy: {QUEUE_STRATEGY}",
        f"Per profile: {per_profile}",
        "",
        "Summary:",
        f"- candidates after filters: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        "",
        "Candidate profiles:",
        *[f"- {key}: {value:,}" for key, value in candidate_profiles.most_common()],
        "",
        "Selected profiles:",
        *[f"- {key}: {value:,}" for key, value in selected_profiles.most_common()],
        "",
        "Selected packages:",
        *[f"- {key}: {value:,}" for key, value in selected_packages.most_common(20)],
        "",
        "Samples:",
    ]
    for row in selected[:40]:
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number')} | "
            f"{row['source_key']} | {row['profile']} | score={row['priority_score']:.2f} | "
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
            "- This queue audits candidates already allowed by the guarded expansion dry-run.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, dry_run_id: int | None = None, per_profile: int = DEFAULT_PER_PROFILE, include_existing: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_dry_run_id = dry_run_id or latest_dry_run_id(conn)
        dry_run = fetch_dry_run(conn, dry_run_id=selected_dry_run_id)
        candidates = fetch_candidates(conn, dry_run=dry_run, include_existing=include_existing)
        selected = select_stratified(candidates, per_profile=per_profile)
        paths = report_paths(settings)
        queue_run_id = insert_queue_run(
            conn,
            dry_run=dry_run,
            selected=selected,
            paths=paths,
            per_profile=per_profile,
        )
        insert_queue_items(conn, queue_run_id=queue_run_id, dry_run=dry_run, rows=selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        dry_run=dry_run,
        candidates=candidates,
        selected=selected,
        per_profile=per_profile,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Queue generated")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Queue run id: {queue_run_id}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Dry-run id: {selected_dry_run_id}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Candidates after filters: {len(candidates):,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Selected: {len(selected):,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Report: {txt_path}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] CSV: {csv_path}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] JSONL: {jsonl_path}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_audit_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "dry_run_id": selected_dry_run_id,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an audit queue for guarded semantic + short-label expansion.")
    parser.add_argument("--dry-run-id", type=int, default=None)
    parser.add_argument("--per-profile", type=int, default=DEFAULT_PER_PROFILE)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(dry_run_id=args.dry_run_id, per_profile=args.per_profile, include_existing=args.include_existing)
