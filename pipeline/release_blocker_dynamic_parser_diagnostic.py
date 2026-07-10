from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_blocker_dynamic_parser_diagnostic_v1"
DEFAULT_INPUT = Path("reports/20260703_182038_026776_release_readiness_post544_diagnostic.jsonl")
DEFAULT_PROBE = Path("reports/20260703_182103_140517_release_readiness_narrative_strict_roi_approve_ok_readiness.jsonl")
DEFAULT_RUN_ID = 585
TARGET_GROUPS = {
    "narrative_events",
    "ui_tooltips_short_labels",
    "religion_faith_doctrine",
    "culture_tradition_innovation",
    "interactions_schemes_contracts",
}
EXPLICIT_HOLDS = {
    120831: "hold_token_dynamic_pending_apply_confirmed",
    127174: "old_human_confirmation_equal_output_candidate_id_mismatch",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only release blocker dynamic/parser diagnostic.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--probe-jsonl", type=Path, default=DEFAULT_PROBE)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--sample-limit", type=int, default=160)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("spanish_text", "english_text", "output_text", "confirmed_text", "source_key", "issue_families", "issue_kinds")
    )


def has_concept_glossary_literal(row: dict[str, Any]) -> bool:
    blob = text_blob(row)
    return any(marker in blob for marker in ("Concept(", "Glossary(", "#T", "[Concept", "game_concept", "game_concept_desc"))


def has_spanish_literal_residue(row: dict[str, Any]) -> bool:
    if not row.get("spanish_residue_visible"):
        return False
    return row.get("token_surface") in {"plain_text", "light_token"}


def surface_flags(row: dict[str, Any], probe_flags: dict[int, set[str]]) -> list[str]:
    segment_id = int(row.get("segment_id") or 0)
    flags: list[str] = []
    token_surface = str(row.get("token_surface") or "unknown")
    if token_surface in {"dynamic_getter", "dynamic_select", "multiline"}:
        flags.append(token_surface)
    if row.get("release_class") == "parser_later":
        flags.append("parser_later")
    if has_concept_glossary_literal(row):
        flags.append("concept_glossary_literal_token")
    if not bool(row.get("canonical_output_equals_confirmed", True)):
        flags.append("canonical_l10n_mismatch")
    if segment_id in probe_flags and "output_differs_from_packet" in probe_flags[segment_id]:
        flags.append("output_differs_from_packet")
    if has_spanish_literal_residue(row):
        flags.append("spanish_residue_literal")
    if row.get("gender_or_perspective"):
        flags.append("gender_perspective")
    if not flags:
        flags.append("other_release_blocker")
    return flags


def dominant_issue_family(row: dict[str, Any]) -> str:
    families = split_csv(row.get("issue_families"))
    if not families:
        return "none"
    priority = [
        "dynamic_ck3_expression_microagent",
        "gender_token_microagent",
        "high_issue_auditor",
        "semantic_review_router",
        "short_label_style_microagent",
        "spanish_residual_microagent",
        "culture_semantic_microagent",
        "religion_semantic_microagent",
        "surface_boundary_microagent",
        "long_text_composer",
    ]
    for family in priority:
        if family in families:
            return family
    return families[0]


def risk_bucket(row: dict[str, Any], flags: list[str]) -> str:
    if int(row.get("needs_output_apply") or 0) == 1 or not bool(row.get("canonical_output_equals_confirmed", True)):
        return "hold_output_apply_or_text_mismatch"
    if int(row.get("high_issue_count") or 0) > 0 and any(flag in flags for flag in ("dynamic_getter", "dynamic_select", "multiline")):
        return "parser_policy_high_risk"
    if int(row.get("high_issue_count") or 0) > 0:
        return "high_issue_human_triage"
    if any(flag in flags for flag in ("dynamic_getter", "dynamic_select", "multiline", "concept_glossary_literal_token")):
        return "parser_policy_candidate"
    if row.get("token_surface") in {"plain_text", "light_token"}:
        return "manual_small_packet_candidate"
    return "hold_context"


def recommended_next(row: dict[str, Any], flags: list[str], risk: str) -> str:
    if int(row.get("segment_id") or 0) in EXPLICIT_HOLDS:
        return "hold"
    if risk == "hold_output_apply_or_text_mismatch":
        return "hold"
    if risk == "parser_policy_high_risk":
        return "parser_policy"
    if risk == "high_issue_human_triage":
        return "triagem_humana_high_issue"
    if risk == "parser_policy_candidate":
        return "parser_policy"
    if risk == "manual_small_packet_candidate":
        return "pacote_manual_pequeno"
    return "hold"


def compact_record(row: dict[str, Any], flags: list[str], risk: str, recommendation: str) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "record_type": "release_blocker_dynamic_parser_sample",
        "segment_id": int(row.get("segment_id") or 0),
        "hold_reason": EXPLICIT_HOLDS.get(int(row.get("segment_id") or 0)),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "visibility_group": row.get("visibility_group"),
        "release_class": row.get("release_class"),
        "token_surface": row.get("token_surface"),
        "surface_flags": flags,
        "risk_bucket": risk,
        "recommended_next_front": recommendation,
        "dominant_issue_family": dominant_issue_family(row),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "canonical_output_equals_confirmed": bool(row.get("canonical_output_equals_confirmed")),
        "output_differs_from_confirmed": bool(row.get("output_differs_from_confirmed")),
        "spanish_residue_visible": bool(row.get("spanish_residue_visible")),
        "gender_or_perspective": bool(row.get("gender_or_perspective")),
        "source_text": row.get("spanish_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def load_probe_flags(rows: list[dict[str, Any]]) -> dict[int, set[str]]:
    output: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        segment_id = int(row.get("segment_id") or 0)
        for reason in row.get("block_reasons") or []:
            output[segment_id].add(str(reason))
    return output


def choose_balanced_samples(records: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    per_key: Counter[str] = Counter()
    sorted_records = sorted(
        records,
        key=lambda row: (
            row["recommended_next_front"] != "parser_policy",
            row["risk_bucket"] != "parser_policy_high_risk",
            -int(row.get("high_issue_count") or 0),
            -int(row.get("open_issue_count") or 0),
            row["visibility_group"],
            row["segment_id"],
        ),
    )
    for record in sorted_records:
        key = f"{record['visibility_group']}::{record['token_surface']}::{record['dominant_issue_family']}"
        if per_key[key] >= 5:
            continue
        selected.append(record)
        per_key[key] += 1
        if len(selected) >= sample_limit:
            break
    return selected


def ratio(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2) if denominator else 0.0


def build_markdown(summary: dict[str, Any], sample_rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Release Blocker Dynamic/Parser Diagnostic",
        "",
        f"- Segment-state run: {summary['segment_state_run_id']}",
        f"- Release blockers analisados: {summary['release_blocker_count']}",
        f"- Holds explícitos mantidos: {', '.join(str(item['segment_id']) for item in summary['explicit_holds'])}",
        f"- Produção full recomendada agora: {str(summary['production_full_recommended_now']).lower()}",
        "",
        "## Superfícies Principais",
    ]
    for name, count in summary["surface_flag_counts"].items():
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Grupos Visíveis"])
    for group, count in summary["target_group_counts"].items():
        lines.append(f"- {group}: {count}")
    lines.extend(["", "## Top Famílias Para Arquitetura/Parser"])
    for item in summary["top_policy_parser_opportunities"][:8]:
        lines.append(
            f"- {item['visibility_group']} / {item['dominant_issue_family']} / {item['primary_surface']}: "
            f"{item['count']} casos, risco={item['risk_bucket']}, recomendação={item['recommended_next_front']}"
        )
    lines.extend(["", "## Lote Manual Seguro"])
    manual = summary["safe_small_manual_packet_probe"]
    lines.append(
        f"- candidatos plain/light sem high issue e sem mismatch: {manual['candidate_count']} "
        f"(recomendação: {manual['recommendation']})"
    )
    lines.extend(["", "## Recomendação"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Amostras"])
    for row in sample_rows[:30]:
        lines.append(
            f"- {row['segment_id']} | {row['visibility_group']} | {row['token_surface']} | "
            f"{row['dominant_issue_family']} | {row['risk_bucket']} | {row['source_key']}"
        )
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    readiness_rows = read_jsonl(args.input_jsonl)
    probe_flags = load_probe_flags(read_jsonl(args.probe_jsonl))
    release_rows = [
        row
        for row in readiness_rows
        if row.get("release_class") == "release_blocker" or int(row.get("segment_id") or 0) in EXPLICIT_HOLDS
    ]
    records: list[dict[str, Any]] = []
    for row in release_rows:
        flags = surface_flags(row, probe_flags)
        risk = risk_bucket(row, flags)
        recommendation = recommended_next(row, flags, risk)
        records.append(compact_record(row, flags, risk, recommendation))

    surface_counts: Counter[str] = Counter()
    for record in records:
        surface_counts.update(record["surface_flags"])
    target_group_counts = Counter(record["visibility_group"] for record in records if record["visibility_group"] in TARGET_GROUPS)
    group_surface_counts = Counter(
        f"{record['visibility_group']}::{flag}"
        for record in records
        if record["visibility_group"] in TARGET_GROUPS
        for flag in record["surface_flags"]
    )
    family_counts = Counter(record["dominant_issue_family"] for record in records)
    risk_counts = Counter(record["risk_bucket"] for record in records)
    recommendation_counts = Counter(record["recommended_next_front"] for record in records)
    token_counts = Counter(record["token_surface"] for record in records)

    opportunities: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        primary_surface = record["surface_flags"][0]
        key = (
            record["visibility_group"],
            record["dominant_issue_family"],
            primary_surface,
            record["risk_bucket"],
            record["recommended_next_front"],
        )
        opportunities[key].append(record)
    top_opportunities = []
    for (visibility, family, primary_surface, risk, recommendation), rows in opportunities.items():
        top_opportunities.append(
            {
                "visibility_group": visibility,
                "dominant_issue_family": family,
                "primary_surface": primary_surface,
                "risk_bucket": risk,
                "recommended_next_front": recommendation,
                "count": len(rows),
                "high_issue_count": sum(1 for row in rows if int(row.get("high_issue_count") or 0) > 0),
                "example_segment_ids": [int(row["segment_id"]) for row in rows[:8]],
            }
        )
    top_opportunities.sort(key=lambda item: (item["recommended_next_front"] != "parser_policy", -item["count"]))

    manual_candidates = [
        record
        for record in records
        if record["recommended_next_front"] == "pacote_manual_pequeno"
        and record["token_surface"] in {"plain_text", "light_token"}
        and int(record["high_issue_count"] or 0) == 0
        and int(record["needs_output_apply"] or 0) == 0
        and record["canonical_output_equals_confirmed"]
    ]
    corrected_or_approve_probe = {
        "candidate_count": len(manual_candidates),
        "sample_segment_ids": [int(row["segment_id"]) for row in manual_candidates[:30]],
        "recommendation": "manual_packet_possible" if len(manual_candidates) >= 10 else "not_enough_safe_manual_yield",
    }

    sample_rows = choose_balanced_samples(records, args.sample_limit)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_release_blocker_dynamic_parser_diagnostic",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "probe_jsonl": str(args.probe_jsonl),
        "release_blocker_count": sum(1 for row in records if row["release_class"] == "release_blocker"),
        "sample_record_count": len(sample_rows),
        "explicit_holds": [
            {"segment_id": segment_id, "hold_reason": reason}
            for segment_id, reason in sorted(EXPLICIT_HOLDS.items())
        ],
        "surface_flag_counts": dict(surface_counts.most_common()),
        "token_surface_counts": dict(token_counts.most_common()),
        "target_group_counts": dict(target_group_counts.most_common()),
        "group_surface_counts": dict(group_surface_counts.most_common()),
        "dominant_issue_family_counts": dict(family_counts.most_common(30)),
        "risk_bucket_counts": dict(risk_counts.most_common()),
        "recommended_next_front_counts": dict(recommendation_counts.most_common()),
        "top_policy_parser_opportunities": top_opportunities[:25],
        "safe_small_manual_packet_probe": corrected_or_approve_probe,
        "release_blocker_dynamic_parser_ratio_pct": ratio(
            sum(1 for row in records if row["token_surface"] in {"dynamic_getter", "dynamic_select", "multiline"}),
            sum(1 for row in records if row["release_class"] == "release_blocker"),
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Stop narrative strict ROI approve_already_ok as an immediate lane. The remaining release blockers are dominated by dynamic getter/select, "
            "gender/perspective, and high-issue parser surfaces. Next best front is a read-only architecture/parser review for dynamic_getter + gender "
            "in narrative_events and ui_tooltips_short_labels, while keeping 120831 and 127174 in explicit hold."
        ),
    }
    markdown = build_markdown(summary, sample_rows)
    return sample_rows, summary, markdown


def write_outputs(rows: list[dict[str, Any]], summary: dict[str, Any], markdown: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_dynamic_parser_diagnostic"
    txt_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    jsonl_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    summary["output_files"] = {
        "markdown": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    txt_path.write_text(markdown, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows, summary, markdown = build(args)
    output_files = write_outputs(rows, summary, markdown)
    print(f"markdown={output_files['markdown']}")
    print(f"jsonl={output_files['jsonl']}")
    print(f"summary={output_files['summary']}")
    print(f"release_blocker_count={summary['release_blocker_count']}")
    print(f"surface_flag_counts={json.dumps(summary['surface_flag_counts'], ensure_ascii=False)}")
    print(f"target_group_counts={json.dumps(summary['target_group_counts'], ensure_ascii=False)}")
    print(f"recommended_next_front_counts={json.dumps(summary['recommended_next_front_counts'], ensure_ascii=False)}")
    print(f"safe_small_manual_packet_candidate_count={summary['safe_small_manual_packet_probe']['candidate_count']}")
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
