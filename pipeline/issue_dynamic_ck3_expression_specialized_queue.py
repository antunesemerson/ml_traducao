from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_review_queue import (
    DYNAMIC_BUCKET_ORDER,
    decision_options_for_agent,
    package_name,
    parse_json,
    priority_score,
    queue_bucket,
    review_guidance_for_agent,
    short,
    suggested_decision,
)


RULE_VERSION = "issue_dynamic_ck3_expression_specialized_queue_v1"
AGENT_KEY = "micro_dynamic_ck3_expression"
ISSUE_FAMILY = "dynamic_ck3_expression_microagent"
QUEUE_STRATEGY = "stratified_issue_ledger"
DEFAULT_ISSUE_KIND = "custom_localization_expression"
DEFAULT_LIMIT = 180
DEFAULT_PER_BUCKET = 40


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished ml_issue_ledger_runs found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], issue_kind: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_kind = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in issue_kind)
    base = reports_dir / f"{stamp}_issue_dynamic_ck3_expression_specialized_queue_{safe_kind}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def existing_ledger_item_ids(conn, *, ledger_run_id: int, issue_kind: str) -> set[int]:
    rows = conn.execute(
        """
        SELECT queued.ledger_item_id
        FROM ml_issue_review_queue_items queued
        JOIN ml_issue_review_queue_runs run ON run.id = queued.run_id
        JOIN ml_issue_ledger_items ledger ON ledger.id = queued.ledger_item_id
        WHERE queued.ledger_run_id = ?
          AND run.agent_key = ?
          AND run.issue_family = ?
          AND ledger.issue_kind = ?
        """,
        (ledger_run_id, AGENT_KEY, ISSUE_FAMILY, issue_kind),
    ).fetchall()
    return {int(row["ledger_item_id"]) for row in rows}


def existing_segment_ids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT queued.segment_id
        FROM ml_issue_review_queue_items queued
        JOIN ml_issue_review_queue_runs run ON run.id = queued.run_id
        WHERE run.agent_key = ?
          AND run.issue_family = ?
        """,
        (AGENT_KEY, ISSUE_FAMILY),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def fetch_candidates(
    conn,
    *,
    ledger_run_id: int,
    issue_kind: str,
    include_existing: bool,
    exclude_existing_segments: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = [ledger_run_id, AGENT_KEY, ISSUE_FAMILY, issue_kind]
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_ledger_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.agent_key = ?
          AND item.issue_family = ?
          AND item.issue_kind = ?
          AND item.status = 'open'
        ORDER BY item.segment_id
        """,
        tuple(params),
    ).fetchall()
    candidates = [dict(row) for row in rows]

    if not include_existing:
        existing_ids = existing_ledger_item_ids(conn, ledger_run_id=ledger_run_id, issue_kind=issue_kind)
        candidates = [row for row in candidates if int(row["id"]) not in existing_ids]

    if exclude_existing_segments:
        existing_segments = existing_segment_ids(conn)
        candidates = [row for row in candidates if int(row["segment_id"]) not in existing_segments]

    for row in candidates:
        row["queue_bucket"] = queue_bucket(row)
        row["priority_score"] = priority_score(row)
        row["suggested_decision"] = suggested_decision(row)
    return candidates


def select_rows(candidates: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"]))

    bucket_order = [bucket for bucket in DYNAMIC_BUCKET_ORDER if grouped.get(bucket)]
    bucket_order.extend(sorted(bucket for bucket in grouped if bucket not in set(bucket_order)))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for index in range(per_bucket):
        for bucket in bucket_order:
            rows = grouped[bucket]
            if index >= len(rows):
                continue
            if len(selected) >= limit:
                return selected
            row = rows[index]
            selected.append(row)
            selected_ids.add(int(row["id"]))

    if len(selected) < limit:
        remaining = [row for row in candidates if int(row["id"]) not in selected_ids]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"], item["relative_path"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
            selected_ids.add(int(row["id"]))
    return selected


def insert_queue_run(
    conn,
    *,
    ledger_run_id: int,
    issue_kind: str,
    limit: int,
    per_bucket: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    run_id = conn.execute(
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
            ledger_run_id,
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            limit,
            per_bucket,
            len(selected),
            len(selected),
            json.dumps({"issue_kind": issue_kind, "buckets": dict(bucket_counts.most_common())}, ensure_ascii=False),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    ).lastrowid
    return int(run_id)


def insert_queue_items(conn, *, run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    payload = []
    for row in rows:
        payload.append(
            (
                run_id,
                ledger_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["issue_family"],
                row["issue_kind"],
                row["agent_key"],
                row["queue_bucket"],
                row["priority_score"],
                "pending",
                row["suggested_decision"],
                row.get("evidence_text"),
                row.get("evidence_json"),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            )
        )
    conn.executemany(
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    ledger_run_id: int,
    issue_kind: str,
    rows: list[dict[str, Any]],
    candidates_count: int,
    limit: int,
    per_bucket: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    package_counts = Counter(package_name(row.get("relative_path")) for row in rows)
    domain_counts = Counter(parse_json(row.get("evidence_json"), {}).get("domain") or "domain_unknown" for row in rows)

    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "issue_family",
        "issue_kind",
        "token_impact",
        "token_status",
        "active_action",
        "candidate_action",
        "policy_action",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "ledger_item_id": row["id"],
                    **{name: row.get(name) for name in fieldnames if name not in {"queue_run_id", "ledger_item_id"}},
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_run_id": ledger_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": AGENT_KEY,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "issue_family": ISSUE_FAMILY,
                "issue_kind": row["issue_kind"],
                "token_impact": row["token_impact"],
                "token_status": row["token_status"],
                "active_action": row["active_action"],
                "candidate_action": row["candidate_action"],
                "policy_action": row["policy_action"],
                "evidence": parse_json(row.get("evidence_json"), {}),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        decision_options = decision_options_for_agent(AGENT_KEY)
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "decision": "",
                "decision_options": decision_options,
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic CK3 expression specialized review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Issue kind: {issue_kind}",
        f"Queue strategy: {QUEUE_STRATEGY}",
        "",
        "Coverage:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(rows):,}",
        f"- Limit: {limit:,}",
        f"- Per bucket: {per_bucket:,}",
        "",
        "Buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Domains:",
        *[f"- {domain}: {count:,}" for domain, count in domain_counts.most_common()],
        "",
        "Packages:",
        *[f"- {pkg}: {count:,}" for pkg, count in package_counts.most_common(25)],
        "",
        "Review guidance:",
        *review_guidance_for_agent(AGENT_KEY),
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in rows[:30]:
        lines.append(
            f"- ledger {row['id']} | segment {row['segment_id']} | {row['queue_bucket']} | "
            f"{row['relative_path']}::{row['source_key']} | {row['suggested_decision']}"
        )
        lines.append(f"  evidence: {short(row.get('evidence_text'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    ledger_run_id: int | None = None,
    issue_kind: str = DEFAULT_ISSUE_KIND,
    limit: int = DEFAULT_LIMIT,
    per_bucket: int = DEFAULT_PER_BUCKET,
    include_existing: bool = False,
    exclude_existing_segments: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings, issue_kind)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        candidates = fetch_candidates(
            conn,
            ledger_run_id=selected_ledger_run_id,
            issue_kind=issue_kind,
            include_existing=include_existing,
            exclude_existing_segments=exclude_existing_segments,
        )
        if not candidates:
            raise RuntimeError(f"No candidates found for {issue_kind}.")
        selected = select_rows(candidates, limit=limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            ledger_run_id=selected_ledger_run_id,
            issue_kind=issue_kind,
            limit=limit,
            per_bucket=per_bucket,
            selected=selected,
            paths=paths,
        )
        insert_queue_items(conn, run_id=queue_run_id, ledger_run_id=selected_ledger_run_id, rows=selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        ledger_run_id=selected_ledger_run_id,
        issue_kind=issue_kind,
        rows=selected,
        candidates_count=len(candidates),
        limit=limit,
        per_bucket=per_bucket,
    )

    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_dynamic_ck3_expression_specialized_queue] Queue generated")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Queue run id: {queue_run_id}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Issue kind: {issue_kind}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Candidates: {len(candidates):,}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Selected: {len(selected):,}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Report: {txt_path}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] JSONL: {jsonl_path}")
    print(f"[issue_dynamic_ck3_expression_specialized_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "ledger_run_id": selected_ledger_run_id,
        "issue_kind": issue_kind,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a fast specialized review queue for dynamic CK3 expression issues.")
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--issue-kind", default=DEFAULT_ISSUE_KIND)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument("--exclude-existing-segments", action="store_true")
    args = parser.parse_args()
    main(
        ledger_run_id=args.ledger_run_id,
        issue_kind=args.issue_kind,
        limit=args.limit,
        per_bucket=args.per_bucket,
        include_existing=args.include_existing,
        exclude_existing_segments=args.exclude_existing_segments,
    )
