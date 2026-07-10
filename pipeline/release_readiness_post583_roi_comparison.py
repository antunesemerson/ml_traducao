from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_post583_roi_comparison_v1"
READINESS_SUMMARY = Path("reports/20260703_174605_494007_release_readiness_post544_diagnostic_summary.json")
READINESS_JSONL = Path("reports/20260703_174605_494007_release_readiness_post544_diagnostic.jsonl")
NARRATIVE_PACKET_JSONL = Path("reports/20260703_174614_156446_release_readiness_narrative_post580_strict_roi_human_packet.jsonl")
NARRATIVE_PACKET_SUMMARY = Path("reports/20260703_174614_156446_release_readiness_narrative_post580_strict_roi_human_packet_summary.json")
HOLDS = {120831}

STRICT_CYCLES = [
    {
        "name": "strict_roi_run580",
        "delta": "reports/20260703_180725_346578_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json",
        "issue": "reports/20260703_150115_993371_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json",
        "packet": "reports/20260703_145023_784855_release_readiness_narrative_strict_roi_human_packet.jsonl",
    },
    {
        "name": "strict_roi_run581",
        "delta": "reports/20260703_192946_946866_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json",
        "issue": "reports/20260703_161922_790041_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json",
        "packet": "reports/20260703_161046_043590_release_readiness_narrative_post580_strict_roi_human_packet.jsonl",
    },
    {
        "name": "strict_roi_run582",
        "delta": "reports/20260703_200517_384182_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json",
        "issue": "reports/20260703_165821_998573_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json",
        "packet": "reports/20260703_164226_745902_release_readiness_narrative_post580_strict_roi_human_packet.jsonl",
    },
    {
        "name": "strict_roi_run583",
        "delta": "reports/20260703_204108_471795_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json",
        "issue": "reports/20260703_173626_725967_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json",
        "packet": "reports/20260703_171204_295370_release_readiness_narrative_post580_strict_roi_human_packet.jsonl",
    },
]

BANNED_PATTERNS = [
    "Concept(",
    "Glossary(",
    "SelectLocalization",
    "Select_CString",
    ".Get",
    "MakeScope",
    "ScriptValue",
    "ROOT.",
    "$EFFECT_LIST_BULLET$",
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: str | Path) -> dict[str, Any]:
    resolved = db.project_path(path)
    if not resolved.exists():
        return {}
    return json.loads(resolved.read_text(encoding="utf-8"))


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical_localization_text(left or "") == canonical_localization_text(right or "")


def has_banned_surface(row: dict[str, Any]) -> bool:
    blob = "\n".join(
        str(row.get(key) or "")
        for key in ("spanish_text", "english_text", "output_text", "confirmed_text", "source_key")
    )
    return any(pattern in blob for pattern in BANNED_PATTERNS) or "\n" in str(row.get("output_text") or "")


def suggested_corrected_text(row: dict[str, Any]) -> str:
    return str(row.get("suggested_corrected_text") or row.get("proposed_corrected_text") or "").strip()


def word_count(row: dict[str, Any]) -> int:
    return len(re.findall(r"\w+", str(row.get("output_text") or "")))


def classify_culture(row: dict[str, Any]) -> tuple[str, str]:
    if int(row.get("segment_id") or 0) in HOLDS:
        return "reject", "explicit_hold"
    if row.get("visibility_group") != "culture_tradition_innovation":
        return "reject", "not_culture_tradition_innovation"
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        return "parser_later", "not_plain_light"
    if row.get("release_class") not in {"release_blocker", "review_before_release"}:
        return "reject", "not_release_relevant"
    if "high_issue_auditor" in set(split_csv(row.get("issue_families"))):
        return "reject", "high_issue_auditor"
    if has_banned_surface(row):
        return "parser_later", "dynamic_or_structural_surface"
    corrected = suggested_corrected_text(row)
    if corrected:
        if canonical_equal(corrected, row.get("output_text")):
            return "reject", "corrected_text_no_output_change"
        return "corrected_text_ready", "deterministic_corrected_text_present"
    if bool(row.get("spanish_residue_visible")):
        return "needs_more_context", "spanish_residue_without_deterministic_correction"
    if int(row.get("needs_output_apply") or 0) != 0:
        return "needs_more_context", "needs_output_apply"
    if not canonical_equal(row.get("output_text"), row.get("confirmed_text")):
        return "needs_more_context", "output_confirmed_mismatch"
    if int(row.get("confirmed_matches_output") or 0) != 1:
        return "needs_more_context", "state_not_confirmed_matches_output"
    return "approve_already_ok_ready", "output_already_good"


def priority(row: dict[str, Any], decision: str) -> tuple[int, int, int, int, int]:
    release_rank = 0 if row.get("release_class") == "release_blocker" else 1
    decision_rank = 0 if decision == "approve_already_ok_ready" else 1
    token_rank = 0 if row.get("token_surface") == "plain_text" else 1
    return (release_rank, decision_rank, token_rank, word_count(row), -int(row.get("impact_score") or 0))


def selected_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    file_counts = Counter(str(row.get("relative_path") or "") for row in rows)
    family_counts = Counter(family for row in rows for family in split_csv(row.get("issue_families")))
    issue_total = sum(int(row.get("open_issue_count") or 0) for row in rows)
    return {
        "record_count": len(rows),
        "distinct_files": len(file_counts),
        "top_file_count": file_counts.most_common(1)[0][1] if file_counts else 0,
        "top_file": file_counts.most_common(1)[0][0] if file_counts else "",
        "file_counts_top10": dict(file_counts.most_common(10)),
        "distinct_issue_families": len(family_counts),
        "issue_family_counts_top10": dict(family_counts.most_common(10)),
        "open_issue_count_total": issue_total,
    }


def build_culture_probe(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    eligible: list[tuple[dict[str, Any], str, str]] = []
    for row in rows:
        decision, reason = classify_culture(row)
        decision_counts[decision] += 1
        reason_counts[reason] += 1
        if decision in {"approve_already_ok_ready", "corrected_text_ready", "needs_more_context"}:
            eligible.append((row, decision, reason))
    selected_items = sorted(eligible, key=lambda item: priority(item[0], item[1]))[:30]
    records: list[dict[str, Any]] = []
    for index, (row, decision, reason) in enumerate(selected_items, 1):
        records.append(
            {
                "source": SOURCE,
                "record_type": "culture_tradition_innovation_plain_light_probe_item",
                "review_index": index,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "release_class": row.get("release_class"),
                "token_surface": row.get("token_surface"),
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "suggested_corrected_text": suggested_corrected_text(row),
                "suggested_human_decision": decision,
                "selection_reason": reason,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "learning_ingest_count": 0,
                "issue_closure_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    selected_decisions = Counter(record["suggested_human_decision"] for record in records)
    summary = {
        "eligible_probe_count": len(eligible),
        "record_count": len(records),
        "classified_decision_counts_all_rows": dict(decision_counts.most_common()),
        "classification_reason_counts_all_rows": dict(reason_counts.most_common(20)),
        "selected_decision_counts": dict(selected_decisions.most_common()),
        "expected_yield": {
            "approve_already_ok_ready": selected_decisions.get("approve_already_ok_ready", 0),
            "corrected_text_ready": selected_decisions.get("corrected_text_ready", 0),
            "needs_more_context": selected_decisions.get("needs_more_context", 0),
            "parser_later": 0,
            "reject": 0,
        },
        "selected_stats": selected_stats(records),
    }
    return records, summary


def cycle_summary(cycle: dict[str, str]) -> dict[str, Any]:
    delta = read_json(cycle["delta"])
    issue = read_json(cycle["issue"])
    packet_rows = read_jsonl(cycle["packet"])
    stats = selected_stats(packet_rows)
    return {
        "name": cycle["name"],
        "closed_count": int((delta.get("global_delta") or {}).get("closed_count") or 0),
        "issues_closed_count": int(issue.get("closed_issue_count") or 0),
        "issue_segment_count": int(issue.get("segment_count") or 0),
        "packet_stats": stats,
    }


def write_outputs(
    summary: dict[str, Any],
    culture_records: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_post583_roi_comparison"
    summary_path = Path(str(base) + "_summary.json")
    culture_jsonl = Path(str(base) + "_culture_probe.jsonl")
    md_path = base.with_suffix(".md")
    with culture_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in culture_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {
        "summary": str(summary_path),
        "markdown": str(md_path),
        "culture_probe_jsonl": str(culture_jsonl),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Release Readiness Post-583 ROI Comparison",
        "",
        f"- Pending total: {summary['readiness']['pending_total']}",
        f"- Needs output apply: {summary['readiness']['needs_output_apply']} ({summary['readiness']['needs_output_apply_segment_ids']})",
        f"- Narrative eligible remaining: {summary['narrative_probe']['eligible_ready_count']}",
        f"- Narrative next expected ready: {summary['narrative_probe']['expected_ready']}",
        f"- Culture selected expected yield: {summary['culture_probe']['expected_yield']}",
        "",
        f"Recommendation: {summary['decision']['recommendation']}",
        f"Reason: {summary['decision']['reason']}",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, md_path, culture_jsonl


def main() -> None:
    readiness = read_json(READINESS_SUMMARY)
    rows = read_jsonl(READINESS_JSONL)
    narrative_summary = read_json(NARRATIVE_PACKET_SUMMARY)
    narrative_rows = read_jsonl(NARRATIVE_PACKET_JSONL)
    culture_records, culture_summary = build_culture_probe(rows)
    cycles = [cycle_summary(cycle) for cycle in STRICT_CYCLES]
    narrative_stats = selected_stats(narrative_rows)
    narrative_expected_ready = int((narrative_summary.get("expected_yield") or {}).get("total_expected_ready") or 0)
    narrative_repetition_excessive = narrative_stats["top_file_count"] > 10
    culture_ready = int((culture_summary.get("expected_yield") or {}).get("approve_already_ok_ready") or 0) + int(
        (culture_summary.get("expected_yield") or {}).get("corrected_text_ready") or 0
    )
    if narrative_expected_ready >= 20 and not narrative_repetition_excessive:
        recommendation = "continue_narrative_strict_roi_limit_30"
        reason = "Narrative strict ROI still has >=20 ready probable items and file repetition is not excessive."
    elif culture_ready >= 20:
        recommendation = "switch_to_culture_tradition_innovation_plain_light"
        reason = "Narrative ready count/repetition no longer clears threshold, while culture has sufficient ready probable volume."
    else:
        recommendation = "triage_high_issue_or_parser_architecture"
        reason = "Neither narrative nor culture offers a clean >=20 ready tranche; next ROI is targeted high-issue or parser work."

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_roi_comparison_post583",
        "readiness": {
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
            "top_visible_groups": readiness.get("top_10_release_impact_groups"),
        },
        "narrative_probe": {
            "summary": str(NARRATIVE_PACKET_SUMMARY),
            "jsonl": str(NARRATIVE_PACKET_JSONL),
            "eligible_ready_count": narrative_summary.get("eligible_ready_count"),
            "record_count": narrative_summary.get("record_count"),
            "expected_ready": narrative_expected_ready,
            "expected_yield": narrative_summary.get("expected_yield"),
            "selected_stats": narrative_stats,
            "repetition_excessive": narrative_repetition_excessive,
        },
        "narrative_strict_roi_last_4_cycles": cycles,
        "culture_probe": culture_summary,
        "decision": {"recommendation": recommendation, "reason": reason},
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "production_full_recommended_now": False,
    }
    summary_path, md_path, culture_jsonl = write_outputs(summary, culture_records)
    print(f"summary={summary_path}")
    print(f"markdown={md_path}")
    print(f"culture_probe_jsonl={culture_jsonl}")
    print(f"pending_total={summary['readiness']['pending_total']}")
    print(f"narrative_eligible_ready={summary['narrative_probe']['eligible_ready_count']}")
    print(f"narrative_expected_ready={summary['narrative_probe']['expected_ready']}")
    print(f"culture_expected_yield={json.dumps(summary['culture_probe']['expected_yield'], ensure_ascii=False, sort_keys=True)}")
    print(f"recommendation={summary['decision']['recommendation']}")
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
