from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_getter_perspective_omitted_review_v1"
INPUT_PATH = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run.jsonl")
INPUT_SUMMARY = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run_summary.json")
AGENT_KEY = "medium_dynamic_light_getter_perspective_omitted_policy"
EXPECTED_COUNT = 5

MANUAL_CLASSIFICATIONS: dict[int, dict[str, Any]] = {
    31464: {
        "omission_role": "subject_pronoun",
        "operational_subtype": "subject_pronoun_omitted_fluency_ok",
        "omitted_source_surface": "[liege.GetSheHe]",
        "output_preserves_meaning": True,
        "needs_human_packet": False,
        "needs_architecture_policy": True,
        "recommended_handling": "policy_allows_ptbr_null_subject_when_verb_subject_is_recoverable",
        "rationale": "English has an explicit subject pronoun before 'replies', while PT-BR can naturally omit it in 'responde'. No repair should be generated automatically.",
    },
    49411: {
        "omission_role": "possessive",
        "operational_subtype": "possessive_lexicalized_output_ok",
        "omitted_source_surface": "[evangelizer.GetHerHis]",
        "output_preserves_meaning": True,
        "needs_human_packet": False,
        "needs_architecture_policy": True,
        "recommended_handling": "policy_allows_possessive_lexicalization_when_ptbr_possessive_is_non_gendered",
        "rationale": "The source has a possessive getter for liege; output uses 'seu suserano', which preserves the possessive without needing a gendered token in PT-BR.",
    },
    113354: {
        "omission_role": "not_omitted_false_positive",
        "operational_subtype": "primary_getter_present_possessive_rephrased",
        "omitted_source_surface": "your/tus possessive surface",
        "output_preserves_meaning": True,
        "needs_human_packet": False,
        "needs_architecture_policy": True,
        "recommended_handling": "policy_should_not_flag_when_primary_getter_is_preserved_and_possessive_is_rephrased",
        "rationale": "The visible [diarch.GetSheHe|U] getter remains present. The possessive 'your/tus' is rephrased as 'para você', so this is a false-positive style case for omitted perspective.",
    },
    118731: {
        "omission_role": "possessive",
        "operational_subtype": "possessive_lexicalized_output_ok",
        "omitted_source_surface": "[illuminator.GetHerHis]",
        "output_preserves_meaning": True,
        "needs_human_packet": False,
        "needs_architecture_policy": True,
        "recommended_handling": "policy_allows_possessive_lexicalization_when_ptbr_possessive_is_non_gendered",
        "rationale": "The possessive getter modifies 'illuminations'; output uses 'suas ilustrações'. PT-BR possessive does not need a runtime gender token here.",
    },
    162874: {
        "omission_role": "possessive",
        "operational_subtype": "possessive_lexicalized_output_ok",
        "omitted_source_surface": "[religious_leader.GetHerHis]",
        "output_preserves_meaning": True,
        "needs_human_packet": False,
        "needs_architecture_policy": True,
        "recommended_handling": "policy_allows_possessive_lexicalization_when_ptbr_possessive_is_non_gendered",
        "rationale": "The possessive getter modifies faith; output uses 'a sua fé'. The semantic possessive is retained without a gendered runtime token.",
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


def fetch_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing registry agent: {AGENT_KEY}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    return {
        "exists": True,
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "status": payload.get("status"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "parent_agent_key": payload.get("parent_agent_key"),
        "scope_group": payload.get("scope_group"),
        "dashboard_group": payload.get("dashboard_group"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
    }


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_architecture_parser_routing_dry_run":
        raise SystemExit("summary mode guard failed")
    if summary.get("registry_guard_ok") is not True:
        raise SystemExit("registry guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production_full_recommended_now guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")
    selected = [row for row in rows if row.get("target_policy") == AGENT_KEY]
    if len(selected) != EXPECTED_COUNT:
        raise SystemExit(f"selected count guard failed: {len(selected)}")
    selected_ids = {int(row["segment_id"]) for row in selected}
    if selected_ids != set(MANUAL_CLASSIFICATIONS):
        raise SystemExit(f"segment id guard failed: {sorted(selected_ids)}")
    return selected


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    classification = MANUAL_CLASSIFICATIONS[segment_id]
    return {
        **row,
        **classification,
        "agent_key": AGENT_KEY,
        "review_mode": "read_only_getter_perspective_omitted_review",
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "recommended_next_step": (
            "architecture_policy_note"
            if classification["needs_architecture_policy"] and not classification["needs_human_packet"]
            else "human_packet_future"
        ),
    }


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light getter perspective omitted review",
        "",
        f"agent_key: {AGENT_KEY}",
        f"review_count: {summary['review_count']}",
        "",
        "operational_subtype_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["operational_subtype_counts"])
    lines.extend(["", "omission_role_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["omission_role_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['operational_subtype']}",
                f"- source_key: {row['source_key']}",
                f"- path: {row['relative_path']}",
                f"- omitted_source_surface: {row['omitted_source_surface']}",
                f"- dynamic_tokens: {json.dumps(row.get('dynamic_tokens') or [], ensure_ascii=False)}",
                f"- english: {row['english_text']}",
                f"- spanish: {row['spanish_text']}",
                f"- output_ptbr: {row['current_output_text']}",
                f"- rationale: {row['rationale']}",
                f"- recommended_handling: {row['recommended_handling']}",
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
        registry = fetch_registry(conn)
    reviewed = [review_row(row) for row in selected]
    reviewed.sort(key=lambda row: (row["operational_subtype"], row["segment_id"]))

    subtype_counts = Counter(row["operational_subtype"] for row in reviewed)
    role_counts = Counter(row["omission_role"] for row in reviewed)
    handling_counts = Counter(row["recommended_handling"] for row in reviewed)
    next_step_counts = Counter(row["recommended_next_step"] for row in reviewed)
    output_ok_count = sum(1 for row in reviewed if row["output_preserves_meaning"])
    human_packet_count = sum(1 for row in reviewed if row["needs_human_packet"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_getter_perspective_omitted_review",
        "agent_key": AGENT_KEY,
        "input_path": str(INPUT_PATH),
        "input_summary": str(INPUT_SUMMARY),
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "registry": registry,
        "omission_role_counts": top_counter(role_counts),
        "operational_subtype_counts": top_counter(subtype_counts),
        "recommended_handling_counts": top_counter(handling_counts),
        "recommended_next_step_counts": top_counter(next_step_counts),
        "output_preserves_meaning_count": output_ok_count,
        "needs_human_packet_count": human_packet_count,
        "architecture_policy_note_count": len(reviewed) - human_packet_count,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Do not create candidates from these 5. Update the shadow policy notes to treat PT-BR null subject and non-gendered possessive lexicalization as allowed read-only classifications, and mark one primary-getter-present case as a false-positive guard.",
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_medium_dynamic_light_getter_perspective_omitted_review"
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
