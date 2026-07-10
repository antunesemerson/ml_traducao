from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_possessive_relation_review_v1"
CONSOLIDATED_JSONL_PATH = Path(
    "reports/20260630_111111_866545_domain_policy_vote_candidate_religion_faith_post_second_policy_consolidated_diagnostic.jsonl"
)
CONSOLIDATED_SUMMARY_PATH = Path(
    "reports/20260630_111111_866545_domain_policy_vote_candidate_religion_faith_post_second_policy_consolidated_diagnostic_summary.json"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_FAMILY = "faith_possessive_or_relation"
EXPECTED_COUNT = 22

SECRET_FAITH_RE = re.compile(r"SecretFaith|secret_.*faith|crypto|ocult|hidden faith|cren[çc]a|cultuar", re.I)
FAMILY_WAR_RE = re.compile(r"family|famil|war\.|war_|ex[ée]rcitos|guerra|generous_family", re.I)
PRIEST_RELATION_RE = re.compile(r"priest|wise_priest|clergy|sacerdote|TitleAsName|GetTitle", re.I)
POSSESSIVE_GETTER_RE = re.compile(r"GetHerHis|GetSheHe|GetHerHim|GetHisHer|GetName|GetFirstName|GetTitledFirstName", re.I)
FAITH_GETTER_RE = re.compile(r"GetFaith|Faith\.|faith_scope|root_crypto_faith|SecretFaith|GetSecretFaith", re.I)
RELATION_RE = re.compile(r"relation|spouse|friend|rival|mentor|disciple|vassal|liege|holder|family|priest", re.I)
MULTILINE_RE = re.compile(r"\\n|\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)
DENSE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Concept\(|Custom\(|SCOPE\.|ROOT\.|TARGET_CHARACTER|CHARACTER")
SPANISH_RESIDUE_RE = re.compile(r"Ã|Â|\b(?:carta|ocultar|creencia|guerra|familias|misterio)\b", re.I)


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
    risk = str(row.get("risk_bucket") or "")
    tags: list[str] = []
    for label, pattern in (
        ("secret_or_crypto_faith", SECRET_FAITH_RE),
        ("family_or_war_context", FAMILY_WAR_RE),
        ("priest_relation_context", PRIEST_RELATION_RE),
        ("possessive_or_person_getter", POSSESSIVE_GETTER_RE),
        ("faith_getter", FAITH_GETTER_RE),
        ("relation_surface", RELATION_RE),
        ("multiline_or_effect_list", MULTILINE_RE),
        ("dense_token_surface", DENSE_TOKEN_RE),
        ("spanish_residue_or_mojibake", SPANISH_RESIDUE_RE),
    ):
        if pattern.search(blob):
            tags.append(label)

    if "secret_or_crypto_faith" in tags and "faith_getter" in tags:
        return (
            "secret_or_crypto_faith_relation",
            "hold_context_or_parser_later",
            tags,
            "Secret/crypto faith relation needs semantic context and should not be candidate-generated.",
        )
    if "family_or_war_context" in tags:
        return (
            "family_war_relation_narrative",
            "hold_context_or_human_later",
            tags,
            "Family/war relation narrative is context-heavy.",
        )
    if "priest_relation_context" in tags and "multiline_or_effect_list" in tags:
        return (
            "priest_relation_multiline_narrative",
            "hold_context_or_human_later",
            tags,
            "Priest/relation narrative appears in multiline prose.",
        )
    if "faith_getter" in tags and "possessive_or_person_getter" in tags:
        return (
            "faith_getter_person_possessive",
            "needs_perspective_relation_parser_review",
            tags,
            "Faith getter combined with person/possessive getter needs perspective-aware parser review.",
        )
    if "faith_getter" in tags:
        return (
            "faith_getter_relation_context",
            "needs_faith_relation_parser_review",
            tags,
            "Faith getter relation context needs parser role classification.",
        )
    if "multiline_or_effect_list" in tags:
        return (
            "multiline_relation_context",
            "hold_context_or_human_later",
            tags,
            "Multiline relation text is not a low-risk automatic target.",
        )
    if risk in {"low_plain_domain", "medium_dynamic_light"}:
        return (
            "light_relation_context",
            "small_human_review_possible",
            tags,
            "Light relation context may be reviewed manually in a tiny packet.",
        )
    return (
        "residual_possessive_relation_context",
        "hold_or_architecture_later",
        tags,
        "Residual possessive/relation context lacks safe automatic role.",
    )


def build_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (str(item.get("risk_bucket") or ""), str(item.get("source_key") or ""), int(item["segment_id"]))):
        subtype, action, tags, reason = classify(row)
        reviewed.append(
            {
                "record_type": "faith_possessive_relation_review",
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
        if action in {"needs_perspective_relation_parser_review", "needs_faith_relation_parser_review"}
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
            "Keep faith_possessive_or_relation as read-only context split; register shadow splitter only if "
            "perspective/faith-relation parser routing is useful, otherwise hold this family."
        ),
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_possessive_relation_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine faith_possessive_or_relation review",
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
