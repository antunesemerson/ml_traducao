from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_review_router_pending_deep_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 486
LEDGER_RUN_ID = 76
SAMPLE_PER_BUCKET = 6

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+"
)
SELECT_CSTRING_RE = re.compile(r"Select_CString\(")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:cielos|maravilloso|hacerte|hacerle|eres|estancia|galard[oÃ³]n|"
    r"coste|actual|siguiente|elige|del|la|los|las|tu|tus|su|sus)\b",
    re.IGNORECASE,
)
RELIGION_RE = re.compile(r"\b(?:faith|religion|doctrine|tenet|holy|piety|church|clergy)\b", re.IGNORECASE)
CULTURE_RE = re.compile(r"\b(?:culture|tradition|heritage|language|ethos|innovation)\b", re.IGNORECASE)
TITLE_RE = re.compile(r"\b(?:title|duchy|kingdom|empire|county|barony|realm|vassal|liege)\b", re.IGNORECASE)
ACTIVITY_RE = re.compile(r"\b(?:activity|travel|hunt|feast|pilgrimage|tour|survey|contract)\b", re.IGNORECASE)
ACCOLADE_RE = re.compile(r"\b(?:accolade|acclaimed_knight|glory|knight)\b", re.IGNORECASE)
NAME_RE = re.compile(r"\b(?:nickname|dynasty|house|GetName|GetFirstName|GetShortUIName)\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_router_pending_deep_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir() / f"{base.name}_summary.json"


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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


def short(text: str | None, limit: int = 260) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def surface_bucket(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("source_key", "relative_path", "english_text", "spanish_text", "current_output_text")
    )
    if SELECT_CSTRING_RE.search(text):
        return "select_cstring_or_branching"
    if ES_HELPER_RE.search(text):
        return "es_helper_gendered_surface"
    if ACCOLADE_RE.search(text):
        return "accolade_knight_glory"
    if RELIGION_RE.search(text):
        return "religion_faith_doctrine"
    if CULTURE_RE.search(text):
        return "culture_tradition_innovation"
    if TITLE_RE.search(text):
        return "title_realm_governance"
    if NAME_RE.search(text):
        return "name_nickname_dynasty"
    if ACTIVITY_RE.search(text):
        return "activity_contract_event"
    if len(str(row.get("current_output_text") or "")) <= 90 and BRACKET_RE.search(str(row.get("current_output_text") or "")):
        return "short_dynamic_label"
    if len(str(row.get("current_output_text") or "")) <= 90:
        return "short_plain_label"
    return "general_semantic_prose"


def risk_bucket(row: dict[str, Any]) -> str:
    text = str(row.get("current_output_text") or "")
    tokens = TOKEN_RE.findall(text)
    if SELECT_CSTRING_RE.search(text):
        return "high_context_select_cstring"
    if ES_HELPER_RE.search(text):
        return "high_context_es_helper"
    if len(tokens) >= 4:
        return "medium_dynamic_dense"
    if SPANISH_RESIDUE_RE.search(text):
        return "context_risk_spanish_residue"
    if len(tokens) > 0:
        return "medium_dynamic_light"
    return "low_plain_text"


def policy_lane(row: dict[str, Any], surface: str, risk: str) -> str:
    text = str(row.get("current_output_text") or "")
    if risk.startswith("high_context"):
        return "human_review_required_dynamic_gender_or_branching"
    if surface in {"religion_faith_doctrine", "culture_tradition_innovation", "title_realm_governance"}:
        return "domain_policy_vote_candidate"
    if surface in {"accolade_knight_glory", "activity_contract_event", "general_semantic_prose"} and risk in {
        "medium_dynamic_light",
        "low_plain_text",
    }:
        return "semantic_review_policy_design_candidate"
    if surface in {"short_dynamic_label", "short_plain_label"}:
        return "short_label_hold_or_existing_policy_surface"
    if SPANISH_RESIDUE_RE.search(text):
        return "human_review_spanish_residue_context_risk"
    return "manual_semantic_triage"


def fetch_rows(conn: sqlite3.Connection, excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [LEDGER_RUN_ID, SEGMENT_STATE_RUN_ID]
    if excluded_ids:
        placeholders = ",".join("?" for _ in excluded_ids)
        excluded_clause = f"AND s.segment_id NOT IN ({placeholders})"
        params.extend(sorted(excluded_ids))
    query = f"""
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.review_state,
            s.confirmed_matches_output,
            s.priority_score,
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
          AND COALESCE(l.agent_key, '') = 'micro_semantic_review_router'
          AND COALESCE(l.issue_family, '') = 'semantic_review_router'
          AND COALESCE(l.route_status, '') = 'audit_required'
          AND COALESCE(l.proposed_action, '') = 'route_to_human_or_semantic_specialist'
          AND COALESCE(s.final_state, '') = 'reopen_auto_confirmed_autofix'
          {excluded_clause}
        ORDER BY s.priority_score DESC, s.segment_id
    """
    return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    surface = surface_bucket(row)
    risk = risk_bucket(row)
    lane = policy_lane(row, surface, risk)
    current = str(row.get("current_output_text") or "")
    english = str(row.get("english_text") or "")
    spanish = str(row.get("spanish_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "issue_kind": row.get("issue_kind"),
        "issue_severity": row.get("issue_severity"),
        "surface_bucket": surface,
        "risk_bucket": risk,
        "policy_lane": lane,
        "token_count": len(TOKEN_RE.findall(current)),
        "bracket_token_count": len(BRACKET_RE.findall(current)),
        "variable_count": len(VARIABLE_RE.findall(current)),
        "text_length": len(current),
        "english_text": short(english),
        "spanish_text": short(spanish),
        "old_text": short(row.get("old_text")),
        "current_output_text": short(current),
    }


def top_counter(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def summarize(rows: list[dict[str, Any]], preflight_path: Path | None, excluded_count: int) -> dict[str, Any]:
    enriched = [enrich_row(row) for row in rows]
    surface_counts = Counter(row["surface_bucket"] for row in enriched)
    risk_counts = Counter(row["risk_bucket"] for row in enriched)
    lane_counts = Counter(row["policy_lane"] for row in enriched)
    file_counts = Counter(str(row["relative_path"]) for row in enriched)
    source_prefix_counts = Counter(str(row["source_key"] or "").split("_", 1)[0] for row in enriched)

    sample_by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        bucket = row["policy_lane"]
        if len(sample_by_lane[bucket]) < SAMPLE_PER_BUCKET:
            sample_by_lane[bucket].append(row)

    likely_policy_close_lanes = {
        "semantic_review_policy_design_candidate",
        "domain_policy_vote_candidate",
    }
    policy_candidate_count = sum(lane_counts[lane] for lane in likely_policy_close_lanes)
    human_required_count = sum(
        count
        for lane, count in lane_counts.items()
        if lane.startswith("human_review") or lane == "manual_semantic_triage"
    )

    recommended_lane = None
    if lane_counts:
        recommended_lane = max(
            lane_counts,
            key=lambda lane: (
                lane_counts[lane],
                lane in likely_policy_close_lanes,
                not lane.startswith("human_review"),
            ),
        )

    architecture_needed = bool(
        recommended_lane in {"domain_policy_vote_candidate", "semantic_review_policy_design_candidate"}
    )
    next_action = (
        "prepare_policy_design_question_for_architecture"
        if architecture_needed
        else "prepare_human_review_packet_readonly"
        if recommended_lane
        else "hold_no_semantic_rows"
    )

    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": excluded_count,
        "rows_reviewed": len(enriched),
        "surface_counts": top_counter(surface_counts),
        "risk_counts": top_counter(risk_counts),
        "policy_lane_counts": top_counter(lane_counts),
        "top_files": top_counter(file_counts),
        "top_source_key_prefixes": top_counter(source_prefix_counts),
        "policy_candidate_count": policy_candidate_count,
        "human_required_or_manual_count": human_required_count,
        "recommended_lane": recommended_lane,
        "recommended_lane_count": lane_counts[recommended_lane] if recommended_lane else 0,
        "architecture_needed_before_next_step": architecture_needed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "discovery_recommended_now": False,
        "retarget_recommended_now": False,
        "next_action": next_action,
        "sample_by_lane": dict(sample_by_lane),
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(enrich_row(row), ensure_ascii=False, sort_keys=True) + "\n")
    with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    lines = [
        "semantic review router pending deep diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"preflight_excluded_segment_count={summary['preflight_excluded_segment_count']}",
        "",
        "policy_lane_counts:",
    ]
    for item in summary["policy_lane_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "risk_counts:"])
    for item in summary["risk_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "surface_counts:"])
    for item in summary["surface_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "top_files:"])
    for item in summary["top_files"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"policy_candidate_count={summary['policy_candidate_count']}",
            f"human_required_or_manual_count={summary['human_required_or_manual_count']}",
            f"recommended_lane={summary['recommended_lane']}",
            f"recommended_lane_count={summary['recommended_lane_count']}",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    preflight_path, excluded_ids = load_preflight_exclusions()
    with connect_readonly() as conn:
        current_run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (SEGMENT_STATE_RUN_ID,)).fetchone()
        if not current_run:
            raise SystemExit(f"segment_state_run_id not found: {SEGMENT_STATE_RUN_ID}")
        rows = fetch_rows(conn, excluded_ids)
    summary = summarize(rows, preflight_path, len(excluded_ids))
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
