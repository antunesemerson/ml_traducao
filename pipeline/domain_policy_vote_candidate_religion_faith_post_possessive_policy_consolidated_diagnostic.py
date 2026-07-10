from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_post_possessive_policy_consolidated_diagnostic_v1"
BASE_JSONL_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic.jsonl"
)
BASE_SUMMARY_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic_summary.json"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_SURFACE_BUCKET = "religion_faith_doctrine"
COVERED_FAMILIES = {
    "debug_placeholder_hold": "religion_faith_doctrine_debug_placeholder_hold_policy",
    "holy_site_effect_or_requirement": "religion_faith_doctrine_holy_site_effect_policy",
    "tenet_doctrine_dynamic_tokens": "religion_faith_doctrine_tenet_doctrine_dynamic_policy",
    "clergy_adherent_priest_terms": "religion_faith_doctrine_clergy_adherent_policy",
    "faith_possessive_or_relation": "religion_faith_doctrine_possessive_relation_policy",
}


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


def validate_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    keys = list(COVERED_FAMILIES.values())
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT agent_key, agent_type, operational_state, decision_role, status, notes_json
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
        ORDER BY agent_key
        """,
        tuple(keys),
    ).fetchall()
    policies: dict[str, Any] = {}
    for row in rows:
        notes = json.loads(row["notes_json"] or "{}")
        policies[str(row["agent_key"])] = {
            "agent_key": row["agent_key"],
            "agent_type": row["agent_type"],
            "operational_state": row["operational_state"],
            "decision_role": row["decision_role"],
            "status": row["status"],
            "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
            "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
            "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
            "production_release_allowed": bool(notes.get("production_release_allowed")),
        }
    missing = [key for key in keys if key not in policies]
    if missing:
        raise SystemExit(f"missing policy registry entries: {missing}")
    bad = {
        key: value
        for key, value in policies.items()
        if value["candidate_generation_allowed"]
        or value["auto_apply_allowed"]
        or value["lifecycle_allowed"]
        or value["production_release_allowed"]
    }
    if bad:
        raise SystemExit(f"policy guard failed: {bad}")
    return {"policies": policies, "missing": missing}


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) AS closed_count,
          SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN COALESCE(needs_output_apply, 0) = 1 THEN 1 ELSE 0 END) AS needs_output_apply_count
        FROM segment_state_items
        WHERE run_id = ?
        """,
        (EXPECTED_SEGMENT_STATE_RUN_ID,),
    ).fetchone()
    return {
        "closed_count": int(row["closed_count"] or 0),
        "pending_count": int(row["pending_count"] or 0),
        "needs_output_apply_count": int(row["needs_output_apply_count"] or 0),
    }


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if summary.get("surface_bucket") != EXPECTED_SURFACE_BUCKET:
        raise SystemExit("surface_bucket guard failed")
    if int(summary.get("diagnostic_count") or 0) != len(rows):
        raise SystemExit("diagnostic_count guard failed")
    if summary.get("source_changed") is not False:
        raise SystemExit("source_changed guard failed")


def classify_residual(row: dict[str, Any]) -> str:
    family = str(row.get("hold_family") or "")
    risk = str(row.get("risk_bucket") or "")
    if family in {"religion_family_runtime_adjective", "faith_adjective_holy_war_runtime", "holy_war_fervor_runtime"}:
        return "runtime_getter_parser_later"
    if family in {"dense_structural_token_cluster", "multiline_effect_list_hold"}:
        return "structural_hold_or_policy"
    if family == "human_only_or_unknown_hold":
        return "human_review_or_unknown"
    if family == "pantheon_divine_runtime_name":
        return "divine_term_runtime_parser_later"
    if family in {"faith_name_article_preposition_context", "faith_adjective_runtime", "faith_name_runtime"}:
        return "faith_name_context_parser_later"
    if family == "low_plain_leftover" or risk in {"low_plain_domain", "medium_dynamic_light"}:
        return "small_human_review_possible"
    return "residual_unknown"


def representative(rows: list[dict[str, Any]], key: str, limit: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "segment_id": int(row["segment_id"]),
            "source_key": row.get("source_key"),
            "relative_path": row.get("relative_path"),
            "risk_bucket": row.get("risk_bucket"),
            "current_output_text": row.get("current_output_text"),
            "english_text": row.get("english_text"),
        }
        for row in [item for item in rows if item.get("hold_family") == key][:limit]
    ]


def choose_next_family(counts: Counter[str]) -> tuple[str, int, str]:
    order = [
        "religion_family_runtime_adjective",
        "human_only_or_unknown_hold",
        "pantheon_divine_runtime_name",
        "dense_structural_token_cluster",
        "multiline_effect_list_hold",
    ]
    for family in order:
        count = int(counts.get(family, 0))
        if count:
            if family == "religion_family_runtime_adjective":
                return family, count, "read_only_runtime_getter_parser_review"
            if family == "human_only_or_unknown_hold":
                return family, count, "small_human_or_hold_review"
            return family, count, "read_only_hold_or_policy_review"
    return "", 0, "hold_lane"


def write_reports(
    *,
    rows: list[dict[str, Any]],
    residual_rows: list[dict[str, Any]],
    registry: dict[str, Any],
    states: dict[str, int],
) -> tuple[Path, Path, Path, dict[str, Any]]:
    base_family_counts = Counter(str(row.get("hold_family") or "") for row in rows)
    covered_counts = Counter(str(row.get("hold_family") or "") for row in rows if row.get("hold_family") in COVERED_FAMILIES)
    residual_family_counts = Counter(str(row.get("hold_family") or "") for row in residual_rows)
    residual_risk_counts = Counter(str(row.get("risk_bucket") or "") for row in residual_rows)
    residual_class_counts = Counter(classify_residual(row) for row in residual_rows)
    next_family, next_count, next_mode = choose_next_family(residual_family_counts)
    recommendation = (
        f"Review {next_family} read-only next ({next_count} rows), mode={next_mode}; do not generate candidates yet."
        if next_family
        else "No coherent next family selected; hold lane and return to broader prioritization."
    )

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_post_possessive_policy_consolidated_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    residual_records = [
        {
            "record_type": "post_possessive_policy_residual",
            "source": SOURCE,
            "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
            "segment_id": int(row["segment_id"]),
            "source_key": row.get("source_key"),
            "relative_path": row.get("relative_path"),
            "surface_bucket": row.get("surface_bucket"),
            "hold_family": row.get("hold_family"),
            "risk_bucket": row.get("risk_bucket"),
            "residual_class": classify_residual(row),
            "current_output_text": row.get("current_output_text"),
            "english_text": row.get("english_text"),
            "candidate_generation_allowed": False,
            "auto_apply_allowed": False,
            "lifecycle_allowed": False,
            "production_release_allowed": False,
        }
        for row in sorted(residual_rows, key=lambda item: (str(item.get("hold_family") or ""), str(item.get("risk_bucket") or ""), int(item["segment_id"])))
    ]
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in residual_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_consolidated_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "state_counts": states,
        "surface_bucket": EXPECTED_SURFACE_BUCKET,
        "base_diagnostic_count": len(rows),
        "covered_by_policy_count": sum(covered_counts.values()),
        "covered_family_counts": dict(covered_counts),
        "residual_count": len(residual_rows),
        "base_family_counts": dict(base_family_counts),
        "residual_family_counts": dict(residual_family_counts),
        "residual_risk_counts": dict(residual_risk_counts),
        "residual_class_counts": dict(residual_class_counts),
        "top_residual_examples": {family: representative(residual_rows, family) for family, _count in residual_family_counts.most_common(8)},
        "registry_validation": registry,
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
        "next_recommended_family": next_family,
        "next_recommended_count": next_count,
        "next_recommended_mode": next_mode,
        "single_operational_recommendation": recommendation,
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine post-possessive-policy consolidated diagnostic",
        "",
        f"segment_state_run_id: {EXPECTED_SEGMENT_STATE_RUN_ID}",
        f"global_pending_count: {states['pending_count']}",
        f"needs_output_apply_count: {states['needs_output_apply_count']}",
        f"base_diagnostic_count: {len(rows)}",
        f"covered_by_policy_count: {sum(covered_counts.values())}",
        f"residual_count: {len(residual_rows)}",
        "",
        "covered_family_counts:",
    ]
    lines.extend(f"- {count} | {family}" for family, count in covered_counts.most_common())
    lines.extend(["", "residual_family_counts:"])
    lines.extend(f"- {count} | {family}" for family, count in residual_family_counts.most_common())
    lines.extend(["", "residual_risk_counts:"])
    lines.extend(f"- {count} | {risk}" for risk, count in residual_risk_counts.most_common())
    lines.extend(["", "residual_class_counts:"])
    lines.extend(f"- {count} | {klass}" for klass, count in residual_class_counts.most_common())
    lines.extend(["", f"recommendation: {recommendation}", "", "guards:", "- candidate_generation_allowed: false", "- auto_apply_allowed: false", "- lifecycle_allowed: false", "- production_release_allowed: false", "- apply: not_run", "- lifecycle: not_run", "- segment_state: not_run", "- reindex: not_run", "- full_production: not_run"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, summary


def main() -> None:
    summary = read_json(BASE_SUMMARY_PATH)
    base_rows = read_jsonl(BASE_JSONL_PATH)
    validate_inputs(summary, base_rows)
    rows = [row for row in base_rows if row.get("surface_bucket") == EXPECTED_SURFACE_BUCKET]
    residual_rows = [row for row in rows if row.get("hold_family") not in COVERED_FAMILIES]
    with connect_readonly() as conn:
        registry = validate_registry(conn)
        states = state_counts(conn)
    _txt, _jsonl, _summary, result = write_reports(rows=rows, residual_rows=residual_rows, registry=registry, states=states)
    print(f"summary={_summary}")
    print(f"base_diagnostic_count={result['base_diagnostic_count']}")
    print(f"covered_by_policy_count={result['covered_by_policy_count']}")
    print(f"residual_count={result['residual_count']}")
    print(f"residual_family_counts={json.dumps(result['residual_family_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"next_recommended_family={result['next_recommended_family']}")
    print(f"next_recommended_count={result['next_recommended_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
