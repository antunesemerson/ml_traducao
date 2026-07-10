from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_faith_route_review"
INPUT_PATH = Path("reports/20260629_145447_528523_medium_dynamic_light_getter_role_policy_dry_run.jsonl")
TARGET_ROUTE = "route_getter_faith_name"
EXPECTED_COUNT = 19

DEITY_RE = re.compile(r"(?:HighGodName|PantheonTerm)", re.IGNORECASE)
CLERGY_RE = re.compile(r"(?:Priest|Bishop|Cleric|Clergy)", re.IGNORECASE)
ADHERENT_RE = re.compile(r"(?:AdherentName|GetAdjective)", re.IGNORECASE)
FAITH_NAME_RE = re.compile(r"(?:Faith\.GetName|faith\.GetName|Faith\.GetNameNoTooltip|getname\|l)", re.IGNORECASE)
HOLY_ORDER_OR_WAR_RE = re.compile(r"(?:holy order|Holy Order|ordem sagrada|orden militar|holy war|guerra santa|GREAT_HOLY_WAR)", re.IGNORECASE)
PLACE_OR_PERSON_RE = re.compile(r"(?:GetShortUIName|GetFirstName|GetNameNoTier|temple_location|progenitor_holy_blood|unity_target|recipient)", re.IGNORECASE)
ARTICLE_PREP_RE = re.compile(r"\b(?:of|in|from|de|do|da|dos|das|del|en|no|na|nos|nas|ao|aos|à|às)\b", re.IGNORECASE)
VERB_AGREEMENT_RE = re.compile(r"\b(?:demand|intended|quiso|reclame|reclamem|exijam|quis|pretendia)\b", re.IGNORECASE)


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
            "english_text",
            "spanish_text",
            "current_output_text",
            "output_token",
            "source_key",
            "relative_path",
        )
    )


def classify_subtype(row: dict[str, Any]) -> tuple[str, list[str], str]:
    blob = text_blob(row)
    token = str(row.get("output_token") or "")
    tags: list[str] = []

    if PLACE_OR_PERSON_RE.search(blob) and not FAITH_NAME_RE.search(token):
        tags.append("non_faith_entity_token_in_religious_context")
        return (
            "non_faith_entity_context",
            tags,
            "hold_context; token is person/place/title/war target inside religious text, not a faith-name rule",
        )

    if DEITY_RE.search(token):
        tags.append("deity_or_pantheon_token")
        if VERB_AGREEMENT_RE.search(blob):
            tags.append("verb_agreement_surface")
            return (
                "deity_or_pantheon_verb_agreement",
                tags,
                "needs human/parser context; deity/pantheon terms can shift singular/plural agreement",
            )
        return (
            "deity_name_preserve_as_proper_name",
            tags,
            "possible read-only splitter child, but no candidate generation without deity agreement policy",
        )

    if CLERGY_RE.search(token):
        tags.append("clergy_plural_token")
        return (
            "clergy_plural_role_agreement",
            tags,
            "hold_context; clergy plural needs article/number/gender agreement in Portuguese",
        )

    if ADHERENT_RE.search(token):
        tags.append("adherent_or_adjective_token")
        return (
            "adherent_or_faith_adjective_context",
            tags,
            "hold_context; adherent/adjective token needs noun role and gender/number context",
        )

    if FAITH_NAME_RE.search(token):
        tags.append("faith_name_token")
        if ARTICLE_PREP_RE.search(blob):
            tags.append("article_or_preposition_surface")
            return (
                "faith_name_article_preposition_context",
                tags,
                "hold_context; faith name may need article/preposition contraction",
            )
        return (
            "faith_name_preserve_as_proper_name",
            tags,
            "possible read-only splitter child; preserve faith-name token without candidate generation",
        )

    if HOLY_ORDER_OR_WAR_RE.search(blob):
        tags.append("holy_order_or_war_surface")
        return (
            "religious_institution_or_war_context",
            tags,
            "hold_context; religious surface is not enough to infer faith-name behavior",
        )

    return (
        "hold_context_uncertain",
        ["no_clear_faith_subtype_signal"],
        "hold_context; requires manual/context review",
    )


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def representative_samples(rows: list[dict[str, Any]], per_subtype: int = 3) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["operational_subtype"])].append(row)
    sample: list[dict[str, Any]] = []
    for subtype in sorted(grouped):
        sample.extend(grouped[subtype][:per_subtype])
    return sample


def write_txt(path: Path, summary: dict[str, Any], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light faith route review",
        "",
        f"input_path: {summary['input_path']}",
        f"policy_parent: {summary['policy_parent']}",
        f"target_route: {summary['target_route']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "subtype_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["subtype_counts"])
    lines.extend(["", "source_family_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["source_family_subtype_counts"])
    lines.extend(["", "surface_subtype_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_subtype_counts"])
    lines.extend(["", "representative_examples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['operational_subtype']} | {row.get('source_family')}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- subtype_tags: {', '.join(row.get('subtype_tags') or [])}",
                f"- subtype_recommendation: {row.get('subtype_recommendation')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            "recommendation:",
            summary["recommendation"],
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
    input_rows = [row for row in read_jsonl(INPUT_PATH) if row.get("route") == TARGET_ROUTE]
    if len(input_rows) != EXPECTED_COUNT:
        raise SystemExit(f"route count guard failed: {len(input_rows)} expected {EXPECTED_COUNT}")

    rows: list[dict[str, Any]] = []
    for row in input_rows:
        subtype, tags, subtype_recommendation = classify_subtype(row)
        rows.append(
            {
                **row,
                "operational_subtype": subtype,
                "subtype_tags": tags,
                "subtype_recommendation": subtype_recommendation,
                "review_source": SOURCE,
            }
        )
    rows.sort(key=lambda row: (str(row["operational_subtype"]), str(row.get("source_family") or ""), int(row["segment_id"])))

    subtype_counts = Counter(str(row["operational_subtype"]) for row in rows)
    source_subtype_counts = Counter(f"{row.get('source_family')} | {row.get('operational_subtype')}" for row in rows)
    surface_subtype_counts = Counter(f"{row.get('surface_bucket')} | {row.get('operational_subtype')}" for row in rows)
    child_splitter_candidate_count = sum(
        subtype_counts[subtype]
        for subtype in ("deity_name_preserve_as_proper_name", "faith_name_preserve_as_proper_name")
    )
    hold_count = len(rows) - child_splitter_candidate_count
    recommendation = (
        "Do not register a faith-name child splitter yet. This route is mixed: most rows need article/preposition, "
        "verb agreement, clergy/adherent role, or non-faith entity context. The safest next step is a small human "
        "packet for the hold_context rows, or a parser-policy design limited to deity/pantheon agreement and "
        "faith-name article/preposition handling."
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_route_review",
        "input_path": str(INPUT_PATH),
        "policy_parent": "medium_dynamic_light_getter_role_policy",
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "target_route": TARGET_ROUTE,
        "expected_count": EXPECTED_COUNT,
        "review_count": len(rows),
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "subtype_counts": top_counter(subtype_counts),
        "source_family_subtype_counts": top_counter(source_subtype_counts),
        "surface_subtype_counts": top_counter(surface_subtype_counts),
        "child_splitter_candidate_count": child_splitter_candidate_count,
        "hold_or_context_count": hold_count,
        "representative_sample_count": len(representative_samples(rows)),
        "recommendation": recommendation,
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
    sample = representative_samples(rows)
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
