from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "short_label_style_run406_sublane_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 406
LEDGER_RUN_ID = 76
SAMPLE_PER_LANE = 6
RECENT_LEARNING_RUN_IDS = (691, 692, 693, 694)
RECENT_HOLD_SEGMENTS = {281274, 9291, 3934, 153501}

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|SelectLocalization\([^)]*\)|AddLocalizationIf\([^)]*\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+|"
    r"ScriptValue|Concept",
    re.IGNORECASE,
)
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)|ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b", re.IGNORECASE)
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf", re.IGNORECASE)
WARNING_RE = re.compile(r"@warning_icon|#X|#warning", re.IGNORECASE)
ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|glory|knight", re.IGNORECASE)
REQUIREMENT_RE = re.compile(r"requirement|unlock|valid|invalid|cannot|can_be|trigger|effect|tooltip|_tt$", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc$|desc\.|option|toast|activity|travel|journey|scheme|interaction", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"culture|religion|faith|title|law|dynasty|house|trait|nickname|artifact|building|war|realm|vassal",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:el|la|los|las|un|una|verdadero|verdadera|coste|actual|siguiente|"
    r"the|will|must|cannot|should|current|next)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_preflight_exclusions() -> tuple[Path | None, set[int]]:
    path = latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        return None, set()
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    return path, blocked | superseded


def recent_learning_segments(conn: sqlite3.Connection) -> set[int]:
    placeholders = ",".join("?" for _ in RECENT_LEARNING_RUN_IDS)
    return {
        int(row["segment_id"])
        for row in conn.execute(
            f"SELECT segment_id FROM local_learning_candidates WHERE run_id IN ({placeholders})",
            RECENT_LEARNING_RUN_IDS,
        )
    }


def short(text: str | None, limit: int = 220) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def word_count(text: str) -> int:
    return len(WORD_RE.findall(TOKEN_RE.sub(" ", text)))


def fetch_rows(conn: sqlite3.Connection, excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [LEDGER_RUN_ID, SEGMENT_STATE_RUN_ID]
    if excluded_ids:
        excluded_clause = "AND s.segment_id NOT IN (" + ",".join("?" for _ in excluded_ids) + ")"
        params.extend(sorted(excluded_ids))
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.review_state,
            s.confirmed_matches_output,
            s.needs_output_apply,
            l.issue_family,
            l.issue_kind,
            l.issue_severity,
            l.agent_key,
            l.route_status,
            l.proposed_action,
            src.english_text,
            src.spanish_text,
            src.old_text,
            o.portuguese_text AS current_output_text
        FROM segment_state_items s
        JOIN ml_issue_ledger_items l
          ON l.segment_id = s.segment_id
         AND l.run_id = ?
         AND l.status = 'open'
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.state_group = 'pending'
          AND COALESCE(s.needs_output_apply, 0) = 0
          AND COALESCE(l.agent_key, '') = 'micro_short_label_style'
          AND COALESCE(l.issue_family, '') = 'short_label_style_microagent'
          AND COALESCE(l.route_status, '') = 'candidate'
          AND COALESCE(l.proposed_action, '') = 'sample_short_label_style_policy'
          AND COALESCE(s.final_state, '') = 'reopen_auto_confirmed_autofix'
          {excluded_clause}
        ORDER BY s.priority_score DESC, s.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def shape_bucket(row: dict[str, Any]) -> str:
    text = str(row.get("current_output_text") or "")
    key = str(row.get("source_key") or "")
    path = str(row.get("relative_path") or "")
    haystack = " ".join([path, key, text])
    tokens = TOKEN_RE.findall(text)
    wc = word_count(text)
    if ES_HELPER_RE.search(haystack):
        return "gender_or_es_helper"
    if SELECT_RE.search(text):
        return "select_or_localization_expression"
    if WARNING_RE.search(text):
        return "warning_or_blocked_tooltip"
    if ACCOLADE_RE.search(haystack):
        return "accolade_ui_label"
    if REQUIREMENT_RE.search(haystack):
        return "requirement_tooltip"
    if EVENT_RE.search(haystack):
        return "event_option_or_short_context"
    if DOMAIN_RE.search(haystack):
        return "domain_short_label"
    if "\n" in text or len(tokens) >= 4:
        return "token_dense_multiline_label"
    if tokens:
        return "light_token_short_label"
    if wc <= 4 and len(text) <= 60:
        return "plain_short_label"
    if len(text) <= 110:
        return "plain_short_phrase"
    return "longer_label_or_context"


def risk_bucket(row: dict[str, Any], shape: str) -> str:
    text = str(row.get("current_output_text") or "")
    token_count = len(TOKEN_RE.findall(text))
    if RESIDUAL_RE.search(text):
        return "residual_language_risk"
    if shape in {"gender_or_es_helper", "select_or_localization_expression"}:
        return "high_dynamic_context"
    if token_count >= 4:
        return "medium_dynamic_dense"
    if token_count:
        return "medium_dynamic_light"
    return "low_plain"


def decision_for(shape: str, risk: str, row: dict[str, Any]) -> tuple[str, str, bool]:
    if risk == "residual_language_risk":
        return "needs_human_residual_review", "visible residual-language risk", False
    if risk == "high_dynamic_context":
        return "needs_architecture_or_domain_policy", "branching/gender/localization expression surface", True
    if shape in {"warning_or_blocked_tooltip", "requirement_tooltip", "accolade_ui_label"} and risk in {
        "medium_dynamic_light",
        "medium_dynamic_dense",
    }:
        return "needs_guarded_policy_review", "UI/tooltip token surface needs guarded policy triage", False
    if shape in {"plain_short_label", "plain_short_phrase"} and risk == "low_plain":
        return "human_sample_or_policy_closure_candidate", "plain short surface with no token risk", False
    if shape == "light_token_short_label" and risk == "medium_dynamic_light":
        return "guarded_human_sample_candidate", "short label with light token surface", False
    if shape in {"domain_short_label", "event_option_or_short_context"}:
        return "needs_domain_or_event_context_review", "short label depends on domain/event semantics", False
    return "hold_mixed_or_token_dense", "mixed or dense surface not suitable for broad policy", False


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    shape = shape_bucket(row)
    risk = risk_bucket(row, shape)
    decision, rationale, requires_architecture = decision_for(shape, risk, row)
    text = str(row.get("current_output_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "shape_bucket": shape,
        "risk_bucket": risk,
        "decision": decision,
        "rationale": rationale,
        "requires_architecture": requires_architecture,
        "token_count": len(TOKEN_RE.findall(text)),
        "word_count": word_count(text),
        "text_length": len(text),
        "current_output_text": short(text),
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
    }


def build_summary(rows: list[dict[str, Any]], preflight_path: Path | None, excluded_count: int) -> dict[str, Any]:
    enriched = [enrich(row) for row in rows]
    shape_counts = Counter(row["shape_bucket"] for row in enriched)
    risk_counts = Counter(row["risk_bucket"] for row in enriched)
    decision_counts = Counter(row["decision"] for row in enriched)
    sample_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        if len(sample_by_decision[row["decision"]]) < SAMPLE_PER_LANE:
            sample_by_decision[row["decision"]].append(row)
    recommended_decision = None
    priority = [
        "human_sample_or_policy_closure_candidate",
        "guarded_human_sample_candidate",
        "needs_guarded_policy_review",
        "needs_domain_or_event_context_review",
        "needs_architecture_or_domain_policy",
    ]
    for decision in priority:
        if decision_counts.get(decision):
            recommended_decision = decision
            break
    architecture_needed = bool(recommended_decision == "needs_architecture_or_domain_policy")
    next_action = (
        "prepare_readonly_human_sample_for_plain_short_label"
        if recommended_decision == "human_sample_or_policy_closure_candidate"
        else "prepare_guarded_readonly_policy_review_for_short_label"
        if recommended_decision in {"guarded_human_sample_candidate", "needs_guarded_policy_review"}
        else "prepare_architecture_prompt_for_short_label_dynamic_surface"
        if architecture_needed
        else "continue_readonly_sublane_review"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "excluded_segment_count": excluded_count,
        "rows_reviewed": len(enriched),
        "shape_counts": [{"key": key, "count": value} for key, value in shape_counts.most_common()],
        "risk_counts": [{"key": key, "count": value} for key, value in risk_counts.most_common()],
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "recommended_decision": recommended_decision,
        "recommended_decision_count": int(decision_counts.get(recommended_decision or "", 0)),
        "architecture_needed_before_next_step": architecture_needed,
        "sample_by_decision": dict(sample_by_decision),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_short_label_style_run406_sublane_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(enrich(row), ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "short label style run406 sublane diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"excluded_segment_count={summary['excluded_segment_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "shape_counts:"])
    for item in summary["shape_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "risk_counts:"])
    for item in summary["risk_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"recommended_decision={summary['recommended_decision']}",
            f"recommended_decision_count={summary['recommended_decision_count']}",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    preflight_path, preflight_excluded = load_preflight_exclusions()
    with connect_readonly() as conn:
        excluded = preflight_excluded | recent_learning_segments(conn) | RECENT_HOLD_SEGMENTS
        rows = fetch_rows(conn, excluded)
    summary = build_summary(rows, preflight_path, len(excluded))
    txt_path, jsonl_path, summary_path = write_outputs(summary, rows)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_reviewed={summary['rows_reviewed']}")
    print(f"recommended_decision={summary['recommended_decision']}")
    print(f"recommended_decision_count={summary['recommended_decision_count']}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
