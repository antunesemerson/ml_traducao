from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "post_lifecycle_run406_status_v1"
BEFORE_RUN_ID = 404
CURRENT_RUN_ID = 406
LEDGER_RUN_ID = 76
SAMPLE_PER_GROUP = 5

HIGH_RISK_AGENTS = {
    "micro_gender_token",
    "micro_dynamic_ck3_expression",
    "micro_select_cstring",
}
EXCLUDED_OR_HOLD_COHORTS = {
    "semantic_review_router_single_family",
    "semantic_review_router_plus_short_label_no_structural_overlap",
    "short_label_style_microagent_clean_no_dynamic_parser",
    "gender_token_microagent_plus_semantic_review_router",
    "dynamic_ck3_expression_microagent_plus_gender_token_microagent",
    "autofix_unknown_microagent_single_family",
    "autofix_unknown_microagent_plus_semantic_no_structural_overlap",
    "culture_semantic_microagent_single_family",
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


def run_row(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    return dict(row)


def load_preflight_exclusions() -> tuple[Path | None, set[int], dict[str, Any]]:
    path = latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        return None, set(), {}
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    return path, blocked | superseded, data


def changed_segments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            before.segment_id,
            before.relative_path,
            before.source_key,
            before.final_state AS before_final_state,
            after.final_state AS after_final_state,
            before.state_group AS before_state_group,
            after.state_group AS after_state_group,
            before.needs_output_apply AS before_needs_output_apply,
            after.needs_output_apply AS after_needs_output_apply
        FROM segment_state_items before
        JOIN segment_state_items after ON after.segment_id = before.segment_id
        WHERE before.run_id = ?
          AND after.run_id = ?
          AND (
            before.final_state != after.final_state
            OR before.state_group != after.state_group
            OR before.needs_output_apply != after.needs_output_apply
            OR before.confirmed_matches_output != after.confirmed_matches_output
          )
        ORDER BY before.segment_id
        """,
        (BEFORE_RUN_ID, CURRENT_RUN_ID),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_groups(conn: sqlite3.Connection, excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [LEDGER_RUN_ID, CURRENT_RUN_ID]
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


def score_group(group: dict[str, Any]) -> tuple[float, list[str]]:
    count = int(group["segment_count"] or 0)
    high_rate = int(group["high_issue_count"] or 0) / max(count, 1)
    dynamic_rate = int(group["dynamic_surface_count"] or 0) / max(count, 1)
    confirmed_rate = int(group["confirmed_matches_output_count"] or 0) / max(count, 1)
    auto_rate = int(group["auto_confirmed_count"] or 0) / max(count, 1)
    score = min(count, 2000) / 2000 * 0.35
    score += confirmed_rate * 0.20 + auto_rate * 0.15
    score -= high_rate * 0.25 + dynamic_rate * 0.15
    reasons: list[str] = []
    if group["agent_key"] in HIGH_RISK_AGENTS:
        score -= 0.15
        reasons.append("high_risk_agent")
    if group["issue_family"] in {"semantic_review_router", "short_label_style_microagent", "autofix_unknown_microagent"}:
        score += 0.08
        reasons.append("known_policy_surface")
    if group["route_status"] in {"audit_required", "candidate", "cluster_required"}:
        score += 0.04
        reasons.append("route_status_actionable_for_readonly_review")
    if int(group["high_issue_count"] or 0) == 0:
        reasons.append("no_high_issue")
    if dynamic_rate < 0.35:
        reasons.append("manageable_dynamic_surface_rate")
    if count >= 100:
        reasons.append("meaningful_volume")
    return round(score, 4), reasons


def sample_group(conn: sqlite3.Connection, group: dict[str, Any], excluded_ids: set[int]) -> list[dict[str, Any]]:
    excluded_clause = ""
    params: list[Any] = [
        LEDGER_RUN_ID,
        CURRENT_RUN_ID,
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


def learning_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS count,
            SUM(CASE WHEN human_label = 'correct' THEN 1 ELSE 0 END) AS correct_count,
            SUM(CASE WHEN human_label = 'semantic_error' THEN 1 ELSE 0 END) AS semantic_error_count,
            SUM(CASE WHEN corrected_text IS NOT NULL AND corrected_text != '' THEN 1 ELSE 0 END) AS corrected_text_count,
            SUM(CASE WHEN learned_at IS NOT NULL THEN 1 ELSE 0 END) AS learned_count,
            SUM(CASE WHEN confirmation_synced_at IS NOT NULL THEN 1 ELSE 0 END) AS confirmation_synced_count,
            MAX(updated_at) AS last_updated_at
        FROM local_learning_candidates
        WHERE queue_source = 'post-apply-protected'
           OR origin = 'candidate_apply_protected'
        """
    ).fetchone()
    dist = [
        dict(item)
        for item in conn.execute(
            """
            SELECT human_label, local_status, COUNT(*) AS count
            FROM local_learning_candidates
            WHERE queue_source = 'post-apply-protected'
               OR origin = 'candidate_apply_protected'
            GROUP BY human_label, local_status
            ORDER BY count DESC
            """
        )
    ]
    return {
        "post_apply_rows": int(row["count"] or 0),
        "correct_rows": int(row["correct_count"] or 0),
        "semantic_error_rows": int(row["semantic_error_count"] or 0),
        "corrected_text_rows": int(row["corrected_text_count"] or 0),
        "learned_rows": int(row["learned_count"] or 0),
        "confirmation_synced_rows": int(row["confirmation_synced_count"] or 0),
        "last_updated_at": row["last_updated_at"],
        "distribution": dist,
    }


def human_backlog_snapshot() -> dict[str, Any]:
    decision_path = latest_file("*candidate_human_review_decision_record_summary.json")
    closeout_path = latest_file("*post_apply_candidate_closeout_status_summary.json")
    decision = read_json(decision_path) if decision_path else {}
    closeout = read_json(closeout_path) if closeout_path else {}
    return {
        "decision_summary_path": str(decision_path) if decision_path else None,
        "closeout_summary_path": str(closeout_path) if closeout_path else None,
        "needs_more_context": int(decision.get("needs_more_context_count") or 0),
        "duplicates": int(decision.get("duplicate_of_existing_candidate_count") or 0),
        "semantic_error": int(decision.get("semantic_error_count") or 0),
        "rejected": int(decision.get("reject_count") or 0),
        "approved_already_applied": int(closeout.get("approved_already_applied_count") or 0),
        "closeout_status_counts": closeout.get("status_counts", {}),
    }


def build_summary() -> dict[str, Any]:
    preflight_path, excluded_ids, preflight = load_preflight_exclusions()
    with connect_readonly() as conn:
        before = run_row(conn, BEFORE_RUN_ID)
        current = run_row(conn, CURRENT_RUN_ID)
        changed = changed_segments(conn)
        groups = fetch_groups(conn, excluded_ids)
        enriched = []
        for group in groups:
            group = dict(group)
            score, reasons = score_group(group)
            count = int(group["segment_count"] or 0)
            group["opportunity_score"] = score
            group["recommendation_reasons"] = reasons
            group["high_issue_rate"] = round(int(group["high_issue_count"] or 0) / max(count, 1), 4)
            group["dynamic_surface_rate"] = round(int(group["dynamic_surface_count"] or 0) / max(count, 1), 4)
            group["sample"] = sample_group(conn, group, excluded_ids)
            enriched.append(group)
        learning = learning_snapshot(conn)
    transition_counts = Counter(
        f"{row['before_state_group']}:{row['before_final_state']} -> {row['after_state_group']}:{row['after_final_state']}"
        for row in changed
    )
    top_by_score = sorted(enriched, key=lambda row: (row["opportunity_score"], int(row["segment_count"])), reverse=True)[:12]
    top_by_size = sorted(enriched, key=lambda row: (int(row["segment_count"]), row["opportunity_score"]), reverse=True)[:12]
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
        "prepare_readonly_review_or_architecture_question_for_recommended_group"
        if recommended and architecture_needed
        else "retarget_readonly_only_or_deepen_recommended_group_no_apply"
        if recommended
        else "hold_no_viable_pending_group"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "before_run_id": BEFORE_RUN_ID,
        "current_run_id": CURRENT_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "before": before,
        "current": current,
        "delta": {
            "closed_count": int(current["closed_count"] or 0) - int(before["closed_count"] or 0),
            "pending_count": int(current["pending_count"] or 0) - int(before["pending_count"] or 0),
            "reopen_count": int(current["reopen_count"] or 0) - int(before["reopen_count"] or 0),
            "output_apply_pending_count": int(current["output_apply_pending_count"] or 0)
            - int(before["output_apply_pending_count"] or 0),
            "total_segments": int(current["total_segments"] or 0) - int(before["total_segments"] or 0),
        },
        "changed_segment_count": len(changed),
        "transition_counts": dict(transition_counts.most_common(10)),
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(excluded_ids),
        "preflight_context_risk_count": int(preflight.get("context_risk_segment_count") or 0),
        "excluded_or_hold_cohorts": sorted(EXCLUDED_OR_HOLD_COHORTS),
        "groups_reviewed": len(enriched),
        "top_groups_by_size": top_by_size,
        "top_groups_by_score": top_by_score,
        "recommended_group": recommended,
        "architecture_needed_before_next_step": architecture_needed,
        "learning": learning,
        "human_backlog": human_backlog_snapshot(),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": True,
        "discovery_recommended_now": False,
        "apply_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_post_lifecycle_run406_status"
    txt_path = base.with_suffix(".txt")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post lifecycle run406 status",
        f"source={SOURCE}",
        f"before_run_id={summary['before_run_id']}",
        f"current_run_id={summary['current_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        "current:",
        f"- total_segments: {summary['current']['total_segments']}",
        f"- closed_count: {summary['current']['closed_count']}",
        f"- pending_count: {summary['current']['pending_count']}",
        f"- output_apply_pending_count: {summary['current']['output_apply_pending_count']}",
        f"- reopen_count: {summary['current']['reopen_count']}",
        "",
        "delta_from_404:",
        *[f"- {key}: {value}" for key, value in summary["delta"].items()],
        f"- changed_segment_count: {summary['changed_segment_count']}",
        "",
        "top_groups_by_score:",
    ]
    for group in summary["top_groups_by_score"][:8]:
        lines.append(
            "- {segment_count} segs | {agent_key} | {issue_family} | {route_status} | {proposed_action} | score={opportunity_score}".format(
                **group
            )
        )
    recommended = summary.get("recommended_group") or {}
    lines.extend(
        [
            "",
            "recommended_group:",
            f"- agent_key={recommended.get('agent_key')}",
            f"- issue_family={recommended.get('issue_family')}",
            f"- route_status={recommended.get('route_status')}",
            f"- proposed_action={recommended.get('proposed_action')}",
            f"- segment_count={recommended.get('segment_count')}",
            f"- high_issue_rate={recommended.get('high_issue_rate')}",
            f"- dynamic_surface_rate={recommended.get('dynamic_surface_rate')}",
            f"- opportunity_score={recommended.get('opportunity_score')}",
            "",
            "learning:",
            f"- post_apply_rows: {summary['learning']['post_apply_rows']}",
            f"- correct_rows: {summary['learning']['correct_rows']}",
            f"- semantic_error_rows: {summary['learning']['semantic_error_rows']}",
            f"- corrected_text_rows: {summary['learning']['corrected_text_rows']}",
            f"- learned_rows: {summary['learning']['learned_rows']}",
            f"- confirmation_synced_rows: {summary['learning']['confirmation_synced_rows']}",
            "",
            "human_backlog:",
            f"- needs_more_context: {summary['human_backlog']['needs_more_context']}",
            f"- duplicates: {summary['human_backlog']['duplicates']}",
            f"- semantic_error: {summary['human_backlog']['semantic_error']}",
            f"- rejected: {summary['human_backlog']['rejected']}",
            f"- approved_already_applied: {summary['human_backlog']['approved_already_applied']}",
            "",
            f"preflight_excluded_segment_count={summary['preflight_excluded_segment_count']}",
            f"groups_reviewed={summary['groups_reviewed']}",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"apply_recommended_now={str(summary['apply_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    summary = build_summary()
    txt_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print(f"pending_count={summary['current']['pending_count']}")
    print(f"needs_output_apply={summary['current']['output_apply_pending_count']}")
    print(f"groups_reviewed={summary['groups_reviewed']}")
    print(f"recommended_agent_key={(summary.get('recommended_group') or {}).get('agent_key')}")
    print(f"recommended_issue_family={(summary.get('recommended_group') or {}).get('issue_family')}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
