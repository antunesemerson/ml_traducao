from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_deep_diagnostic_v1"
SAMPLE_LIMIT = 400
SAMPLE_PER_SUBLANE = 40

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+"
)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
SELECT_CSTRING_RE = re.compile(r"Select_CString\(")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:cielos|maravilloso|hacerte|hacerle|eres|estancia|galard[oó]n|"
    r"coste|actual|siguiente|elige|del|la|los|las|tu|tus|su|sus)\b",
    re.IGNORECASE,
)

RELIGION_RE = re.compile(r"\b(?:faith|religion|doctrine|tenet|holy|piety|church|clergy|heresy|worship|pilgrim|hajj|jihad|crusade|sin|virtue|zeal|devotion|sacred|temple)\b", re.IGNORECASE)
HOLY_WAR_RE = re.compile(r"\b(?:holy[_ ]?war|crusade|jihad|great[_ ]holy|piety|pilgrim|hajj|safa|marwah|zakat|damm|sadaqah)\b", re.IGNORECASE)
CULTURE_RE = re.compile(r"\b(?:culture|tradition|heritage|language|ethos|innovation|cultural|regional|tribal|clan|dynastic_cycle)\b", re.IGNORECASE)
CULTURE_PARAMETER_RE = re.compile(r"\b(?:parameter|modifier|tradition_|innovation_|culture_parameter|cultural_tradition|traditions/)\b", re.IGNORECASE)
TITLE_RE = re.compile(r"\b(?:title|duchy|kingdom|empire|county|barony|realm|vassal|liege|sovereignty|tributar|de_jure|governance|rank|landed_title|house)\b", re.IGNORECASE)
ACCOLADE_TRAIT_RE = re.compile(r"\b(?:accolade|acclaimed_knight|glory|knight|trait|perk|aptitude|GetTrait|TRAIT)\b", re.IGNORECASE)
BUILDING_ACTIVITY_RE = re.compile(r"\b(?:building|holding|modifier|activity|wedding|hunt|tournament|travel|contract|GetActivityType)\b", re.IGNORECASE)
NAME_DYNASTY_RE = re.compile(r"\b(?:nickname|cognomen|epithet|dynasty|house|GetName|GetFirstName|GetShortUIName)\b", re.IGNORECASE)
BASE_RELIGION_RE = re.compile(r"\b(?:faith|religion|doctrine|tenet|holy|piety|church|clergy)\b", re.IGNORECASE)
BASE_CULTURE_RE = re.compile(r"\b(?:culture|tradition|heritage|language|ethos|innovation)\b", re.IGNORECASE)
BASE_TITLE_RE = re.compile(r"\b(?:title|duchy|kingdom|empire|county|barony|realm|vassal|liege)\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_deep_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir() / f"{base.name}_summary.json"


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def latest_segment_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs WHERE finished_at IS NOT NULL").fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing finished segment_state_runs")
    return int(row["id"])


def latest_ledger_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT MAX(run_id) AS id
        FROM ml_issue_ledger_items
        WHERE status = 'open'
          AND agent_key = 'micro_semantic_review_router'
          AND issue_family = 'semantic_review_router'
        """
    ).fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing compatible semantic review ledger run")
    return int(row["id"])


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def load_preflight_exclusions() -> tuple[Path | None, set[int]]:
    path = latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        return None, set()
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    return path, blocked | superseded


def short(text: str | None, limit: int = 360) -> str:
    value = str(text or "")
    compact = value.replace("\r\n", "\\n").replace("\n", "\\n")
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def combined_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "english_text", "spanish_text", "current_output_text")
    )


def base_surface_bucket(row: dict[str, Any]) -> str:
    text = combined_text(row)
    current = str(row.get("current_output_text") or "")
    if SELECT_CSTRING_RE.search(text):
        return "select_cstring_or_branching"
    if ES_HELPER_RE.search(text):
        return "es_helper_gendered_surface"
    if re.search(r"\b(?:accolade|acclaimed_knight|glory|knight)\b", text, re.IGNORECASE):
        return "accolade_knight_glory"
    if BASE_RELIGION_RE.search(text):
        return "religion_faith_doctrine"
    if BASE_CULTURE_RE.search(text):
        return "culture_tradition_innovation"
    if BASE_TITLE_RE.search(text):
        return "title_realm_governance"
    if re.search(r"\b(?:nickname|dynasty|house|GetName|GetFirstName|GetShortUIName)\b", text, re.IGNORECASE):
        return "name_nickname_dynasty"
    if re.search(r"\b(?:activity|travel|hunt|feast|pilgrimage|tour|survey|contract)\b", text, re.IGNORECASE):
        return "activity_contract_event"
    if len(current) <= 90 and BRACKET_RE.search(current):
        return "short_dynamic_label"
    if len(current) <= 90:
        return "short_plain_label"
    return "other"


def is_domain_policy_vote_candidate(row: dict[str, Any]) -> bool:
    return base_surface_bucket(row) in {"religion_faith_doctrine", "culture_tradition_innovation", "title_realm_governance"}


def surface_bucket(row: dict[str, Any]) -> str:
    text = combined_text(row)
    path = str(row.get("relative_path") or "")
    source_key = str(row.get("source_key") or "")
    if HOLY_WAR_RE.search(text):
        return "religion_holy_war_piety"
    if RELIGION_RE.search(text):
        return "religion_faith_doctrine"
    if CULTURE_PARAMETER_RE.search(text):
        return "culture_parameter_modifier"
    if CULTURE_RE.search(text):
        return "culture_tradition_innovation"
    if ACCOLADE_TRAIT_RE.search(text):
        return "accolade_trait_knight"
    if BUILDING_ACTIVITY_RE.search(text):
        return "building_modifier_activity"
    if TITLE_RE.search(text):
        return "title_realm_governance"
    if NAME_DYNASTY_RE.search(text) or "dynasty" in path or "house" in source_key.lower():
        return "name_title_dynasty"
    return "other_domain_policy_vote"


def risk_bucket(row: dict[str, Any]) -> str:
    text = str(row.get("current_output_text") or "")
    token_count = len(TOKEN_RE.findall(text))
    line_count = text.count("\\n") + text.count("\n") + 1
    if SELECT_CSTRING_RE.search(text) or ES_HELPER_RE.search(text):
        return "high_select_cstring_or_es_helper"
    if line_count >= 3:
        return "high_multiline_effect_list"
    if SPANISH_RESIDUE_RE.search(text):
        return "high_spanish_residue_context"
    if token_count >= 4:
        return "high_structural_token_density"
    if token_count >= 2:
        return "medium_dynamic_dense"
    if token_count == 1:
        return "medium_dynamic_light"
    return "low_plain_domain"


def token_count_bucket(token_count: int) -> str:
    if token_count == 0:
        return "0"
    if token_count == 1:
        return "1"
    if token_count == 2:
        return "2"
    if token_count == 3:
        return "3"
    if token_count <= 6:
        return "4-6"
    return "7+"


def fetch_rows(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    ledger_run_id: int,
    excluded_segment_ids: set[int],
) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [ledger_run_id, segment_state_run_id]
    if excluded_segment_ids:
        placeholders = ",".join("?" for _ in excluded_segment_ids)
        excluded_clause = f"AND state.segment_id NOT IN ({placeholders})"
        params.extend(sorted(excluded_segment_ids))
    rows = conn.execute(
        f"""
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.final_state,
            state.state_group,
            state.review_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.priority_score,
            ledger.issue_family,
            ledger.issue_kind,
            ledger.issue_severity,
            ledger.agent_key,
            ledger.route_status,
            ledger.proposed_action,
            ledger.token_impact,
            ledger.token_status,
            src.english_text,
            src.spanish_text,
            src.old_text,
            output.portuguese_text AS current_output_text
        FROM segment_state_items state
        JOIN ml_issue_ledger_items ledger
          ON ledger.segment_id = state.segment_id
         AND ledger.run_id = ?
         AND ledger.status = 'open'
        LEFT JOIN source_segments src ON src.id = state.segment_id
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.state_group = 'pending'
          AND state.final_state = 'reopen_auto_confirmed_autofix'
          AND state.review_state = 'auto_confirmed'
          AND COALESCE(state.needs_output_apply, 0) = 0
          AND COALESCE(state.confirmed_matches_output, 0) = 1
          AND COALESCE(ledger.agent_key, '') = 'micro_semantic_review_router'
          AND COALESCE(ledger.issue_family, '') = 'semantic_review_router'
          AND COALESCE(ledger.route_status, '') = 'audit_required'
          AND COALESCE(ledger.proposed_action, '') = 'route_to_human_or_semantic_specialist'
          {excluded_clause}
        ORDER BY state.priority_score DESC, state.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows if is_domain_policy_vote_candidate(dict(row))]


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    current = str(row.get("current_output_text") or "")
    token_count = len(TOKEN_RE.findall(current))
    bracket_count = len(BRACKET_RE.findall(current))
    variable_count = len(VARIABLE_RE.findall(current))
    surface = surface_bucket(row)
    risk = risk_bucket(row)
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "surface_bucket": surface,
        "risk_bucket": risk,
        "token_count": token_count,
        "token_count_bucket": token_count_bucket(token_count),
        "bracket_token_count": bracket_count,
        "variable_count": variable_count,
        "text_length": len(current),
        "classification_reason": classification_reason(surface, risk, row),
        "issue_kind": row.get("issue_kind"),
        "issue_severity": row.get("issue_severity"),
        "token_impact": row.get("token_impact"),
        "token_status": row.get("token_status"),
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
        "current_output_text": short(row.get("current_output_text")),
    }


def classification_reason(surface: str, risk: str, row: dict[str, Any]) -> str:
    reasons = [surface, risk]
    text = combined_text(row)
    if RELIGION_RE.search(text):
        reasons.append("religion_terms")
    if CULTURE_RE.search(text):
        reasons.append("culture_terms")
    if TITLE_RE.search(text):
        reasons.append("title_governance_terms")
    if SELECT_CSTRING_RE.search(text) or ES_HELPER_RE.search(text):
        reasons.append("branching_or_es_helper")
    return ";".join(reasons)


def top_counter(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_recommendations(enriched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_surface: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        by_surface[row["surface_bucket"]].append(row)

    recommendations: list[dict[str, Any]] = []
    high_risks = {
        "high_multiline_effect_list",
        "high_select_cstring_or_es_helper",
        "high_spanish_residue_context",
        "high_structural_token_density",
    }
    terminal_risks = {"low_plain_domain", "medium_dynamic_light"}
    for surface, rows in sorted(by_surface.items(), key=lambda item: len(item[1]), reverse=True):
        risk_counts = Counter(row["risk_bucket"] for row in rows)
        high_count = sum(risk_counts[risk] for risk in high_risks)
        terminal_count = sum(risk_counts[risk] for risk in terminal_risks)
        false_safe_risk = 0 if high_count == 0 else high_count
        if len(rows) >= 120 and false_safe_risk == 0:
            action = "recommend_narrow_readonly_review_before_architecture"
        elif len(rows) >= 50 and terminal_count / max(len(rows), 1) >= 0.7:
            action = "recommend_terminal_readonly_review"
        elif high_count:
            action = "hold_for_architecture_or_domain_policy_design"
        else:
            action = "human_packet_readonly"
        recommendations.append(
            {
                "surface_bucket": surface,
                "count": len(rows),
                "risk_counts": top_counter(risk_counts, 10),
                "high_risk_count": high_count,
                "terminal_or_light_count": terminal_count,
                "false_safe_risk": false_safe_risk,
                "recommended_action": action,
            }
        )
    return recommendations


def balanced_sample(enriched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_surface: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        by_surface[row["surface_bucket"]].append(row)

    sample: list[dict[str, Any]] = []
    for surface in sorted(by_surface, key=lambda key: len(by_surface[key]), reverse=True):
        sample.extend(by_surface[surface][:SAMPLE_PER_SUBLANE])
    seen: set[int] = set()
    deduped = []
    for row in sample:
        if row["segment_id"] in seen:
            continue
        seen.add(row["segment_id"])
        deduped.append(row)
    if len(deduped) < SAMPLE_LIMIT:
        for row in enriched:
            if row["segment_id"] in seen:
                continue
            seen.add(row["segment_id"])
            deduped.append(row)
            if len(deduped) >= SAMPLE_LIMIT:
                break
    return deduped[:SAMPLE_LIMIT]


def next_prompt(recommendations: list[dict[str, Any]]) -> str:
    for rec in recommendations:
        if rec["recommended_action"] in {"recommend_narrow_readonly_review_before_architecture", "recommend_terminal_readonly_review"}:
            return f"chat_exec_domain_policy_vote_{rec['surface_bucket']}_review_prompt.md"
    if len(recommendations) == 1 and recommendations[0]["count"] >= 120:
        return "chat_exec_domain_policy_vote_candidate_policy_registration_prompt.md"
    if recommendations:
        return "chat_exec_domain_policy_vote_candidate_human_packet_prompt.md"
    return "chat_exec_manual_semantic_triage_review_prompt.md"


def build_summary(
    segment_state_run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    preflight_path: Path | None,
    preflight_excluded_count: int,
) -> dict[str, Any]:
    surface_counts = Counter(row["surface_bucket"] for row in enriched)
    risk_counts = Counter(row["risk_bucket"] for row in enriched)
    token_buckets = Counter(row["token_count_bucket"] for row in enriched)
    path_counts = Counter(str(row["relative_path"]) for row in enriched)
    prefix_counts = Counter(str(row["source_key"] or "").split("_", 1)[0] for row in enriched)
    recommendations = build_recommendations(enriched)
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": preflight_excluded_count,
        "total_reviewed": len(rows),
        "domain_policy_vote_candidate_count": len(enriched),
        "needs_output_apply_count": sum(1 for row in rows if int(row.get("needs_output_apply") or 0) != 0),
        "confirmed_matches_output_count": sum(1 for row in rows if int(row.get("confirmed_matches_output") or 0) == 1),
        "surface_bucket_counts": top_counter(surface_counts),
        "risk_bucket_counts": top_counter(risk_counts),
        "token_count_buckets": top_counter(token_buckets),
        "top_relative_paths": top_counter(path_counts),
        "top_source_key_prefixes": top_counter(prefix_counts),
        "sublane_recommendations": recommendations,
        "recommended_next_prompt": next_prompt(recommendations),
        "apply_ready_now": False,
        "lifecycle_ready_now": False,
        "production_full_recommended_now": False,
        "ran_apply": False,
        "ran_lifecycle": False,
        "ran_segment_state": False,
        "ran_reindex": False,
        "ran_production_full": False,
        "source_changed": False,
        "output_changed": False,
    }


def write_outputs(summary: dict[str, Any], sample: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sample:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain policy vote candidate deep diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"total_reviewed={summary['total_reviewed']}",
        f"domain_policy_vote_candidate_count={summary['domain_policy_vote_candidate_count']}",
        f"needs_output_apply_count={summary['needs_output_apply_count']}",
        f"confirmed_matches_output_count={summary['confirmed_matches_output_count']}",
        "",
        "surface_bucket_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_bucket_counts"])
    lines.extend(["", "risk_bucket_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["risk_bucket_counts"])
    lines.extend(["", "sublane_recommendations:"])
    for rec in summary["sublane_recommendations"]:
        lines.append(
            f"- {rec['count']} | {rec['surface_bucket']} | high_risk={rec['high_risk_count']} | "
            f"false_safe_risk={rec['false_safe_risk']} | {rec['recommended_action']}"
        )
    lines.extend(
        [
            "",
            f"recommended_next_prompt={summary['recommended_next_prompt']}",
            "apply_ready_now=false",
            "lifecycle_ready_now=false",
            "production_full_recommended_now=false",
            "ran_apply=false",
            "ran_lifecycle=false",
            "ran_segment_state=false",
            "ran_reindex=false",
            "ran_production_full=false",
            "source_changed=false",
            "output_changed=false",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    preflight_path, excluded_segment_ids = load_preflight_exclusions()
    with connect_readonly() as conn:
        segment_state_run_id = latest_segment_state_run_id(conn)
        ledger_run_id = latest_ledger_run_id(conn)
        rows = fetch_rows(conn, segment_state_run_id, ledger_run_id, excluded_segment_ids)
    enriched = [enrich_row(row) for row in rows]
    sample = balanced_sample(enriched)
    summary = build_summary(
        segment_state_run_id,
        ledger_run_id,
        rows,
        enriched,
        preflight_path,
        len(excluded_segment_ids),
    )
    txt_path, jsonl_path, summary_path = write_outputs(summary, sample)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"segment_state_run_id={summary['segment_state_run_id']}")
    print(f"ledger_run_id={summary['ledger_run_id']}")
    print(f"domain_policy_vote_candidate_count={summary['domain_policy_vote_candidate_count']}")
    print(f"sample_count={len(sample)}")
    print(f"recommended_next_prompt={summary['recommended_next_prompt']}")
    print("apply_ready_now=false")
    print("lifecycle_ready_now=false")
    print("production_full_recommended_now=false")
    print("ran_apply=false")
    print("ran_lifecycle=false")
    print("ran_segment_state=false")
    print("ran_reindex=false")
    print("ran_production_full=false")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
