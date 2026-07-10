from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_clergy_adherent_review_v1"
CONSOLIDATED_JSONL_PATH = Path(
    "reports/20260630_102017_125063_domain_policy_vote_candidate_religion_faith_post_policy_consolidated_diagnostic.jsonl"
)
CONSOLIDATED_SUMMARY_PATH = Path(
    "reports/20260630_102017_125063_domain_policy_vote_candidate_religion_faith_post_policy_consolidated_diagnostic_summary.json"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_FAMILY = "clergy_adherent_priest_terms"
EXPECTED_COUNT = 23

HEAD_OF_FAITH_RE = re.compile(r"head_of_faith|Head of Faith|Chefe da F", re.I)
PRIEST_GETTER_RE = re.compile(r"Priest[A-Za-z]*|GetFaith\.GetAdherent|Faith\.GetAdherent|GetAdherent", re.I)
ADHERENT_VAR_RE = re.compile(r"\$[a-z0-9_]*(?:adherent|pagan|faithful)[a-z0-9_]*\$", re.I)
CLERGY_TEXT_RE = re.compile(r"\b(?:clergy|clero|priest|priests|sacerdote|sacerdotes|patriarch|patriarca|bispo|bishop|imam|monk|monges|freiras)\b", re.I)
FAITHFUL_TEXT_RE = re.compile(r"\b(?:faithful|fi[eé]is|adeptos|praticantes|adherents?)\b", re.I)
MULTILINE_RE = re.compile(r"\\n|\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)
ACHIEVEMENT_RE = re.compile(r"ACHIEVEMENT|achievement|rd_character_blocked", re.I)
DENSE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Concept\(|Custom\(|SCOPE\.|ROOT\.|TARGET_CHARACTER|CHARACTER")
SPANISH_RESIDUE_RE = re.compile(r"Ã|Â|\b(?:de sus|consideran|cabeza|practicantes)\b", re.I)


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


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_consolidated_diagnostic":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    target = [row for row in rows if row.get("hold_family") == EXPECTED_FAMILY]
    if len(target) != EXPECTED_COUNT:
        raise SystemExit(f"target count guard failed: {len(target)} expected {EXPECTED_COUNT}")
    duplicates = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in target).items() if count > 1]
    if duplicates:
        raise SystemExit(f"duplicate segment_id guard failed: {duplicates[:10]}")
    return target


def classify(row: dict[str, Any]) -> tuple[str, str, list[str], str]:
    blob = " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "current_output_text", "english_text")
    )
    risk = str(row.get("risk_bucket") or "")
    tags: list[str] = []
    for label, pattern in (
        ("head_of_faith", HEAD_OF_FAITH_RE),
        ("priest_getter", PRIEST_GETTER_RE),
        ("adherent_variable", ADHERENT_VAR_RE),
        ("clergy_text", CLERGY_TEXT_RE),
        ("faithful_text", FAITHFUL_TEXT_RE),
        ("multiline_or_effect_list", MULTILINE_RE),
        ("achievement_surface", ACHIEVEMENT_RE),
        ("dense_token_surface", DENSE_TOKEN_RE),
        ("spanish_residue_or_mojibake", SPANISH_RESIDUE_RE),
    ):
        if pattern.search(blob):
            tags.append(label)

    if "head_of_faith" in tags and "multiline_or_effect_list" in tags:
        return (
            "head_of_faith_multiline_requirement",
            "hold_structural_or_parser_later",
            tags,
            "Head-of-faith requirement inside multiline/activity text needs structural context.",
        )
    if "priest_getter" in tags:
        return (
            "priest_adherent_runtime_getter",
            "needs_read_only_getter_role_policy",
            tags,
            "Runtime priest/adherent getter needs role classification before any human packet or candidate.",
        )
    if "adherent_variable" in tags:
        return (
            "adherent_variable_achievement_or_requirement",
            "needs_variable_domain_policy_review",
            tags,
            "Adherent variable in achievement/requirement surface is token-sensitive.",
        )
    if "multiline_or_effect_list" in tags and ("clergy_text" in tags or "faithful_text" in tags):
        return (
            "clergy_faithful_multiline_narrative",
            "hold_context_or_human_later",
            tags,
            "Narrative/multiline clergy-faithful text is not a parser-safe low-risk target.",
        )
    if "clergy_text" in tags or "faithful_text" in tags:
        if risk in {"low_plain_domain", "medium_dynamic_light"}:
            return (
                "plain_clergy_faithful_text",
                "small_human_review_possible",
                tags,
                "Plain clergy/faithful prose can be considered in a small human packet.",
            )
        return (
            "clergy_faithful_dense_context",
            "hold_context_or_parser_later",
            tags,
            "Clergy/faithful term appears in dense or risky context.",
        )
    return (
        "residual_clergy_adherent_context",
        "hold_or_architecture_later",
        tags,
        "Residual clergy/adherent context lacks a safe operational role.",
    )


def build_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (str(item.get("risk_bucket") or ""), str(item.get("source_key") or ""), int(item["segment_id"]))):
        subtype, action, tags, reason = classify(row)
        reviewed.append(
            {
                "record_type": "clergy_adherent_review",
                "source": SOURCE,
                "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "surface_bucket": row.get("surface_bucket"),
                "hold_family": row.get("hold_family"),
                "risk_bucket": row.get("risk_bucket"),
                "residual_class": row.get("residual_class"),
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
                "requires_output_apply_later": False,
                "requires_lifecycle_later": False,
            }
        )
    return reviewed


def write_reports(reviewed: list[dict[str, Any]]) -> tuple[Path, Path, Path, dict[str, Any]]:
    subtype_counts = Counter(str(row["operational_subtype"]) for row in reviewed)
    action_counts = Counter(str(row["recommended_action"]) for row in reviewed)
    risk_counts = Counter(str(row["risk_bucket"]) for row in reviewed)
    tag_counts = Counter(tag for row in reviewed for tag in row["review_tags"])
    parser_or_policy_count = sum(
        count
        for action, count in action_counts.items()
        if action in {"needs_read_only_getter_role_policy", "needs_variable_domain_policy_review"}
    )
    human_possible_count = int(action_counts.get("small_human_review_possible", 0))
    summary = {
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
        "tag_counts": dict(tag_counts),
        "parser_or_policy_count": parser_or_policy_count,
        "small_human_review_possible_count": human_possible_count,
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
            "Register a read-only clergy/adherent splitter only if the getter/variable subtypes are useful; "
            "otherwise keep most rows in hold and review the small plain prose subset manually later."
        ),
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_clergy_adherent_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine clergy_adherent_priest_terms review",
        "",
        f"segment_state_run_id: {EXPECTED_SEGMENT_STATE_RUN_ID}",
        f"review_count: {len(reviewed)}",
        "",
        "operational_subtype_counts:",
    ]
    lines.extend(f"- {count} | {key}" for key, count in subtype_counts.most_common())
    lines.extend(["", "recommended_action_counts:"])
    lines.extend(f"- {count} | {key}" for key, count in action_counts.most_common())
    lines.extend(["", "risk_bucket_counts:"])
    lines.extend(f"- {count} | {key}" for key, count in risk_counts.most_common())
    lines.extend(["", "representative_examples:"])
    for subtype in subtype_counts:
        lines.extend(["", f"## {subtype}"])
        for row in [item for item in reviewed if item["operational_subtype"] == subtype][:4]:
            output = str(row.get("current_output_text") or "").replace("\n", "\\n")
            lines.append(f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['source_key']}")
            lines.append(f"  action: {row['recommended_action']}")
            lines.append(f"  reason: {row['review_reason']}")
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
    summary = read_json(CONSOLIDATED_SUMMARY_PATH)
    rows = read_jsonl(CONSOLIDATED_JSONL_PATH)
    target = validate_inputs(summary, rows)
    reviewed = build_review_rows(target)
    txt_path, jsonl_path, summary_path, result = write_reports(reviewed)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"review_count={result['review_count']}")
    print(f"operational_subtype_counts={json.dumps(result['operational_subtype_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"recommended_action_counts={json.dumps(result['recommended_action_counts'], ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
