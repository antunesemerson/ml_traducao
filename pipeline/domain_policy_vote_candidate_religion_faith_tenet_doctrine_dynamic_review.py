from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_tenet_doctrine_dynamic_review_v1"
CONSOLIDATED_JSONL_PATH = Path(
    "reports/20260630_102017_125063_domain_policy_vote_candidate_religion_faith_post_policy_consolidated_diagnostic.jsonl"
)
CONSOLIDATED_SUMMARY_PATH = Path(
    "reports/20260630_102017_125063_domain_policy_vote_candidate_religion_faith_post_policy_consolidated_diagnostic_summary.json"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_FAMILY = "tenet_doctrine_dynamic_tokens"
EXPECTED_COUNT = 51

DOCTRINE_PARAMETER_RE = re.compile(r"doctrine_parameter|TOKEN_PARAMETER|GetHostilityLevelName|hostility_override|special_doctrine", re.I)
TENET_DESC_RE = re.compile(r"^tenet_.*_desc$|_GLOSS$", re.I)
TENET_EFFECT_RE = re.compile(r"tenet_.*(?:effect|modifier|cost|active|inactive)|reduced_|increased_|bonus|penalty", re.I)
GENDER_RULE_RE = re.compile(
    r"dominant_ruling_gender|GetWomenMen|\b(?:women|men|male|female|gender|adultery|homosexuality)\b",
    re.I,
)
FAITH_CUSTOM_RE = re.compile(r"FAITH\.Custom|Custom\(|ROOT\.Faith|Faith\.|GetFaith|FAITH\.GetName", re.I)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|EmptyScope|SCOPE\.|TOKEN_PARAMETER|\|=|#P|#N|[0-9]+\s*%", re.I)
MULTILINE_RE = re.compile(r"\\n|\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)
SPANISH_RESIDUE_RE = re.compile(r"\b(?:debe|permite|contra|otras|considere|sabedora|prazers|exluir|velocidad)\b|Ã", re.I)
DENSE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Concept\(|Get[A-Za-z0-9_]+\(|Custom\(")


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
    if summary.get("next_recommended_family") != EXPECTED_FAMILY:
        raise SystemExit("next recommended family guard failed")
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
    key = str(row.get("source_key") or "")
    risk = str(row.get("risk_bucket") or "")
    tags: list[str] = []
    for label, pattern in (
        ("doctrine_parameter", DOCTRINE_PARAMETER_RE),
        ("tenet_effect_or_modifier", TENET_EFFECT_RE),
        ("gender_rule", GENDER_RULE_RE),
        ("faith_custom_or_getter", FAITH_CUSTOM_RE),
        ("script_value_or_token_parameter", SCRIPT_VALUE_RE),
        ("multiline_or_effect_list", MULTILINE_RE),
        ("spanish_residue_or_mojibake", SPANISH_RESIDUE_RE),
        ("dense_token_surface", DENSE_TOKEN_RE),
    ):
        if pattern.search(blob):
            tags.append(label)
    if TENET_DESC_RE.search(key):
        tags.append("tenet_description")

    if "doctrine_parameter" in tags and ("script_value_or_token_parameter" in tags or "dense_token_surface" in tags):
        return (
            "doctrine_parameter_token_policy",
            "needs_read_only_parser_policy",
            tags,
            "Doctrine parameter with dynamic token/hostility parameter; parser/splitter needed before any candidate.",
        )
    if "gender_rule" in tags:
        return (
            "doctrine_gender_rule_requirement",
            "needs_gender_rule_parser_or_human_review",
            tags,
            "Doctrine gender/ruling rule surface has grammatical role and target/person risk.",
        )
    if "tenet_description" in tags and risk in {"high_spanish_residue_context", "low_plain_domain"}:
        return (
            "tenet_plain_description_residue",
            "small_human_review_possible",
            tags,
            "Plain tenet description with language/residue risk; better as tiny human packet, not auto policy.",
        )
    if "tenet_description" in tags and "multiline_or_effect_list" in tags:
        return (
            "tenet_multiline_description",
            "hold_structural_or_human_later",
            tags,
            "Long/multiline tenet description is context-heavy.",
        )
    if "tenet_effect_or_modifier" in tags or "script_value_or_token_parameter" in tags:
        return (
            "tenet_effect_modifier_token",
            "needs_effect_or_script_value_policy_review",
            tags,
            "Tenet effect/modifier with numeric/script/token surface.",
        )
    if "faith_custom_or_getter" in tags:
        return (
            "faith_custom_doctrine_requirement",
            "needs_getter_custom_parser_review",
            tags,
            "Faith custom/getter around doctrine requirement needs parser role classification.",
        )
    if risk == "medium_dynamic_light":
        return (
            "light_doctrine_tenet_context",
            "small_human_review_possible",
            tags,
            "Light token surface may be human-reviewed in a small packet.",
        )
    return (
        "residual_doctrine_tenet_context",
        "hold_or_architecture_later",
        tags,
        "Residual tenet/doctrine context not safe for automation.",
    )


def build_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (str(item.get("risk_bucket") or ""), str(item.get("source_key") or ""), int(item["segment_id"]))):
        subtype, action, tags, reason = classify(row)
        reviewed.append(
            {
                "record_type": "tenet_doctrine_dynamic_review",
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
    architecture_count = sum(
        count
        for action, count in action_counts.items()
        if action
        in {
            "needs_read_only_parser_policy",
            "needs_gender_rule_parser_or_human_review",
            "needs_effect_or_script_value_policy_review",
            "needs_getter_custom_parser_review",
        }
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
        "architecture_or_parser_count": architecture_count,
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
            "Register a read-only tenet/doctrine dynamic splitter if architecture accepts the subtypes; "
            "only the plain tenet-description residue should become a small human packet later."
        ),
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_tenet_doctrine_dynamic_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine tenet_doctrine_dynamic_tokens review",
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
