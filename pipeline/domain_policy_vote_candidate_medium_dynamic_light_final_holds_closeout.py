from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_final_holds_closeout_v1"
INPUT_PATH = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run.jsonl")
INPUT_SUMMARY = Path("reports/20260630_001739_361277_medium_dynamic_light_architecture_parser_routing_dry_run_summary.json")
EXPECTED_COUNT = 3

MANUAL_CLASSIFICATIONS: dict[int, dict[str, Any]] = {
    237775: {
        "hold_group": "PantheonTerm",
        "hold_reason": "requires_runtime_number_agreement",
        "policy_now": False,
        "human_packet_now": False,
        "parser_needed": True,
        "recommended_state": "explicit_hold_until_pantheonterm_number_policy",
        "rationale": "The output chooses plural agreement ('exijam'), but PantheonTerm runtime expansion may vary by faith and number. Needs a dedicated agreement policy before any repair.",
    },
    238052: {
        "hold_group": "PantheonTerm",
        "hold_reason": "requires_runtime_number_agreement",
        "policy_now": False,
        "human_packet_now": False,
        "parser_needed": True,
        "recommended_state": "explicit_hold_until_pantheonterm_number_policy",
        "rationale": "The output uses singular agreement ('nos quis'), but PantheonTerm may expand to plural/divine collective depending on faith. Needs number behavior evidence.",
    },
    126280: {
        "hold_group": "relation_or_possessive",
        "hold_reason": "requires_relation_discourse_context",
        "policy_now": False,
        "human_packet_now": False,
        "parser_needed": False,
        "recommended_state": "hold_collect_more_relation_perspective_signal",
        "rationale": "Custom2('RelationToMe') encodes relation to player/scope and the surrounding utterance uses first-person discourse. One example is not enough for a subpolicy.",
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


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_architecture_parser_routing_dry_run":
        raise SystemExit("summary mode guard failed")
    if summary.get("registry_guard_ok") is not True:
        raise SystemExit("registry guard failed")
    if int(summary.get("pantheonterm_hold_count") or 0) != 2:
        raise SystemExit("pantheonterm_hold_count guard failed")
    if int(summary.get("relation_or_possessive_hold_count") or 0) != 1:
        raise SystemExit("relation_or_possessive_hold_count guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")
    selected = [
        row
        for row in rows
        if row.get("target_policy") in {"explicit_pantheonterm_agreement_hold", "relation_perspective_hold"}
    ]
    if len(selected) != EXPECTED_COUNT:
        raise SystemExit(f"selected count guard failed: {len(selected)}")
    selected_ids = {int(row["segment_id"]) for row in selected}
    if selected_ids != set(MANUAL_CLASSIFICATIONS):
        raise SystemExit(f"segment id guard failed: {sorted(selected_ids)}")
    return selected


def closeout_row(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    classification = MANUAL_CLASSIFICATIONS[segment_id]
    return {
        **row,
        **classification,
        "closeout_mode": "read_only_final_hold_closeout",
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
    }


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "medium_dynamic_light final holds closeout",
        "",
        f"hold_count: {summary['hold_count']}",
        "",
        "hold_group_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["hold_group_counts"])
    lines.extend(["", "recommended_state_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["recommended_state_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['hold_group']}",
                f"- source_key: {row['source_key']}",
                f"- path: {row['relative_path']}",
                f"- hold_reason: {row['hold_reason']}",
                f"- recommended_state: {row['recommended_state']}",
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
    rows = [closeout_row(row) for row in selected]
    rows.sort(key=lambda row: (row["hold_group"], row["segment_id"]))

    group_counts = Counter(row["hold_group"] for row in rows)
    reason_counts = Counter(row["hold_reason"] for row in rows)
    state_counts = Counter(row["recommended_state"] for row in rows)
    policy_now_count = sum(1 for row in rows if row["policy_now"])
    human_packet_now_count = sum(1 for row in rows if row["human_packet_now"])
    parser_needed_count = sum(1 for row in rows if row["parser_needed"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_final_hold_closeout",
        "input_path": str(INPUT_PATH),
        "input_summary": str(INPUT_SUMMARY),
        "hold_count": len(rows),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(rows) == EXPECTED_COUNT,
        "hold_group_counts": top_counter(group_counts),
        "hold_reason_counts": top_counter(reason_counts),
        "recommended_state_counts": top_counter(state_counts),
        "policy_now_count": policy_now_count,
        "human_packet_now_count": human_packet_now_count,
        "parser_needed_count": parser_needed_count,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Close this residual micro-block as explicit hold: keep 2 PantheonTerm cases parked until a runtime number/agreement policy exists, and keep 1 relation_or_possessive case parked until more relation/perspective examples accumulate.",
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_medium_dynamic_light_final_holds_closeout"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
