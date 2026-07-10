from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post596_final_readonly_v1"
READINESS_SUMMARY = Path("reports/20260704_115052_970334_release_readiness_post544_diagnostic_summary.json")
READINESS_JSONL = Path("reports/20260704_115052_970334_release_readiness_post544_diagnostic.jsonl")
CONCEPT_HOLDS = {79601, 104908}
TOKEN_DYNAMIC_HOLDS = {120831}
BLOCKED_RECENT = {65282, 75914}
CONTEXT_HOLDS = {99428}
PREVIOUS_PACKET_HOLDS = {30464, 45089, 54888, 68315, 76377, 77588, 103547, 104983, 112620, 114265}
EXCLUDED_IDS = CONCEPT_HOLDS | TOKEN_DYNAMIC_HOLDS | BLOCKED_RECENT | CONTEXT_HOLDS | PREVIOUS_PACKET_HOLDS
DYNAMIC_RE = re.compile(r"Concept\(|Glossary\(|Select_CString|SelectLocalization|\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\(|\n")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path) -> dict[str, Any]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def classify_tail(row: dict[str, Any]) -> str:
    segment_id = int(row.get("segment_id") or 0)
    if segment_id in TOKEN_DYNAMIC_HOLDS:
        return "hold_token_dynamic_needs_output_apply"
    if segment_id in CONCEPT_HOLDS:
        return "hold_concept_literal_surface_review"
    if segment_id in BLOCKED_RECENT:
        return "blocked_issue_resolution_uncertain"
    if segment_id in CONTEXT_HOLDS:
        return "hold_pronoun_or_actor_context"
    if segment_id in PREVIOUS_PACKET_HOLDS:
        return "hold_previous_packet_context"
    if row.get("release_class") != "release_blocker":
        return "not_release_blocker"
    output = str(row.get("output_text") or "")
    if row.get("token_surface") not in {"plain_text", "light_token"} or DYNAMIC_RE.search(output):
        return "parser_later_or_dynamic_excluded"
    if int(row.get("high_issue_count") or 0) > 0:
        return "corrected_text_needs_human_text"
    return "other_release_blocker"


def main() -> None:
    readiness = load_json(READINESS_SUMMARY)
    rows = read_jsonl(READINESS_JSONL)
    classified: list[dict[str, Any]] = []
    for row in rows:
        cls = classify_tail(row)
        if cls == "not_release_blocker" and int(row.get("segment_id") or 0) not in EXCLUDED_IDS:
            continue
        classified.append(
            {
                "segment_id": int(row.get("segment_id") or 0),
                "classification": cls,
                "visibility_group": row.get("visibility_group"),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "token_surface": row.get("token_surface"),
                "release_class": row.get("release_class"),
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "output_text": row.get("output_text"),
            }
        )
    counts = Counter(row["classification"] for row in classified)
    release_blockers = [row for row in rows if row.get("release_class") == "release_blocker"]
    top_visible = Counter(str(row.get("visibility_group") or "unknown") for row in release_blockers)
    strict_micro_candidates = [
        row
        for row in classified
        if row["classification"] == "corrected_text_needs_human_text"
        and row["segment_id"] not in EXCLUDED_IDS
        and row["visibility_group"] == "narrative_events"
        and row["token_surface"] in {"plain_text", "light_token"}
        and not DYNAMIC_RE.search(str(row.get("output_text") or ""))
    ]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "final_release_decision_read_only",
        "run_id": readiness.get("run_id"),
        "pending_total": readiness.get("pending_global_count"),
        "release_blocker": readiness.get("release_blocker_count"),
        "review_before_release": readiness.get("review_before_release_count"),
        "known_non_blocking_hold": readiness.get("known_non_blocking_hold_count"),
        "parser_later": readiness.get("parser_later_count"),
        "needs_output_apply": readiness.get("needs_output_apply_count"),
        "needs_output_apply_segment_ids": readiness.get("needs_output_apply_segment_ids", []),
        "segregated_holds": {
            "hold_token_dynamic": sorted(TOKEN_DYNAMIC_HOLDS),
            "hold_concept_literal_surface_review": sorted(CONCEPT_HOLDS),
            "blocked_issue_resolution_uncertain": sorted(BLOCKED_RECENT),
            "hold_pronoun_or_actor_context": sorted(CONTEXT_HOLDS),
            "hold_previous_packet_context": sorted(PREVIOUS_PACKET_HOLDS),
        },
        "tail_classification_counts": dict(counts.most_common()),
        "strict_micro_lote_candidate_count": len(strict_micro_candidates),
        "strict_micro_lote_candidate_ids": [row["segment_id"] for row in strict_micro_candidates],
        "top_visible_release_blocker_groups": dict(top_visible.most_common(12)),
        "single_operational_recommendation": "parar_pre_release_e_preparar_release_notes_backlog_parser_dynamic",
        "recommendation_reason": "Após o último micro-lote, restam menos de 8 candidatos narrativos curtos/seguros pelos filtros rígidos; o ganho manual imediato caiu abaixo do critério de continuação. A cauda dominante é parser/dynamic/holds conhecidos.",
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    base = reports_dir() / f"{stamp()}_release_decision_post596_final_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in classified:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Final Release Decision Post-596",
        "",
        f"- pending_total: {summary['pending_total']}",
        f"- release_blocker: {summary['release_blocker']}",
        f"- review_before_release: {summary['review_before_release']}",
        f"- known_non_blocking_hold: {summary['known_non_blocking_hold']}",
        f"- parser_later: {summary['parser_later']}",
        f"- needs_output_apply: {summary['needs_output_apply']} {summary['needs_output_apply_segment_ids']}",
        f"- strict_micro_lote_candidate_count: {summary['strict_micro_lote_candidate_count']}",
        "",
        "## Known Holds",
        f"- hold_token_dynamic: {sorted(TOKEN_DYNAMIC_HOLDS)}",
        f"- hold_concept_literal_surface_review: {sorted(CONCEPT_HOLDS)}",
        f"- blocked_issue_resolution_uncertain: {sorted(BLOCKED_RECENT)}",
        f"- hold_pronoun_or_actor_context: {sorted(CONTEXT_HOLDS)}",
        f"- hold_previous_packet_context: {sorted(PREVIOUS_PACKET_HOLDS)}",
        "",
        "## Tail Split",
        *[f"- {key}: {value}" for key, value in summary["tail_classification_counts"].items()],
        "",
        "## Top Visible Groups",
        *[f"- {key}: {value}" for key, value in summary["top_visible_release_blocker_groups"].items()],
        "",
        "## Recommendation",
        f"- {summary['single_operational_recommendation']}",
        f"- reason: {summary['recommendation_reason']}",
        "",
    ]
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"pending_total={summary['pending_total']}")
    print(f"release_blocker={summary['release_blocker']}")
    print(f"review_before_release={summary['review_before_release']}")
    print(f"known_non_blocking_hold={summary['known_non_blocking_hold']}")
    print(f"parser_later={summary['parser_later']}")
    print(f"needs_output_apply={summary['needs_output_apply']}")
    print(f"strict_micro_lote_candidate_count={summary['strict_micro_lote_candidate_count']}")
    print(f"single_operational_recommendation={summary['single_operational_recommendation']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
