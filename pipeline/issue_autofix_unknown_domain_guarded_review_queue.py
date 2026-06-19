from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_autofix_unknown_domain_guarded_review_queue_v1"
AGENT_KEY = "micro_autofix_unknown_router"
ISSUE_FAMILY = "autofix_unknown_microagent"
QUEUE_STRATEGY = "autofix_unknown_domain_guarded_shadow_review"


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_allowed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished autofix unknown domain guarded checkpoint run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_domain_guarded_review_queue_checkpoint_{checkpoint_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def fetch_candidates(conn, *, checkpoint_run_id: int, include_existing: bool) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.ledger_item_id = checkpoint.ledger_item_id
                AND decision.agent_key = ?
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
          )
        """
    )
    params: list[Any] = [checkpoint_run_id]
    if not include_existing:
        params.append(AGENT_KEY)
    rows = conn.execute(
        f"""
        SELECT
            checkpoint.*,
            ledger.evidence_json,
            ledger.evidence_text AS ledger_evidence_text,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_items checkpoint
        JOIN ml_issue_ledger_items ledger ON ledger.id = checkpoint.ledger_item_id
        JOIN source_segments source ON source.id = checkpoint.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = checkpoint.segment_id
        WHERE checkpoint.checkpoint_run_id = ?
          AND checkpoint.checkpoint_allowed = 1
          {existing_filter}
        ORDER BY checkpoint.subpolicy_name, checkpoint.text_length DESC, checkpoint.word_count DESC, checkpoint.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_rows(rows: list[dict[str, Any]], *, limit: int, per_subpolicy: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["subpolicy_name"])].append(row)
    for items in grouped.values():
        items.sort(
            key=lambda row: (
                -int(row.get("text_length") or 0),
                -int(row.get("word_count") or 0),
                -int(row.get("token_count") or 0),
                str(row.get("relative_path") or ""),
                int(row.get("segment_id") or 0),
            )
        )

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    order = sorted(grouped.keys())
    for index in range(per_subpolicy):
        for subpolicy in order:
            bucket = grouped.get(subpolicy, [])
            if index >= len(bucket):
                continue
            if len(selected) >= limit:
                return selected
            row = bucket[index]
            selected.append(row)
            selected_ids.add(int(row["id"]))

    if len(selected) < limit:
        remaining = [row for row in rows if int(row["id"]) not in selected_ids]
        remaining.sort(
            key=lambda row: (
                -int(row.get("text_length") or 0),
                -int(row.get("word_count") or 0),
                -int(row.get("token_count") or 0),
            )
        )
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
    return selected


def insert_queue_run(
    conn,
    *,
    checkpoint_run: dict[str, Any],
    selected: list[dict[str, Any]],
    limit: int,
    per_subpolicy: int,
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["subpolicy_name"] for row in selected)
    run_id = int(
        conn.execute(
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
                int(checkpoint_run["ledger_run_id"]),
                AGENT_KEY,
                ISSUE_FAMILY,
                QUEUE_STRATEGY,
                limit,
                per_subpolicy,
                len(selected),
                len(selected),
                json.dumps(
                    {
                        "checkpoint_run_id": checkpoint_run["id"],
                        "selected": dict(bucket_counts.most_common()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                now,
                now,
                now,
            ),
        ).lastrowid
    )
    return run_id


def insert_queue_items(conn, *, queue_run_id: int, ledger_run_id: int, selected: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    payload = []
    for row in selected:
        priority_score = (
            float(row.get("text_length") or 0)
            + float(row.get("word_count") or 0) * 2.0
            + float(row.get("token_count") or 0) * 8.0
        )
        payload.append(
            (
                queue_run_id,
                ledger_run_id,
                row["ledger_item_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["issue_family"],
                row["issue_kind"],
                AGENT_KEY,
                row["subpolicy_name"],
                round(priority_score, 4),
                "pending",
                "review_domain_guarded_false_reopen",
                row.get("evidence_text") or row.get("ledger_evidence_text"),
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
    checkpoint_run: dict[str, Any],
    candidate_count: int,
    selected: list[dict[str, Any]],
    limit: int,
    per_subpolicy: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["subpolicy_name"] for row in selected)
    fieldnames = [
        "queue_run_id",
        "checkpoint_run_id",
        "checkpoint_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "text_length",
        "word_count",
        "token_count",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "checkpoint_run_id": checkpoint_run["id"],
                    "checkpoint_item_id": row["id"],
                    "queue_bucket": row["subpolicy_name"],
                    **{
                        field: row.get(field)
                        for field in fieldnames
                        if field
                        not in {
                            "queue_run_id",
                            "checkpoint_run_id",
                            "checkpoint_item_id",
                            "queue_bucket",
                        }
                    },
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "checkpoint_run_id": checkpoint_run["id"],
                "checkpoint_item_id": row["id"],
                "ledger_run_id": checkpoint_run["ledger_run_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": AGENT_KEY,
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "queue_bucket": row["subpolicy_name"],
                "cluster": row["cluster"],
                "text_length": row["text_length"],
                "word_count": row["word_count"],
                "token_count": row["token_count"],
                "texts": {
                    "evidence_text": row.get("evidence_text") or row.get("ledger_evidence_text"),
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    options = [
        "false_positive_reopen",
        "needs_repair",
        "needs_domain_context",
        "needs_new_microagent",
        "manual_exception",
    ]
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "checkpoint_run_id": checkpoint_run["id"],
                "checkpoint_item_id": row["id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "cluster": row["cluster"],
                "subpolicy_name": row["subpolicy_name"],
                "decision": "pending",
                "decision_options": options,
                "corrected_text": "",
                "notes": "",
                "review_hint": (
                    "Validate whether this guarded autofix_unknown candidate is a false reopen. "
                    "Use false_positive_reopen only if PT-BR is natural enough, semantically aligned, "
                    "and token-safe. Use needs_repair for visible wording/fluency problems."
                ),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix Unknown Domain Guarded Review Queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Checkpoint run id: {checkpoint_run['id']}",
        f"Ledger run id: {checkpoint_run['ledger_run_id']}",
        f"Segment-state run id: {checkpoint_run['segment_state_run_id']}",
        "",
        "Coverage:",
        f"- Candidates available: {candidate_count:,}",
        f"- Selected: {len(selected):,}",
        f"- Limit: {limit:,}",
        f"- Per subpolicy: {per_subpolicy:,}",
        "",
        "Subpolicies:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Review guidance:",
        "- This queue is an audit sample; it does not authorize output writes or lifecycle closure.",
        "- Prefer needs_repair when wording is odd but structurally safe.",
        "- Prefer needs_domain_context when game context is needed before judging.",
        "- A high false_positive_reopen rate can justify a future guarded lifecycle bridge.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in selected[:35]:
        lines.append(
            f"- segment={row['segment_id']} | {row['subpolicy_name']} | "
            f"{row['relative_path']}::{row['source_key']} | {short(row.get('evidence_text'), 220)}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    checkpoint_run_id: int | None = None,
    limit: int = 120,
    per_subpolicy: int = 30,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint_row = conn.execute(
            "SELECT * FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_runs WHERE id = ?",
            (selected_checkpoint_run_id,),
        ).fetchone()
        if checkpoint_row is None:
            raise RuntimeError(f"Checkpoint run not found: {selected_checkpoint_run_id}")
        checkpoint_run = dict(checkpoint_row)
        paths = report_paths(settings, selected_checkpoint_run_id)
        candidates = fetch_candidates(conn, checkpoint_run_id=selected_checkpoint_run_id, include_existing=include_existing)
        selected = select_rows(candidates, limit=limit, per_subpolicy=per_subpolicy)
        queue_run_id = insert_queue_run(
            conn,
            checkpoint_run=checkpoint_run,
            selected=selected,
            limit=limit,
            per_subpolicy=per_subpolicy,
            paths=paths,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            ledger_run_id=int(checkpoint_run["ledger_run_id"]),
            selected=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        checkpoint_run=checkpoint_run,
        candidate_count=len(candidates),
        selected=selected,
        limit=limit,
        per_subpolicy=per_subpolicy,
    )
    print("[issue_autofix_unknown_domain_guarded_review_queue] Queue generated")
    print(f"[issue_autofix_unknown_domain_guarded_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_autofix_unknown_domain_guarded_review_queue] Checkpoint run id: {checkpoint_run['id']}")
    print(f"[issue_autofix_unknown_domain_guarded_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_autofix_unknown_domain_guarded_review_queue] Selected: {len(selected):,}")
    print(f"[issue_autofix_unknown_domain_guarded_review_queue] Report: {paths[0]}")
    print(f"[issue_autofix_unknown_domain_guarded_review_queue] Decisions template: {paths[3]}")
    return {
        "queue_run_id": queue_run_id,
        "checkpoint_run_id": checkpoint_run["id"],
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(paths[0]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create review queue from autofix_unknown domain guarded checkpoint.")
    parser.add_argument("--checkpoint-run-id", type=int)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-subpolicy", type=int, default=30)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        checkpoint_run_id=args.checkpoint_run_id,
        limit=args.limit,
        per_subpolicy=args.per_subpolicy,
        include_existing=args.include_existing,
    )
