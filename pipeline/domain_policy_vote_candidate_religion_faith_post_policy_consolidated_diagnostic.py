from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_post_policy_consolidated_diagnostic_v1"
BASE_SUMMARY_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic_summary.json"
)
BASE_JSONL_PATH = Path(
    "reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic.jsonl"
)
DEBUG_POLICY_SUMMARY_PATH = Path(
    "reports/20260630_095236_740302_religion_faith_doctrine_debug_placeholder_hold_policy_registry_apply_summary.json"
)
HOLY_SITE_POLICY_SUMMARY_PATH = Path(
    "reports/20260630_095905_781783_religion_faith_doctrine_holy_site_effect_policy_registry_apply_summary.json"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_SURFACE_BUCKET = "religion_faith_doctrine"
COVERED_FAMILIES = {
    "debug_placeholder_hold": "religion_faith_doctrine_debug_placeholder_hold_policy",
    "holy_site_effect_or_requirement": "religion_faith_doctrine_holy_site_effect_policy",
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


def registry_validation(conn: sqlite3.Connection) -> dict[str, Any]:
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
        raise SystemExit(f"missing covered policy registry entries: {missing}")
    bad = {
        key: value
        for key, value in policies.items()
        if value["candidate_generation_allowed"]
        or value["auto_apply_allowed"]
        or value["lifecycle_allowed"]
        or value["production_release_allowed"]
    }
    if bad:
        raise SystemExit(f"covered policy guard failed: {bad}")
    return {"policies": policies, "missing": missing}


def validate_inputs(base_summary: dict[str, Any], rows: list[dict[str, Any]], debug_summary: dict[str, Any], holy_summary: dict[str, Any]) -> None:
    if int(base_summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("base segment_state_run_id guard failed")
    if base_summary.get("surface_bucket") != EXPECTED_SURFACE_BUCKET:
        raise SystemExit("base surface_bucket guard failed")
    if int(base_summary.get("diagnostic_count") or 0) != len(rows):
        raise SystemExit("base diagnostic_count guard failed")
    if debug_summary.get("mode") != "apply" or debug_summary.get("agent_key") != COVERED_FAMILIES["debug_placeholder_hold"]:
        raise SystemExit("debug policy summary guard failed")
    if holy_summary.get("mode") != "apply" or holy_summary.get("agent_key") != COVERED_FAMILIES["holy_site_effect_or_requirement"]:
        raise SystemExit("holy-site policy summary guard failed")
    for row in rows:
        if row.get("surface_bucket") != EXPECTED_SURFACE_BUCKET:
            raise SystemExit(f"unexpected surface_bucket for segment {row.get('segment_id')}")


def classify_residual(row: dict[str, Any]) -> str:
    family = str(row.get("hold_family") or "")
    risk = str(row.get("risk_bucket") or "")
    if family in {"tenet_doctrine_dynamic_tokens", "clergy_adherent_priest_terms", "faith_possessive_or_relation"}:
        if risk in {"high_structural_token_density", "medium_dynamic_dense", "high_multiline_effect_list"}:
            return "architecture_parser_candidate"
        return "small_human_or_parser_review"
    if family in {"religion_family_runtime_adjective", "faith_adjective_holy_war_runtime", "holy_war_fervor_runtime"}:
        return "runtime_getter_parser_later"
    if family in {"multiline_effect_list_hold", "dense_structural_token_cluster"}:
        return "structural_hold_or_policy"
    if family in {"human_only_or_unknown_hold", "low_plain_leftover"}:
        return "human_review_possible"
    if family in {"pantheon_divine_runtime_name", "faith_name_article_preposition_context", "faith_adjective_runtime", "faith_name_runtime"}:
        return "context_parser_or_hold"
    return "residual_unknown"


def representative(rows: list[dict[str, Any]], key: str, limit: int = 5) -> list[dict[str, Any]]:
    selected = [row for row in rows if row.get("hold_family") == key][:limit]
    return [
        {
            "segment_id": int(row["segment_id"]),
            "source_key": row.get("source_key"),
            "relative_path": row.get("relative_path"),
            "risk_bucket": row.get("risk_bucket"),
            "recommended_action": row.get("recommended_action"),
            "current_output_text": row.get("current_output_text"),
            "english_text": row.get("english_text"),
        }
        for row in selected
    ]


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

    residual_records: list[dict[str, Any]] = []
    for row in sorted(residual_rows, key=lambda item: (str(item.get("hold_family") or ""), str(item.get("risk_bucket") or ""), int(item["segment_id"]))):
        residual_records.append(
            {
                "record_type": "post_policy_residual",
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
        )

    next_family, next_count = ("", 0)
    for family, count in residual_family_counts.most_common():
        if family not in {"dense_structural_token_cluster", "multiline_effect_list_hold"}:
            next_family, next_count = family, count
            break
    recommendation = (
        f"Review {next_family} read-only next ({next_count} rows), prioritizing parser/splitter design and not candidate generation."
        if next_family
        else "No coherent residual family selected; hold lane and return to broader prioritization."
    )

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
        "single_operational_recommendation": recommendation,
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_post_policy_consolidated_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in residual_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine post-policy consolidated diagnostic",
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
    lines.extend(["", "representative_examples:"])
    for family, _count in residual_family_counts.most_common(6):
        lines.extend(["", f"## {family}"])
        for row in representative(residual_rows, family, 3):
            output = str(row.get("current_output_text") or "").replace("\n", "\\n")
            lines.append(f"- segment_id {row['segment_id']} | {row['risk_bucket']} | {row['source_key']}")
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
            f"recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, summary


def main() -> None:
    base_summary = read_json(BASE_SUMMARY_PATH)
    base_rows = read_jsonl(BASE_JSONL_PATH)
    debug_summary = read_json(DEBUG_POLICY_SUMMARY_PATH)
    holy_summary = read_json(HOLY_SITE_POLICY_SUMMARY_PATH)
    validate_inputs(base_summary, base_rows, debug_summary, holy_summary)

    rows = [row for row in base_rows if row.get("surface_bucket") == EXPECTED_SURFACE_BUCKET]
    residual_rows = [row for row in rows if row.get("hold_family") not in COVERED_FAMILIES]
    with connect_readonly() as conn:
        registry = registry_validation(conn)
        states = state_counts(conn)
    txt_path, jsonl_path, summary_path, result = write_reports(
        rows=rows,
        residual_rows=residual_rows,
        registry=registry,
        states=states,
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
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
