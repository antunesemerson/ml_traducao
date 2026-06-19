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


RULE_VERSION = "auto_confirmation_reopen_lifecycle_policy_v1"
POLICY_NAME = "guarded_mechanical_formatting_reopen_closure"
POLICY_ACTION = "close_reopen_guarded_formatting"
DEFAULT_FAMILY = "mechanical_formatting"


def latest_queue_run_id(conn, *, label_family: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_guarded_queue_runs
        WHERE finished_at IS NOT NULL
          AND label_family = ?
          AND selected_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (label_family,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No guarded queue found for label family {label_family!r}.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_lifecycle_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, queue_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    queue_run = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_guarded_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    if queue_run is None:
        raise RuntimeError(f"Guarded queue run not found: {queue_run_id}")
    rows = conn.execute(
        """
        SELECT
            item.*,
            audit.state_run_id,
            audit.state_item_id,
            audit.recommendation AS audit_recommendation,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_guarded_queue_items item
        LEFT JOIN auto_confirmation_reopen_audit_items audit ON audit.id = item.audit_item_id
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
        ORDER BY item.queue_rank, item.relative_path, item.source_line_number
        """,
        (queue_run_id,),
    ).fetchall()
    return dict(queue_run), [dict(row) for row in rows]


def evaluate_row(row: dict[str, Any], *, label_family: str) -> tuple[int, str]:
    if row.get("label_family") != label_family:
        return 0, "wrong_label_family"
    if row.get("suggested_decision") != "accept_guarded_formatting_policy_candidate":
        return 0, "not_guarded_formatting_candidate"
    if int(row.get("policy_candidate") or 0) != 1:
        return 0, "queue_item_not_policy_candidate"
    if int(row.get("manual_boundary") or 0) != 0:
        return 0, "manual_boundary"
    if row.get("output_match_kind") not in {"exact_match", "display_equivalent_escape_delta"}:
        return 0, "output_not_equivalent"
    if row.get("token_status") != "ok":
        return 0, "token_status_not_ok"
    if int(row.get("issue_count") or 0) > 0 or int(row.get("high_issue_count") or 0) > 0:
        return 0, "issue_signal_present"
    return 1, ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    policy_run_id: int,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    policy_status: str,
) -> None:
    fieldnames = [
        "policy_item_id",
        "queue_item_id",
        "audit_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "label_family",
        "confirmation_label",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "output_match_kind",
        "token_status",
        "issue_count",
        "high_issue_count",
        "model_safe_probability",
        "review_priority",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
    matches = Counter(row.get("output_match_kind") or "" for row in rows if row["policy_allowed"])
    labels = Counter(row.get("confirmation_label") or "" for row in rows if row["policy_allowed"])
    lines = [
        "Auto-confirmation reopen lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy status: {policy_status}",
        f"Policy run id: {policy_run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Audit run id: {queue_run['audit_run_id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Queue candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Released output/confirmation match:",
        *[f"- {key}: {value:,}" for key, value in matches.most_common()],
        "",
        "Released labels:",
        *[f"- {key or '(empty)'}: {value:,}" for key, value in labels.most_common()],
        "",
        "Blocked sample:",
    ]
    blocked = [row for row in rows if not row["policy_allowed"]]
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  label={row.get('confirmation_label')}; match={row.get('output_match_kind')}; issues={row.get('issue_count')}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This policy only authorizes segment-state lifecycle closure for matching auto-confirmed formatting cases.",
            "- It does not write output files and does not alter segment confirmations.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    queue_run_id: int | None = None,
    label_family: str = DEFAULT_FAMILY,
    policy_status: str = "active",
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn, label_family=label_family)
        queue_run, rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)
        if queue_run.get("label_family") != label_family:
            raise RuntimeError(
                f"Queue run {selected_queue_run_id} is {queue_run.get('label_family')!r}, expected {label_family!r}."
            )
        for row in rows:
            allowed, block_reason = evaluate_row(row, label_family=label_family)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
        txt_path, csv_path, jsonl_path = report_paths(settings)
        counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
                rule_version,
                queue_run_id,
                audit_run_id,
                policy_name,
                label_family,
                policy_status,
                candidate_count,
                released_count,
                blocked_count,
                manual_boundary_count,
                invalid_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_queue_run_id,
                queue_run.get("audit_run_id"),
                POLICY_NAME,
                label_family,
                policy_status,
                len(rows),
                counts["released"],
                len(rows) - counts["released"],
                counts["manual_boundary"],
                sum(value for key, value in counts.items() if key not in {"released", "manual_boundary"}),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        policy_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    audit_run_id,
                    audit_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    label_family,
                    confirmation_label,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    output_match_kind,
                    token_status,
                    issue_count,
                    high_issue_count,
                    model_safe_probability,
                    review_priority,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    selected_queue_run_id,
                    row["id"],
                    queue_run.get("audit_run_id"),
                    row.get("audit_item_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["label_family"],
                    row.get("confirmation_label"),
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    row.get("output_match_kind"),
                    row.get("token_status"),
                    int(row.get("issue_count") or 0),
                    int(row.get("high_issue_count") or 0),
                    row.get("model_safe_probability"),
                    float(row.get("review_priority") or 0),
                    row.get("reasons_json"),
                    now,
                ),
            )
            row["policy_item_id"] = int(item_cursor.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            queue_run=queue_run,
            rows=rows,
            started_at=started_at,
            policy_status=policy_status,
        )
        conn.commit()

    print("[auto_confirmation_reopen_lifecycle_policy] Policy generated")
    print(f"[auto_confirmation_reopen_lifecycle_policy] Run id: {policy_run_id}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] Queue run id: {selected_queue_run_id}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] Status: {policy_status}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] Released: {counts['released']:,}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] Blocked: {len(rows) - counts['released']:,}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_lifecycle_policy] JSONL: {jsonl_path}")
    return {
        "run_id": policy_run_id,
        "queue_run_id": selected_queue_run_id,
        "policy_status": policy_status,
        "released": counts["released"],
        "blocked": len(rows) - counts["released"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create guarded lifecycle policy for reopened auto-confirmed rows.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--family", default=DEFAULT_FAMILY)
    parser.add_argument("--status", choices=["shadow", "active"], default="active")
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, label_family=args.family, policy_status=args.status)
