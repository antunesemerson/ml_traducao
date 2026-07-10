from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_pattern_yield_report_v1"


UI_PACKETS = [
    {
        "name": "ui_tooltips_packet3",
        "packet_summary": "reports/20260703_122244_761708_release_readiness_ui_tooltips_plain_light_human_packet_summary.json",
        "learning_summary": "reports/20260703_131509_449626_release_readiness_ui_tooltips_packet3_approve_ok_learning_ingest_apply_summary.json",
        "corrected_apply_summary": "reports/20260703_123535_959335_release_readiness_ui_tooltips_packet2_corrected_apply_apply_summary.json",
        "issue_closure_summary": "reports/20260703_131530_198249_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json",
        "materializer_summaries": [
            "reports/20260703_125356_039621_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json",
            "reports/20260703_131643_217776_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json",
        ],
    },
    {
        "name": "ui_tooltips_packet4",
        "packet_summary": "reports/20260703_134652_257558_release_readiness_ui_tooltips_plain_light_human_packet_summary.json",
        "learning_summary": "reports/20260703_135336_978871_release_readiness_ui_tooltips_packet4_learning_ingest_apply_summary.json",
        "corrected_apply_summary": "reports/20260703_135214_374570_release_readiness_ui_tooltips_packet4_corrected_apply_apply_summary.json",
        "issue_closure_summary": None,
        "materializer_summaries": [
            "reports/20260703_135445_189246_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json",
        ],
    },
    {
        "name": "ui_tooltips_packet5",
        "packet_summary": "reports/20260703_140007_775717_release_readiness_ui_tooltips_plain_light_human_packet_summary.json",
        "learning_summary": "reports/20260703_140707_560133_release_readiness_ui_tooltips_packet4_learning_ingest_apply_summary.json",
        "corrected_apply_summary": "reports/20260703_140613_847297_release_readiness_ui_tooltips_packet5_corrected_apply_apply_summary.json",
        "issue_closure_summary": None,
        "materializer_summaries": [
            "reports/20260703_140731_410964_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json",
        ],
    },
    {
        "name": "ui_tooltips_final_7_read_only",
        "packet_summary": "reports/20260703_142012_800811_release_readiness_ui_tooltips_plain_light_human_packet_summary.json",
        "learning_summary": None,
        "corrected_apply_summary": None,
        "issue_closure_summary": None,
        "materializer_summaries": [],
    },
]

NARRATIVE_PACKETS = [
    {
        "name": "narrative_plain_light_batch1",
        "packet_summary": "reports/20260703_005406_073466_release_readiness_narrative_plain_light_human_packet_summary.json",
        "learning_summary": "reports/20260703_011402_082674_release_readiness_narrative_plain_light_batch1_learning_ingest_summary.json",
        "corrected_apply_summary": "reports/20260703_011310_587569_release_readiness_narrative_plain_light_batch1_apply_apply_summary.json",
        "issue_closure_summary": "reports/20260703_011505_616067_release_readiness_narrative_plain_light_batch1_issue_closure_apply_summary.json",
        "materializer_summaries": [
            "reports/20260703_013130_317442_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json",
        ],
    },
    {
        "name": "narrative_plain_light_batch2",
        "packet_summary": "reports/20260703_021344_772801_release_readiness_narrative_plain_light_human_packet_summary.json",
        "learning_summary": "reports/20260703_023352_066592_release_readiness_narrative_plain_light_batch1_learning_ingest_summary.json",
        "corrected_apply_summary": "reports/20260703_022735_847419_release_readiness_narrative_plain_light_batch1_apply_apply_summary.json",
        "issue_closure_summary": "reports/20260703_023422_191063_release_readiness_narrative_plain_light_batch1_issue_closure_apply_summary.json",
        "materializer_summaries": [
            "reports/20260703_025441_422921_human_confirmed_local_learning_lifecycle_policy_materializer_apply_summary.json",
        ],
    },
]

READINESS_SUMMARY = "reports/20260703_141154_876754_release_readiness_post544_diagnostic_summary.json"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    resolved = db.project_path(path)
    if not resolved.exists():
        return {}
    return json.loads(resolved.read_text(encoding="utf-8"))


def add_counter(target: Counter[str], source: dict[str, Any] | None) -> None:
    for key, value in (source or {}).items():
        target[str(key)] += int(value or 0)


def pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def packet_count(summary: dict[str, Any]) -> int:
    for key in ("selected_count", "record_count"):
        if key in summary:
            return int(summary.get(key) or 0)
    return 0


def summarize_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    packet = read_json(cycle["packet_summary"])
    learning = read_json(cycle.get("learning_summary"))
    apply_summary = read_json(cycle.get("corrected_apply_summary"))
    issue_closure = read_json(cycle.get("issue_closure_summary"))
    materializers = [read_json(path) for path in cycle.get("materializer_summaries", [])]

    suggested = packet.get("suggested_human_decision_counts") or packet.get("suggested_decision_counts") or {}
    learned = int(learning.get("inserted_count") or 0)
    corrected_applied = int(apply_summary.get("apply_count") or apply_summary.get("applied_count") or 0)
    issue_closed = int(issue_closure.get("closed_issue_count") or 0)
    closed = sum(int(item.get("released_count") or 0) for item in materializers)
    blocked_apply = int(apply_summary.get("blocked_count") or 0)
    blocked_materializer = sum(int(item.get("blocked_count") or 0) for item in materializers)
    approve = int((learning.get("decision_kind_counts") or {}).get("approve_already_ok") or 0)
    if approve == 0 and cycle["name"] == "ui_tooltips_packet3":
        approve = int(learning.get("inserted_count") or 0)
    corrected_learned = int((learning.get("decision_kind_counts") or {}).get("corrected_text") or corrected_applied)

    issue_families = Counter()
    add_counter(issue_families, packet.get("issue_family_counts"))
    add_counter(issue_families, apply_summary.get("block_reason_counts"))
    add_counter(issue_families, issue_closure.get("issue_family_counts"))

    selected = packet_count(packet)
    processed = approve + corrected_applied

    return {
        "name": cycle["name"],
        "packet_count": selected,
        "suggested_counts": suggested,
        "approve_already_ok_count": approve,
        "corrected_text_applied_count": corrected_applied,
        "corrected_text_learned_count": corrected_learned,
        "learned_count": learned,
        "closed_count": closed,
        "issues_closed_count": issue_closed,
        "blocked_apply_count": blocked_apply,
        "blocked_materializer_count": blocked_materializer,
        "processed_count": processed,
        "corrected_text_rate_of_packet": pct(corrected_applied, selected),
        "approve_already_ok_rate_of_packet": pct(approve, selected),
        "closure_rate_of_packet": pct(closed, selected),
        "closure_rate_of_processed": pct(closed, processed),
        "issue_family_counts": dict(issue_families.most_common()),
        "token_surface_counts": packet.get("token_surface_counts") or {},
        "risk_type_counts": packet.get("risk_type_counts") or {},
        "output_files": {
            "packet_summary": cycle["packet_summary"],
            "learning_summary": cycle.get("learning_summary"),
            "corrected_apply_summary": cycle.get("corrected_apply_summary"),
            "issue_closure_summary": cycle.get("issue_closure_summary"),
            "materializer_summaries": cycle.get("materializer_summaries", []),
        },
    }


def aggregate(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    total_packet = sum(item["packet_count"] for item in cycles)
    total_approve = sum(item["approve_already_ok_count"] for item in cycles)
    total_corrected = sum(item["corrected_text_applied_count"] for item in cycles)
    total_learned = sum(item["learned_count"] for item in cycles)
    total_closed = sum(item["closed_count"] for item in cycles)
    total_issues = sum(item["issues_closed_count"] for item in cycles)
    total_processed = sum(item["processed_count"] for item in cycles)
    families = Counter()
    labels = Counter()
    for item in cycles:
        add_counter(families, item["issue_family_counts"])
        labels["approve_already_ok"] += item["approve_already_ok_count"]
        labels["corrected_text"] += item["corrected_text_learned_count"]
    return {
        "packet_count": total_packet,
        "approve_already_ok_count": total_approve,
        "corrected_text_applied_count": total_corrected,
        "learned_count": total_learned,
        "closed_count": total_closed,
        "issues_closed_count": total_issues,
        "processed_count": total_processed,
        "corrected_text_rate_of_packet": pct(total_corrected, total_packet),
        "approve_already_ok_rate_of_packet": pct(total_approve, total_packet),
        "closure_rate_of_packet": pct(total_closed, total_packet),
        "closure_rate_of_processed": pct(total_closed, total_processed),
        "learned_label_counts": dict(labels),
        "issue_family_counts": dict(families.most_common()),
    }


def build_summary() -> dict[str, Any]:
    ui_cycles = [summarize_cycle(cycle) for cycle in UI_PACKETS]
    narrative_cycles = [summarize_cycle(cycle) for cycle in NARRATIVE_PACKETS]
    readiness = read_json(READINESS_SUMMARY)

    completed_ui_cycles = [cycle for cycle in ui_cycles if cycle["closed_count"] > 0 or cycle["learned_count"] > 0]
    all_completed = completed_ui_cycles + narrative_cycles

    release_recommendations = readiness.get("final_package_recommendations") or []
    top_groups = readiness.get("top_10_release_impact_groups") or []
    recommendation = {
        "next_group": "narrative_events_plain_light_small_packet",
        "reason": (
            "UI/tooltips plain/light is now down to a 7-item read-only packet and likely has limited immediate yield. "
            "Narrative events remains the largest player-visible release blocker group; use another small plain/light packet, "
            "excluding dynamic_getter, dynamic_select, multiline and parser_later."
        ),
        "guardrails": [
            "read_only_packet_first",
            "protected_apply_only_for_human_corrected_text",
            "ingest_only_confirmed_human_signals",
            "materializer_dry_run_before_apply",
            "no_reindex",
            "no_production_full",
            "keep_120831_hold_token_dynamic",
        ],
    }

    return {
        "source": SOURCE,
        "schema_version": 1,
        "mode": "read_only_pattern_yield_report",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ui_tooltips_cycles": ui_cycles,
        "narrative_cycles": narrative_cycles,
        "ui_tooltips_totals_completed": aggregate(completed_ui_cycles),
        "narrative_totals": aggregate(narrative_cycles),
        "combined_completed_totals": aggregate(all_completed),
        "pending_read_only_packets": [cycle for cycle in ui_cycles if cycle["closed_count"] == 0 and cycle["learned_count"] == 0],
        "latest_release_readiness": {
            "run_id": readiness.get("run_id"),
            "pending_global_count": readiness.get("pending_global_count"),
            "release_blocker_count": readiness.get("release_blocker_count"),
            "review_before_release_count": readiness.get("review_before_release_count"),
            "known_non_blocking_hold_count": readiness.get("known_non_blocking_hold_count"),
            "parser_later_count": readiness.get("parser_later_count"),
            "needs_output_apply_count": readiness.get("needs_output_apply_count"),
            "needs_output_apply_segment_ids": readiness.get("needs_output_apply_segment_ids"),
            "top_10_release_impact_groups": top_groups[:10],
            "final_package_recommendations": release_recommendations,
        },
        "next_recommendation": recommendation,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "production_full_recommended_now": False,
        "source_changed": False,
        "output_changed": False,
    }


def markdown(summary: dict[str, Any]) -> str:
    combined = summary["combined_completed_totals"]
    ui = summary["ui_tooltips_totals_completed"]
    narrative = summary["narrative_totals"]
    pending = summary["pending_read_only_packets"]
    readiness = summary["latest_release_readiness"]
    lines = [
        "# Pattern yield report",
        "",
        "## Scope",
        "",
        "- UI/tooltips recent packets",
        "- Narrative plain/light batch1 and batch2",
        "- Learning labels, corrected_text/apply, closures and blocked families",
        "- Read-only report: no candidate generation, apply, lifecycle, segment-state, reindex or production full",
        "",
        "## Combined completed yield",
        "",
        f"- Reviewed packet items: {combined['packet_count']}",
        f"- Processed human-confirmed items: {combined['processed_count']}",
        f"- Learned rows: {combined['learned_count']}",
        f"- Corrected text applied: {combined['corrected_text_applied_count']} ({combined['corrected_text_rate_of_packet']}% of packet)",
        f"- Approve already ok: {combined['approve_already_ok_count']} ({combined['approve_already_ok_rate_of_packet']}% of packet)",
        f"- Closed by lifecycle bridge: {combined['closed_count']} ({combined['closure_rate_of_packet']}% of packet; {combined['closure_rate_of_processed']}% of processed)",
        f"- Issues closed: {combined['issues_closed_count']}",
        "",
        "## UI/tooltips yield",
        "",
        f"- Packet items: {ui['packet_count']}",
        f"- Corrected text applied: {ui['corrected_text_applied_count']} ({ui['corrected_text_rate_of_packet']}%)",
        f"- Approve already ok: {ui['approve_already_ok_count']} ({ui['approve_already_ok_rate_of_packet']}%)",
        f"- Closed: {ui['closed_count']} ({ui['closure_rate_of_packet']}% of packet)",
        f"- Issues closed: {ui['issues_closed_count']}",
        "",
        "## Narrative plain/light yield",
        "",
        f"- Packet items: {narrative['packet_count']}",
        f"- Corrected text applied: {narrative['corrected_text_applied_count']} ({narrative['corrected_text_rate_of_packet']}%)",
        f"- Approve already ok: {narrative['approve_already_ok_count']} ({narrative['approve_already_ok_rate_of_packet']}%)",
        f"- Closed: {narrative['closed_count']} ({narrative['closure_rate_of_packet']}% of packet)",
        f"- Issues closed: {narrative['issues_closed_count']}",
        "",
        "## Pending read-only packet",
        "",
    ]
    for item in pending:
        lines.append(
            f"- {item['name']}: {item['packet_count']} items, suggested {item['suggested_counts']}, token surfaces {item['token_surface_counts']}"
        )
    lines.extend(
        [
            "",
            "## Latest readiness",
            "",
            f"- Run: {readiness['run_id']}",
            f"- Pending: {readiness['pending_global_count']}",
            f"- Release blockers: {readiness['release_blocker_count']}",
            f"- Review before release: {readiness['review_before_release_count']}",
            f"- Known non-blocking holds: {readiness['known_non_blocking_hold_count']}",
            f"- Parser later: {readiness['parser_later_count']}",
            f"- Needs output apply: {readiness['needs_output_apply_count']} ({readiness['needs_output_apply_segment_ids']})",
            "",
            "## Recommendation",
            "",
            f"- Next group: `{summary['next_recommendation']['next_group']}`",
            f"- Reason: {summary['next_recommendation']['reason']}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_pattern_yield_report"
    summary_path = Path(str(base) + "_summary.json")
    md_path = base.with_suffix(".md")
    summary["output_files"] = {"summary": str(summary_path), "markdown": str(md_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown(summary), encoding="utf-8")
    return summary_path, md_path


def main() -> None:
    summary = build_summary()
    summary_path, md_path = write_reports(summary)
    combined = summary["combined_completed_totals"]
    print(f"summary={summary_path}")
    print(f"markdown={md_path}")
    print(f"reviewed_packet_items={combined['packet_count']}")
    print(f"processed_human_confirmed={combined['processed_count']}")
    print(f"learned_count={combined['learned_count']}")
    print(f"corrected_text_applied={combined['corrected_text_applied_count']}")
    print(f"approve_already_ok={combined['approve_already_ok_count']}")
    print(f"closed_count={combined['closed_count']}")
    print(f"issues_closed={combined['issues_closed_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
