from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as base


SOURCE = "domain_policy_vote_candidate_post_medium_dynamic_light_diagnostic_v1"
MEDIUM_DYNAMIC_LIGHT_CLOSEOUT_SUMMARY = Path("reports/20260630_080936_868538_medium_dynamic_light_post_architecture_status_summary.json")
EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS = {
    18256,
    18708,
    31464,
    49411,
    113354,
    118731,
    126280,
    162874,
    237775,
    238052,
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def top_counter(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def direct_recommendation(recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    if not recommendations:
        return {
            "recommended_action": "hold_domain_policy_vote_candidate",
            "target_surface_bucket": None,
            "rationale": "No eligible domain_policy_vote_candidate rows remain after exclusions.",
        }

    low_plain = [
        rec
        for rec in recommendations
        if any(item["key"] == "low_plain_domain" and item["count"] >= 20 for item in rec.get("risk_counts") or [])
    ]
    if low_plain:
        target = max(
            low_plain,
            key=lambda rec: next((item["count"] for item in rec["risk_counts"] if item["key"] == "low_plain_domain"), 0),
        )
        return {
            "recommended_action": "human_packet_readonly_low_plain_domain",
            "target_surface_bucket": target["surface_bucket"],
            "rationale": "There is still enough low_plain_domain volume to create a controlled human packet before any broader policy.",
        }

    terminal = [
        rec
        for rec in recommendations
        if rec["recommended_action"] in {"recommend_terminal_readonly_review", "human_packet_readonly"}
    ]
    if terminal:
        target = max(terminal, key=lambda rec: rec["terminal_or_light_count"])
        return {
            "recommended_action": "narrow_readonly_review",
            "target_surface_bucket": target["surface_bucket"],
            "rationale": "Terminal/light cases remain, but the next step should measure a narrow sample before human packet or policy.",
        }

    target = max(recommendations, key=lambda rec: rec["count"])
    return {
        "recommended_action": "architecture_or_high_risk_hold_review",
        "target_surface_bucket": target["surface_bucket"],
        "rationale": "The remaining volume is dominated by high-risk or structurally dense cases; do not apply or generate candidates.",
    }


def write_outputs(summary: dict[str, Any], sample: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base_path = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_post_medium_dynamic_light_diagnostic"
    txt_path = base_path.with_suffix(".txt")
    jsonl_path = base_path.with_suffix(".jsonl")
    summary_path = Path(str(base_path) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sample:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate post medium_dynamic_light diagnostic",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"ledger_run_id: {summary['ledger_run_id']}",
        f"domain_policy_vote_candidate_count: {summary['domain_policy_vote_candidate_count']}",
        f"excluded_medium_dynamic_light_count: {summary['excluded_medium_dynamic_light_count']}",
        f"needs_output_apply_count: {summary['needs_output_apply_count']}",
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
            f"terminal_light={rec['terminal_or_light_count']} | action={rec['recommended_action']}"
        )
    lines.extend(
        [
            "",
            "operational_recommendation:",
            f"- action: {summary['direct_operational_recommendation']['recommended_action']}",
            f"- target: {summary['direct_operational_recommendation']['target_surface_bucket']}",
            f"- rationale: {summary['direct_operational_recommendation']['rationale']}",
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    closeout = read_json(MEDIUM_DYNAMIC_LIGHT_CLOSEOUT_SUMMARY)
    if closeout.get("mode") != "read_only_post_architecture_status":
        raise SystemExit("medium_dynamic_light closeout mode guard failed")
    if closeout.get("production_full_recommended_now") is not False:
        raise SystemExit("medium_dynamic_light production guard failed")

    preflight_path, preflight_excluded = base.load_preflight_exclusions()
    excluded = set(preflight_excluded) | EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS
    with base.connect_readonly() as conn:
        segment_state_run_id = base.latest_segment_state_run_id(conn)
        ledger_run_id = base.latest_ledger_run_id(conn)
        rows = base.fetch_rows(conn, segment_state_run_id, ledger_run_id, excluded)

    enriched = [base.enrich_row(row) for row in rows]
    sample = base.balanced_sample(enriched)
    surface_counts = Counter(row["surface_bucket"] for row in enriched)
    risk_counts = Counter(row["risk_bucket"] for row in enriched)
    recommendations = base.build_recommendations(enriched)
    direct = direct_recommendation(recommendations)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_post_medium_dynamic_light_diagnostic",
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "medium_dynamic_light_closeout_summary": str(MEDIUM_DYNAMIC_LIGHT_CLOSEOUT_SUMMARY),
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "preflight_excluded_segment_count": len(preflight_excluded),
        "excluded_medium_dynamic_light_count": len(EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS),
        "excluded_medium_dynamic_light_segment_ids": sorted(EXCLUDED_MEDIUM_DYNAMIC_LIGHT_SEGMENT_IDS),
        "total_reviewed": len(rows),
        "domain_policy_vote_candidate_count": len(enriched),
        "needs_output_apply_count": sum(1 for row in rows if int(row.get("needs_output_apply") or 0) != 0),
        "confirmed_matches_output_count": sum(1 for row in rows if int(row.get("confirmed_matches_output") or 0) == 1),
        "surface_bucket_counts": top_counter(surface_counts),
        "risk_bucket_counts": top_counter(risk_counts),
        "sublane_recommendations": recommendations,
        "direct_operational_recommendation": direct,
        "sample_count": len(sample),
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    txt_path, jsonl_path, summary_path = write_outputs(summary, sample)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"domain_policy_vote_candidate_count={summary['domain_policy_vote_candidate_count']}")
    print(f"recommendation={direct['recommended_action']} target={direct['target_surface_bucket']}")
    print("candidate_generation_count=0")
    print("apply_output_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
