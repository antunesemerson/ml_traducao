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
RESIDUAL_RE = re.compile(
    r"\b(?:buena mujer|buen hombre|lugareñ[ao]|pasasteis|pasaron|reinas|reyes|"
    r"o[ií]ste|oy[oó]|diestr[ao]|niñ[ao]|vassal[ao]s)\b",
    re.IGNORECASE,
)
TITLE_LAW_RE = re.compile(
    r"war|guerra|governance|governor|government|reino|realm|title|struggle|vassal|"
    r"contract|claim|courtier|cortes[aã]o|opinion|governo",
    re.IGNORECASE,
)
RELIGION_RE = re.compile(r"pilgrimage|divinity|religion|faith|tenet|douctrine|doutrina|peregrin", re.IGNORECASE)
CULTURE_RE = re.compile(r"culture|tradition|innovation|heritage|ethos", re.IGNORECASE)
NAME_RE = re.compile(r"GetShortUIName|GetUIName|CHARACTER|TARGET_CHARACTER|personagem|nickname|dynasty|house", re.IGNORECASE)
TRAIT_RE = re.compile(r"trait|modifier|accolade|knight|descriptor", re.IGNORECASE)
ARTIFACT_ACTIVITY_RE = re.compile(
    r"activity|activities|hunt|pilgrimage|wedding|coronation|roaming|tour|travel|legend|"
    r"journey|banquet|chariot|intent|cerim[oô]nia|ca[çc]a|p[âa]ntano|monumento",
    re.IGNORECASE,
)
PLACE_RE = re.compile(r"province|oasis|building|holding|county|barony|temple|castle|city|lugar|ponto mais alto", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc|desc\.|_log$|_key$|flavor|dialog|perspectiv|subject|object", re.IGNORECASE)
DYNAMIC_RE = re.compile(r"Get[A-Za-z0-9_]*|ROOT\.|TARGET_|CHARACTER|IsLocalPlayer", re.IGNORECASE)
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
        if row.get("select_cstring_decision") != "needs_select_cstring_domain_context":
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
        ("DynamicGetter", DYNAMIC_RE),
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
    if "dynamic_ck3_expression_microagent" not in row.get("open_issue_families", []):
        return None
    if "gender_token_microagent" not in row.get("open_issue_families", []):
        return None
    if not SELECT_RE.search(text):
        return None
    if any(
        pattern.search(haystack)
        for pattern in (CUSTOM_RE, RESIDUAL_RE, TITLE_LAW_RE, RELIGION_RE, CULTURE_RE, NAME_RE, TRAIT_RE, ARTIFACT_ACTIVITY_RE, PLACE_RE, EVENT_RE)
    ):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return "select_cstring_domain_ready_false_reopen"
    return "select_cstring_domain_ready_lifecycle"


def policy_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])
    matched_domains: list[tuple[str, str]] = []

    if RESIDUAL_RE.search(text):
        return "needs_select_cstring_residual_repair", "visible_spanish_or_gender_residual"
    if CUSTOM_RE.search(text):
        return "needs_select_cstring_custom_loc_gender_policy", "custom_loc_gender"
    if TITLE_LAW_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_title_law_policy", "title_law_government_or_realm"))
    if RELIGION_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_religion_policy", "religion_faith_or_doctrine"))
    if CULTURE_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_culture_policy", "culture_tradition_or_innovation"))
    if TRAIT_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_trait_epithet_policy", "trait_modifier_accolade_or_epithet"))
    if PLACE_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_place_building_policy", "place_building_or_holding"))
    if ARTIFACT_ACTIVITY_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_artifact_activity_policy", "artifact_activity_legend_or_travel"))
    if NAME_RE.search(haystack):
        matched_domains.append(("needs_select_cstring_name_nickname_policy", "name_nickname_dynasty_or_character"))

    unique = []
    seen = set()
    for item in matched_domains:
        if item[0] not in seen:
            unique.append(item)
            seen.add(item[0])

    if len(unique) >= 3:
        return "needs_select_cstring_mixed_domain_policy", "mixed_domain"
    if unique:
        return unique[0]
    if EVENT_RE.search(haystack):
        return "needs_select_cstring_event_context", "event_dialogue_or_perspective"
    if DYNAMIC_RE.search(text):
        return "needs_select_cstring_dynamic_expression_policy", "dynamic_expression"
    if SELECT_RE.search(text):
        return "needs_new_microagent", "select_cstring_domain_uncategorized"
    return "blocked_uncertain", "blocked_uncertain"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    ready = ready_decision(row, state)
    if ready:
        return {
            "domain_decision": ready,
            "domain_subpolicy": ready.removeprefix("select_cstring_domain_ready_"),
            "tokens_seen": tokens_seen(row["current_text"]),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "Select_CString domain surface appears aligned for future narrow lifecycle",
        }
    decision, subpolicy = policy_decision(row)
    return {
        "domain_decision": decision,
        "domain_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": f"routed to {decision}; no apply or lifecycle emitted by this review",
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_select_cstring_domain_context_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["domain_decision"] for row in rows)
    subpolicy_counts = Counter(row["domain_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["domain_decision"].startswith("select_cstring_domain_ready_"))

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    top_need = next(
        ((decision, count) for decision, count in decision_counts.most_common() if decision.startswith("needs_select_cstring_") and decision.endswith("_policy")),
        ("", 0),
    )
    if ready_count >= 10:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif top_need[1] >= 15:
        recommendation = f"prepare_specific_policy_microagent:{top_need[0]}"
    else:
        recommendation = "migrate_to_needs_es_oa_policy_or_global_diagnostic"

    lines = [
        "Dynamic gender Select_CString domain context review",
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
    parser.add_argument("--select-cstring-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.select_cstring_jsonl)
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
                "source_select_cstring_decision": "needs_select_cstring_domain_context",
                **decide(row, states.get(segment_id)),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={sum(1 for row in reviewed if row['domain_decision'].startswith('select_cstring_domain_ready_'))}")
    print("apply_candidates_future=0")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
