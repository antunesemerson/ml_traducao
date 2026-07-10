from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_selectlocalization_review_v1"
INPUT_PATH = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run.jsonl")
INPUT_SUMMARY = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run_summary.json")
EXPECTED_COUNT = 2

MANUAL_CLASSIFICATIONS: dict[int, dict[str, Any]] = {
    18256: {
        "select_surface_type": "SelectLocalization",
        "structural_subtype": "external_suffix_after_selectlocalization",
        "external_affix_position": "suffix",
        "pipe_modifier_present": False,
        "token_internal_change_allowed": False,
        "external_affix_translation_review": "shah/sah -> xá",
        "structural_risk": "medium_structural_affix_spacing",
        "recommended_handling": "parser_read_only_affix_boundary_review",
        "recommended_policy_decision": "subpolicy_read_only",
        "human_packet_future": False,
        "rationale": "The SelectLocalization token should remain opaque; only the visible suffix after the token is localized, and spacing/attachment must be reviewed by a structural parser before any apply.",
    },
    18708: {
        "select_surface_type": "SelectLocalization",
        "structural_subtype": "external_prefix_before_selectlocalization_pipe_l",
        "external_affix_position": "prefix",
        "pipe_modifier_present": True,
        "token_internal_change_allowed": False,
        "external_affix_translation_review": "Co- prefix preserved",
        "structural_risk": "medium_structural_prefix_pipe",
        "recommended_handling": "parser_read_only_prefix_pipe_review",
        "recommended_policy_decision": "subpolicy_read_only",
        "human_packet_future": False,
        "rationale": "The visible prefix is outside the token while the SelectLocalization token includes a pipe modifier; this is parser territory, not medium_dynamic_light text repair.",
    },
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


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_architecture_parser_routing_dry_run":
        raise SystemExit("summary mode guard failed")
    if summary.get("registry_guard_ok") is not True:
        raise SystemExit("registry guard failed")
    if int(summary.get("selectlocalization_hold_count") or 0) != EXPECTED_COUNT:
        raise SystemExit("selectlocalization_hold_count guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")
    selected = [row for row in rows if row.get("target_policy") == "select_localization_parser_or_structural_hold"]
    if len(selected) != EXPECTED_COUNT:
        raise SystemExit(f"selected count guard failed: {len(selected)}")
    selected_ids = {int(row["segment_id"]) for row in selected}
    if selected_ids != set(MANUAL_CLASSIFICATIONS):
        raise SystemExit(f"segment id guard failed: {sorted(selected_ids)}")
    return selected


def fetch_parent_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    agent_key = "medium_dynamic_light_residual_parser_policy"
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (agent_key,)).fetchone()
    if row is None:
        raise SystemExit(f"missing registry agent: {agent_key}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "exists": True,
        "agent_key": payload.get("agent_key"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
    }


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    classification = MANUAL_CLASSIFICATIONS[segment_id]
    return {
        **row,
        **classification,
        "review_mode": "read_only_selectlocalization_structural_review",
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "recommended_next_step": "register_shadow_selectlocalization_affix_policy",
    }


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light SelectLocalization structural review",
        "",
        f"review_count: {summary['review_count']}",
        "",
        "structural_subtype_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["structural_subtype_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['structural_subtype']}",
                f"- source_key: {row['source_key']}",
                f"- path: {row['relative_path']}",
                f"- affix_position: {row['external_affix_position']}",
                f"- pipe_modifier_present: {row['pipe_modifier_present']}",
                f"- external_affix_translation_review: {row['external_affix_translation_review']}",
                f"- structural_risk: {row['structural_risk']}",
                f"- dynamic_tokens: {json.dumps(row.get('dynamic_tokens') or [], ensure_ascii=False)}",
                f"- english: {row['english_text']}",
                f"- spanish: {row['spanish_text']}",
                f"- output_ptbr: {row['current_output_text']}",
                f"- rationale: {row['rationale']}",
            ]
        )
    lines.extend(
        [
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
    summary_in = read_json(INPUT_SUMMARY)
    rows_in = read_jsonl(INPUT_PATH)
    selected = validate_inputs(summary_in, rows_in)
    with connect_readonly() as conn:
        parent_registry = fetch_parent_registry(conn)
    reviewed = [review_row(row) for row in selected]
    reviewed.sort(key=lambda row: (row["structural_subtype"], row["segment_id"]))

    subtype_counts = Counter(row["structural_subtype"] for row in reviewed)
    affix_counts = Counter(row["external_affix_position"] for row in reviewed)
    risk_counts = Counter(row["structural_risk"] for row in reviewed)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_selectlocalization_structural_review",
        "input_path": str(INPUT_PATH),
        "input_summary": str(INPUT_SUMMARY),
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "parent_registry": parent_registry,
        "structural_subtype_counts": top_counter(subtype_counts),
        "external_affix_position_counts": top_counter(affix_counts),
        "structural_risk_counts": top_counter(risk_counts),
        "recommended_policy_decision": "subpolicy_read_only",
        "human_packet_future_count": sum(1 for row in reviewed if row["human_packet_future"]),
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Register a shadow read-only SelectLocalization affix/pipe splitter under medium_dynamic_light_residual_parser_policy; keep candidate generation disabled because both cases require structural token boundary handling.",
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_medium_dynamic_light_selectlocalization_structural_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, reviewed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, reviewed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
