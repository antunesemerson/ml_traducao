from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_debug_placeholder_hold_review_v1"
SUMMARY_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic_summary.json"
)
JSONL_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_SURFACE_BUCKET = "religion_faith_doctrine"
EXPECTED_FAMILY = "debug_placeholder_hold"
EXPECTED_COUNT = 61

DEBUG_OR_PLACEHOLDER_RE = re.compile(
    r"#D\b|#T\b|PLACEHOLDER|\bTODO\b|\bDEBUG\b|LOC_PLACEHOLDER|placeholder",
    re.IGNORECASE,
)
RUNTIME_GETTER_RE = re.compile(
    r"\[(?:ROOT|CHARACTER|TARGET_CHARACTER|THIS|SCOPE|FAITH|RELIGION|TOKEN_PARAMETER|Get[A-Za-z]+)[^\]]+\]"
)
SCRIPTED_PROPERTY_RE = re.compile(r"\[(?:ROOT|CHARACTER|TARGET_CHARACTER|THIS|SCOPE)[^\]]+\.(?:GetProperty|Var|Custom|Custom2)\(")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def classify(row: dict[str, Any]) -> tuple[str, str, list[str]]:
    text = str(row.get("current_output_text") or "")
    source_key = str(row.get("source_key") or "")
    risk_bucket = str(row.get("risk_bucket") or "")
    token_count = int(row.get("token_count") or 0)
    bracket_count = int(row.get("bracket_token_count") or 0)
    tags: list[str] = []

    if DEBUG_OR_PLACEHOLDER_RE.search(text) or DEBUG_OR_PLACEHOLDER_RE.search(source_key):
        tags.append("explicit_debug_or_tooltip_marker")
    if SCRIPTED_PROPERTY_RE.search(text):
        tags.append("scripted_property_or_scope_getter")
    if RUNTIME_GETTER_RE.search(text):
        tags.append("runtime_getter_surface")
    if risk_bucket in {"high_structural_token_density", "medium_dynamic_dense", "high_multiline_effect_list"}:
        tags.append("dense_or_multiline_risk")
    if token_count >= 7 or bracket_count >= 7:
        tags.append("high_token_count")

    if "explicit_debug_or_tooltip_marker" in tags and (
        "dense_or_multiline_risk" in tags or "runtime_getter_surface" in tags or "scripted_property_or_scope_getter" in tags
    ):
        return "terminal_hold_debug_placeholder", "explicit marker plus dense/runtime localization structure", tags
    if "explicit_debug_or_tooltip_marker" in tags:
        return "terminal_hold_debug_placeholder", "explicit debug/tooltip/placeholder marker", tags
    if "scripted_property_or_scope_getter" in tags or ("runtime_getter_surface" in tags and "dense_or_multiline_risk" in tags):
        return "needs_parser_policy", "dense runtime getter surface without explicit placeholder marker", tags
    if risk_bucket in {"medium_dynamic_light", "low_plain_domain"}:
        return "needs_human_context", "light/plain surface needs human check before terminal hold", tags
    return "false_positive_not_placeholder", "no explicit debug/placeholder signal and no dense runtime marker", tags


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if summary.get("surface_bucket") != EXPECTED_SURFACE_BUCKET:
        raise SystemExit("surface_bucket guard failed")
    if summary.get("source_changed") is not False:
        raise SystemExit("source_changed guard failed")
    if int(summary.get("candidate_generation_count") or 0) != 0:
        raise SystemExit("candidate_generation_count guard failed")
    if int(summary.get("apply_count") or 0) != 0:
        raise SystemExit("apply_count guard failed")

    family_rows = [
        row
        for row in rows
        if row.get("surface_bucket") == EXPECTED_SURFACE_BUCKET and row.get("hold_family") == EXPECTED_FAMILY
    ]
    if len(family_rows) != EXPECTED_COUNT:
        raise SystemExit(f"debug_placeholder_hold count guard failed: {len(family_rows)} expected {EXPECTED_COUNT}")
    duplicates = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in family_rows).items() if count > 1]
    if duplicates:
        raise SystemExit(f"duplicate segment_id guard failed: {duplicates[:10]}")
    return family_rows


def build_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (str(item.get("risk_bucket") or ""), int(item["segment_id"]))):
        decision, reason, tags = classify(row)
        reviewed.append(
            {
                "record_type": "debug_placeholder_hold_review",
                "source": SOURCE,
                "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "source_line_number": row.get("source_line_number"),
                "surface_bucket": row.get("surface_bucket"),
                "hold_family": row.get("hold_family"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": int(row.get("token_count") or 0),
                "bracket_token_count": int(row.get("bracket_token_count") or 0),
                "current_output_text": row.get("current_output_text"),
                "spanish_text": row.get("spanish_text"),
                "english_text": row.get("english_text"),
                "decision": decision,
                "decision_reason": reason,
                "decision_tags": tags,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
                "requires_output_apply_later": False,
                "requires_lifecycle_later": False,
            }
        )
    return reviewed


def write_reports(reviewed: list[dict[str, Any]]) -> tuple[Path, Path, Path, dict[str, Any]]:
    decision_counts = Counter(str(row["decision"]) for row in reviewed)
    risk_counts = Counter(str(row["risk_bucket"]) for row in reviewed)
    tag_counts = Counter(tag for row in reviewed for tag in row["decision_tags"])
    false_positive_count = int(decision_counts.get("false_positive_not_placeholder", 0))
    terminal_hold_count = int(decision_counts.get("terminal_hold_debug_placeholder", 0))
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_review",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "surface_bucket": EXPECTED_SURFACE_BUCKET,
        "hold_family": EXPECTED_FAMILY,
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "decision_counts": dict(decision_counts),
        "risk_bucket_counts": dict(risk_counts),
        "decision_tag_counts": dict(tag_counts),
        "terminal_hold_debug_placeholder_count": terminal_hold_count,
        "needs_parser_policy_count": int(decision_counts.get("needs_parser_policy", 0)),
        "needs_human_context_count": int(decision_counts.get("needs_human_context", 0)),
        "false_positive_not_placeholder_count": false_positive_count,
        "registry_registration_allowed": false_positive_count == 0 and terminal_hold_count > 0,
        "recommended_agent_key": "religion_faith_doctrine_debug_placeholder_hold_policy",
        "recommended_agent_type": "symbolic_subpolicy",
        "recommended_operational_state": "shadow",
        "recommended_decision_role": "terminal_guard",
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
        "single_operational_recommendation": (
            "Register read-only terminal hold policy for terminal_hold_debug_placeholder rows; "
            "route parser/human residuals through future architecture review without candidate generation."
        ),
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_debug_placeholder_hold_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine debug_placeholder_hold review",
        "",
        f"segment_state_run_id: {EXPECTED_SEGMENT_STATE_RUN_ID}",
        f"review_count: {len(reviewed)}",
        "",
        "decision_counts:",
    ]
    lines.extend(f"- {count} | {key}" for key, count in decision_counts.most_common())
    lines.extend(["", "risk_bucket_counts:"])
    lines.extend(f"- {count} | {key}" for key, count in risk_counts.most_common())
    lines.extend(["", "representative_examples:"])
    for decision in ("terminal_hold_debug_placeholder", "needs_parser_policy", "needs_human_context", "false_positive_not_placeholder"):
        examples = [row for row in reviewed if row["decision"] == decision][:5]
        if not examples:
            continue
        lines.extend(["", f"## {decision}"])
        for row in examples:
            output = str(row.get("current_output_text") or "").replace("\n", "\\n")
            lines.append(f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['source_key']}")
            lines.append(f"  reason: {row['decision_reason']}")
            lines.append(f"  output: {output[:420]}")
    lines.extend(
        [
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
            "",
            f"recommendation: {summary['single_operational_recommendation']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, summary


def main() -> None:
    summary = read_json(SUMMARY_PATH)
    rows = read_jsonl(JSONL_PATH)
    family_rows = validate_inputs(summary, rows)
    reviewed = build_review_rows(family_rows)
    txt_path, jsonl_path, summary_path, result = write_reports(reviewed)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"review_count={result['review_count']}")
    print(f"decision_counts={json.dumps(result['decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"registry_registration_allowed={str(result['registry_registration_allowed']).lower()}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
