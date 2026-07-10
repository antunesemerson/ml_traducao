from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_packet2_needs_context_diagnostic_v1"
PACKET_JSONL = Path("reports/20260702_175422_889616_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 559
LEDGER_RUN_ID = 76
EXPECTED_COUNT = 45


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_focus_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(PACKET_JSONL).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("suggested_human_decision") == "needs_more_context":
                row["_line_number"] = line_number
                rows.append(row)
    if len(rows) != EXPECTED_COUNT:
        raise SystemExit(f"expected {EXPECTED_COUNT} needs_more_context rows, got {len(rows)}")
    return rows


def fetch_current_state(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        WITH open_issues AS (
            SELECT
              segment_id,
              COUNT(*) AS open_issue_count,
              SUM(CASE WHEN lower(COALESCE(issue_severity,'')) IN ('high','critical','error') THEN 1 ELSE 0 END) AS high_issue_count,
              GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
              GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            GROUP BY segment_id
        )
        SELECT
          state.segment_id,
          state.final_state,
          state.state_group,
          state.review_state,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          output.portuguese_text AS output_text,
          confirmation.confirmed_text,
          confirmation.confirmation_source,
          COALESCE(open_issues.open_issue_count, 0) AS current_open_issue_count,
          COALESCE(open_issues.high_issue_count, 0) AS current_high_issue_count,
          COALESCE(open_issues.issue_families, '') AS current_issue_families,
          COALESCE(open_issues.issue_kinds, '') AS current_issue_kinds
        FROM segment_state_items state
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = state.segment_id
        LEFT JOIN open_issues ON open_issues.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        [LEDGER_RUN_ID, *ids, SEGMENT_STATE_RUN_ID, *ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def has_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def token_markers(row: dict[str, Any]) -> dict[str, bool]:
    blob = "\n".join(str(row.get(key) or "") for key in ("source_text", "output_text", "confirmed_text", "english_text"))
    return {
        "has_select": "Select_CString" in blob or "SelectLocalization" in blob,
        "has_conditional": "LocalPlayerString" in blob or "PlayerString" in blob,
        "has_getter": bool(re.search(r"\[[^\]]*(?:\.Get|ROOT\.|scope:|SCOPE\.|GetCourtPositionType|GetReligionByKey|GetPlayer|GetScriptValue)", blob)),
        "has_concept": "[Concept(" in blob,
        "has_es_helper": "ES_" in blob or "Loc_ES_" in blob,
        "has_spanish_residue": bool(
            re.search(
                r"\b(?:debe|debes|tienes|tiene|tenga|ser[aá]n|tomados|actual|puntuaci[oó]n|probabilidades|[a-z]*ci[oó]n)\b",
                blob,
                flags=re.IGNORECASE,
            )
        ),
        "has_marker_or_icon": "#!" in blob or "#weak" in blob or "#italic" in blob or "@" in blob,
    }


def classify(row: dict[str, Any], state: dict[str, Any]) -> tuple[str, str, str, int]:
    issue_family = ",".join([str(row.get("issue_family") or ""), str(state.get("current_issue_families") or "")])
    issue_kind = ",".join([str(row.get("issue_kind") or ""), str(state.get("current_issue_kinds") or "")])
    markers = token_markers(row)
    reasons: list[str] = []
    priority = 50

    if markers["has_select"] or markers["has_conditional"]:
        reasons.append("Select/conditional runtime branch affects wording or perspective.")
        return "parser_later_select_or_conditional", "; ".join(reasons), "parser_later", 90

    if markers["has_getter"] or markers["has_concept"] or markers["has_es_helper"] or "dynamic_ck3_expression" in issue_family:
        reasons.append("Dynamic getter/concept/helper needs runtime expansion policy before safe approval.")
        if markers["has_spanish_residue"]:
            reasons.append("Visible Spanish residue is entangled with tokenized expression.")
        return "parser_later_dynamic_getter", "; ".join(reasons), "parser_later", 80

    if markers["has_spanish_residue"] or "spanish_residual" in issue_family or "spanish_residue" in issue_kind:
        reasons.append("Visible Spanish residue likely needs human rewrite but token surface is light/plain.")
        return "spanish_residue_visible", "; ".join(reasons), "corrected_text", 20

    if "surface_boundary" in issue_family or "space_before_punctuation" in issue_kind:
        reasons.append("Boundary/punctuation issue may be correctable after checking UI context.")
        return "corrected_text_possible", "; ".join(reasons), "corrected_text", 15

    if "short_label_style" in issue_family and int(state.get("current_open_issue_count") or 0) <= 2:
        reasons.append("Short/compact UI label needs quick human semantic/style check.")
        return "human_simple_context_review", "; ".join(reasons), "approve_already_ok", 10

    if int(state.get("current_open_issue_count") or 0) > 2:
        reasons.append("Multiple unresolved issue families; keep as hold until narrower parser/human packet.")
        return "token_or_structure_hold", "; ".join(reasons), "hold", 95

    reasons.append("No obvious release blocker after prior batch, keep as non-blocking hold unless revisited.")
    return "release_non_blocking_hold", "; ".join(reasons), "hold", priority


def build_records(rows: list[dict[str, Any]], states: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id, {})
        family, context_reason, recommendation, priority = classify(row, state)
        records.append(
            {
                "source": SOURCE,
                "record_type": "needs_more_context_diagnostic_item",
                "review_index": row.get("review_index"),
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "token_surface": row.get("token_surface"),
                "source_text": row.get("source_text"),
                "english_text": row.get("english_text"),
                "output_text": state.get("output_text") or row.get("output_text"),
                "packet_output_text": row.get("output_text"),
                "confirmed_text": state.get("confirmed_text") or row.get("confirmed_text"),
                "packet_confirmed_text": row.get("confirmed_text"),
                "final_state": state.get("final_state"),
                "state_group": state.get("state_group"),
                "review_state": state.get("review_state"),
                "confirmation_level": state.get("confirmation_level"),
                "confirmation_label": state.get("confirmation_label"),
                "locked": int(state.get("locked") or 0),
                "confirmed_matches_output": int(state.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(state.get("needs_output_apply") or 0),
                "open_issue_count": int(state.get("current_open_issue_count") or 0),
                "high_issue_count": int(state.get("current_high_issue_count") or 0),
                "issue_family": state.get("current_issue_families") or row.get("issue_family"),
                "issue_kind": state.get("current_issue_kinds") or row.get("issue_kind"),
                "original_packet_open_issue_count": int(row.get("open_issue_count") or 0),
                "original_packet_high_issue_count": int(row.get("high_issue_count") or 0),
                "operational_family": family,
                "why_needs_context": context_reason,
                "recommendation": recommendation,
                "manual_review_priority": priority,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def select_next_sublote(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preferred = [
        row
        for row in records
        if row["operational_family"] in {"spanish_residue_visible", "corrected_text_possible", "human_simple_context_review"}
    ]
    preferred.sort(key=lambda row: (int(row["manual_review_priority"]), int(row["review_index"] or 9999)))
    return preferred[:15]


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_needs_context_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "UI/tooltips packet2 needs_more_context diagnostic",
        f"record_count={summary['record_count']}",
        f"family_counts={json.dumps(summary['family_counts'], ensure_ascii=False, sort_keys=True)}",
        f"recommendation_counts={json.dumps(summary['recommendation_counts'], ensure_ascii=False, sort_keys=True)}",
        f"next_sublote_count={summary['next_sublote_count']}",
        f"next_sublote_segment_ids={summary['next_sublote_segment_ids']}",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
        "",
        f"single_operational_recommendation={summary['single_operational_recommendation']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    rows = read_focus_rows()
    ids = [int(row["segment_id"]) for row in rows]
    with db.connect(db.load_settings()) as conn:
        states = fetch_current_state(conn, ids)
    records = build_records(rows, states)
    by_family: dict[str, list[int]] = defaultdict(list)
    for record in records:
        by_family[record["operational_family"]].append(int(record["segment_id"]))
    next_sublote = select_next_sublote(records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_needs_more_context_diagnostic",
        "input_jsonl": str(PACKET_JSONL),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "record_count": len(records),
        "family_counts": dict(Counter(record["operational_family"] for record in records).most_common()),
        "recommendation_counts": dict(Counter(record["recommendation"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "issue_family_counts": dict(Counter(record["issue_family"] for record in records).most_common()),
        "family_segment_ids": {family: ids for family, ids in sorted(by_family.items())},
        "next_sublote_count": len(next_sublote),
        "next_sublote_segment_ids": [int(row["segment_id"]) for row in next_sublote],
        "next_sublote": [
            {
                "segment_id": int(row["segment_id"]),
                "review_index": row["review_index"],
                "operational_family": row["operational_family"],
                "recommendation": row["recommendation"],
                "why_needs_context": row["why_needs_context"],
            }
            for row in next_sublote
        ],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Review the next_sublote manually first. Prefer corrected_text for visible Spanish residue, approve_already_ok only when the UI wording is confirmed, and keep dynamic/select groups parser_later."
        ),
    }
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"family_counts={json.dumps(summary['family_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"next_sublote_count={summary['next_sublote_count']}")
    print(f"next_sublote_segment_ids={summary['next_sublote_segment_ids']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
