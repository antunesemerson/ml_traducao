from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_pantheon_divine_runtime_review_v1"
CONSOLIDATED_JSONL_PATH = Path("reports/20260630_115336_788090_domain_policy_vote_candidate_religion_faith_post_religion_family_policy_consolidated_diagnostic.jsonl")
CONSOLIDATED_SUMMARY_PATH = Path("reports/20260630_115336_788090_domain_policy_vote_candidate_religion_faith_post_religion_family_policy_consolidated_diagnostic_summary.json")
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_FAMILY = "pantheon_divine_runtime_name"
EXPECTED_COUNT = 10

PANTHEON_RE = re.compile(r"PantheonTerm", re.I)
HIGH_GOD_RE = re.compile(r"HighGodName|HighGodNamePossessive|GodName|divine|deus|god", re.I)
RELIGIOUS_TEXT_RE = re.compile(r"ReligiousText|scripture|texto religioso", re.I)
LEGEND_RE = re.compile(r"legend|Legend|ROOT\.Legend|chapter", re.I)
CULTURE_EVENT_RE = re.compile(r"culture_tradition|tradition_|event", re.I)
POSSESSIVE_RE = re.compile(r"Possessive|de [A-Z]|do |da ", re.I)
DENSE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|ROOT\.|GetProperty|faith_scope|Religion\.|Faith\.")
SPANISH_RESIDUE_RE = re.compile(r"Ã|Â|\b(?:doctrina|pecado|salvaci[oó]n)\b", re.I)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_consolidated_diagnostic":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if summary.get("next_recommended_family") != EXPECTED_FAMILY:
        raise SystemExit("next recommended family guard failed")
    target = [row for row in rows if row.get("hold_family") == EXPECTED_FAMILY]
    if len(target) != EXPECTED_COUNT:
        raise SystemExit(f"target count guard failed: {len(target)} expected {EXPECTED_COUNT}")
    return target


def classify(row: dict[str, Any]) -> tuple[str, str, list[str], str]:
    blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "current_output_text", "english_text"))
    tags: list[str] = []
    for label, pattern in (
        ("pantheon_term", PANTHEON_RE),
        ("high_god_name", HIGH_GOD_RE),
        ("religious_text", RELIGIOUS_TEXT_RE),
        ("legend_context", LEGEND_RE),
        ("culture_event_context", CULTURE_EVENT_RE),
        ("possessive_surface", POSSESSIVE_RE),
        ("dense_token_surface", DENSE_TOKEN_RE),
        ("spanish_residue_or_mojibake", SPANISH_RESIDUE_RE),
    ):
        if pattern.search(blob):
            tags.append(label)
    if "legend_context" in tags and ("pantheon_term" in tags or "religious_text" in tags):
        return (
            "legend_pantheon_religious_text_runtime",
            "hold_legend_runtime_context",
            tags,
            "Legend runtime combines pantheon/religious text and story context.",
        )
    if "pantheon_term" in tags:
        return (
            "pantheon_term_runtime",
            "needs_pantheon_term_parser_review",
            tags,
            "PantheonTerm runtime needs parser role classification.",
        )
    if "high_god_name" in tags and "possessive_surface" in tags:
        return (
            "high_god_possessive_runtime",
            "needs_divine_possessive_parser_review",
            tags,
            "HighGodName possessive surface needs parser/grammar context.",
        )
    if "high_god_name" in tags:
        return (
            "high_god_name_runtime",
            "needs_divine_name_parser_review",
            tags,
            "HighGodName runtime needs role classification.",
        )
    return (
        "residual_divine_runtime_context",
        "hold_or_architecture_later",
        tags,
        "Residual divine runtime context lacks a safe route.",
    )


def main() -> None:
    summary = read_json(CONSOLIDATED_SUMMARY_PATH)
    rows = read_jsonl(CONSOLIDATED_JSONL_PATH)
    reviewed = []
    for row in validate_inputs(summary, rows):
        subtype, action, tags, reason = classify(row)
        reviewed.append(
            {
                "record_type": "pantheon_divine_runtime_review",
                "source": SOURCE,
                "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "surface_bucket": row.get("surface_bucket"),
                "hold_family": row.get("hold_family"),
                "risk_bucket": row.get("risk_bucket"),
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "operational_subtype": subtype,
                "recommended_action": action,
                "review_reason": reason,
                "review_tags": tags,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )
    subtype_counts = Counter(row["operational_subtype"] for row in reviewed)
    action_counts = Counter(row["recommended_action"] for row in reviewed)
    risk_counts = Counter(row["risk_bucket"] for row in reviewed)
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_pantheon_divine_runtime_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    result = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_review",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "hold_family": EXPECTED_FAMILY,
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "operational_subtype_counts": dict(subtype_counts),
        "recommended_action_counts": dict(action_counts),
        "risk_bucket_counts": dict(risk_counts),
        "parser_or_policy_count": int(action_counts.get("needs_pantheon_term_parser_review", 0))
        + int(action_counts.get("needs_divine_possessive_parser_review", 0))
        + int(action_counts.get("needs_divine_name_parser_review", 0)),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Register a read-only pantheon/divine runtime splitter with legend contexts held.",
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "domain_policy_vote_candidate religion_faith_doctrine pantheon_divine_runtime_name review",
                "",
                f"segment_state_run_id: {EXPECTED_SEGMENT_STATE_RUN_ID}",
                f"review_count: {len(reviewed)}",
                "",
                "operational_subtype_counts:",
                *[f"- {count} | {key}" for key, count in subtype_counts.most_common()],
                "",
                "recommended_action_counts:",
                *[f"- {count} | {key}" for key, count in action_counts.most_common()],
                "",
                "guards:",
                "- candidate_generation_allowed: false",
                "- auto_apply_allowed: false",
                "- lifecycle_allowed: false",
                "- production_release_allowed: false",
                "- apply: not_run",
                "- lifecycle: not_run",
                "- segment_state: not_run",
                "- reindex: not_run",
                "- full_production: not_run",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"summary={summary_path}")
    print(f"review_count={len(reviewed)}")
    print(f"operational_subtype_counts={json.dumps(dict(subtype_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"recommended_action_counts={json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
