from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_post580_progress_report_v1"

READINESS_SUMMARY = Path("reports/20260703_152505_293246_release_readiness_post544_diagnostic_summary.json")
PREVIOUS_YIELD_SUMMARY = Path("reports/20260703_142026_531263_release_readiness_pattern_yield_report_summary.json")
STRICT_LEARNING_SUMMARY = Path("reports/20260703_150056_253365_release_readiness_narrative_strict_roi_approve_ok_learning_ingest_apply_summary.json")
STRICT_ISSUE_CLOSURE_SUMMARY = Path("reports/20260703_150115_993371_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json")
STRICT_MATERIALIZER_SUMMARY = Path("reports/20260703_150125_517290_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json")
STRICT_DELTA_SUMMARY = Path("reports/20260703_180725_346578_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    resolved = db.project_path(path)
    if not resolved.exists():
        return {}
    return json.loads(resolved.read_text(encoding="utf-8"))


def pct(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2) if denominator else 0.0


def build_summary() -> dict[str, Any]:
    readiness = read_json(READINESS_SUMMARY)
    previous_yield = read_json(PREVIOUS_YIELD_SUMMARY)
    strict_learning = read_json(STRICT_LEARNING_SUMMARY)
    strict_issue = read_json(STRICT_ISSUE_CLOSURE_SUMMARY)
    strict_materializer = read_json(STRICT_MATERIALIZER_SUMMARY)
    strict_delta = read_json(STRICT_DELTA_SUMMARY)

    ui = previous_yield.get("ui_tooltips_totals_completed") or {}
    narrative_corrected = previous_yield.get("narrative_totals") or {}
    strict = {
        "packet_count": int(strict_learning.get("record_count") or 29),
        "processed_count": int(strict_learning.get("inserted_count") or 0),
        "learned_count": int(strict_learning.get("inserted_count") or 0),
        "corrected_text_applied_count": 0,
        "approve_already_ok_count": int(strict_learning.get("inserted_count") or 0),
        "closed_count": int(strict_materializer.get("released_count") or 0),
        "issues_closed_count": int(strict_issue.get("closed_issue_count") or 0),
        "blocked_count": int(strict_materializer.get("blocked_count") or 0),
        "policy_run_id": strict_materializer.get("policy_run_id"),
        "learning_run_id": strict_learning.get("run_id") or strict_materializer.get("learning_run_id"),
        "delta": strict_delta.get("global_delta") or {},
    }

    combined = {
        "packet_count": int(ui.get("packet_count") or 0)
        + int(narrative_corrected.get("packet_count") or 0)
        + strict["packet_count"],
        "processed_count": int(ui.get("processed_count") or 0)
        + int(narrative_corrected.get("processed_count") or 0)
        + strict["processed_count"],
        "learned_count": int(ui.get("learned_count") or 0)
        + int(narrative_corrected.get("learned_count") or 0)
        + strict["learned_count"],
        "corrected_text_applied_count": int(ui.get("corrected_text_applied_count") or 0)
        + int(narrative_corrected.get("corrected_text_applied_count") or 0),
        "approve_already_ok_count": int(ui.get("approve_already_ok_count") or 0)
        + int(narrative_corrected.get("approve_already_ok_count") or 0)
        + strict["approve_already_ok_count"],
        "closed_count": int(ui.get("closed_count") or 0)
        + int(narrative_corrected.get("closed_count") or 0)
        + strict["closed_count"],
        "issues_closed_count": int(ui.get("issues_closed_count") or 0)
        + int(narrative_corrected.get("issues_closed_count") or 0)
        + strict["issues_closed_count"],
    }
    combined["closure_rate_of_packet"] = pct(combined["closed_count"], combined["packet_count"])
    combined["closure_rate_of_processed"] = pct(combined["closed_count"], combined["processed_count"])

    top_groups = readiness.get("top_10_release_impact_groups") or []
    recommendation = {
        "next_group": "narrative_events_plain_light_strict_review_or_high_issue_triage",
        "reason": (
            "narrative_events remains the largest visible release-impact group, but most remaining blocker volume is dynamic/multiline. "
            "Use another strict plain/light pass only if it excludes high_issue_auditor and ambiguous token/literal Spanish; otherwise triage the high-issue blocker family before more packets."
        ),
        "do_not_include": [120831, 126552],
        "guardrails": [
            "read_only_packet_first",
            "exclude_high_issue_auditor_unless_specific_triage",
            "exclude_dynamic_getter_dynamic_select_multiline_parser_later",
            "no_output_apply_without_diff_preview",
            "no_reindex",
            "no_production_full",
        ],
    }

    return {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post580_release_readiness_progress",
        "readiness_summary": str(READINESS_SUMMARY),
        "run_id": readiness.get("run_id"),
        "pending_total": readiness.get("pending_global_count"),
        "reopen_auto_confirmed_autofix": (readiness.get("run") or {}).get("reopen_count"),
        "pending_apply_confirmed": readiness.get("needs_output_apply_count"),
        "needs_output_apply": readiness.get("needs_output_apply_count"),
        "needs_output_apply_segment_ids": readiness.get("needs_output_apply_segment_ids"),
        "release_blocker": readiness.get("release_blocker_count"),
        "review_before_release": readiness.get("review_before_release_count"),
        "known_non_blocking_hold": readiness.get("known_non_blocking_hold_count"),
        "parser_later": readiness.get("parser_later_count"),
        "top_visible_groups": top_groups,
        "visibility_group_counts": readiness.get("visibility_group_counts"),
        "token_surface_counts": readiness.get("token_surface_counts"),
        "progress_by_front": {
            "ui_tooltips": ui,
            "narrative_plain_light_corrected": narrative_corrected,
            "narrative_strict_roi_approve_ok": strict,
            "combined_recent_completed": combined,
        },
        "explicit_holds": {
            "token_dynamic_pending_apply": [120831],
            "high_issue_specific_review": [126552],
        },
        "next_recommendation": recommendation,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "production_full_recommended_now": False,
        "source_changed": False,
        "output_changed": False,
    }


def write_reports(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_post580_progress_report"
    summary_path = Path(str(base) + "_summary.json")
    md_path = base.with_suffix(".md")
    summary["output_files"] = {"summary": str(summary_path), "markdown": str(md_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    combined = summary["progress_by_front"]["combined_recent_completed"]
    strict = summary["progress_by_front"]["narrative_strict_roi_approve_ok"]
    lines = [
        "# Release readiness post-run 580",
        "",
        f"- Pending total: {summary['pending_total']}",
        f"- Reopen auto-confirmed/autofix: {summary['reopen_auto_confirmed_autofix']}",
        f"- Pending apply confirmed / needs output apply: {summary['needs_output_apply']} ({summary['needs_output_apply_segment_ids']})",
        f"- Release blocker: {summary['release_blocker']}",
        f"- Review before release: {summary['review_before_release']}",
        f"- Known non-blocking hold: {summary['known_non_blocking_hold']}",
        f"- Parser later: {summary['parser_later']}",
        "",
        "## Recent Progress",
        "",
        f"- UI/tooltips: closed {summary['progress_by_front']['ui_tooltips'].get('closed_count')} / learned {summary['progress_by_front']['ui_tooltips'].get('learned_count')} / issues closed {summary['progress_by_front']['ui_tooltips'].get('issues_closed_count')}",
        f"- Narrative corrected: closed {summary['progress_by_front']['narrative_plain_light_corrected'].get('closed_count')} / corrected applied {summary['progress_by_front']['narrative_plain_light_corrected'].get('corrected_text_applied_count')} / issues closed {summary['progress_by_front']['narrative_plain_light_corrected'].get('issues_closed_count')}",
        f"- Narrative strict ROI approve_ok: closed {strict['closed_count']} / learned {strict['learned_count']} / issues closed {strict['issues_closed_count']}",
        f"- Combined recent completed: closed {combined['closed_count']} from {combined['packet_count']} reviewed packet items ({combined['closure_rate_of_packet']}% packet yield)",
        "",
        "## Recommendation",
        "",
        f"- Next group: `{summary['next_recommendation']['next_group']}`",
        f"- Reason: {summary['next_recommendation']['reason']}",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, md_path


def main() -> None:
    summary = build_summary()
    summary_path, md_path = write_reports(summary)
    print(f"summary={summary_path}")
    print(f"markdown={md_path}")
    print(f"pending_total={summary['pending_total']}")
    print(f"reopen_auto_confirmed_autofix={summary['reopen_auto_confirmed_autofix']}")
    print(f"needs_output_apply={summary['needs_output_apply']}")
    print(f"release_blocker={summary['release_blocker']}")
    print(f"review_before_release={summary['review_before_release']}")
    print(f"known_non_blocking_hold={summary['known_non_blocking_hold']}")
    print(f"parser_later={summary['parser_later']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
