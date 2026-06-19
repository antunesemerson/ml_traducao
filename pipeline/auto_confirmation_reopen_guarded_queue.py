from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_guarded_queue_v1"
DEFAULT_FAMILY = "mechanical_formatting"


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned[:80] or "queue"


def split_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete auto_confirmation_reopen_audit_runs entry found.")
    return int(row["id"])


def suggested_decision(row: dict[str, Any]) -> tuple[str, int, int]:
    has_issue = int(row.get("issue_count") or 0) > 0 or int(row.get("high_issue_count") or 0) > 0
    token_ok = (row.get("token_status") or "") == "ok"
    output_match = row.get("output_match_kind") in {"exact_match", "display_equivalent_escape_delta"}
    if (
        row.get("label_family") == "mechanical_formatting"
        and token_ok
        and not has_issue
        and output_match
        and row.get("recommendation") == "sample_for_guarded_lifecycle_policy"
    ):
        return "accept_guarded_formatting_policy_candidate", 1, 0
    if row.get("label_family") == "mechanical_text_replacement" and token_ok and not has_issue and output_match:
        return "sample_text_replacement_before_policy", 0, 0
    return "manual_boundary_review", 0, 1


def fetch_rows(
    conn,
    *,
    audit_run_id: int,
    label_family: str,
    recommendations: set[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    where = ["item.run_id = ?", "item.label_family = ?"]
    params: list[Any] = [audit_run_id, label_family]
    if recommendations:
        placeholders = ", ".join("?" for _ in recommendations)
        where.append(f"item.recommendation IN ({placeholders})")
        params.extend(sorted(recommendations))
    sql = f"""
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_audit_items item
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
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE WHEN item.recommendation LIKE 'manual_review_due%' THEN 0 ELSE 1 END,
            item.review_priority DESC,
            item.confirmation_label,
            item.relative_path,
            item.source_line_number,
            item.segment_id
    """
    if limit is not None:
        sql += "\n        LIMIT ?"
        params.append(limit)
    rows = [dict(row) for row in conn.execute(sql, params)]
    for rank, row in enumerate(rows, start=1):
        decision, policy_candidate, manual_boundary = suggested_decision(row)
        row["queue_rank"] = rank
        row["suggested_decision"] = decision
        row["policy_candidate"] = policy_candidate
        row["manual_boundary"] = manual_boundary
    return rows


def candidate_count(
    conn,
    *,
    audit_run_id: int,
    label_family: str,
    recommendations: set[str],
) -> int:
    where = ["run_id = ?", "label_family = ?"]
    params: list[Any] = [audit_run_id, label_family]
    if recommendations:
        placeholders = ", ".join("?" for _ in recommendations)
        where.append(f"recommendation IN ({placeholders})")
        params.extend(sorted(recommendations))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM auto_confirmation_reopen_audit_items
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone()
    return int(row["n"] or 0)


def report_paths(settings: dict[str, Any], *, label_family: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_guarded_queue_{slugify(label_family)}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def insert_queue_run(
    conn,
    *,
    audit_run_id: int,
    label_family: str,
    recommendations_value: str | None,
    candidate_total: int,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    counts = Counter(row["suggested_decision"] for row in rows)
    match_counts = Counter(row.get("output_match_kind") or "" for row in rows)
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_guarded_queue_runs (
            rule_version,
            audit_run_id,
            label_family,
            recommendation_filter,
            candidate_count,
            selected_count,
            guarded_policy_candidate_count,
            manual_boundary_count,
            exact_output_match_count,
            display_equivalent_count,
            text_delta_count,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            audit_run_id,
            label_family,
            recommendations_value,
            candidate_total,
            len(rows),
            counts["accept_guarded_formatting_policy_candidate"],
            counts["manual_boundary_review"],
            match_counts["exact_match"],
            match_counts["display_equivalent_escape_delta"],
            match_counts["text_delta"],
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in rows:
        item_cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_guarded_queue_items (
                run_id,
                audit_run_id,
                audit_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                confirmation_label,
                label_family,
                recommendation,
                suggested_decision,
                policy_candidate,
                manual_boundary,
                output_match_kind,
                token_status,
                issue_count,
                high_issue_count,
                word_count,
                model_safe_probability,
                model_confidence,
                review_priority,
                queue_rank,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                audit_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row.get("confirmation_label"),
                row["label_family"],
                row["recommendation"],
                row["suggested_decision"],
                int(row["policy_candidate"]),
                int(row["manual_boundary"]),
                row.get("output_match_kind"),
                row.get("token_status"),
                int(row.get("issue_count") or 0),
                int(row.get("high_issue_count") or 0),
                int(row.get("word_count") or 0),
                row.get("model_safe_probability"),
                row.get("model_confidence"),
                float(row.get("review_priority") or 0),
                int(row["queue_rank"]),
                row.get("reasons_json"),
                now,
            ),
        )
        row["queue_item_id"] = int(item_cursor.lastrowid)
    return run_id


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    rows: list[dict[str, Any]],
    audit_run_id: int,
    label_family: str,
    candidate_total: int,
    started_at: datetime,
) -> None:
    fieldnames = [
        "queue_item_id",
        "queue_rank",
        "audit_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "confirmation_label",
        "label_family",
        "recommendation",
        "suggested_decision",
        "policy_candidate",
        "manual_boundary",
        "output_match_kind",
        "token_status",
        "issue_count",
        "high_issue_count",
        "word_count",
        "model_safe_probability",
        "model_confidence",
        "review_priority",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["audit_item_id"] = row["id"]
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "audit_item_id": row["id"],
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "confirmation_label": row.get("confirmation_label"),
                "suggested_decision": row["suggested_decision"],
                "decision": "pending",
                "allowed_decisions": [
                    "accept_guarded_formatting_policy_candidate",
                    "reject_guarded_policy_candidate",
                    "manual_boundary_review",
                    "needs_more_evidence",
                ],
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    decisions = Counter(row["suggested_decision"] for row in rows)
    labels = Counter(row.get("confirmation_label") or "" for row in rows)
    matches = Counter(row.get("output_match_kind") or "" for row in rows)
    paths = Counter(row.get("relative_path") or "" for row in rows)
    lines = [
        "Auto-confirmation reopen guarded queue",
        f"Rule version: {RULE_VERSION}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Audit run id: {audit_run_id}",
        f"Label family: {label_family}",
        "",
        "Summary:",
        f"- Candidates in source audit: {candidate_total:,}",
        f"- Rows selected: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in decisions.most_common()],
        "",
        "Output/confirmation match:",
        *[f"- {key}: {value:,}" for key, value in matches.most_common()],
        "",
        "Top labels:",
        *[f"- {key or '(empty)'}: {value:,}" for key, value in labels.most_common(20)],
        "",
        "Top paths:",
        *[f"- {key}: {value:,}" for key, value in paths.most_common(20)],
        "",
        "Review sample:",
    ]
    for row in rows[:40]:
        lines.extend(
            [
                (
                    f"- #{row['queue_rank']} | {row['suggested_decision']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                (
                    f"  label={row.get('confirmation_label')}; match={row.get('output_match_kind')}; "
                    f"issues={row.get('issue_count')}; safe_prob={row.get('model_safe_probability')}"
                ),
                f"  OUT: {short(row.get('portuguese_text'))}",
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This queue only creates evidence for review and guarded policy design.",
            "- It does not close lifecycle states, alter confirmations, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    audit_run_id: int | None = None,
    label_family: str = DEFAULT_FAMILY,
    recommendations_value: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        recommendations = split_csv(recommendations_value)
        candidate_total = candidate_count(
            conn,
            audit_run_id=selected_audit_run_id,
            label_family=label_family,
            recommendations=recommendations,
        )
        rows = fetch_rows(
            conn,
            audit_run_id=selected_audit_run_id,
            label_family=label_family,
            recommendations=recommendations,
            limit=limit,
        )
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(
            settings,
            label_family=label_family,
        )
        run_id = insert_queue_run(
            conn,
            audit_run_id=selected_audit_run_id,
            label_family=label_family,
            recommendations_value=recommendations_value,
            candidate_total=candidate_total,
            rows=rows,
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
            rows=rows,
            audit_run_id=selected_audit_run_id,
            label_family=label_family,
            candidate_total=candidate_total,
            started_at=started_at,
        )
        conn.commit()

    decision_counts = Counter(row["suggested_decision"] for row in rows)
    print("[auto_confirmation_reopen_guarded_queue] Queue generated")
    print(f"[auto_confirmation_reopen_guarded_queue] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_guarded_queue] Audit run id: {selected_audit_run_id}")
    print(f"[auto_confirmation_reopen_guarded_queue] Label family: {label_family}")
    print(f"[auto_confirmation_reopen_guarded_queue] Candidates: {candidate_total:,}")
    print(f"[auto_confirmation_reopen_guarded_queue] Selected: {len(rows):,}")
    for key, value in decision_counts.most_common():
        print(f"[auto_confirmation_reopen_guarded_queue] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_guarded_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_guarded_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_guarded_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_guarded_queue] Decisions template: {decisions_template_path}")
    return {
        "run_id": run_id,
        "audit_run_id": selected_audit_run_id,
        "candidate_total": candidate_total,
        "selected_count": len(rows),
        "decision_counts": dict(decision_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build guarded review queue from reopened auto-confirmed audit rows.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--family", default=DEFAULT_FAMILY)
    parser.add_argument("--recommendations", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(
        audit_run_id=args.audit_run_id,
        label_family=args.family,
        recommendations_value=args.recommendations,
        limit=args.limit,
    )
