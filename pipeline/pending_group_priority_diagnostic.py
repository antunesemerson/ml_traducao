from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "pending_group_priority_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 404
LEDGER_RUN_ID = 76
SAMPLE_PER_GROUP = 8

HIGH_RISK_AGENTS = {
    "micro_gender_token",
    "micro_dynamic_ck3_expression",
    "micro_select_cstring",
}
LOW_POLICY_FAMILIES = {
    "semantic_review_router",
    "short_label_style_microagent",
    "autofix_unknown_microagent",
}
DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|Select_CString\(|\.Custom\('ES_|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+"
)


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


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def short(text: str | None, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def load_preflight_exclusions() -> tuple[Path | None, set[int], dict[str, Any]]:
    path = latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        return None, set(), {}
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    return path, blocked | superseded, data


def fetch_groups(conn: sqlite3.Connection, excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [SEGMENT_STATE_RUN_ID, LEDGER_RUN_ID]
    if excluded_ids:
        placeholders = ",".join("?" for _ in excluded_ids)
        excluded_clause = f"AND s.segment_id NOT IN ({placeholders})"
        params.extend(sorted(excluded_ids))
    query = f"""
        SELECT
            COALESCE(l.agent_key, '') AS agent_key,
            COALESCE(s.final_state, '') AS final_state,
            COALESCE(l.route_status, '') AS route_status,
            COALESCE(l.proposed_action, '') AS proposed_action,
            COALESCE(l.issue_family, '') AS issue_family,
            COUNT(DISTINCT s.segment_id) AS segment_count,
            COUNT(*) AS issue_count,
            SUM(CASE WHEN lower(COALESCE(l.issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
            SUM(CASE WHEN COALESCE(s.needs_output_apply, 0) = 1 THEN 1 ELSE 0 END) AS needs_output_apply_count,
            SUM(CASE WHEN COALESCE(s.confirmed_matches_output, 0) = 1 THEN 1 ELSE 0 END) AS confirmed_matches_output_count,
            SUM(CASE WHEN COALESCE(s.review_state, '') = 'auto_confirmed' THEN 1 ELSE 0 END) AS auto_confirmed_count,
            SUM(CASE WHEN COALESCE(s.locked, 0) = 1 THEN 1 ELSE 0 END) AS locked_count,
            SUM(CASE WHEN COALESCE(o.portuguese_text, '') GLOB '*[*]*'
                       OR COALESCE(o.portuguese_text, '') LIKE '%$%'
                       OR COALESCE(o.portuguese_text, '') LIKE '%Select_CString(%'
                       OR COALESCE(o.portuguese_text, '') LIKE '%.Custom(''ES_%'
                     THEN 1 ELSE 0 END) AS dynamic_surface_count
        FROM segment_state_items s
        JOIN ml_issue_ledger_items l
          ON l.segment_id = s.segment_id
         AND l.run_id = ?
         AND l.status = 'open'
        LEFT JOIN output_segments o ON o.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.state_group = 'pending'
          AND COALESCE(s.needs_output_apply, 0) = 0
          {excluded_clause}
        GROUP BY
            COALESCE(l.agent_key, ''),
            COALESCE(s.final_state, ''),
            COALESCE(l.route_status, ''),
            COALESCE(l.proposed_action, ''),
            COALESCE(l.issue_family, '')
        ORDER BY segment_count DESC, issue_count DESC
    """
    return [dict(row) for row in conn.execute(query, (LEDGER_RUN_ID, SEGMENT_STATE_RUN_ID, *sorted(excluded_ids))).fetchall()]


def score_group(row: dict[str, Any]) -> tuple[float, list[str]]:
    segment_count = int(row["segment_count"] or 0)
    high_issue_count = int(row["high_issue_count"] or 0)
    dynamic_count = int(row["dynamic_surface_count"] or 0)
    auto_count = int(row["auto_confirmed_count"] or 0)
    confirmed_count = int(row["confirmed_matches_output_count"] or 0)
    agent = row["agent_key"]
    family = row["issue_family"]
    high_rate = high_issue_count / max(segment_count, 1)
    dynamic_rate = dynamic_count / max(segment_count, 1)
    auto_rate = auto_count / max(segment_count, 1)
    confirmed_rate = confirmed_count / max(segment_count, 1)
    reasons: list[str] = []
    score = min(segment_count, 2000) / 2000 * 0.35
    score += confirmed_rate * 0.20
    score += auto_rate * 0.15
    score -= high_rate * 0.25
    score -= dynamic_rate * 0.15
    if family in LOW_POLICY_FAMILIES:
        score += 0.08
        reasons.append("known_family_with_existing_policy_surface")
    if agent in HIGH_RISK_AGENTS:
        score -= 0.15
        reasons.append("high_risk_agent_requires_architecture_or_human_context")
    if row["route_status"] in {"audit_required", "candidate"}:
        score += 0.04
        reasons.append("route_status_is_actionable_for_review")
    if high_issue_count == 0:
        reasons.append("no_high_issue_in_group")
    if dynamic_rate < 0.35:
        reasons.append("manageable_dynamic_surface_rate")
    if segment_count >= 100:
        reasons.append("meaningful_volume")
    return round(score, 4), reasons


def sample_group(conn: sqlite3.Connection, group: dict[str, Any], excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [
        LEDGER_RUN_ID,
        SEGMENT_STATE_RUN_ID,
        group["agent_key"],
        group["final_state"],
        group["route_status"],
        group["proposed_action"],
        group["issue_family"],
    ]
    if excluded_ids:
        placeholders = ",".join("?" for _ in excluded_ids)
        excluded_clause = f"AND s.segment_id NOT IN ({placeholders})"
        params.extend(sorted(excluded_ids))
    params.append(SAMPLE_PER_GROUP)
    query = f"""
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.review_state,
            s.confirmed_matches_output,
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
          AND COALESCE(l.agent_key, '') = ?
          AND COALESCE(s.final_state, '') = ?
          AND COALESCE(l.route_status, '') = ?
          AND COALESCE(l.proposed_action, '') = ?
          AND COALESCE(l.issue_family, '') = ?
          {excluded_clause}
        ORDER BY s.priority_score DESC, s.segment_id
        LIMIT ?
    """
    samples: list[dict[str, Any]] = []
    for row in conn.execute(query, tuple(params)):
        item = dict(row)
        item["english_text"] = short(item.get("english_text"))
        item["spanish_text"] = short(item.get("spanish_text"))
        item["old_text"] = short(item.get("old_text"))
        item["current_output_text"] = short(item.get("current_output_text"))
        item["dynamic_surface"] = bool(DYNAMIC_RE.search(item.get("current_output_text") or ""))
        samples.append(item)
    return samples


def build_summary() -> dict[str, Any]:
    preflight_path, excluded_ids, preflight = load_preflight_exclusions()
    with connect_readonly() as conn:
        current_run = dict(conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (SEGMENT_STATE_RUN_ID,)).fetchone())
        groups = fetch_groups(conn, excluded_ids)
        enriched: list[dict[str, Any]] = []
        for group in groups:
            score, reasons = score_group(group)
            group = dict(group)
            group["opportunity_score"] = score
            group["recommendation_reasons"] = reasons
            group["dynamic_surface_rate"] = round(int(group["dynamic_surface_count"] or 0) / max(int(group["segment_count"] or 0), 1), 4)
            group["high_issue_rate"] = round(int(group["high_issue_count"] or 0) / max(int(group["segment_count"] or 0), 1), 4)
            group["sample"] = sample_group(conn, group, excluded_ids)
            enriched.append(group)
    top_by_size = sorted(enriched, key=lambda row: (int(row["segment_count"]), row["opportunity_score"]), reverse=True)[:20]
    top_by_score = sorted(enriched, key=lambda row: (row["opportunity_score"], int(row["segment_count"])), reverse=True)[:20]
    recommended = top_by_score[0] if top_by_score else None
    architecture_needed = bool(
        recommended
        and (
            recommended["agent_key"] in HIGH_RISK_AGENTS
            or recommended["high_issue_rate"] > 0.2
            or recommended["dynamic_surface_rate"] > 0.65
        )
    )
    next_action = (
        "prepare_human_review_or_policy_design_for_recommended_group_readonly"
        if recommended and not architecture_needed
        else "consult_architecture_before_policy_or_discovery"
        if recommended
        else "hold_no_group_available"
    )
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "current_run": {
            "total_segments": int(current_run.get("total_segments") or 0),
            "closed_count": int(current_run.get("closed_count") or 0),
            "pending_count": int(current_run.get("pending_count") or 0),
            "output_apply_pending_count": int(current_run.get("output_apply_pending_count") or 0),
            "reopen_count": int(current_run.get("reopen_count") or 0),
        },
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(excluded_ids),
        "groups_reviewed": len(enriched),
        "top_groups_by_size": top_by_size,
        "top_groups_by_score": top_by_score,
        "recommended_group": recommended,
        "architecture_needed_before_next_step": architecture_needed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_pending_group_priority_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for group in summary["top_groups_by_size"]:
            handle.write(json.dumps({"rank_type": "size", **group}, ensure_ascii=False, sort_keys=True) + "\n")
        for group in summary["top_groups_by_score"]:
            handle.write(json.dumps({"rank_type": "score", **group}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recommended = summary["recommended_group"] or {}
    lines = [
        "pending group priority diagnostic",
        f"source={RULE_VERSION}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"pending_count={summary['current_run']['pending_count']}",
        f"needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"preflight_excluded_segment_count={summary['preflight_excluded_segment_count']}",
        f"groups_reviewed={summary['groups_reviewed']}",
        "",
        "top_groups_by_size:",
    ]
    for group in summary["top_groups_by_size"][:10]:
        lines.append(
            "- "
            f"{group['segment_count']} segs | {group['agent_key']} | {group['issue_family']} | "
            f"{group['route_status']} | {group['proposed_action']} | score={group['opportunity_score']}"
        )
    lines.extend(["", "recommended_group:"])
    if recommended:
        lines.extend(
            [
                f"- agent_key={recommended['agent_key']}",
                f"- issue_family={recommended['issue_family']}",
                f"- route_status={recommended['route_status']}",
                f"- proposed_action={recommended['proposed_action']}",
                f"- final_state={recommended['final_state']}",
                f"- segment_count={recommended['segment_count']}",
                f"- issue_count={recommended['issue_count']}",
                f"- high_issue_rate={recommended['high_issue_rate']}",
                f"- dynamic_surface_rate={recommended['dynamic_surface_rate']}",
                f"- opportunity_score={recommended['opportunity_score']}",
                f"- reasons={recommended['recommendation_reasons']}",
            ]
        )
    lines.extend(
        [
            "",
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
    summary = build_summary()
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    recommended = summary["recommended_group"] or {}
    print(f"groups_reviewed={summary['groups_reviewed']}")
    print(f"recommended_agent_key={recommended.get('agent_key')}")
    print(f"recommended_issue_family={recommended.get('issue_family')}")
    print(f"recommended_segment_count={recommended.get('segment_count')}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
