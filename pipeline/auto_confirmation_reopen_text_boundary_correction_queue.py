from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_text_boundary_correction_queue_v1"
AGENT_KEY = "boundary_correction_specialist"
RUN_SUBFAMILY = "boundary_missing_or_token_change_corrections"
BOUNDARY_STATUSES = ("boundary_block_only", "repair_candidate_token_change")


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_policy_runs
        WHERE finished_at IS NOT NULL
          AND policy_status = 'shadow'
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed text boundary policy run found.")
    return int(row["id"])


def slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value).strip("_") or "boundary"


def report_paths(settings: dict[str, Any], *, scope: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_correction_queue_{slug(scope)}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def risk_for(row: dict[str, Any]) -> str:
    if row["boundary_status"] == "repair_candidate_token_change":
        return "high"
    risk = str(row.get("decision_risk_level") or "").strip().lower()
    if risk in {"critical", "high", "medium", "low"}:
        return risk
    if int(row.get("issue_count") or 0) > 0:
        return "medium"
    return "low"


def suggested_review_label(row: dict[str, Any]) -> str:
    if row["boundary_status"] == "repair_candidate_token_change":
        return "structure_error"
    if int(row.get("issue_count") or 0) > 0:
        return "likely_needs_fix"
    if int(row.get("spanish_literal_hint_count") or 0) > 0:
        return "residual_spanish"
    return "needs_fix"


def existing_boundary_correction_segments(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT item.segment_id
        FROM auto_confirmation_reopen_text_review_queue_items item
        JOIN auto_confirmation_reopen_text_review_queue_runs run
          ON run.id = item.run_id
        WHERE run.agent_key = ?
        """,
        (AGENT_KEY,),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def fetch_candidates(
    conn,
    *,
    policy_run_id: int,
    include_existing: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    existing_segments = set() if include_existing else existing_boundary_correction_segments(conn)
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                item.*,
                decision.corrected_text AS prior_corrected_text,
                decision.notes AS prior_notes,
                decision.risk_level AS decision_risk_level,
                source.english_text,
                source.spanish_text,
                output.portuguese_text,
                confirmation.confirmed_text,
                confirmation.confirmation_label,
                ROW_NUMBER() OVER (
                    PARTITION BY item.segment_id
                    ORDER BY
                        CASE item.boundary_status
                            WHEN 'repair_candidate_token_change' THEN 0
                            ELSE 1
                        END,
                        item.issue_count DESC,
                        item.spanish_literal_hint_count DESC,
                        item.select_cstring_count DESC,
                        item.id DESC
                ) AS rn
            FROM auto_confirmation_reopen_text_boundary_policy_items item
            JOIN auto_confirmation_reopen_text_review_decisions decision
              ON decision.id = item.review_decision_id
            JOIN source_segments source
              ON source.id = item.segment_id
            LEFT JOIN output_segments output
              ON output.segment_id = item.segment_id
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.id = (
                  SELECT c.id
                  FROM segment_confirmations c
                  WHERE c.segment_id = item.segment_id
                  ORDER BY c.updated_at DESC, c.id DESC
                  LIMIT 1
              )
            WHERE item.run_id = ?
              AND item.boundary_status IN ('boundary_block_only', 'repair_candidate_token_change')
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY
            CASE boundary_status
                WHEN 'repair_candidate_token_change' THEN 0
                ELSE 1
            END,
            issue_count DESC,
            spanish_literal_hint_count DESC,
            select_cstring_count DESC,
            relative_path,
            source_line_number,
            source_key
        """,
        (policy_run_id,),
    ).fetchall()
    all_rows = [dict(row) for row in rows]
    selected = [row for row in all_rows if int(row["segment_id"]) not in existing_segments]
    skipped = len(all_rows) - len(selected)
    return selected, len(all_rows), skipped


def insert_queue(
    conn,
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    total_candidates: int,
    skipped_existing: int,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    high_count = sum(1 for row in rows if row["risk_level"] == "high")
    medium_count = sum(1 for row in rows if row["risk_level"] == "medium")
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_review_queue_runs (
            rule_version,
            diagnostic_run_id,
            agent_key,
            text_subfamily,
            total_candidates,
            selected_count,
            high_risk_count,
            medium_risk_count,
            skipped_previously_queued_count,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            policy_run_id,
            AGENT_KEY,
            RUN_SUBFAMILY,
            total_candidates,
            len(rows),
            high_count,
            medium_count,
            skipped_existing,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    queue_run_id = int(cursor.lastrowid)
    for rank, row in enumerate(rows, start=1):
        row["queue_rank"] = rank
        row["suggested_review_label"] = suggested_review_label(row)
        row["risk_level"] = risk_for(row)
        item_cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_review_queue_items (
                run_id,
                diagnostic_run_id,
                diagnostic_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                confirmation_label,
                text_subfamily,
                suggested_agent_key,
                risk_level,
                select_cstring_count,
                spanish_literal_hint_count,
                issue_count,
                model_safe_probability,
                review_priority,
                queue_rank,
                suggested_review_label,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_run_id,
                row.get("diagnostic_run_id"),
                row.get("diagnostic_item_id"),
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row.get("confirmation_label"),
                row["source_text_subfamily"],
                row["boundary_agent_key"],
                row["risk_level"],
                int(row.get("select_cstring_count") or 0),
                int(row.get("spanish_literal_hint_count") or 0),
                int(row.get("issue_count") or 0),
                None,
                float(1000 - rank),
                rank,
                row["suggested_review_label"],
                now,
            ),
        )
        row["queue_item_id"] = int(item_cursor.lastrowid)
    return queue_run_id


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    queue_run_id: int,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    total_candidates: int,
    skipped_existing: int,
    started_at: datetime,
) -> None:
    fieldnames = [
        "queue_item_id",
        "queue_rank",
        "boundary_policy_item_id",
        "review_decision_id",
        "diagnostic_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "boundary_status",
        "boundary_agent_key",
        "boundary_policy",
        "token_status",
        "block_reason",
        "text_subfamily",
        "risk_level",
        "select_cstring_count",
        "spanish_literal_hint_count",
        "issue_count",
        "suggested_review_label",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["boundary_policy_item_id"] = row.get("id")
            payload["text_subfamily"] = row.get("source_text_subfamily")
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "queue_rank": row["queue_rank"],
                "boundary_policy_item_id": row["id"],
                "review_decision_id": row["review_decision_id"],
                "diagnostic_item_id": row["diagnostic_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "boundary_status": row["boundary_status"],
                "boundary_agent_key": row["boundary_agent_key"],
                "boundary_policy": row["boundary_policy"],
                "token_status": row["token_status"],
                "block_reason": row["block_reason"],
                "text_subfamily": row["source_text_subfamily"],
                "risk_level": row["risk_level"],
                "suggested_review_label": row["suggested_review_label"],
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "prior_corrected_preview": short(row.get("prior_corrected_text")),
                "prior_notes": row.get("prior_notes") or "",
                "reasons": json.loads(row.get("reasons_json") or "[]"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": AGENT_KEY,
                "boundary_agent_key": row["boundary_agent_key"],
                "boundary_policy": row["boundary_policy"],
                "boundary_status": row["boundary_status"],
                "token_status": row["token_status"],
                "text_subfamily": row["source_text_subfamily"],
                "risk_level": row["risk_level"],
                "suggested_review_label": row["suggested_review_label"],
                "decision": "pending",
                "human_label": "",
                "corrected_text": "",
                "notes": "",
                "english_text": row.get("english_text") or "",
                "spanish_text": row.get("spanish_text") or "",
                "current_output_text": row.get("portuguese_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
                "prior_corrected_text": row.get("prior_corrected_text") or "",
                "prior_notes": row.get("prior_notes") or "",
                "review_instruction": (
                    "If current confirmed/output is wrong, set human_label to needs_fix/minor_fix/major_fix "
                    "and fill corrected_text preserving CK3 tokens. If it is actually safe, use approve_current_confirmed. "
                    "If token structure would change, keep human_label as structure_error and explain in notes."
                ),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["boundary_status"] for row in rows)
    by_agent = Counter(row["boundary_agent_key"] for row in rows)
    by_subfamily = Counter(row["source_text_subfamily"] for row in rows)
    by_label = Counter(row["suggested_review_label"] for row in rows)
    lines = [
        "Auto-confirmation text boundary correction queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Boundary policy run id: {policy_run_id}",
        f"Agent: {AGENT_KEY}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Distinct candidate segments: {total_candidates:,}",
        f"- Selected for this queue: {len(rows):,}",
        f"- Skipped existing boundary correction queue rows: {skipped_existing:,}",
        "",
        "By boundary status:",
        *[f"- {key}: {value:,}" for key, value in by_status.most_common()],
        "",
        "By boundary agent:",
        *[f"- {key}: {value:,}" for key, value in by_agent.most_common()],
        "",
        "By source subfamily:",
        *[f"- {key}: {value:,}" for key, value in by_subfamily.most_common()],
        "",
        "By suggested review label:",
        *[f"- {key}: {value:,}" for key, value in by_label.most_common()],
        "",
        "Priority samples:",
    ]
    for row in rows[:25]:
        lines.extend(
            [
                (
                    f"- #{row['queue_rank']} {row['risk_level']} | {row['boundary_status']} | "
                    f"{row['relative_path']}:{row.get('source_line_number')}:{row['source_key']}"
                ),
                (
                    "  "
                    f"agent={row['boundary_agent_key']}; token={row['token_status']}; "
                    f"label={row['suggested_review_label']}"
                ),
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
                f"  PRIOR CORRECTION: {short(row.get('prior_corrected_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This queue only creates review evidence and a decisions template.",
            "- It does not change confirmations, train models, promote policies, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    policy_run_id: int | None = None,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        rows, total_candidates, skipped_existing = fetch_candidates(
            conn,
            policy_run_id=selected_policy_run_id,
            include_existing=include_existing,
        )
        for row in rows:
            row["risk_level"] = risk_for(row)
            row["suggested_review_label"] = suggested_review_label(row)
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings, scope="all")
        queue_run_id = insert_queue(
            conn,
            policy_run_id=selected_policy_run_id,
            rows=rows,
            total_candidates=total_candidates,
            skipped_existing=skipped_existing,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            started_at=started_at,
        )
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            queue_run_id=queue_run_id,
            policy_run_id=selected_policy_run_id,
            rows=rows,
            total_candidates=total_candidates,
            skipped_existing=skipped_existing,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_boundary_correction_queue] Queue generated")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Queue run id: {queue_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Boundary policy run id: {selected_policy_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Distinct candidates: {total_candidates:,}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Selected: {len(rows):,}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Skipped existing: {skipped_existing:,}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_text_boundary_correction_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "policy_run_id": selected_policy_run_id,
        "total_candidates": total_candidates,
        "selected_count": len(rows),
        "skipped_existing": skipped_existing,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a correction queue from boundary block-only/token-change evidence.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--include-existing", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(policy_run_id=args.policy_run_id, include_existing=args.include_existing)
