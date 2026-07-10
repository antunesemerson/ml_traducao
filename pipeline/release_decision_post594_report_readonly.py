from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post594_report_readonly_v1"
DEFAULT_READINESS_SUMMARY = Path("reports/20260704_105253_874759_release_readiness_post544_diagnostic_summary.json")
DEFAULT_READINESS_JSONL = Path("reports/20260704_105253_874759_release_readiness_post544_diagnostic.jsonl")

CONCEPT_HOLDS = {79601, 104908}
TOKEN_DYNAMIC_HOLDS = {120831}
BLOCKED_RECENT = {65282, 75914}
PREVIOUS_PACKET_HOLDS = {
    30464,
    45089,
    54888,
    68315,
    76377,
    77588,
    103547,
    104983,
    112620,
    114265,
}
SEGREGATED_IDS = CONCEPT_HOLDS | TOKEN_DYNAMIC_HOLDS | BLOCKED_RECENT | PREVIOUS_PACKET_HOLDS

DYNAMIC_RE = re.compile(r"Select_CString|SelectLocalization|\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\.")
SPANISH_RE = re.compile(
    r"\b(ninguna|ninguno|puede|sostener|obligaci[oó]n|puestos de la corte|por las|"
    r"c[oó]nyuge|halago|ofenderse|compa[ñn]ero|v[ií]nculo|guantelete|gente|mayor|"
    r"terminar[aá]|fallos|trabajos|or[aá]culo|lanzamiento|predicador|cachorrillo)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def project_path(path: Path) -> Path:
    return db.project_path(path)


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only release decision report after segment-state run 594.")
    parser.add_argument("--readiness-summary", type=Path, default=DEFAULT_READINESS_SUMMARY)
    parser.add_argument("--readiness-jsonl", type=Path, default=DEFAULT_READINESS_JSONL)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with project_path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def text_len(row: dict[str, Any]) -> int:
    return len(str(row.get("output_text") or row.get("confirmed_text") or ""))


def segregated_class(segment_id: int) -> str | None:
    if segment_id in CONCEPT_HOLDS:
        return "hold_concept_literal_surface_review"
    if segment_id in TOKEN_DYNAMIC_HOLDS:
        return "hold_token_dynamic_needs_output_apply"
    if segment_id in BLOCKED_RECENT:
        return "blocked_issue_resolution_uncertain"
    if segment_id in PREVIOUS_PACKET_HOLDS:
        return "hold_previous_packet_context"
    return None


def classify(row: dict[str, Any]) -> str:
    segment_id = int(row.get("segment_id") or 0)
    explicit = segregated_class(segment_id)
    if explicit:
        return explicit
    if row.get("release_class") != "release_blocker":
        return "not_release_blocker"
    output = str(row.get("output_text") or "")
    token_surface = row.get("token_surface")
    issue_families = str(row.get("issue_families") or "").lower()
    issue_kinds = str(row.get("issue_kinds") or "").lower()
    if token_surface not in {"plain_text", "light_token"} or DYNAMIC_RE.search(output):
        return "parser_later_or_dynamic_excluded"
    if int(row.get("high_issue_count") or 0) > 0 and (
        SPANISH_RE.search(output)
        or "spanish_residual" in issue_families
        or "spanish_residue" in issue_kinds
        or "concept_expression" in issue_kinds
    ):
        return "corrected_text_needs_human_text"
    if (
        int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("high_issue_count") or 0) > 0
    ):
        return "approve_already_ok_possible"
    return "other_release_blocker"


def compact_row(row: dict[str, Any], classification: str) -> dict[str, Any]:
    return {
        "segment_id": int(row.get("segment_id") or 0),
        "classification": classification,
        "visibility_group": row.get("visibility_group"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "release_class": row.get("release_class"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "text_length": text_len(row),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
    }


def recommend(corrected_rows: list[dict[str, Any]], approve_rows: list[dict[str, Any]]) -> tuple[str, str]:
    short_safe = [
        row
        for row in corrected_rows
        if row["token_surface"] in {"plain_text", "light_token"}
        and row["text_length"] <= 220
        and row["segment_id"] not in SEGREGATED_IDS
    ]
    if len(short_safe) >= 8:
        return (
            "mais_um_micro_lote_humano",
            f"Há {len(short_safe)} corrected_text_needs_human_text curtos/plain-light fora dos holds; vale um micro-lote humano de 8-10 antes do corte pré-release.",
        )
    if approve_rows:
        return (
            "revisar_manualmente_blocked_holds_especificos",
            f"Há só {len(short_safe)} corrected_text curtos prováveis; os {len(approve_rows)} approve_already_ok_possible podem ser drenados, mas o ganho é pequeno.",
        )
    return (
        "parar_pre_release_e_documentar_limitacoes",
        "A cauda restante é majoritariamente parser/dynamic/hold; melhor preparar release notes e deixar sprint parser para pós-release.",
    )


def main() -> None:
    args = parse_args()
    readiness = load_json(args.readiness_summary)
    rows = read_jsonl(args.readiness_jsonl)
    release_blockers = [row for row in rows if row.get("release_class") == "release_blocker"]

    classified = []
    for row in rows:
        classification = classify(row)
        if classification == "not_release_blocker" and int(row.get("segment_id") or 0) not in SEGREGATED_IDS:
            continue
        classified.append(compact_row(row, classification))

    counts = Counter(row["classification"] for row in classified)
    top_visible = Counter(str(row.get("visibility_group") or "unknown") for row in release_blockers)
    top_files = Counter(str(row.get("relative_path") or "unknown") for row in release_blockers)
    corrected_rows = [row for row in classified if row["classification"] == "corrected_text_needs_human_text"]
    approve_rows = [row for row in classified if row["classification"] == "approve_already_ok_possible"]
    parser_rows = [row for row in classified if row["classification"] == "parser_later_or_dynamic_excluded"]
    short_safe_corrected = [
        row for row in corrected_rows if row["text_length"] <= 220 and row["token_surface"] in {"plain_text", "light_token"}
    ]
    recommendation, reason = recommend(corrected_rows, approve_rows)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_release_decision_post594",
        "run_id": readiness.get("run_id"),
        "pending_total": readiness.get("pending_global_count"),
        "release_blocker": readiness.get("release_blocker_count"),
        "review_before_release": readiness.get("review_before_release_count"),
        "known_non_blocking_hold": readiness.get("known_non_blocking_hold_count"),
        "parser_later": readiness.get("parser_later_count"),
        "needs_output_apply": readiness.get("needs_output_apply_count"),
        "needs_output_apply_segment_ids": readiness.get("needs_output_apply_segment_ids", []),
        "segregated_holds": {
            "hold_concept_literal_surface_review": sorted(CONCEPT_HOLDS),
            "hold_token_dynamic": sorted(TOKEN_DYNAMIC_HOLDS),
            "blocked_issue_resolution_uncertain": sorted(BLOCKED_RECENT),
            "hold_previous_packet_context": sorted(PREVIOUS_PACKET_HOLDS),
        },
        "blocker_classification_counts": dict(counts.most_common()),
        "corrected_text_needs_human_text_remaining": len(corrected_rows),
        "corrected_text_needs_human_text_short_safe_estimate": len(short_safe_corrected),
        "corrected_text_needs_human_text_ids": [row["segment_id"] for row in corrected_rows[:80]],
        "approve_already_ok_possible_remaining": len(approve_rows),
        "approve_already_ok_possible_ids": [row["segment_id"] for row in approve_rows[:80]],
        "parser_later_or_dynamic_excluded_count": len(parser_rows),
        "top_visible_release_blocker_groups": dict(top_visible.most_common(12)),
        "top_release_blocker_files": dict(top_files.most_common(12)),
        "recommendation_options": {
            "1_mais_um_micro_lote_humano": "recommended" if recommendation == "mais_um_micro_lote_humano" else "not_primary",
            "2_revisar_manualmente_blocked_holds_especificos": "recommended" if recommendation == "revisar_manualmente_blocked_holds_especificos" else "secondary",
            "3_parar_pre_release_e_preparar_release_notes_limitacoes": "recommended" if recommendation == "parar_pre_release_e_documentar_limitacoes" else "after_micro_lote",
        },
        "single_operational_recommendation": recommendation,
        "recommendation_reason": reason,
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

    base = reports_dir() / f"{stamp()}_release_decision_post594_report_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in classified:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Release Decision Post-594",
        "",
        "## Current State",
        f"- pending_total: {summary['pending_total']}",
        f"- release_blocker: {summary['release_blocker']}",
        f"- review_before_release: {summary['review_before_release']}",
        f"- known_non_blocking_hold: {summary['known_non_blocking_hold']}",
        f"- parser_later: {summary['parser_later']}",
        f"- needs_output_apply: {summary['needs_output_apply']} {summary['needs_output_apply_segment_ids']}",
        "",
        "## Explicit Segregation",
        f"- hold_concept_literal_surface_review: {sorted(CONCEPT_HOLDS)}",
        f"- hold_token_dynamic: {sorted(TOKEN_DYNAMIC_HOLDS)}",
        f"- blocked_issue_resolution_uncertain: {sorted(BLOCKED_RECENT)}",
        f"- hold_previous_packet_context: {sorted(PREVIOUS_PACKET_HOLDS)}",
        "",
        "## Blocker Split",
        *[f"- {key}: {value}" for key, value in summary["blocker_classification_counts"].items()],
        "",
        "## Top Visible Groups",
        *[f"- {key}: {value}" for key, value in summary["top_visible_release_blocker_groups"].items()],
        "",
        "## Recommendation",
        f"- {recommendation}",
        f"- reason: {reason}",
        "",
    ]
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "pending_total",
        "release_blocker",
        "review_before_release",
        "known_non_blocking_hold",
        "parser_later",
        "needs_output_apply",
        "corrected_text_needs_human_text_remaining",
        "approve_already_ok_possible_remaining",
        "single_operational_recommendation",
    ]:
        print(f"{key}={summary[key]}")
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
