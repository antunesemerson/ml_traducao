from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_getter_role_review_v1"
INPUT_PATH = Path("reports/20260630_082029_893563_domain_policy_vote_candidate_religion_faith_doctrine_architecture_diagnostic.jsonl")
TARGET_FAMILY = "faith/doctrine getters"
EXPECTED_COUNT = 196

FAITH_ADHERENT_RE = re.compile(r"Adherent|faithful|adherent|fiel|fi[eé]is", re.IGNORECASE)
FAITH_ADJECTIVE_RE = re.compile(r"GetAdjective|AdjectiveNoTooltip|faith.*adjective|religion.*adjective", re.IGNORECASE)
FAITH_CLERGY_RE = re.compile(
    r"Priest|Clergy|ClergyName|HeadOfFaith|head_of_faith|court_chaplain|realm_priest|"
    r"ReligiousHead|Bishop|Chaplain|priest|clergy|clero|sacerd",
    re.IGNORECASE,
)
FAITH_DIVINE_RE = re.compile(
    r"PantheonTerm|HighGod|HighGodName|GoodGod|EvilGod|DivineRealm|HouseOfWorship|"
    r"ReligiousText|DevilName|divine|divino|pante",
    re.IGNORECASE,
)
FAITH_NAME_RE = re.compile(
    r"Faith\.GetName|Faith\.GetNameNoTooltip|GetFaith\.GetName|faith\.GetName|"
    r"GetFaithName|faith_name|GetFaith\]",
    re.IGNORECASE,
)
FAITH_POSSESSIVE_RE = re.compile(
    r"Possessive|GetNamePossessive|HighGodNamePossessive|GetHerHis|GetHisHer|"
    r"GetTheir|possessiv|posse",
    re.IGNORECASE,
)
RELIGION_FAMILY_RE = re.compile(r"ReligionFamily|GetReligionFamily|religion_family", re.IGNORECASE)
RELIGION_NAME_RE = re.compile(
    r"Religion\.GetName|Religion\.GetNameNoTooltip|GetReligion\.GetName|religion\.GetName|"
    r"Religion.GetAdjective|religion_name|religi",
    re.IGNORECASE,
)
TENET_DOCTRINE_RE = re.compile(
    r"tenet|doctrine|GetDoctrine|doctrine_parameter|religious_doctrine|dogma|doutrina",
    re.IGNORECASE,
)
ARTICLE_PREP_RE = re.compile(
    r"\b(?:de|do|da|dos|das|em|no|na|nos|nas|para|pela|pelo|ao|a|o|os|as)\s+"
    r"(?:\[?(?:faith|religion|doctrine|tenet|head_of_faith|clergy)|f[eé]|religi|doutrina|clero)",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in (
            "relative_path",
            "source_key",
            "english_text",
            "spanish_text",
            "current_output_text",
        )
    )


def classify_role(row: dict[str, Any]) -> tuple[str, list[str], str]:
    text = text_blob(row)
    current = str(row.get("current_output_text") or "")
    tags: list[str] = []

    if row.get("risk_bucket") == "high_structural_token_density":
        tags.append("dense_structural_getter_cluster")
    if row.get("risk_bucket") == "high_multiline_effect_list":
        tags.append("multiline_getter_context")
    if row.get("risk_bucket") == "high_spanish_residue_context":
        tags.append("spanish_residue_context")
    if ARTICLE_PREP_RE.search(current):
        tags.append("article_preposition_context")
    if FAITH_POSSESSIVE_RE.search(text):
        tags.append("possessive_surface")
    if FAITH_DIVINE_RE.search(text):
        tags.append("faith_divine_runtime_surface")
    if FAITH_CLERGY_RE.search(text):
        tags.append("faith_clergy_surface")
    if FAITH_ADHERENT_RE.search(text):
        tags.append("faith_adherent_surface")
    if TENET_DOCTRINE_RE.search(text):
        tags.append("doctrine_or_tenet_surface")
    if RELIGION_FAMILY_RE.search(text):
        tags.append("religion_family_surface")
    if RELIGION_NAME_RE.search(text):
        tags.append("religion_name_surface")
    if FAITH_ADJECTIVE_RE.search(text):
        tags.append("faith_adjective_surface")
    if FAITH_NAME_RE.search(text):
        tags.append("faith_name_surface")

    role_priority = [
        ("faith_priest_or_clergy_getter", "faith_clergy_surface"),
        ("faith_adherent_getter", "faith_adherent_surface"),
        ("faith_high_god_or_divine_name", "faith_divine_runtime_surface"),
        ("faith_possessive", "possessive_surface"),
        ("doctrine_or_tenet_context", "doctrine_or_tenet_surface"),
        ("religion_family_name", "religion_family_surface"),
        ("religion_name", "religion_name_surface"),
        ("faith_adjective", "faith_adjective_surface"),
        ("faith_name", "faith_name_surface"),
    ]
    for role, tag in role_priority:
        if tag in tags:
            return role, tags, recommendation_for_role(role, row, tags)

    if "article_preposition_context" in tags:
        return "article_preposition_context", tags, recommendation_for_role("article_preposition_context", row, tags)
    if "multiline_getter_context" in tags:
        return "multiline_getter_context", tags, "hold_for_multiline_parser"
    if "dense_structural_getter_cluster" in tags:
        return "dense_structural_getter_cluster", tags, "hold_for_dense_token_parser"
    return "unknown_hold", tags or ["no_role_signal"], "hold_unknown_needs_context"


def recommendation_for_role(role: str, row: dict[str, Any], tags: list[str]) -> str:
    high_risk = row.get("risk_bucket") in {"high_structural_token_density", "high_multiline_effect_list", "high_spanish_residue_context"}
    if high_risk:
        return "hold_for_parser_or_architecture"
    if role in {"faith_name", "religion_name", "religion_family_name", "faith_adjective"}:
        return "read_only_splitter_candidate"
    if role in {"faith_priest_or_clergy_getter", "faith_adherent_getter"}:
        return "route_to_clergy_adherent_review"
    if role == "article_preposition_context":
        return "route_to_article_preposition_review"
    return "hold_for_context_or_runtime_semantics"


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_examples(rows: list[dict[str, Any]], key: str, limit: int = 4) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "")].append(row)
    examples: dict[str, list[dict[str, Any]]] = {}
    for group_key, group_rows in grouped.items():
        ordered = sorted(
            group_rows,
            key=lambda item: (
                item.get("risk_bucket") not in {"high_structural_token_density", "high_multiline_effect_list"},
                -int(item.get("token_count") or 0),
                int(item["segment_id"]),
            ),
        )
        examples[group_key] = [
            {
                "segment_id": row["segment_id"],
                "risk_bucket": row.get("risk_bucket"),
                "role": row.get("getter_role"),
                "recommendation": row.get("role_recommendation"),
                "source_key": row.get("source_key"),
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
            }
            for row in ordered[:limit]
        ]
    return examples


def operational_recommendation(rows: list[dict[str, Any]], role_counts: Counter[str], risk_counts: Counter[str]) -> str:
    safe_splitter = sum(
        1
        for row in rows
        if row["role_recommendation"] == "read_only_splitter_candidate"
    )
    high_risk = sum(
        1
        for row in rows
        if row.get("risk_bucket") in {"high_structural_token_density", "high_multiline_effect_list", "high_spanish_residue_context"}
    )
    if safe_splitter >= 20:
        return (
            "Register a read-only faith/religion getter role splitter in shadow mode, limited to routing "
            "faith_name, religion_name, religion_family_name, and faith_adjective; keep dense, multiline, "
            "divine runtime, possessive, clergy/adherent, and doctrine contexts in hold or their existing reviews."
        )
    return (
        "Do not register a new splitter yet; the safe low-risk getter roles are too sparse. Keep this as diagnostics "
        "and move to a narrower human or parser packet."
    )


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine faith/doctrine getter role review",
        "",
        f"input_path: {summary['input_path']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "getter_role_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["getter_role_counts"])
    lines.extend(["", "risk_bucket_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["risk_bucket_counts"])
    lines.extend(["", "role_recommendation_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["role_recommendation_counts"])
    lines.extend(["", "role_x_risk_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["role_x_risk_counts"])
    lines.extend(["", "representative_examples_by_role:"])
    for role, examples in summary["representative_examples_by_role"].items():
        lines.extend(["", f"## {role}"])
        for row in examples:
            lines.extend(
                [
                    f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['recommendation']} | {row['source_key']}",
                    f"  output: {row['current_output_text']}",
                ]
            )
    lines.extend(
        [
            "",
            "operational_recommendation:",
            f"- {summary['single_operational_recommendation']}",
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    input_rows = read_jsonl(INPUT_PATH)
    rows: list[dict[str, Any]] = []
    for row in input_rows:
        if row.get("architecture_family") != TARGET_FAMILY:
            continue
        role, tags, recommendation = classify_role(row)
        rows.append(
            {
                **row,
                "review_source": SOURCE,
                "getter_role": role,
                "getter_role_tags": tags,
                "role_recommendation": recommendation,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )

    rows.sort(key=lambda row: (str(row["getter_role"]), str(row.get("risk_bucket") or ""), int(row["segment_id"])))
    if len(rows) != EXPECTED_COUNT:
        raise SystemExit(f"review count guard failed: {len(rows)} expected {EXPECTED_COUNT}")

    role_counts = Counter(str(row["getter_role"]) for row in rows)
    risk_counts = Counter(str(row.get("risk_bucket") or "") for row in rows)
    recommendation_counts = Counter(str(row["role_recommendation"]) for row in rows)
    role_x_risk_counts = Counter(f"{row['getter_role']} | {row.get('risk_bucket')}" for row in rows)
    rec_x_role_counts = Counter(f"{row['role_recommendation']} | {row['getter_role']}" for row in rows)

    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_getter_role_review",
        "input_path": str(INPUT_PATH),
        "lane": "domain_policy_vote_candidate",
        "surface_bucket": "religion_faith_doctrine",
        "architecture_family": TARGET_FAMILY,
        "expected_count": EXPECTED_COUNT,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "getter_role_counts": top_counter(role_counts),
        "risk_bucket_counts": top_counter(risk_counts),
        "role_recommendation_counts": top_counter(recommendation_counts),
        "role_x_risk_counts": top_counter(role_x_risk_counts),
        "recommendation_x_role_counts": top_counter(rec_x_role_counts),
        "representative_examples_by_role": representative_examples(rows, "getter_role"),
        "single_operational_recommendation": operational_recommendation(rows, role_counts, risk_counts),
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }

    prefix = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_getter_role_review"
    jsonl_path = prefix.with_suffix(".jsonl")
    summary_path = Path(f"{prefix}_summary.json")
    txt_path = prefix.with_suffix(".txt")
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
        "txt": str(txt_path),
    }

    write_jsonl(jsonl_path, rows)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
