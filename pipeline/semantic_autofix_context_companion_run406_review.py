from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_autofix_context_companion_run406_review_v1"
INPUT_JSONL = Path("reports/20260624_223044_119277_autofix_unknown_run406_cluster_diagnostic.jsonl")
SEGMENT_STATE_RUN_ID = 406
LEDGER_RUN_ID = 76
TARGET_LANE = "semantic_autofix_context_companion_review"
SAMPLE_PER_DECISION = 6

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+|"
    r"ScriptValue|Concept|AddLocalizationIf|SelectLocalization",
    re.IGNORECASE,
)
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf", re.IGNORECASE)
ES_HELPER_RE = re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|\.Custom\('ES_", re.IGNORECASE)
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:adem[aá]s|ahora|alg[uú]n|aunque|caballero|cielos|consejo|coste|cualquier|"
    r"elige|eres|hacerte|hacerle|maravilloso|mientras|ning[uú]n|puede|pueden|quieres|"
    r"siguiente|tambi[eé]n|vuestro|vuestra|vuestras|vuestros)\b",
    re.IGNORECASE,
)
ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|glory|knight", re.IGNORECASE)
RELIGION_RE = re.compile(r"religion|faith|doctrine|tenet|holy|piety|church|clergy", re.IGNORECASE)
CULTURE_RE = re.compile(r"culture|tradition|heritage|language|ethos|innovation", re.IGNORECASE)
TITLE_RE = re.compile(r"title|duchy|kingdom|empire|county|barony|realm|vassal|liege|succession", re.IGNORECASE)
NAME_RE = re.compile(r"nickname|dynasty|house|GetName|GetFirstName|GetShortUIName|historical", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc|option|toast|letter|flavor|activity|travel|journey|tour|scheme|contract|hold_court", re.IGNORECASE)
UI_RE = re.compile(r"tooltip|_tt$|_desc$|modifier|effect|opinion|bonus|penalty|game_concept", re.IGNORECASE)
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


def select_input_ids(rows: list[dict[str, Any]]) -> list[int]:
    ids = sorted({int(row["segment_id"]) for row in rows if row.get("action_lane") == TARGET_LANE})
    if not ids:
        raise SystemExit(f"no rows for action_lane={TARGET_LANE}")
    return ids


def fetch_live_rows(conn: sqlite3.Connection, segment_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
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
            s.needs_reopen,
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
        (LEDGER_RUN_ID, *segment_ids, SEGMENT_STATE_RUN_ID, *segment_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def domain_bucket(row: dict[str, Any]) -> str:
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "english_text", "spanish_text", "current_output_text"))
    if ACCOLADE_RE.search(haystack):
        return "accolade_knight_glory"
    if RELIGION_RE.search(haystack):
        return "religion_faith_doctrine"
    if CULTURE_RE.search(haystack):
        return "culture_tradition_innovation"
    if TITLE_RE.search(haystack):
        return "title_realm_governance"
    if NAME_RE.search(haystack):
        return "name_dynasty_historical"
    if EVENT_RE.search(haystack):
        return "event_activity_context"
    if UI_RE.search(haystack):
        return "ui_tooltip_modifier"
    return "general_context"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = str(row.get("current_output_text") or "")
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "issue_kinds", "english_text", "spanish_text", "current_output_text"))
    token_count = len(TOKEN_RE.findall(current))
    words = word_count(current)
    bucket = domain_bucket(row)
    state_ok = (
        row.get("state_group") == "pending"
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("autofix_count") or 0) > 0
        and int(row.get("semantic_count") or 0) > 0
        and int(row.get("other_issue_count") or 0) == 0
    )
    decision = "blocked_uncertain"
    reason = "fallback uncertain companion context"
    lifecycle_candidate = False
    requires_architecture = False
    false_safe_risk = False

    if not state_ok:
        decision = "blocked_state_guard"
        reason = "pending/output/issue-pair guard failed in current run"
        false_safe_risk = True
    elif int(row.get("high_issue_count") or 0) > 0:
        decision = "needs_human_review_high_issue"
        reason = "high severity companion issue present"
        false_safe_risk = True
    elif ES_HELPER_RE.search(haystack) or SELECT_RE.search(haystack):
        decision = "needs_dynamic_expression_or_gender_policy"
        reason = "SelectLocalization/Select_CString/ES helper requires dynamic policy"
        requires_architecture = True
    elif SPANISH_RESIDUE_RE.search(current):
        decision = "needs_repair_spanish_residue"
        reason = "visible Spanish residue risk"
        false_safe_risk = True
    elif token_count >= 4:
        decision = "needs_dynamic_context_companion_policy"
        reason = "token-dense CK3 surface needs guarded companion policy"
        requires_architecture = bucket in {"accolade_knight_glory", "religion_faith_doctrine", "culture_tradition_innovation"}
    elif bucket in {"accolade_knight_glory", "religion_faith_doctrine", "culture_tradition_innovation", "title_realm_governance", "name_dynasty_historical"}:
        decision = "needs_domain_context_companion_policy"
        reason = f"domain-sensitive companion surface: {bucket}"
    elif bucket == "event_activity_context" or words > 18 or "\\n" in current or "\n" in current:
        decision = "needs_event_context_companion_composer"
        reason = "event/prose/context window required before companion lifecycle"
    elif token_count > 0:
        decision = "needs_light_dynamic_companion_policy"
        reason = "light token surface can be reviewed as a narrow guarded sublane"
    elif bucket == "ui_tooltip_modifier" and words <= 16:
        decision = "companion_lifecycle_candidate_ui_modifier"
        reason = "short UI/modifier text with exact autofix+semantic pair and clean state guards"
        lifecycle_candidate = True
    elif words <= 12:
        decision = "companion_lifecycle_candidate_plain_short"
        reason = "short plain companion text with exact autofix+semantic pair and clean state guards"
        lifecycle_candidate = True
    else:
        decision = "needs_plain_context_companion_composer"
        reason = "plain text still needs context composer before lifecycle"

    return {
        "segment_id": int(row["segment_id"]),
        "decision": decision,
        "reason": reason,
        "domain_bucket": bucket,
        "lifecycle_candidate": lifecycle_candidate,
        "requires_apply_later": False,
        "requires_lifecycle_later": lifecycle_candidate,
        "requires_architecture": requires_architecture,
        "false_safe_risk": false_safe_risk,
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state"),
        "state_group": row.get("state_group"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "issue_families": row.get("issue_families"),
        "issue_kinds": row.get("issue_kinds"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "token_count": token_count,
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
    domain_counts = Counter(row["domain_bucket"] for row in reviewed)
    file_counts = Counter(row["relative_path"] for row in reviewed)
    prefix_counts = Counter(str(row["source_key"] or "").split("_", 1)[0] for row in reviewed)
    lifecycle_count = sum(1 for row in reviewed if row["lifecycle_candidate"])
    architecture_count = sum(1 for row in reviewed if row["requires_architecture"])
    false_safe_count = sum(1 for row in reviewed if row["false_safe_risk"])
    human_or_context_count = len(reviewed) - lifecycle_count
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reviewed:
        if len(samples[row["decision"]]) < SAMPLE_PER_DECISION:
            samples[row["decision"]].append(row)

    dominant_decision = decision_counts.most_common(1)[0][0] if decision_counts else None
    if architecture_count > 0 and dominant_decision == "needs_dynamic_context_companion_policy":
        next_action = "consult_architecture_for_dynamic_domain_companion_policy"
        recommendation = "architecture_first_for_dominant_dynamic_domain_surface"
    elif lifecycle_count >= 120 and false_safe_count == 0:
        next_action = "prepare_narrow_readonly_lifecycle_bridge_for_lifecycle_candidates"
        recommendation = "narrow_lifecycle_policy_candidate"
    else:
        next_action = "split_top_context_sublanes_before_policy_or_lifecycle"
        recommendation = "split_context_companion_lane"

    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(project_file(INPUT_JSONL)),
        "target_lane": TARGET_LANE,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "input_lane_count": input_count,
        "reviewed_count": len(reviewed),
        "decision_counts": counter_list(decision_counts),
        "domain_counts": counter_list(domain_counts),
        "top_files": counter_list(file_counts),
        "top_source_key_prefixes": counter_list(prefix_counts),
        "lifecycle_candidate_count": lifecycle_count,
        "human_or_context_count": human_or_context_count,
        "requires_architecture_count": architecture_count,
        "requires_apply_later_count": 0,
        "false_safe_risk_count": false_safe_count,
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
    base = reports_dir() / f"{stamp()}_semantic_autofix_context_companion_run406_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic autofix context companion run406 review",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"target_lane={TARGET_LANE}",
        f"input_lane_count={summary['input_lane_count']}",
        f"reviewed_count={summary['reviewed_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "domain_counts:"])
    for item in summary["domain_counts"][:15]:
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
    if not input_path.exists():
        raise SystemExit(f"input not found: {input_path}")
    input_rows = read_jsonl(input_path)
    segment_ids = select_input_ids(input_rows)
    with connect_readonly() as conn:
        live_rows = fetch_live_rows(conn, segment_ids)
    if len(live_rows) != len(segment_ids):
        raise SystemExit(f"live row count mismatch: expected={len(segment_ids)} got={len(live_rows)}")
    reviewed = [classify(row) for row in live_rows]
    summary = build_summary(reviewed, len(segment_ids))
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
