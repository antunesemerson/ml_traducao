from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "learning_next_focus_diagnostic_v1"


def latest_report(pattern: str) -> Path:
    reports = sorted(db.project_path("reports").glob(pattern))
    if not reports:
        raise RuntimeError(f"No report found for pattern: {pattern}")
    return reports[-1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: float) -> str:
    return f"{value:.2%}"


def score_family(*, total: int, open_count: int, covered: int, primary_count: int) -> float:
    if total <= 0:
        return 0.0
    coverage = covered / total
    volume_score = min(open_count / 10_000, 1.0)
    primary_score = min(primary_count / 10_000, 1.0)
    low_coverage_bonus = 1.0 - coverage
    return round((volume_score * 0.45) + (primary_score * 0.35) + (low_coverage_bonus * 0.20), 4)


def family_recommendation(family: str, *, total: int, covered: int, primary_count: int) -> tuple[str, str]:
    coverage = covered / total if total else 0.0
    if family == "short_label_style_microagent":
        return (
            "highest_volume_policy",
            "Create a guarded short-label segmentation pass: split pure no-token labels, compact UI nouns, dynamic-short labels and semantic-short labels before another broad lifecycle bridge.",
        )
    if family == "autofix_unknown_microagent":
        return (
            "cluster_before_microagent",
            "Cluster unknown autofix rows by text shape and confirmation source; do not build one generic neuron. This is likely the best router-improvement target.",
        )
    if family == "title_policy_microagent":
        return (
            "domain_policy_batch",
            "Use policy-by-subgroup: adjectives, baronies/counties, cultural title labels, nicknames/titles. Broad title logic remains risky.",
        )
    if family == "dynamic_ck3_expression_microagent":
        return (
            "continue_composition",
            "Continue Select_CString/Custom helper decomposition; it has proven lifecycle gains but remaining open volume needs subtype queues.",
        )
    if family == "gender_token_microagent":
        return (
            "repair_specialist",
            "Build focused gender-token queues by helper/token form; it is smaller than short-label but higher structural risk.",
        )
    if coverage < 0.01 and primary_count > 1_000:
        return (
            "needs_seed_evidence",
            "Very low coverage with material volume: create a small stratified review queue before designing promotion logic.",
        )
    return (
        "monitor_or_secondary",
        "Keep as secondary unless it appears as a blocker inside high-volume compositions.",
    )


def main() -> dict[str, Any]:
    settings = db.load_settings()
    pending_path = latest_report("*_pending_architecture_diagnostic.json")
    coverage_path = latest_report("*_issue_partial_coverage.json")
    network_path = latest_report("*_learning_network_diagnostic.json")

    pending = load_json(pending_path)
    coverage = load_json(coverage_path)
    network = load_json(network_path)

    state_run = pending["state_run"]
    counters = pending["summary"]["counters"]
    primary_counts = counters["primary_family"]
    coverage_summary = coverage["summary"]
    family_counts = coverage_summary["family_counts"]

    rows: list[dict[str, Any]] = []
    for family, stats in family_counts.items():
        total = int(stats.get("total") or 0)
        covered = int(stats.get("covered") or 0)
        open_count = int(stats.get("open") or 0)
        primary_count = int(primary_counts.get(family) or 0)
        lane, recommendation = family_recommendation(
            family,
            total=total,
            covered=covered,
            primary_count=primary_count,
        )
        rows.append(
            {
                "family": family,
                "issue_total": total,
                "issue_open": open_count,
                "issue_covered": covered,
                "coverage_ratio": covered / total if total else 0.0,
                "primary_pending_segments": primary_count,
                "priority_score": score_family(
                    total=total,
                    open_count=open_count,
                    covered=covered,
                    primary_count=primary_count,
                ),
                "lane": lane,
                "recommendation": recommendation,
            }
        )
    rows.sort(key=lambda row: (row["priority_score"], row["primary_pending_segments"], row["issue_open"]), reverse=True)

    coverage_state = coverage_summary["coverage_state_counts"]
    domain_counts = counters["domain"]
    length_counts = counters["length_bucket"]
    token_counts = counters["token_bucket"]
    package_counts = counters["package"]

    network_bits = {
        "text_shadow_ready_rate": network.get("text_shadow_policies", {}).get("ready_rate"),
        "token_subpolicy_unique_ready_rate": network.get("text_boundary_token_subpolicy_shadows", {}).get("unique_ready_rate"),
        "long_text_potential_close_rate": network.get("issue_long_text_partial_composition_impact", {}).get("potential_close_rate"),
        "short_label_latest_lifecycle_closed": network.get("short_label_guarded_lifecycle_bridge", {}).get("totals", {}),
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = db.project_path(settings["reports_dir"]) / f"{stamp}_learning_next_focus_diagnostic"
    txt_path = base.with_suffix(".txt")
    json_path = base.with_suffix(".json")

    lines = [
        "Learning Next Focus Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Source reports:",
        f"- pending: {pending_path}",
        f"- coverage: {coverage_path}",
        f"- network: {network_path}",
        "",
        "Current state:",
        f"- segment_state_run_id: {state_run['id']}",
        f"- closed_consolidated: {state_run['closed_count']:,}",
        f"- raw_pending: {state_run['pending_count']:,}",
        f"- needs_apply: {state_run['output_apply_pending_count']:,}",
        f"- covered issues: {coverage_summary['covered_issue_items']:,}/{coverage_summary['total_issue_items']:,} ({pct(coverage_summary['weighted_coverage_ratio'])})",
        f"- full coverage segments: {coverage_state.get('full', 0):,}",
        f"- partial coverage segments: {coverage_state.get('partial', 0):,}",
        f"- no coverage segments: {coverage_state.get('none', 0):,}",
        "",
        "Top pending by primary family:",
    ]
    for row in rows[:12]:
        lines.append(
            "- {family}: primary={primary:,}, open_issues={open_issues:,}, coverage={coverage}, score={score}, lane={lane}".format(
                family=row["family"],
                primary=row["primary_pending_segments"],
                open_issues=row["issue_open"],
                coverage=pct(row["coverage_ratio"]),
                score=row["priority_score"],
                lane=row["lane"],
            )
        )
    lines.extend(
        [
            "",
            "Recommended next focus:",
            "1. short_label_style_microagent: largest remaining lever. Do not treat as one broad neuron; split into pure no-token labels, compact UI nouns, dynamic-short labels and semantic-short labels.",
            "2. autofix_unknown_microagent: second best because it is a router problem. Cluster before creating repair rules.",
            "3. dynamic_ck3_expression + gender_token: continue as composition inputs, not standalone segment closers.",
            "",
            "Why this is different from the old approach:",
            "- Old path closed full segments one small queue at a time.",
            "- New path should mature issue families, then compose segment closure only when every open family is covered.",
            "- The next measurable target is raising issue coverage from about 12.8% toward 20%+, not immediately running production.",
            "",
            "Domain distribution:",
        ]
    )
    for key, value in list(domain_counts.items())[:10]:
        lines.append(f"- {key}: {value:,}")
    lines.append("")
    lines.append("Length distribution:")
    for key, value in length_counts.items():
        lines.append(f"- {key}: {value:,}")
    lines.append("")
    lines.append("Token distribution:")
    for key, value in token_counts.items():
        lines.append(f"- {key}: {value:,}")
    lines.append("")
    lines.append("Top packages:")
    for key, value in list(package_counts.items())[:15]:
        lines.append(f"- {key}: {value:,}")

    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_reports": {
            "pending": str(pending_path),
            "coverage": str(coverage_path),
            "network": str(network_path),
        },
        "state": {
            "segment_state_run_id": state_run["id"],
            "closed_consolidated": state_run["closed_count"],
            "raw_pending": state_run["pending_count"],
            "needs_apply": state_run["output_apply_pending_count"],
        },
        "coverage": {
            "covered_issue_items": coverage_summary["covered_issue_items"],
            "total_issue_items": coverage_summary["total_issue_items"],
            "weighted_coverage_ratio": coverage_summary["weighted_coverage_ratio"],
            "coverage_state_counts": coverage_state,
        },
        "ranked_families": rows,
        "distribution": {
            "domain": domain_counts,
            "length_bucket": length_counts,
            "token_bucket": token_counts,
            "package": package_counts,
        },
        "network_bits": network_bits,
        "recommended_next_focus": [
            "short_label_style_microagent_subsplit",
            "autofix_unknown_cluster_router",
            "dynamic_ck3_gender_composition_inputs",
        ],
    }

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("[learning_next_focus_diagnostic] Diagnostic generated")
    print(f"[learning_next_focus_diagnostic] State run id: {state_run['id']}")
    print(f"[learning_next_focus_diagnostic] Raw pending: {state_run['pending_count']:,}")
    print(f"[learning_next_focus_diagnostic] Weighted coverage: {pct(coverage_summary['weighted_coverage_ratio'])}")
    print(f"[learning_next_focus_diagnostic] Top focus: {rows[0]['family'] if rows else 'none'}")
    print(f"[learning_next_focus_diagnostic] Report: {txt_path}")
    print(f"[learning_next_focus_diagnostic] JSON: {json_path}")
    return payload


if __name__ == "__main__":
    main()
