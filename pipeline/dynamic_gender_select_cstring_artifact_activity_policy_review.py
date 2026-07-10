from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
SELECT_RE = re.compile(r"Select_CString", re.IGNORECASE)
CUSTOM_RE = re.compile(r"Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)", re.IGNORECASE)
RESIDUAL_RE = re.compile(r"\b(?:o[ií]ste|oy[oó]|pasasteis|pasaron|lugareñ[ao]|reinas|reyes|diestr[ao])\b", re.IGNORECASE)
NAME_TITLE_RE = re.compile(r"Get(?:Short)?UIName|TARGET_CHARACTER|CHARACTER|personagem|GetTitle|title|dynasty|house|named", re.IGNORECASE)
ITEM_RE = re.compile(r"artifact|item|equipment|relic|weapon|treasure|sword|animal comum|cad[aá]ver|presa", re.IGNORECASE)
TRAVEL_RE = re.compile(r"travel|journey|route|guide|pilgrimage|roaming|o[aá]sis|ponto mais alto|peregrin", re.IGNORECASE)
LEGEND_RE = re.compile(r"legend|lore|story|chronicle|hist[oó]ric|narrative", re.IGNORECASE)
REWARD_TOOLTIP_RE = re.compile(r"_tt$|tooltip|reward|effect|#P|@warning_icon|NOT_", re.IGNORECASE)
ACTIVITY_RE = re.compile(
    r"activity|hunt|wedding|feast|tournament|coronation|chariot|cerim[oô]nia|ca[çc]a|"
    r"_log$|_key$|participante|IsLocalPlayer|GetShortUIName|GetUIName",
    re.IGNORECASE,
)
MIXED_RE = re.compile(r"wedding|coronation|struggle|intent|opinion|contract|governance", re.IGNORECASE)
TOKEN_BOUNDARY_RE = re.compile(r"\w\?\w|[\[\]]{2,}|\$\s*\$")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, needs_output_apply, confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def collect_source_rows(path_value: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(path_value)):
        if row.get("domain_decision") != "needs_select_cstring_artifact_activity_policy":
            continue
        if row.get("domain_subpolicy") != "artifact_activity_legend_or_travel":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Select_CString", SELECT_RE),
        ("CustomOrGenderLoc", CUSTOM_RE),
        ("NameOrScopeGetter", NAME_TITLE_RE),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def ready_decision(row: dict[str, Any], state: dict[str, Any] | None) -> str | None:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])
    if not state:
        return None
    if state.get("state_group") != "pending" or int(state.get("needs_output_apply") or 0) != 0:
        return None
    if int(state.get("confirmed_matches_output") or 0) != 1 or int(state.get("is_closed") or 0) != 0:
        return None
    if not SELECT_RE.search(text):
        return None
    if any(pattern.search(haystack) for pattern in (CUSTOM_RE, RESIDUAL_RE, NAME_TITLE_RE, TRAVEL_RE, LEGEND_RE, MIXED_RE)):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return "artifact_activity_select_cstring_ready_false_reopen"
    if len(text) <= 140 and REWARD_TOOLTIP_RE.search(haystack):
        return "artifact_activity_select_cstring_ready_lifecycle"
    return None


def policy_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])
    if RESIDUAL_RE.search(text):
        return "needs_artifact_activity_select_cstring_residual_repair", "visible_spanish_or_gender_residual"
    if CUSTOM_RE.search(text):
        return "needs_artifact_activity_select_cstring_custom_loc_gender_policy", "custom_loc_gender"
    if MIXED_RE.search(haystack) and NAME_TITLE_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_mixed_domain_policy", "mixed_activity_name_or_title"
    if LEGEND_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_legend_context", "legend_lore_story"
    if TRAVEL_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_travel_context", "travel_journey_route_or_guide"
    if ITEM_RE.search(haystack) and not ACTIVITY_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_item_policy", "artifact_item_equipment"
    if REWARD_TOOLTIP_RE.search(haystack) and len(text) <= 180:
        return "needs_artifact_activity_select_cstring_reward_tooltip_policy", "reward_effect_tooltip"
    if NAME_TITLE_RE.search(haystack) and not ACTIVITY_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_name_title_policy", "name_title_or_entity"
    if ACTIVITY_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_activity_context", "activity_participant_perspective"
    if NAME_TITLE_RE.search(haystack):
        return "needs_artifact_activity_select_cstring_name_title_policy", "name_title_or_entity"
    if SELECT_RE.search(text):
        return "needs_new_microagent", "select_cstring_artifact_activity_uncategorized"
    return "blocked_uncertain", "blocked_uncertain"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    ready = ready_decision(row, state)
    if ready:
        return {
            "artifact_activity_decision": ready,
            "artifact_activity_subpolicy": ready.removeprefix("artifact_activity_select_cstring_ready_").removesuffix("_lifecycle").removesuffix("_false_reopen"),
            "tokens_seen": tokens_seen(row["current_text"]),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "artifact/activity Select_CString surface appears aligned for future narrow lifecycle",
        }
    decision, subpolicy = policy_decision(row)
    return {
        "artifact_activity_decision": decision,
        "artifact_activity_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": f"routed to {decision}; no apply or lifecycle emitted by this review",
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_select_cstring_artifact_activity_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["artifact_activity_decision"] for row in rows)
    subpolicy_counts = Counter(row["artifact_activity_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["artifact_activity_decision"].startswith("artifact_activity_select_cstring_ready_"))
    top_need = next(
        ((decision, count) for decision, count in decision_counts.most_common() if decision.startswith("needs_artifact_activity_select_cstring_")),
        ("", 0),
    )
    if ready_count >= 10:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif top_need[1] >= 10:
        recommendation = f"prepare_specific_policy_microagent:{top_need[0]}"
    else:
        recommendation = "migrate_to_needs_es_oa_policy_or_event_context_or_global_diagnostic"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic gender Select_CString artifact/activity policy review",
        "",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_for_future_lifecycle: {ready_count}",
            "apply_candidates_future: 0",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.domain_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_domain_decision": "needs_select_cstring_artifact_activity_policy",
                **decide(row, states.get(segment_id)),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={sum(1 for row in reviewed if row['artifact_activity_decision'].startswith('artifact_activity_select_cstring_ready_'))}")
    print("apply_candidates_future=0")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
