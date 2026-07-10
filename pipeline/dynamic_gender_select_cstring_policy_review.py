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


SELECT_RE = re.compile(r"Select_CString\s*\((.*?)\)", re.IGNORECASE | re.DOTALL)
TOKEN_RE = re.compile(r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!", re.IGNORECASE)
CUSTOM_RE = re.compile(r"Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)", re.IGNORECASE)
SUBJECT_OBJECT_RE = re.compile(r"IsLocalPlayer|GetShortUIName|GetUIName|GetHerHim|GetSheHe|CHARACTER|TARGET_CHARACTER|ROOT\.Char", re.IGNORECASE)
ARTICLE_RE = re.compile(r"'(?:o|a|os|as|um|uma|uns|umas|seu personagem|sua personagem|esta|este|essa|esse|a esta|o este|las|los|você)'", re.IGNORECASE)
SIMPLE_GENDER_RE = re.compile(r"IsFemale|'[^']*(?:mulher|homem|menina|menino|reina|reyes|rainha|rei|pastora|pastor|caçadora|caçador|timadora|timador|buena mujer|buen hombre|niña|niño)[^']*'", re.IGNORECASE)
DOMAIN_RE = re.compile(r"hunt|pilgrimage|coronation|wedding|struggle|domínio|domain|reino|title|intent|opinion|artifact|activity|governor|contract|county|faith", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc|desc\.|_log$|_key$|flavor|activity|activities|hunt_|wedding|coronation|pilgrimage|tour_", re.IGNORECASE)
RESIDUAL_RE = re.compile(r"\b(?:tentará|oíste|oyó|lugareñ[ao]|pasasteis|pasaron|reinas|reyes|niñ[ao]|diestr[ao]|vassal[ao]s)\b", re.IGNORECASE)
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
        SELECT segment_id, state_group, needs_output_apply, confirmed_matches_output, needs_reopen, is_closed
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
        if row.get("decision") != "needs_select_cstring_gender_composer":
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
        ("Select_CString", re.compile(r"Select_CString", re.IGNORECASE)),
        ("CustomOrGenderLoc", CUSTOM_RE),
        ("SubjectObject", SUBJECT_OBJECT_RE),
        ("Article", ARTICLE_RE),
        ("SimpleGender", SIMPLE_GENDER_RE),
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
    if RESIDUAL_RE.search(text) or DOMAIN_RE.search(haystack) or EVENT_RE.search(haystack):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if int(state.get("needs_reopen") or 0) == 1:
        return "select_cstring_gender_ready_false_reopen"
    if ARTICLE_RE.search(text):
        return "select_cstring_gender_ready_article_lifecycle"
    if SIMPLE_GENDER_RE.search(text):
        return "select_cstring_gender_ready_simple_lifecycle"
    return None


def policy_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])
    if CUSTOM_RE.search(text) and not SELECT_RE.search(text):
        return "needs_custom_loc_gender_policy", "custom_loc_gender"
    if RESIDUAL_RE.search(text):
        return "needs_select_cstring_residual_repair", "visible_spanish_or_gender_residual"
    if DOMAIN_RE.search(haystack):
        return "needs_select_cstring_domain_context", "domain_context"
    if EVENT_RE.search(haystack):
        return "needs_select_cstring_event_context", "event_context"
    if CUSTOM_RE.search(text):
        return "needs_custom_loc_gender_policy", "custom_loc_mixed_with_select_cstring"
    if SUBJECT_OBJECT_RE.search(text) and re.search(r"Get(?:Short)?UIName|GetHerHim|GetSheHe", text):
        return "needs_select_cstring_subject_object_composer", "subject_object"
    if ARTICLE_RE.search(text):
        return "needs_select_cstring_article_gender_composer", "article_gender"
    if SIMPLE_GENDER_RE.search(text):
        return "needs_select_cstring_gender_simple_composer", "simple_masculine_feminine"
    if SELECT_RE.search(text):
        return "needs_new_microagent", "select_cstring_uncategorized"
    return "blocked_uncertain", "blocked_uncertain"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    ready = ready_decision(row, state)
    if ready:
        return {
            "select_cstring_decision": ready,
            "select_cstring_subpolicy": ready.removeprefix("select_cstring_gender_ready_").removesuffix("_lifecycle").removesuffix("_false_reopen"),
            "tokens_seen": tokens_seen(row["current_text"]),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "Select_CString gender surface appears aligned for future narrow lifecycle",
        }
    decision, subpolicy = policy_decision(row)
    return {
        "select_cstring_decision": decision,
        "select_cstring_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": f"routed to {decision}; no apply or lifecycle emitted by this review",
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_select_cstring_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["select_cstring_decision"] for row in rows)
    subpolicy_counts = Counter(row["select_cstring_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["select_cstring_decision"].startswith("select_cstring_gender_ready_"))
    needs_select_count = sum(count for decision, count in decision_counts.items() if decision.startswith("needs_select_cstring_"))

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    if ready_count >= 10:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif needs_select_count >= 20:
        recommendation = "prepare_specific_select_cstring_gender_composer"
    else:
        recommendation = "migrate_to_needs_es_oa_policy_or_domain_context"

    lines = [
        "Dynamic gender Select_CString policy review",
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
            f"needs_select_cstring_total: {needs_select_count}",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-gender-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.dynamic_gender_jsonl)
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
                "source_decision": "needs_select_cstring_gender_composer",
                **decide(row, states.get(segment_id)),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={sum(1 for row in reviewed if row['select_cstring_decision'].startswith('select_cstring_gender_ready_'))}")
    print("apply_candidates_future=0")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
