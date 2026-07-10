from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


REVIEW_KEY = "remaining_religion_culture_tenet_policy_review"
SOURCE_BUCKET = "remaining_religion_culture_tenet_policy"
MICRO_POLICY = "remaining_religion_culture_tenet_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_TOTAL = 19


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_remaining_religion_culture_tenet_policy_review"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_spec.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def summary_row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("record_type") == "summary"]
    if len(summaries) != 1:
        raise SystemExit(f"expected exactly one summary in {label}, got {len(summaries)}")
    return summaries[0]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_inputs(
    remaining_rows: list[dict[str, Any]],
    remaining_summary: dict[str, Any],
    blocked_registry: dict[str, Any],
    religion_registry: dict[str, Any],
) -> list[dict[str, Any]]:
    if int(remaining_summary.get("total_reviewed") or 0) != 35:
        raise SystemExit("remaining summary total guard failed")
    if int(remaining_summary.get(SOURCE_BUCKET) or 0) != EXPECTED_TOTAL:
        raise SystemExit("remaining summary tenet guard failed")
    for key in ["requires_apply_later_count", "requires_lifecycle_later_count", "false_safe_risk_count"]:
        if int(remaining_summary.get(key) or 0) != 0:
            raise SystemExit(f"remaining summary zero guard failed: {key}")
    if int(blocked_registry.get("blocked_uncertain_remaining_without_useful_spec") or 0) != 35:
        raise SystemExit("blocked registry remaining guard failed")
    if int(religion_registry.get("blocked_religion_culture_leftovers_projected") or 0) != 28:
        raise SystemExit("religion registry leftovers guard failed")
    if any(int(row.get(key) or 0) for row in [blocked_registry, religion_registry] for key in ["auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"]):
        raise SystemExit("registry permission guard failed")
    selected = [row for row in remaining_rows if row.get("decision") == SOURCE_BUCKET]
    if len(selected) != EXPECTED_TOTAL:
        raise SystemExit(f"selected tenet rows guard failed: {len(selected)}")
    for row in selected:
        if row.get("requires_apply_later") or row.get("requires_lifecycle_later") or row.get("false_safe_risk"):
            raise SystemExit(f"future/risk flag guard failed: {row.get('segment_id')}")
    return selected


def validate_pending(conn: sqlite3.Connection, rows: list[dict[str, Any]], run_id: int) -> None:
    ids = [int(row["segment_id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    state_rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *ids),
    ).fetchall()
    if len(state_rows) != len(ids):
        raise SystemExit(f"missing state rows: {len(ids) - len(state_rows)}")
    bad = [
        dict(row)
        for row in state_rows
        if row["state_group"] != "pending"
        or int(row["is_closed"] or 0) != 0
        or int(row["needs_output_apply"] or 0) != 0
        or int(row["confirmed_matches_output"] or 0) != 1
    ]
    if bad:
        raise SystemExit(f"pending guard failed: {bad[:3]}")


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text_blob = f"{row.get('original_text') or ''}\n{row.get('current_output_text') or ''}".lower()
    families = set(row.get("open_families") or [])
    has_tenet_domain = (
        "religion_semantic_microagent" in families
        and "semantic_review_router" in families
    ) or any(term in text_blob for term in ["fé", "divino", "deus", "sagrado", "doutrina", "misticismo", "ancestrais"])
    if row.get("existing_policy_reuse"):
        decision = "tenet_reuse_domain_context_religion_holy_site_policy"
        reuse = str(row["existing_policy_reuse"])
        terminal_guard = False
        notes = "reuses existing domain/religion policy"
    elif has_tenet_domain:
        decision = "tenet_terminal_guard_with_religion_domain"
        reuse = ""
        terminal_guard = True
        notes = "tenet/religion prose is a terminal domain guard; no candidate/apply"
    else:
        decision = "tenet_blocked_uncertain"
        reuse = ""
        terminal_guard = False
        notes = "tenet domain evidence was not strong enough"
    return {
        "segment_id": int(row["segment_id"]),
        "review_key": REVIEW_KEY,
        "source_bucket": SOURCE_BUCKET,
        "original_text": str(row.get("original_text") or ""),
        "current_output_text": str(row.get("current_output_text") or ""),
        "tenet_decision": decision,
        "existing_policy_reuse": reuse,
        "terminal_guard": terminal_guard,
        "requires_new_policy": False,
        "register_component_now": False,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": notes,
    }


def build_spec(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["tenet_decision"] for row in records)
    terminal_guard_count = sum(1 for row in records if row["terminal_guard"])
    reuse_count = sum(1 for row in records if row["existing_policy_reuse"])
    spec = {
        "schema_version": 1,
        "created_for": "read_only_micro_policy_review",
        "micro_policy": MICRO_POLICY,
        "review_key": REVIEW_KEY,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "total_reviewed": len(records),
        "tenet_terminal_guard_with_religion_domain": decisions.get("tenet_terminal_guard_with_religion_domain", 0),
        "tenet_reuse_domain_context_religion_holy_site_policy": decisions.get("tenet_reuse_domain_context_religion_holy_site_policy", 0),
        "tenet_reuse_not_requirement_effect_culture_religion_router": decisions.get("tenet_reuse_not_requirement_effect_culture_religion_router", 0),
        "needs_tenet_doctrine_policy": decisions.get("needs_tenet_doctrine_policy", 0),
        "needs_tenet_name_short_label_policy": decisions.get("needs_tenet_name_short_label_policy", 0),
        "tenet_blocked_uncertain": decisions.get("tenet_blocked_uncertain", 0),
        "terminal_guard_count": terminal_guard_count,
        "reuse_count": reuse_count,
        "requires_new_policy_count": sum(1 for row in records if row["requires_new_policy"]),
        "register_component_now_count": sum(1 for row in records if row["register_component_now"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "sample_ids": [int(row["segment_id"]) for row in records],
        "policy_decision": "catalog_as_micro_policy_under_blocked_uncertain_religion_culture_no_registry",
        "register_component_now": False,
        "reason_not_registered": "low_volume_leftover_no_architectural_gain_after_parent_router_registration",
        "next_prompt": "chat_exec_resolution_phase_status_and_next_strategy_prompt.md",
    }
    if terminal_guard_count + reuse_count < 15 or spec["tenet_blocked_uncertain"]:
        spec["next_prompt"] = "chat_exec_remaining_tenet_followup_policy_review_prompt.md"
    return spec


def write_outputs(records: list[dict[str, Any]], spec: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_reviewed",
        "tenet_terminal_guard_with_religion_domain",
        "tenet_reuse_domain_context_religion_holy_site_policy",
        "tenet_reuse_not_requirement_effect_culture_religion_router",
        "needs_tenet_doctrine_policy",
        "needs_tenet_name_short_label_policy",
        "tenet_blocked_uncertain",
        "terminal_guard_count",
        "reuse_count",
        "requires_new_policy_count",
        "register_component_now_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
    ]
    lines = [
        "remaining religion/culture tenet policy review",
        f"review_key={REVIEW_KEY}",
        f"micro_policy={MICRO_POLICY}",
        "",
        *[f"{key}={spec[key]}" for key in metric_keys],
        "",
        "decision:",
        "1. A sublane tenet terminaliza como guard de dominio religioso.",
        "2. Reuso direto de policy existente: nao dominante.",
        "3. Componente proprio registrado agora: nao; sem ganho arquitetural claro.",
        "4. Catalogar como micro-policy/spec read-only por baixa volumetria.",
        "5. Candidato de resolver/apply: nao.",
        f"6. Proximo passo: {spec['next_prompt']}.",
        "",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remaining-jsonl", required=True, type=Path)
    parser.add_argument("--remaining-summary-json", required=True, type=Path)
    parser.add_argument("--blocked-registry-jsonl", required=True, type=Path)
    parser.add_argument("--religion-culture-registry-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")

    remaining_rows = read_jsonl(args.remaining_jsonl)
    remaining_summary = read_json(args.remaining_summary_json)
    blocked_registry = summary_row(read_jsonl(args.blocked_registry_jsonl), "blocked registry")
    religion_registry = summary_row(read_jsonl(args.religion_culture_registry_jsonl), "religion/culture registry")
    selected = validate_inputs(remaining_rows, remaining_summary, blocked_registry, religion_registry)
    with connect_readonly() as conn:
        validate_pending(conn, selected, args.segment_state_run_id)

    records = [classify(row) for row in selected]
    spec = build_spec(records)
    if spec["total_reviewed"] != EXPECTED_TOTAL:
        raise SystemExit("total_reviewed output guard failed")
    if spec["requires_apply_later_count"] or spec["requires_lifecycle_later_count"] or spec["false_safe_risk_count"]:
        raise SystemExit("zero-risk output guard failed")
    txt_path, jsonl_path, spec_path = write_outputs(records, spec)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    for key in [
        "total_reviewed",
        "tenet_terminal_guard_with_religion_domain",
        "tenet_reuse_domain_context_religion_holy_site_policy",
        "tenet_blocked_uncertain",
        "terminal_guard_count",
        "reuse_count",
        "requires_new_policy_count",
        "register_component_now_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
        "next_prompt",
    ]:
        print(f"{key}: {spec[key]}")


if __name__ == "__main__":
    main()
