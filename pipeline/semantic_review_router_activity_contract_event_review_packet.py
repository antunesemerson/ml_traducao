from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_review_router_activity_contract_event_review_packet_v1"
INPUT_PATTERN = "*_semantic_review_router_pending_deep_diagnostic.jsonl"
REVIEW_LIMIT = 12
TARGET_LANE = "semantic_review_policy_design_candidate"
TARGET_SURFACE = "activity_contract_event"
ALLOWED_RISKS = {"low_plain_text", "medium_dynamic_light"}
EXCLUDED_SEGMENT_IDS = {
    40667,
    43671,
    66809,
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_jsonl() -> Path:
    matches = sorted(reports_dir().glob(INPUT_PATTERN), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise SystemExit(f"missing input report matching {INPUT_PATTERN}")
    return matches[0]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def full_context(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            state.state_group,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            COALESCE(c.locked, 0) AS confirmation_locked,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = (SELECT MAX(id) FROM segment_state_runs WHERE finished_at IS NOT NULL)
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        tuple(segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def priority(row: dict[str, Any]) -> tuple[int, str, int, int]:
    risk_rank = {"low_plain_text": 0, "medium_dynamic_light": 1}.get(str(row.get("risk_bucket") or ""), 9)
    path = str(row.get("relative_path") or "")
    token_count = int(row.get("token_count") or 0)
    return (risk_rank, path, token_count, int(row.get("segment_id") or 0))


def select_rows(rows: list[dict[str, Any]], context_by_id: dict[int, dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    blocked: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for row in sorted(rows, key=priority):
        segment_id = int(row.get("segment_id") or 0)
        if row.get("policy_lane") != TARGET_LANE:
            continue
        if row.get("surface_bucket") != TARGET_SURFACE:
            continue
        if row.get("risk_bucket") not in ALLOWED_RISKS:
            blocked["risk_not_allowed"] += 1
            continue
        if segment_id in EXCLUDED_SEGMENT_IDS:
            blocked["excluded_previous_hold"] += 1
            continue
        context = context_by_id.get(segment_id)
        if not context:
            blocked["missing_context"] += 1
            continue
        if int(context.get("confirmation_locked") or 0) == 1:
            blocked["confirmation_locked"] += 1
            continue
        if context.get("state_group") != "pending" or context.get("final_state") != "reopen_auto_confirmed_autofix":
            blocked["not_pending_reopen"] += 1
            continue
        if int(context.get("needs_output_apply") or 0) != 0:
            blocked["needs_output_apply"] += 1
            continue
        if int(context.get("confirmed_matches_output") or 0) != 1:
            blocked["confirmed_mismatch"] += 1
            continue
        enriched = dict(row)
        enriched.update(
            {
                "english_text": context.get("english_text"),
                "spanish_text": context.get("spanish_text"),
                "old_text": context.get("old_text"),
                "current_output_text": context.get("current_output_text"),
                "confirmation_locked": context.get("confirmation_locked"),
                "confirmation_level": context.get("confirmation_level"),
                "confirmation_source": context.get("confirmation_source"),
                "confirmation_label": context.get("confirmation_label"),
            }
        )
        selected.append(enriched)
        if len(selected) >= REVIEW_LIMIT:
            break
    return selected, blocked


def summarize(input_path: Path, rows: list[dict[str, Any]], selected: list[dict[str, Any]], blocked: Counter[str]) -> dict[str, Any]:
    target_rows = [
        row
        for row in rows
        if row.get("policy_lane") == TARGET_LANE
        and row.get("surface_bucket") == TARGET_SURFACE
    ]
    eligible_rows = [row for row in target_rows if row.get("risk_bucket") in ALLOWED_RISKS]
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "review_limit": REVIEW_LIMIT,
        "target_lane": TARGET_LANE,
        "target_surface": TARGET_SURFACE,
        "target_count": len(target_rows),
        "eligible_allowed_risk_count": len(eligible_rows),
        "review_count": len(selected),
        "risk_counts_available": [
            {"key": key, "count": value}
            for key, value in Counter(str(row.get("risk_bucket") or "") for row in target_rows).most_common()
        ],
        "file_counts_selected": [
            {"key": key, "count": value}
            for key, value in Counter(str(row.get("relative_path") or "") for row in selected).most_common()
        ],
        "blocked_or_skipped_counts": [{"key": key, "count": value} for key, value in blocked.most_common()],
        "review_rows": selected,
        "apply_ready_now": 0,
        "source_changed": False,
        "output_changed": False,
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "production_full_recommended_now": False,
        "next_action": "human_review_activity_contract_event_rows_then_ingest_confirmed_only",
    }


def write_outputs(packet: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_router_activity_contract_event_review_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in packet["review_rows"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic review router activity contract event review packet",
        f"source={SOURCE}",
        f"input_jsonl={packet['input_jsonl']}",
        f"target_count={packet['target_count']}",
        f"eligible_allowed_risk_count={packet['eligible_allowed_risk_count']}",
        f"review_count={packet['review_count']}",
        "",
        "review_rows:",
    ]
    for row in packet["review_rows"]:
        lines.append(
            f"- {row['segment_id']} | {row['surface_bucket']} | {row['risk_bucket']} | "
            f"{row['relative_path']} | {row['source_key']}"
        )
        lines.append(f"  PT: {row.get('current_output_text')}")
        lines.append(f"  EN: {row.get('english_text')}")
        lines.append(f"  ES: {row.get('spanish_text')}")
    lines.extend(
        [
            "",
            f"apply_ready_now={packet['apply_ready_now']}",
            "source_changed=false",
            "output_changed=false",
            "production_full_recommended_now=false",
            f"next_action={packet['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_jsonl()
    rows = read_jsonl(input_path)
    candidate_ids = [
        int(row["segment_id"])
        for row in rows
        if row.get("policy_lane") == TARGET_LANE
        and row.get("surface_bucket") == TARGET_SURFACE
        and row.get("risk_bucket") in ALLOWED_RISKS
    ]
    with connect_readonly() as conn:
        context_by_id = full_context(conn, candidate_ids)
    selected, blocked = select_rows(rows, context_by_id)
    packet = summarize(input_path, rows, selected, blocked)
    txt_path, jsonl_path, summary_path = write_outputs(packet)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"target_count={packet['target_count']}")
    print(f"eligible_allowed_risk_count={packet['eligible_allowed_risk_count']}")
    print(f"review_count={packet['review_count']}")
    print(f"blocked_or_skipped_counts={packet['blocked_or_skipped_counts']}")
    print(f"next_action={packet['next_action']}")


if __name__ == "__main__":
    main()
