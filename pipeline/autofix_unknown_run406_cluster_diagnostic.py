from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "autofix_unknown_run406_cluster_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 406
LEDGER_RUN_ID = 76
SAMPLE_PER_CLUSTER = 6

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
DOMAIN_RE = re.compile(
    r"historical_characters|culture|religion|faith|holy_order|law|succession|title|rank|"
    r"trait_|nickname|dynasty|house|government|factions|ai_personality|great_project|"
    r"artifact|buildings|regiment|accolade|acclaimed_knight|diarch|struggle|innovation",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc$|desc\.|option|toast|interaction|scheme|memory|memories|bookmark|story_cycle|"
    r"activity|travel|journey|tour|contract|hold_court|court|yearly|lifestyle",
    re.IGNORECASE,
)
UI_RE = re.compile(r"tooltip|_tt$|_desc$|game_rules|tutorial|interface|gui|effect|modifier|opinion", re.IGNORECASE)
NEW_SURFACE_RE = re.compile(
    r"combat|weapon|vassal|stance|claim|casus|belli|prefix|epithet|landed|accolade|knight|"
    r"artifact|building|regiment|travel|activity",
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


def load_preflight_exclusions() -> tuple[Path | None, set[int], dict[str, Any]]:
    path = latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        return None, set(), {}
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    return path, blocked | superseded, data


def short(text: str | None, limit: int = 240) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def word_count(text: str) -> int:
    cleaned = TOKEN_RE.sub(" ", text)
    return len(WORD_RE.findall(cleaned))


def fetch_rows(conn: sqlite3.Connection, excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [LEDGER_RUN_ID, SEGMENT_STATE_RUN_ID]
    if excluded_ids:
        excluded_clause = "AND s.segment_id NOT IN (" + ",".join("?" for _ in excluded_ids) + ")"
        params.extend(sorted(excluded_ids))
    rows = conn.execute(
        f"""
        WITH open_issues AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family = 'short_label_style_microagent' THEN 1 ELSE 0 END) AS short_label_count,
                SUM(CASE WHEN issue_family = 'gender_token_microagent' THEN 1 ELSE 0 END) AS gender_count,
                SUM(CASE WHEN issue_family NOT IN (
                    'autofix_unknown_microagent',
                    'semantic_review_router',
                    'short_label_style_microagent',
                    'gender_token_microagent'
                ) THEN 1 ELSE 0 END) AS other_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
                GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds,
                GROUP_CONCAT(DISTINCT agent_key) AS agent_keys,
                GROUP_CONCAT(DISTINCT route_status) AS route_statuses,
                GROUP_CONCAT(DISTINCT proposed_action) AS proposed_actions
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
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
            s.priority_score,
            src.english_text,
            src.spanish_text,
            src.old_text,
            o.portuguese_text AS current_output_text,
            oi.open_issue_count,
            oi.autofix_count,
            oi.semantic_count,
            oi.short_label_count,
            oi.gender_count,
            oi.other_issue_count,
            oi.high_issue_count,
            oi.issue_families,
            oi.issue_kinds,
            oi.agent_keys,
            oi.route_statuses,
            oi.proposed_actions
        FROM open_issues oi
        JOIN segment_state_items s
          ON s.segment_id = oi.segment_id
         AND s.run_id = ?
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.segment_id
        WHERE s.state_group = 'pending'
          AND COALESCE(s.needs_output_apply, 0) = 0
          AND oi.autofix_count > 0
          {excluded_clause}
        ORDER BY s.priority_score DESC, s.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def issue_combo(row: dict[str, Any]) -> str:
    families = sorted(str(row.get("issue_families") or "").split(","))
    return "+".join(f for f in families if f)


def surface_bucket(row: dict[str, Any]) -> str:
    text = str(row.get("current_output_text") or "")
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "issue_kinds", "issue_families", "english_text", "spanish_text", "current_output_text")
    )
    if ES_HELPER_RE.search(haystack):
        return "gender_or_es_helper"
    if SELECT_RE.search(haystack):
        return "select_or_localization_expression"
    if DOMAIN_RE.search(haystack):
        return "domain_sensitive"
    if EVENT_RE.search(haystack) or word_count(text) >= 12:
        return "event_or_context_prose"
    if UI_RE.search(haystack):
        return "ui_tooltip_modifier"
    if len(text) <= 80:
        return "short_plain_or_label"
    return "plain_sentence_or_fragment"


def risk_bucket(row: dict[str, Any], surface: str) -> str:
    text = str(row.get("current_output_text") or "")
    token_count = len(TOKEN_RE.findall(text))
    if int(row.get("high_issue_count") or 0) > 0:
        return "high_issue"
    if ES_HELPER_RE.search(text) or SELECT_RE.search(text):
        return "high_context_dynamic"
    if token_count >= 4:
        return "medium_dynamic_dense"
    if token_count:
        return "medium_dynamic_light"
    if SPANISH_RESIDUE_RE.search(text):
        return "context_risk_spanish_residue"
    if surface in {"domain_sensitive", "event_or_context_prose"}:
        return "medium_context_plain"
    return "low_plain_text"


def action_lane(row: dict[str, Any], surface: str, risk: str) -> str:
    combo = issue_combo(row)
    text = str(row.get("current_output_text") or "")
    if risk in {"high_issue", "high_context_dynamic"}:
        return "human_or_architecture_required"
    if "semantic_review_router" in combo and "autofix_unknown_microagent" in combo:
        if surface in {"event_or_context_prose", "domain_sensitive"}:
            return "semantic_autofix_context_companion_review"
        return "semantic_autofix_lifecycle_companion_candidate"
    if "short_label_style_microagent" in combo and "autofix_unknown_microagent" in combo:
        return "short_label_autofix_companion_review"
    if int(row.get("open_issue_count") or 0) == 1:
        if risk == "low_plain_text" and surface in {"short_plain_or_label", "plain_sentence_or_fragment", "ui_tooltip_modifier"}:
            return "single_family_lifecycle_review_candidate"
        if surface == "domain_sensitive":
            return "single_family_domain_policy_review"
        if surface == "event_or_context_prose":
            return "single_family_context_composer_review"
        if TOKEN_RE.search(text):
            return "single_family_dynamic_expression_review"
    if NEW_SURFACE_RE.search(" ".join([str(row.get("relative_path") or ""), str(row.get("source_key") or "")])):
        return "new_surface_microagent_split"
    return "manual_cluster_review"


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    surface = surface_bucket(row)
    risk = risk_bucket(row, surface)
    lane = action_lane(row, surface, risk)
    text = str(row.get("current_output_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "issue_combo": issue_combo(row),
        "issue_families": row.get("issue_families"),
        "issue_kinds": row.get("issue_kinds"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "surface_bucket": surface,
        "risk_bucket": risk,
        "action_lane": lane,
        "token_count": len(TOKEN_RE.findall(text)),
        "word_count": word_count(text),
        "text_length": len(text),
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
        "current_output_text": short(text),
    }


def counter_list(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_summary(rows: list[dict[str, Any]], preflight_path: Path | None, excluded_count: int) -> dict[str, Any]:
    enriched = [enrich(row) for row in rows]
    combo_counts = Counter(row["issue_combo"] for row in enriched)
    surface_counts = Counter(row["surface_bucket"] for row in enriched)
    risk_counts = Counter(row["risk_bucket"] for row in enriched)
    lane_counts = Counter(row["action_lane"] for row in enriched)
    file_counts = Counter(str(row["relative_path"]) for row in enriched)
    prefix_counts = Counter(str(row["source_key"] or "").split("_", 1)[0] for row in enriched)

    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        key = row["action_lane"]
        if len(samples[key]) < SAMPLE_PER_CLUSTER:
            samples[key].append(row)

    top_lane = lane_counts.most_common(1)[0][0] if lane_counts else None
    policy_candidate_lanes = {
        "single_family_lifecycle_review_candidate",
        "semantic_autofix_lifecycle_companion_candidate",
    }
    review_candidate_lanes = {
        "semantic_autofix_context_companion_review",
        "single_family_context_composer_review",
        "single_family_domain_policy_review",
        "short_label_autofix_companion_review",
    }
    architecture_needed = bool(top_lane in {"new_surface_microagent_split", "human_or_architecture_required"})
    if top_lane in policy_candidate_lanes:
        next_action = "prepare_narrow_readonly_lifecycle_policy_review"
    elif top_lane in review_candidate_lanes:
        next_action = "prepare_readonly_cluster_review_for_top_lane"
    elif architecture_needed:
        next_action = "consult_architecture_before_new_microagent_or_dynamic_policy"
    else:
        next_action = "manual_cluster_triage_readonly"

    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": excluded_count,
        "rows_reviewed": len(enriched),
        "issue_combo_counts": counter_list(combo_counts),
        "surface_counts": counter_list(surface_counts),
        "risk_counts": counter_list(risk_counts),
        "action_lane_counts": counter_list(lane_counts),
        "top_files": counter_list(file_counts),
        "top_source_key_prefixes": counter_list(prefix_counts),
        "recommended_lane": top_lane,
        "recommended_lane_count": lane_counts[top_lane] if top_lane else 0,
        "architecture_needed_before_next_step": architecture_needed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "discovery_recommended_now": False,
        "retarget_recommended_now": False,
        "apply_recommended_now": False,
        "next_action": next_action,
        "sample_by_action_lane": dict(samples),
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_autofix_unknown_run406_cluster_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(enrich(row), ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "autofix unknown run406 cluster diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"preflight_excluded_segment_count={summary['preflight_excluded_segment_count']}",
        "",
        "action_lane_counts:",
    ]
    for item in summary["action_lane_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "issue_combo_counts:"])
    for item in summary["issue_combo_counts"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "surface_counts:"])
    for item in summary["surface_counts"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "risk_counts:"])
    for item in summary["risk_counts"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"recommended_lane={summary['recommended_lane']}",
            f"recommended_lane_count={summary['recommended_lane_count']}",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"apply_recommended_now={str(summary['apply_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    preflight_path, excluded_ids, _ = load_preflight_exclusions()
    with connect_readonly() as conn:
        rows = fetch_rows(conn, excluded_ids)
    summary = build_summary(rows, preflight_path, len(excluded_ids))
    txt_path, jsonl_path, summary_path = write_outputs(summary, rows)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_reviewed={summary['rows_reviewed']}")
    print(f"recommended_lane={summary['recommended_lane']}")
    print(f"recommended_lane_count={summary['recommended_lane_count']}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
