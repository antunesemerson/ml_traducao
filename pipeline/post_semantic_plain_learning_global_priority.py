from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "post_semantic_plain_learning_global_priority_v1"
LEDGER_RUN_ID = 76
SAMPLE_PER_GROUP = 6
RECENT_LEARNING_RUN_IDS = (691, 692, 693, 694)
RECENT_HOLD_SEGMENTS = {281274, 9291, 3934, 153501}

HIGH_RISK_AGENTS = {
    "micro_gender_token",
    "micro_dynamic_ck3_expression",
    "micro_select_cstring",
}
RECENTLY_SATURATED_HINTS = {
    ("micro_autofix_unknown_router", "autofix_unknown_microagent"): "recent_activity_context_composer_dry_run_no_apply_ready",
    ("micro_semantic_review_router", "semantic_review_router"): "recent_semantic_plain_lowrisk_learning_cycle_drained_human_sample",
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


def latest_segment_state_run(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("missing segment_state_runs")
    return dict(row)


def latest_file(pattern: str) -> Path | None:
    matches = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


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


def fetch_groups(conn: sqlite3.Connection, run_id: int, excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [LEDGER_RUN_ID, run_id]
    if excluded_ids:
        excluded_clause = "AND s.segment_id NOT IN (" + ",".join("?" for _ in excluded_ids) + ")"
        params.extend(sorted(excluded_ids))
    rows = conn.execute(
        f"""
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
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def score_group(group: dict[str, Any]) -> tuple[float, list[str], bool, str | None]:
    count = int(group["segment_count"] or 0)
    high_rate = int(group["high_issue_count"] or 0) / max(count, 1)
    dynamic_rate = int(group["dynamic_surface_count"] or 0) / max(count, 1)
    confirmed_rate = int(group["confirmed_matches_output_count"] or 0) / max(count, 1)
    auto_rate = int(group["auto_confirmed_count"] or 0) / max(count, 1)
    hold_hint = RECENTLY_SATURATED_HINTS.get((group["agent_key"], group["issue_family"]))
    architecture_needed = (
        group["agent_key"] in HIGH_RISK_AGENTS
        or high_rate > 0.2
        or dynamic_rate > 0.65
    )
    score = min(count, 2000) / 2000 * 0.35
    score += confirmed_rate * 0.2 + auto_rate * 0.15
    score -= high_rate * 0.25 + dynamic_rate * 0.15
    reasons: list[str] = []
    if group["route_status"] in {"audit_required", "candidate", "cluster_required"}:
        score += 0.04
        reasons.append("route_status_actionable_for_readonly_review")
    if count >= 100:
        reasons.append("meaningful_volume")
    if int(group["high_issue_count"] or 0) == 0:
        reasons.append("no_high_issue")
    if dynamic_rate < 0.35:
        reasons.append("manageable_dynamic_surface_rate")
    if architecture_needed:
        score -= 0.18
        reasons.append("architecture_or_human_policy_needed_before_apply")
    if hold_hint:
        score -= 0.18
        reasons.append(hold_hint)
    return round(score, 4), reasons, architecture_needed, hold_hint


def short(text: str | None, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def sample_group(conn: sqlite3.Connection, run_id: int, group: dict[str, Any], excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [
        LEDGER_RUN_ID,
        run_id,
        group["agent_key"],
        group["final_state"],
        group["route_status"],
        group["proposed_action"],
        group["issue_family"],
    ]
    if excluded_ids:
        excluded_clause = "AND s.segment_id NOT IN (" + ",".join("?" for _ in excluded_ids) + ")"
        params.extend(sorted(excluded_ids))
    params.append(SAMPLE_PER_GROUP)
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
            l.issue_family,
            l.issue_kind,
            l.issue_severity,
            l.agent_key,
            l.route_status,
            l.proposed_action,
            src.english_text,
            src.spanish_text,
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
        """,
        tuple(params),
    ).fetchall()
    samples = []
    for row in rows:
        item = dict(row)
        for key in ("english_text", "spanish_text", "current_output_text"):
            item[key] = short(item.get(key))
        item["dynamic_surface"] = bool(DYNAMIC_RE.search(item.get("current_output_text") or ""))
        samples.append(item)
    return samples


def build_summary() -> dict[str, Any]:
    preflight_path, preflight_exclusions = load_preflight_exclusions()
    latest_learning_status = latest_file("*_semantic_plain_lowrisk_post_learning_status_summary.json")
    with connect_readonly() as conn:
        current_run = latest_segment_state_run(conn)
        run_id = int(current_run["id"])
        learning_exclusions = recent_learning_segments(conn)
        excluded_ids = preflight_exclusions | learning_exclusions | RECENT_HOLD_SEGMENTS
        groups = fetch_groups(conn, run_id, excluded_ids)
        enriched = []
        for group in groups:
            score, reasons, architecture_needed, hold_hint = score_group(group)
            count = int(group["segment_count"] or 0)
            group = dict(group)
            group["opportunity_score"] = score
            group["recommendation_reasons"] = reasons
            group["architecture_needed_before_next_step"] = architecture_needed
            group["recent_hold_hint"] = hold_hint
            group["dynamic_surface_rate"] = round(int(group["dynamic_surface_count"] or 0) / max(count, 1), 4)
            group["high_issue_rate"] = round(int(group["high_issue_count"] or 0) / max(count, 1), 4)
            group["sample"] = sample_group(conn, run_id, group, excluded_ids)
            enriched.append(group)
    top_by_size = sorted(enriched, key=lambda row: (int(row["segment_count"]), row["opportunity_score"]), reverse=True)[:20]
    top_by_score = sorted(enriched, key=lambda row: (row["opportunity_score"], int(row["segment_count"])), reverse=True)[:20]
    eligible = [row for row in top_by_score if not row["recent_hold_hint"]]
    recommended = eligible[0] if eligible else None
    next_action = "hold_no_safe_group_available"
    if recommended:
        next_action = (
            "prepare_architecture_prompt_for_recommended_group"
            if recommended["architecture_needed_before_next_step"]
            else "prepare_readonly_review_for_recommended_group"
        )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": int(current_run["id"]),
        "ledger_run_id": LEDGER_RUN_ID,
        "current_run": {
            "total_segments": int(current_run.get("total_segments") or 0),
            "closed_count": int(current_run.get("closed_count") or 0),
            "pending_count": int(current_run.get("pending_count") or 0),
            "output_apply_pending_count": int(current_run.get("output_apply_pending_count") or 0),
            "reopen_count": int(current_run.get("reopen_count") or 0),
        },
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(preflight_exclusions),
        "recent_learning_run_ids": list(RECENT_LEARNING_RUN_IDS),
        "recent_learning_excluded_segment_count": len(learning_exclusions),
        "recent_hold_excluded_segment_count": len(RECENT_HOLD_SEGMENTS),
        "total_excluded_segment_count": len(excluded_ids),
        "latest_learning_status_summary": str(latest_learning_status) if latest_learning_status else None,
        "groups_reviewed": len(enriched),
        "top_groups_by_size": top_by_size,
        "top_groups_by_score": top_by_score,
        "recommended_group": recommended,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "segment_state_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_post_semantic_plain_learning_global_priority"
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
        "post semantic plain learning global priority",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"pending_count={summary['current_run']['pending_count']}",
        f"needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"preflight_excluded_segment_count={summary['preflight_excluded_segment_count']}",
        f"recent_learning_excluded_segment_count={summary['recent_learning_excluded_segment_count']}",
        f"recent_hold_excluded_segment_count={summary['recent_hold_excluded_segment_count']}",
        f"total_excluded_segment_count={summary['total_excluded_segment_count']}",
        f"groups_reviewed={summary['groups_reviewed']}",
        "",
        "top_groups_by_score:",
    ]
    for group in summary["top_groups_by_score"][:10]:
        lines.append(
            "- "
            f"{group['opportunity_score']} | {group['segment_count']} segs | {group['agent_key']} | "
            f"{group['issue_family']} | {group['proposed_action']} | "
            f"arch={str(group['architecture_needed_before_next_step']).lower()} | "
            f"hint={group['recent_hold_hint'] or 'no'}"
        )
    lines.extend(["", "recommended_group:"])
    if recommended:
        for key in (
            "agent_key",
            "issue_family",
            "route_status",
            "proposed_action",
            "final_state",
            "segment_count",
            "issue_count",
            "high_issue_rate",
            "dynamic_surface_rate",
            "opportunity_score",
            "architecture_needed_before_next_step",
            "recommendation_reasons",
        ):
            lines.append(f"- {key}={recommended.get(key)}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            f"segment_state_recommended_now={str(summary['segment_state_recommended_now']).lower()}",
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
    recommended = summary["recommended_group"] or {}
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"segment_state_run_id={summary['segment_state_run_id']}")
    print(f"pending_count={summary['current_run']['pending_count']}")
    print(f"groups_reviewed={summary['groups_reviewed']}")
    print(f"recommended_agent_key={recommended.get('agent_key')}")
    print(f"recommended_issue_family={recommended.get('issue_family')}")
    print(f"recommended_segment_count={recommended.get('segment_count')}")
    print(f"architecture_needed_before_next_step={recommended.get('architecture_needed_before_next_step')}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
