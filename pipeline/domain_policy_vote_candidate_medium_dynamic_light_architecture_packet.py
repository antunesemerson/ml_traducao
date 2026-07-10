from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_architecture_packet"
DIAGNOSTIC_PATH = Path("reports/20260629_132903_138034_domain_policy_vote_candidate_medium_dynamic_light_diagnostic.jsonl")
TARGET_CLASSES = {"precisa_arquitetura", "getter_perspective_omitted"}

SELECT_RE = re.compile(r"Select(?:Localization|_CString)|Select_CString", re.IGNORECASE)
PERSPECTIVE_RE = re.compile(
    r"Get(?:HerHim|SheHe|WomanMan|GirlBoy|HerselfHimself|HerHis|HerHisMy|HerHisTheir)\b",
    re.IGNORECASE,
)
ROOT_DOMAIN_RE = re.compile(r"\b(?:ROOT|Faith|Culture|Title|PrimaryTitle|House|Dynasty)\.Get|\.Get(?:Faith|Culture|Title|PrimaryTitle)", re.IGNORECASE)
NAMED_SCOPE_RE = re.compile(r"\b(?:scope|SCOPE|TARGET|CHARACTER|ACTOR|RECIPIENT|FROM|THIS)[A-Za-z0-9_]*\.Get|\[[A-Za-z0-9_]+\.", re.IGNORECASE)
LANDED_TITLE_RE = re.compile(r"(?:LandedTitle|PrimaryTitle|HasLandedTitles|realm|title|liege|vassal|duke|king|emperor)", re.IGNORECASE)
DYNAMIC_TOKEN_RE = re.compile(r"\$[^$]+\$|\[[^\]]+\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def classify_architecture_family(row: dict[str, Any]) -> tuple[str, list[str]]:
    blob = "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "output_token", "source_key", "relative_path")
    )
    tags: list[str] = []
    if SELECT_RE.search(blob):
        tags.append("select_localization_or_select_cstring")
    if PERSPECTIVE_RE.search(blob) or row.get("operational_class") == "getter_perspective_omitted":
        tags.append("getter_perspective_omitted")
    if ROOT_DOMAIN_RE.search(blob):
        tags.append("root_faith_culture_title_getters")
    if NAMED_SCOPE_RE.search(blob):
        tags.append("named_scope_getters")
    if LANDED_TITLE_RE.search(blob):
        tags.append("landed_title_realm_governance_getters")

    token = str(row.get("output_token") or "")
    if DYNAMIC_TOKEN_RE.search(token) and not tags:
        tags.append("dynamic_token_requires_grammatical_role")
    if DYNAMIC_TOKEN_RE.search(token):
        tags.append("has_dynamic_token")

    priority = [
        "select_localization_or_select_cstring",
        "getter_perspective_omitted",
        "root_faith_culture_title_getters",
        "named_scope_getters",
        "landed_title_realm_governance_getters",
        "dynamic_token_requires_grammatical_role",
    ]
    for family in priority:
        if family in tags:
            return family, tags
    return "dynamic_token_requires_grammatical_role", tags or ["unclassified_architecture_surface"]


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_samples(rows: list[dict[str, Any]], per_family: int = 8) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["architecture_family"])].append(row)
    sample: list[dict[str, Any]] = []
    for family in sorted(grouped):
        sample.extend(grouped[family][:per_family])
    return sample


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]], sample: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light architecture packet",
        "",
        f"source_diagnostic: {summary['source_diagnostic']}",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"packet_count: {summary['packet_count']}",
        "",
        "class_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["operational_class_counts"])
    lines.extend(["", "architecture_family_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["architecture_family_counts"])
    lines.extend(["", "surface_family_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_family_counts"])
    lines.extend(["", "representative_samples:"])
    for row in sample:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['architecture_family']} | {row['surface_bucket']}",
                f"- operational_class: {row.get('operational_class')}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- architecture_tags: {', '.join(row.get('architecture_tags') or [])}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    lines.extend(
        [
            "",
            "policy_parser_recommendation:",
            "- Put medium_dynamic_light in operational hold until architecture materializes parser/policy support.",
            "- Route SelectLocalization/Select_CString to dynamic selection parser, not token_simples_preservavel.",
            "- Add explicit getter perspective policy for source getter present + output getter omitted.",
            "- Split ROOT/Faith/Culture/Title and named-scope getters into grammar-role aware sublanes.",
            "- Require grammatical role metadata for dynamic tokens before any candidate generation.",
            "- Keep human packet generation disabled for token_simples_preservavel in this lane until those guards exist.",
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
    source_rows = read_jsonl(DIAGNOSTIC_PATH)
    rows: list[dict[str, Any]] = []
    for row in source_rows:
        if row.get("operational_class") not in TARGET_CLASSES:
            continue
        family, tags = classify_architecture_family(row)
        rows.append(
            {
                **row,
                "architecture_family": family,
                "architecture_tags": tags,
                "architecture_packet_source": SOURCE,
                "recommended_handling": "architecture_policy_or_parser_required_before_candidate_generation",
            }
        )
    rows.sort(key=lambda row: (str(row["architecture_family"]), str(row.get("surface_bucket") or ""), int(row["segment_id"])))

    class_counts = Counter(str(row.get("operational_class") or "") for row in rows)
    family_counts = Counter(str(row.get("architecture_family") or "") for row in rows)
    surface_family_counts = Counter(f"{row.get('surface_bucket')} | {row.get('architecture_family')}" for row in rows)
    sample = representative_samples(rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_architecture_packet",
        "source_diagnostic": str(DIAGNOSTIC_PATH),
        "segment_state_run_id": 498,
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "operational_hold": True,
        "hold_reason": "119 medium_dynamic_light remain; safe human/token-simple packet count is 0; architecture/parser required for dynamic/getter surfaces",
        "packet_count": len(rows),
        "precisa_arquitetura_count": class_counts.get("precisa_arquitetura", 0),
        "getter_perspective_omitted_count": class_counts.get("getter_perspective_omitted", 0),
        "operational_class_counts": top_counter(class_counts),
        "architecture_family_counts": top_counter(family_counts),
        "surface_family_counts": top_counter(surface_family_counts),
        "policy_parser_recommendation": [
            "Hold medium_dynamic_light operationally until architecture adds parser/policy support.",
            "Route SelectLocalization/Select_CString to dynamic selection parser.",
            "Materialize explicit getter perspective policy for source getter present + output getter omitted.",
            "Split ROOT/Faith/Culture/Title and named-scope getters into grammar-role aware sublanes.",
            "Require grammatical role metadata for dynamic tokens before candidate generation.",
        ],
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
    write_txt(txt_path, summary, rows, sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
