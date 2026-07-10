from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "event_context_companion_run406_sublane_review_v1"
INPUT_JSONL = Path("reports/20260624_231450_589881_semantic_autofix_context_companion_run406_review.jsonl")
SEGMENT_STATE_RUN_ID = 406
LEDGER_RUN_ID = 76
TARGET_DECISION = "needs_event_context_companion_composer"
SAMPLE_PER_SUBLANE = 6

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+|"
    r"ScriptValue|Concept|AddLocalizationIf|SelectLocalization",
    re.IGNORECASE,
)
SPANISH_RE = re.compile(
    r"\b(?:adem[aá]s|ahora|alg[uú]n|aunque|caballero|cielos|consejo|coste|cualquier|"
    r"elige|eres|hacerte|hacerle|maravilloso|mientras|ning[uú]n|puede|pueden|quieres|"
    r"siguiente|tambi[eé]n|vuestro|vuestra|vuestras|vuestros)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(r"\b(?:will|must|cannot|should|for each|the\s+[A-Z][a-z]+|your\s+[A-Za-z]+)\b")
EVENT_DIALOGUE_RE = re.compile(r"\.(?:a|b|c|d|e|f|g|h|i|j)$|option|answer|response|toast|letter|dialogue", re.IGNORECASE)
EVENT_DESC_RE = re.compile(r"\.desc|_desc$|flavor|story|outro|intro|event", re.IGNORECASE)
ACTIVITY_RE = re.compile(r"activity|activities|travel|journey|tour|tournament|contest|hunt|feast|roaming|survey", re.IGNORECASE)
SCHEME_RE = re.compile(r"scheme|murder|contract|laamp|task_contract|interaction|hook", re.IGNORECASE)
LORE_RE = re.compile(r"artifact|legend|lore|historical|chronicle|book|poem|sword|relic", re.IGNORECASE)
TUTORIAL_RE = re.compile(r"tutorial|game_concept|game_rule|ai_personality|tooltip|_tt$", re.IGNORECASE)
DOMAIN_RE = re.compile(r"culture|religion|faith|title|law|succession|dynasty|house|government|trait|nickname", re.IGNORECASE)
WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_file(path: Path) -> Path:
    return db.project_path(str(path))


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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


def short(text: str | None, limit: int = 320) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def word_count(text: str) -> int:
    return len(WORD_RE.findall(TOKEN_RE.sub(" ", text)))


def selected_ids(rows: list[dict[str, Any]]) -> list[int]:
    ids = sorted({int(row["segment_id"]) for row in rows if row.get("decision") == TARGET_DECISION})
    if not ids:
        raise SystemExit(f"no input rows for decision={TARGET_DECISION}")
    return ids


def fetch_rows(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        WITH open_issues AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family NOT IN ('autofix_unknown_microagent', 'semantic_review_router') THEN 1 ELSE 0 END) AS other_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
                GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
              AND segment_id IN ({placeholders})
            GROUP BY segment_id
        )
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.state_group,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.review_state,
            src.english_text,
            src.spanish_text,
            src.old_text,
            o.portuguese_text AS current_output_text,
            oi.open_issue_count,
            oi.autofix_count,
            oi.semantic_count,
            oi.other_issue_count,
            oi.high_issue_count,
            oi.issue_families,
            oi.issue_kinds
        FROM segment_state_items s
        JOIN open_issues oi ON oi.segment_id = s.segment_id
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.segment_id IN ({placeholders})
        ORDER BY s.relative_path, s.source_key, s.segment_id
        """,
        (LEDGER_RUN_ID, *ids, SEGMENT_STATE_RUN_ID, *ids),
    ).fetchall()
    return [dict(row) for row in rows]


def sublane(row: dict[str, Any]) -> str:
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "issue_kinds", "english_text", "spanish_text"))
    current = str(row.get("current_output_text") or "")
    if TUTORIAL_RE.search(haystack):
        return "tutorial_ui_or_game_concept"
    if EVENT_DIALOGUE_RE.search(haystack) or any(mark in current for mark in ('"', "“", "”", "«", "»")):
        return "event_option_dialogue"
    if ACTIVITY_RE.search(haystack):
        return "activity_travel_tournament"
    if SCHEME_RE.search(haystack):
        return "scheme_contract_interaction"
    if LORE_RE.search(haystack):
        return "artifact_lore_historical"
    if DOMAIN_RE.search(haystack):
        return "domain_embedded_event_context"
    if EVENT_DESC_RE.search(haystack):
        return "event_description_prose"
    return "general_context_prose"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = str(row.get("current_output_text") or "")
    lane = sublane(row)
    tokens = len(TOKEN_RE.findall(current))
    words = word_count(current)
    state_ok = (
        row.get("state_group") == "pending"
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("autofix_count") or 0) > 0
        and int(row.get("semantic_count") or 0) > 0
        and int(row.get("other_issue_count") or 0) == 0
    )
    false_safe = False
    requires_architecture = False
    lifecycle_candidate = False

    if not state_ok:
        decision = "blocked_state_guard"
        reason = "live state or exact issue-pair guard failed"
        false_safe = True
    elif int(row.get("high_issue_count") or 0) > 0:
        decision = "needs_human_review_high_issue"
        reason = "high severity issue present"
        false_safe = True
    elif SPANISH_RE.search(current) or ENGLISH_RE.search(current):
        decision = "needs_residual_repair"
        reason = "visible Spanish/English residue risk"
        false_safe = True
    elif tokens >= 4:
        decision = "needs_token_dense_event_context_policy"
        reason = "event/context surface is token dense"
        requires_architecture = lane in {"activity_travel_tournament", "domain_embedded_event_context"}
    elif lane == "tutorial_ui_or_game_concept" and words <= 28:
        decision = "event_context_lifecycle_candidate_tutorial_ui"
        reason = "bounded tutorial/UI concept text with clean guards"
        lifecycle_candidate = True
    elif lane == "event_option_dialogue" and words <= 14 and tokens == 0:
        decision = "event_context_lifecycle_candidate_short_option"
        reason = "short plain option/dialogue text with clean guards"
        lifecycle_candidate = True
    elif lane in {"artifact_lore_historical", "domain_embedded_event_context"}:
        decision = "needs_domain_or_lore_context_review"
        reason = f"context semantics depend on {lane}"
    elif lane in {"activity_travel_tournament", "scheme_contract_interaction"}:
        decision = "needs_activity_or_scheme_context_composer"
        reason = f"needs context composer for {lane}"
    elif words > 26 or "\\n" in current or "\n" in current:
        decision = "needs_long_event_context_composer"
        reason = "long or multiline prose requires context window"
    elif tokens > 0:
        decision = "needs_light_token_event_context_policy"
        reason = "light token event context needs guarded policy"
    else:
        decision = "needs_plain_event_context_composer"
        reason = "plain event/context prose needs composer review"

    return {
        "segment_id": int(row["segment_id"]),
        "decision": decision,
        "reason": reason,
        "sublane": lane,
        "lifecycle_candidate": lifecycle_candidate,
        "requires_architecture": requires_architecture,
        "requires_apply_later": False,
        "requires_lifecycle_later": lifecycle_candidate,
        "false_safe_risk": false_safe,
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state"),
        "state_group": row.get("state_group"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "issue_families": row.get("issue_families"),
        "issue_kinds": row.get("issue_kinds"),
        "token_count": tokens,
        "word_count": words,
        "text_length": len(current),
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
        "current_output_text": short(current),
    }


def counter_list(counter: Counter[str], limit: int = 30) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_summary(reviewed: list[dict[str, Any]], input_count: int) -> dict[str, Any]:
    decision_counts = Counter(row["decision"] for row in reviewed)
    sublane_counts = Counter(row["sublane"] for row in reviewed)
    file_counts = Counter(row["relative_path"] for row in reviewed)
    lifecycle_count = sum(1 for row in reviewed if row["lifecycle_candidate"])
    architecture_count = sum(1 for row in reviewed if row["requires_architecture"])
    false_safe = sum(1 for row in reviewed if row["false_safe_risk"])
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reviewed:
        if len(samples[row["decision"]]) < SAMPLE_PER_SUBLANE:
            samples[row["decision"]].append(row)

    dominant = decision_counts.most_common(1)[0][0] if decision_counts else None
    if lifecycle_count >= 80 and false_safe == 0:
        recommendation = "prepare_narrow_event_context_lifecycle_policy"
        next_action = "consult_architecture_for_event_context_lifecycle_bridge"
    elif architecture_count > 0 and dominant == "needs_token_dense_event_context_policy":
        recommendation = "architecture_needed_for_token_dense_event_context_policy"
        next_action = "consult_architecture_before_policy"
    else:
        recommendation = "split_or_review_context_composer_sublanes"
        next_action = "prepare_readonly_review_for_largest_non_architecture_sublane"

    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(project_file(INPUT_JSONL)),
        "target_decision": TARGET_DECISION,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "input_count": input_count,
        "reviewed_count": len(reviewed),
        "decision_counts": counter_list(decision_counts),
        "sublane_counts": counter_list(sublane_counts),
        "top_files": counter_list(file_counts),
        "lifecycle_candidate_count": lifecycle_count,
        "human_or_context_count": len(reviewed) - lifecycle_count,
        "requires_architecture_count": architecture_count,
        "requires_apply_later_count": 0,
        "false_safe_risk_count": false_safe,
        "register_policy_now": False,
        "production_full_recommended_now": False,
        "apply_ready_now": 0,
        "discovery_recommended_now": False,
        "retarget_recommended_now": False,
        "recommendation": recommendation,
        "next_action": next_action,
        "sample_by_decision": dict(samples),
    }


def write_outputs(reviewed: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_event_context_companion_run406_sublane_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "event context companion run406 sublane review",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"target_decision={TARGET_DECISION}",
        f"input_count={summary['input_count']}",
        f"reviewed_count={summary['reviewed_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "sublane_counts:"])
    for item in summary["sublane_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"lifecycle_candidate_count={summary['lifecycle_candidate_count']}",
            f"human_or_context_count={summary['human_or_context_count']}",
            f"requires_architecture_count={summary['requires_architecture_count']}",
            f"requires_apply_later_count={summary['requires_apply_later_count']}",
            f"false_safe_risk_count={summary['false_safe_risk_count']}",
            f"register_policy_now={str(summary['register_policy_now']).lower()}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"recommendation={summary['recommendation']}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = project_file(INPUT_JSONL)
    rows = read_jsonl(input_path)
    ids = selected_ids(rows)
    with connect_readonly() as conn:
        live_rows = fetch_rows(conn, ids)
    if len(live_rows) != len(ids):
        raise SystemExit(f"live row count mismatch: expected={len(ids)} got={len(live_rows)}")
    reviewed = [classify(row) for row in live_rows]
    summary = build_summary(reviewed, len(ids))
    txt_path, jsonl_path, summary_path = write_outputs(reviewed, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"lifecycle_candidate_count={summary['lifecycle_candidate_count']}")
    print(f"requires_architecture_count={summary['requires_architecture_count']}")
    print(f"false_safe_risk_count={summary['false_safe_risk_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
