from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_effect_review_v1"
SUMMARY_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic_summary.json"
)
JSONL_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_SURFACE_BUCKET = "religion_faith_doctrine"
EXPECTED_FAMILY = "holy_site_effect_or_requirement"
EXPECTED_COUNT = 92

HOLY_SITE_GETTER_RE = re.compile(r"GetNumberOf(?:Controlled)?HolySites|holy_sites|holy_site", re.I)
HOLY_SITE_NAME_RE = re.compile(r"holy_site_name|holy_site_[a-z0-9_]+|GetHolySite|holy site name", re.I)
EFFECT_MODIFIER_RE = re.compile(r"effect|modifier|conversion speed|fervor|hostility|advantage|piety|opinion|development|tax|levy", re.I)
REQUIREMENT_RE = re.compile(r"requirement|required|must|needs?|control(?:s|led)?|controlled|cannot|available|valid|trigger", re.I)
PILGRIMAGE_RE = re.compile(r"pilgrimage|pilgrim|hajj|journey|holy_order|great_holy_war|crusade|jihad", re.I)
BUILDING_RE = re.compile(r"building|temple|church|cathedral|mosque|shrine|sanctuary|holding", re.I)
MULTILINE_RE = re.compile(r"\\n|\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)
DENSE_GETTER_RE = re.compile(r"\[(?:Faith|Religion|ROOT|CHARACTER|TARGET_CHARACTER|THIS|SCOPE|FAITH|RELIGION)[^\]]+\]")
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|TOKEN_PARAMETER|GetHostilityLevelName|\|[0-9]?V|\|0v|#N|#P|[0-9]+\s*%", re.I)
SPANISH_RESIDUE_RE = re.compile(r"\b(?:de sus|controlan|velocidad|religiosa|conversi[oó]n|lugares santos)\b|Ã", re.I)


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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def registry_status(conn: sqlite3.Connection) -> dict[str, Any]:
    keys = [
        "holy_site_effect_name_policy",
        "domain_context_religion_holy_site_policy",
        "effect_list_concept_policy",
        "script_value_effect_policy",
    ]
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT agent_key, agent_type, operational_state, decision_role, status
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
        ORDER BY agent_key
        """,
        tuple(keys),
    ).fetchall()
    return {
        "registered_reuse_policies": {str(row["agent_key"]): dict(row) for row in rows},
        "holy_site_effect_name_policy_registered": any(row["agent_key"] == "holy_site_effect_name_policy" for row in rows),
    }


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if summary.get("surface_bucket") != EXPECTED_SURFACE_BUCKET:
        raise SystemExit("surface_bucket guard failed")
    if int(summary.get("candidate_generation_count") or 0) != 0:
        raise SystemExit("candidate_generation_count guard failed")
    if int(summary.get("apply_count") or 0) != 0:
        raise SystemExit("apply_count guard failed")
    if summary.get("source_changed") is not False:
        raise SystemExit("source_changed guard failed")

    family_rows = [
        row
        for row in rows
        if row.get("surface_bucket") == EXPECTED_SURFACE_BUCKET and row.get("hold_family") == EXPECTED_FAMILY
    ]
    if len(family_rows) != EXPECTED_COUNT:
        raise SystemExit(f"holy_site_effect_or_requirement count guard failed: {len(family_rows)} expected {EXPECTED_COUNT}")
    duplicates = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in family_rows).items() if count > 1]
    if duplicates:
        raise SystemExit(f"duplicate segment_id guard failed: {duplicates[:10]}")
    return family_rows


def classify(row: dict[str, Any]) -> tuple[str, str, list[str], str]:
    blob = " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "current_output_text", "spanish_text", "english_text")
    )
    risk = str(row.get("risk_bucket") or "")
    tags: list[str] = []
    for label, pattern in (
        ("holy_site_getter", HOLY_SITE_GETTER_RE),
        ("holy_site_name", HOLY_SITE_NAME_RE),
        ("effect_modifier", EFFECT_MODIFIER_RE),
        ("requirement_surface", REQUIREMENT_RE),
        ("pilgrimage_or_holy_war", PILGRIMAGE_RE),
        ("building_or_temple", BUILDING_RE),
        ("multiline_or_effect_list", MULTILINE_RE),
        ("dense_runtime_getter", DENSE_GETTER_RE),
        ("script_value_or_numeric", SCRIPT_VALUE_RE),
        ("spanish_residue_or_mojibake", SPANISH_RESIDUE_RE),
    ):
        if pattern.search(blob):
            tags.append(label)

    if "holy_site_getter" in tags and "dense_runtime_getter" in tags and "requirement_surface" in tags:
        return (
            "controlled_holy_sites_runtime_requirement",
            "route_to_holy_site_effect_name_policy_or_parser",
            tags,
            "Holy-site count/control runtime requirement; reuse existing holy-site policy if parser handles Faith/Religion getters.",
        )
    if "holy_site_name" in tags and ("effect_modifier" in tags or "requirement_surface" in tags):
        return (
            "holy_site_name_effect_requirement",
            "route_to_holy_site_effect_name_policy",
            tags,
            "Holy-site name/effect surface matches existing holy_site_effect_name_policy territory.",
        )
    if "effect_modifier" in tags and "script_value_or_numeric" in tags:
        return (
            "holy_site_modifier_or_numeric_effect",
            "needs_script_value_or_effect_policy_review",
            tags,
            "Modifier/numeric effect needs effect/script-value policy review before any candidate flow.",
        )
    if "pilgrimage_or_holy_war" in tags:
        return (
            "pilgrimage_holy_war_context",
            "hold_context_or_parser_later",
            tags,
            "Pilgrimage/holy-war semantics are context-heavy and should stay out of automatic candidates.",
        )
    if "building_or_temple" in tags:
        return (
            "holy_site_building_temple_context",
            "needs_domain_context_review",
            tags,
            "Building/temple context may belong to domain-context or building-modifier policy.",
        )
    if "multiline_or_effect_list" in tags:
        return (
            "multiline_effect_list_holy_site",
            "hold_structural_or_effect_list_policy",
            tags,
            "Multiline/effect-list shape needs structural policy, not human low-risk packet.",
        )
    if risk == "medium_dynamic_light":
        return (
            "light_holy_site_context",
            "small_human_review_possible",
            tags,
            "Light dynamic holy-site context may be reviewed manually in a tiny packet.",
        )
    return (
        "residual_holy_site_context",
        "hold_or_architecture_later",
        tags,
        "Residual holy-site context lacks enough safe surface for automation.",
    )


def build_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (str(item.get("risk_bucket") or ""), int(item["segment_id"]))):
        subtype, recommendation, tags, reason = classify(row)
        reviewed.append(
            {
                "record_type": "holy_site_effect_or_requirement_review",
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
                "operational_subtype": subtype,
                "recommended_action": recommendation,
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


def write_reports(reviewed: list[dict[str, Any]], registry: dict[str, Any]) -> tuple[Path, Path, Path, dict[str, Any]]:
    subtype_counts = Counter(str(row["operational_subtype"]) for row in reviewed)
    action_counts = Counter(str(row["recommended_action"]) for row in reviewed)
    risk_counts = Counter(str(row["risk_bucket"]) for row in reviewed)
    tag_counts = Counter(tag for row in reviewed for tag in row["review_tags"])
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
        "operational_subtype_counts": dict(subtype_counts),
        "recommended_action_counts": dict(action_counts),
        "risk_bucket_counts": dict(risk_counts),
        "tag_counts": dict(tag_counts),
        "registry_status": registry,
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
            "Register a read-only holy-site residual splitter that reuses holy_site_effect_name_policy for name/count "
            "surfaces and keeps numeric, holy-war, building, multiline, and residual contexts out of candidate generation."
        ),
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_holy_site_effect_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine holy_site_effect_or_requirement review",
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
        examples = [row for row in reviewed if row["operational_subtype"] == subtype][:4]
        if not examples:
            continue
        lines.extend(["", f"## {subtype}"])
        for row in examples:
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
    summary = read_json(SUMMARY_PATH)
    rows = read_jsonl(JSONL_PATH)
    family_rows = validate_inputs(summary, rows)
    reviewed = build_review_rows(family_rows)
    with connect_readonly() as conn:
        registry = registry_status(conn)
    txt_path, jsonl_path, summary_path, result = write_reports(reviewed, registry)
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
