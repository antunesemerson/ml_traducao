from __future__ import annotations

import json
from pathlib import Path

import post_semantic_plain_learning_global_priority as priority


SOURCE = "post_short_label_plain_learning_global_priority_v1"
RECENT_LEARNING_RUN_IDS = (691, 692, 693, 694, 695, 696, 697, 698)
RECENT_HOLD_SEGMENTS = {
    281274,
    9291,
    3934,
    153501,
    22963,
    22974,
    22977,
    23005,
    34132,
    71234,
}


def configure_priority_module() -> None:
    priority.SOURCE = SOURCE
    priority.RECENT_LEARNING_RUN_IDS = RECENT_LEARNING_RUN_IDS
    priority.RECENT_HOLD_SEGMENTS = RECENT_HOLD_SEGMENTS


def write_outputs(summary: dict) -> tuple[Path, Path, Path]:
    base = priority.reports_dir() / f"{priority.stamp()}_post_short_label_plain_learning_global_priority"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = priority.reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for group in summary["top_groups_by_size"]:
            handle.write(json.dumps({"rank_type": "size", **group}, ensure_ascii=False, sort_keys=True) + "\n")
        for group in summary["top_groups_by_score"]:
            handle.write(json.dumps({"rank_type": "score", **group}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recommended = summary["recommended_group"] or {}
    lines = [
        "post short label plain learning global priority",
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
    configure_priority_module()
    summary = priority.build_summary()
    summary["source"] = SOURCE
    summary["recent_learning_run_ids"] = list(RECENT_LEARNING_RUN_IDS)
    summary["latest_short_label_plain_learning_status_summary"] = str(
        priority.latest_file("*_short_label_plain_post_learning_status_summary.json") or ""
    )
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    recommended = summary["recommended_group"] or {}
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"segment_state_run_id={summary['segment_state_run_id']}")
    print(f"pending_count={summary['current_run']['pending_count']}")
    print(f"preflight_excluded_segment_count={summary['preflight_excluded_segment_count']}")
    print(f"recent_learning_excluded_segment_count={summary['recent_learning_excluded_segment_count']}")
    print(f"recent_hold_excluded_segment_count={summary['recent_hold_excluded_segment_count']}")
    print(f"total_excluded_segment_count={summary['total_excluded_segment_count']}")
    print(f"groups_reviewed={summary['groups_reviewed']}")
    print(f"recommended_agent_key={recommended.get('agent_key')}")
    print(f"recommended_issue_family={recommended.get('issue_family')}")
    print(f"recommended_segment_count={recommended.get('segment_count')}")
    print(f"architecture_needed_before_next_step={recommended.get('architecture_needed_before_next_step')}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
