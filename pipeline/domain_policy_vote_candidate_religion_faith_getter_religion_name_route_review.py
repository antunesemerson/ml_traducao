from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_getter_religion_name_route_review_v1"
INPUT_PATH = Path("reports/20260630_083519_141707_domain_policy_vote_candidate_religion_faith_getter_role_policy_dry_run.jsonl")
TARGET_ROUTE = "route_faith_getter_religion_name"
EXPECTED_COUNT = 63

PLACEHOLDER_RE = re.compile(r"#D\b|PLACEHOLDER|TODO|DEBUG|LOC_PLACEHOLDER|placeholder", re.IGNORECASE)
CONVERSION_RE = re.compile(r"convert|conversion|converte|convers|convertid|faith_conversion|county conversion", re.IGNORECASE)
FAITH_REQUIREMENT_RE = re.compile(r"must be|must have|deve ser|deve estar|tem que|requires?|requirement|blocked|unlocked", re.IGNORECASE)
HOLY_WAR_RE = re.compile(r"holy war|great holy war|GHWName|crusade|fervor|guerra santa|cruzad", re.IGNORECASE)
HOLY_SITE_RE = re.compile(r"holy_site|holy site|holy_sites|local sagrado|sacred site|jerusalem|mecca|rome|coloner|cathedral", re.IGNORECASE)
RELIGION_FAMILY_RE = re.compile(r"ReligionByKey|ReligionFamily|religious_family|rf_", re.IGNORECASE)
SECRET_FAITH_RE = re.compile(r"secret.*faith|faith.*secret|crypto_religionist|verdadeira devo", re.IGNORECASE)
LEDGER_OR_UI_RE = re.compile(r"ledger|window|tooltip|sort|SORT_|OPINION_REASON|_tt$", re.IGNORECASE)
ARTICLE_PREP_RE = re.compile(r"\b(?:de|do|da|dos|das|em|no|na|nos|nas|para|pela|pelo|ao|a|o|os|as)\s+\[?(?:faith|religion)", re.IGNORECASE)


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in (
            "source_key",
            "relative_path",
            "english_text",
            "spanish_text",
            "current_output_text",
        )
    )


def classify_subtype(row: dict[str, Any]) -> tuple[str, list[str], str]:
    blob = text_blob(row)
    tags: list[str] = []
    source_key = str(row.get("source_key") or "")

    if PLACEHOLDER_RE.search(blob):
        return "placeholder_debug_hold", ["placeholder_or_debug_surface"], "hold; debug/placeholder localization is not a semantic getter route"
    if HOLY_SITE_RE.search(blob):
        tags.append("holy_site_surface")
        if CONVERSION_RE.search(blob):
            tags.append("conversion_surface")
        return "holy_site_effect_or_requirement", tags, "read-only review; holy-site faith wording may be rule/effect text"
    if HOLY_WAR_RE.search(blob):
        tags.append("holy_war_or_fervor_surface")
        return "holy_war_fervor_context", tags, "read-only review; holy war/fervor names need domain-specific grammar"
    if CONVERSION_RE.search(blob):
        tags.append("conversion_surface")
        if SECRET_FAITH_RE.search(blob):
            tags.append("secret_faith_surface")
            return "secret_faith_conversion_context", tags, "read-only review; secret-faith wording needs context"
        return "faith_conversion_text", tags, "read-only review; possible human packet later, no candidate"
    if FAITH_REQUIREMENT_RE.search(blob) or source_key.endswith("_tt"):
        tags.append("requirement_or_tooltip_surface")
        if RELIGION_FAMILY_RE.search(blob):
            tags.append("religion_family_surface")
            return "religion_family_requirement", tags, "read-only review; check article/adjective behavior before policy"
        return "faith_requirement_or_condition", tags, "read-only review; condition/requirement text, no candidate"
    if RELIGION_FAMILY_RE.search(blob):
        tags.append("religion_family_surface")
        return "religion_family_reference", tags, "read-only review; route may need split from ordinary faith references"
    if ARTICLE_PREP_RE.search(str(row.get("current_output_text") or "")):
        tags.append("article_preposition_surface")
        return "article_preposition_religion_name_context", tags, "hold; article/preposition contraction needs parser"
    if LEDGER_OR_UI_RE.search(blob):
        tags.append("ui_or_tooltip_surface")
        return "ui_tooltip_religion_reference", tags, "read-only review; UI wording may be safe but should not auto-apply"
    return "generic_religion_name_reference", ["generic_religion_name_surface"], "read-only review; collect examples before policy"


def review_action(subtype: str) -> str:
    if subtype in {"placeholder_debug_hold", "article_preposition_religion_name_context"}:
        return "hold_context"
    if subtype in {"holy_war_fervor_context", "holy_site_effect_or_requirement", "religion_family_requirement"}:
        return "architecture_or_subpolicy_later"
    return "human_packet_or_policy_review_later"


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def representative_examples(rows: list[dict[str, Any]], per_subtype: int = 4) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["operational_subtype"])].append(row)
    examples: dict[str, list[dict[str, Any]]] = {}
    for subtype, subtype_rows in grouped.items():
        ordered = sorted(subtype_rows, key=lambda item: (str(item.get("risk_bucket") or ""), int(item["segment_id"])))
        examples[subtype] = [
            {
                "segment_id": row["segment_id"],
                "risk_bucket": row.get("risk_bucket"),
                "source_key": row.get("source_key"),
                "review_action": row.get("review_action"),
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
            }
            for row in ordered[:per_subtype]
        ]
    return examples


def write_txt(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "domain_policy_vote_candidate religion faith getter religion_name route review",
        "",
        f"input_path: {summary['input_path']}",
        f"target_route: {summary['target_route']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "subtype_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["subtype_counts"])
    lines.extend(["", "review_action_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["review_action_counts"])
    lines.extend(["", "risk_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["risk_subtype_counts"])
    lines.extend(["", "representative_examples:"])
    for subtype, examples in summary["representative_examples_by_subtype"].items():
        lines.extend(["", f"## {subtype}"])
        for row in examples:
            lines.extend(
                [
                    f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['review_action']} | {row['source_key']}",
                    f"  output: {row['current_output_text']}",
                ]
            )
    lines.extend(
        [
            "",
            "recommendation:",
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
    input_rows = [
        row
        for row in read_jsonl(INPUT_PATH)
        if row.get("route") == TARGET_ROUTE and row.get("route_status") == "split_only_no_candidate"
    ]
    if len(input_rows) != EXPECTED_COUNT:
        raise SystemExit(f"route count guard failed: {len(input_rows)} expected {EXPECTED_COUNT}")

    rows: list[dict[str, Any]] = []
    for row in input_rows:
        subtype, tags, subtype_recommendation = classify_subtype(row)
        rows.append(
            {
                **row,
                "review_source": SOURCE,
                "operational_subtype": subtype,
                "subtype_tags": tags,
                "subtype_recommendation": subtype_recommendation,
                "review_action": review_action(subtype),
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )

    rows.sort(key=lambda row: (str(row["review_action"]), str(row["operational_subtype"]), str(row.get("risk_bucket") or ""), int(row["segment_id"])))
    segment_ids = [int(row["segment_id"]) for row in rows]
    if len(segment_ids) != len(set(segment_ids)):
        raise SystemExit("duplicate segment_id guard failed")

    subtype_counts = Counter(str(row["operational_subtype"]) for row in rows)
    action_counts = Counter(str(row["review_action"]) for row in rows)
    risk_subtype_counts = Counter(f"{row.get('risk_bucket')} | {row['operational_subtype']}" for row in rows)
    source_subtype_counts = Counter(f"{row.get('relative_path')} | {row['operational_subtype']}" for row in rows)

    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_route_review",
        "input_path": str(INPUT_PATH),
        "target_route": TARGET_ROUTE,
        "review_count": len(rows),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "subtype_counts": top_counter(subtype_counts),
        "review_action_counts": top_counter(action_counts),
        "risk_subtype_counts": top_counter(risk_subtype_counts),
        "source_subtype_counts": top_counter(source_subtype_counts),
        "representative_examples_by_subtype": representative_examples(rows),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "single_operational_recommendation": (
            "Do not generate candidates. Use this review to create either a tiny human packet for faith_conversion_text "
            "and faith_requirement_or_condition, or first register a read-only religion_name route splitter that holds "
            "placeholder/debug, article/preposition, holy war/fervor, and holy-site effect subtypes."
        ),
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

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_getter_religion_name_route_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
