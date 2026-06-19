from __future__ import annotations

import argparse
import difflib
from typing import Any

import db
from apply_safe_output_updates import escape_localization_value


RULE_VERSION = "select_cstring_pending_taxonomy_report_v1"
BRIDGE_CLOSED_STATE = "closed_auto_confirmed_select_cstring_governed_bridge"


def canonical(value: Any) -> str:
    return escape_localization_value("" if value is None else str(value))


def short(value: Any, limit: int = 260) -> str:
    text = ("" if value is None else str(value)).replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def diff_summary(current: str, corrected: str, limit: int = 220) -> str:
    current_tokens = current.split()
    corrected_tokens = corrected.split()
    diff = list(difflib.ndiff(current_tokens, corrected_tokens))
    changes = [part for part in diff if part.startswith("- ") or part.startswith("+ ")]
    text = " ".join(changes[:24])
    if not text:
        return "no visible token-level diff"
    return short(text, limit)


def recommendation(row: dict[str, Any]) -> str:
    confirmation_source = str(row.get("confirmation_source") or "").lower()
    confirmed_matches_corrected = bool(row.get("confirmed_matches_corrected"))
    output_matches_corrected = bool(row.get("output_matches_corrected"))
    confirmed_matches_output = bool(row.get("confirmed_matches_output"))
    if confirmed_matches_corrected and output_matches_corrected:
        return "accept_bridge_corrected_text"
    if not confirmed_matches_output:
        return "needs_human_review"
    if confirmation_source.startswith("bulk_confirm") or confirmation_source.startswith("learned_validation"):
        return "stale_proposal_requires_new_learning"
    return "needs_human_review"


def latest_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No segment-state run found.")
    return int(row["id"])


def fetch_rows(conn, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_proposal AS (
          SELECT MAX(run_id) AS run_id
          FROM ml_issue_select_cstring_governed_bridge_proposal_items
        )
        SELECT
          item.id AS bridge_item,
          item.segment_id,
          item.relative_path,
          item.source_line_number,
          item.source_key,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.confirmed_text,
          output.portuguese_text AS output_text,
          item.corrected_text,
          state.final_state,
          state.confirmed_matches_output,
          CASE WHEN canonical_l10n(confirmation.confirmed_text) = canonical_l10n(item.corrected_text)
            THEN 1 ELSE 0 END AS confirmed_matches_corrected,
          CASE WHEN canonical_l10n(output.portuguese_text) = canonical_l10n(item.corrected_text)
            THEN 1 ELSE 0 END AS output_matches_corrected
        FROM ml_issue_select_cstring_governed_bridge_proposal_items item
        JOIN latest_proposal proposal
          ON proposal.run_id = item.run_id
        JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE state.final_state != ?
        ORDER BY item.id
        """,
        (run_id, BRIDGE_CLOSED_STATE),
    ).fetchall()
    return [dict(row) for row in rows]


def write_report(settings: dict[str, Any], *, run_id: int, rows: list[dict[str, Any]]) -> str:
    lines = [
        "Select_CString governed bridge pending taxonomy report",
        f"Rule version: {RULE_VERSION}",
        f"Segment-state run id: {run_id}",
        f"Pending rows: {len(rows)}",
        "",
        "Summary:",
    ]
    by_recommendation: dict[str, int] = {}
    for row in rows:
        rec = recommendation(row)
        row["recommendation"] = rec
        by_recommendation[rec] = by_recommendation.get(rec, 0) + 1
    for key, value in sorted(by_recommendation.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Rows:"])
    for row in rows:
        confirmed = row.get("confirmed_text") or ""
        output = row.get("output_text") or ""
        corrected = row.get("corrected_text") or ""
        blockers = []
        if canonical(confirmed) != canonical(corrected):
            blockers.append("confirmed_text != corrected_text")
        if canonical(output) != canonical(corrected):
            blockers.append("output_text != corrected_text")
        if not int(row.get("confirmed_matches_output") or 0):
            blockers.append("confirmed_text != output_text")
        if not blockers:
            blockers.append("state_not_closed_by_bridge")
        lines.extend(
            [
                f"- bridge_item: {row['bridge_item']}",
                f"  segment_id: {row['segment_id']}",
                f"  relative_path: {row['relative_path']}",
                f"  source_line_number: {row['source_line_number']}",
                f"  source_key: {row['source_key']}",
                f"  confirmation_source: {row.get('confirmation_source') or ''}",
                f"  confirmation_label: {row.get('confirmation_label') or ''}",
                f"  confirmed_text: {short(confirmed)}",
                f"  output_text: {short(output)}",
                f"  corrected_text: {short(corrected)}",
                f"  diff_summary: {diff_summary(output, corrected)}",
                f"  block_reason: {', '.join(blockers)}",
                f"  recommendation: {row['recommendation']}",
            ]
        )
    path = db.write_report(settings, "select_cstring_pending_taxonomy", lines)
    return str(path)


def main(run_id: int | None = None) -> str:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        conn.create_function("canonical_l10n", 1, canonical)
        selected_run_id = run_id or latest_segment_state_run_id(conn)
        rows = fetch_rows(conn, selected_run_id)
        report_path = write_report(settings, run_id=selected_run_id, rows=rows)
    print(f"[select_cstring_pending_taxonomy_report] Segment-state run id: {selected_run_id}")
    print(f"[select_cstring_pending_taxonomy_report] Pending rows: {len(rows)}")
    for row in rows:
        print(
            "[select_cstring_pending_taxonomy_report] "
            f"{row['segment_id']} {row['relative_path']}:{row['source_line_number']} "
            f"{row['source_key']} recommendation={row['recommendation']}"
        )
    print(f"[select_cstring_pending_taxonomy_report] Report: {report_path}")
    return report_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only taxonomy report for pending Select_CString bridge items.")
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()
    main(run_id=args.run_id)
