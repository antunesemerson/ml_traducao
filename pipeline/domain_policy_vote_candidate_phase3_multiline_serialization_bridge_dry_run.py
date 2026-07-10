from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "domain_policy_vote_candidate_phase3_multiline_serialization_bridge_dry_run_v1"
DEFAULT_MULTILINE_JSONL = Path(
    "reports/20260702_120329_309821_domain_policy_vote_candidate_phase3_multiline_hold_diagnostic.jsonl"
)
DEFAULT_PHASE3_JSONL = Path(
    "reports/20260702_114424_125868_domain_policy_vote_candidate_phase3_human_misc_closure_diagnostic.jsonl"
)
EXPECTED_COUNT = 1118
POLICY_NAME = "human_confirmed_misc_equal_output_multiline_serialization_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_misc_equal_output_multiline_serialization_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_misc_equal_output_multiline_serialization_lifecycle"
ALLOWED_CLASSES = {"serialization_only_candidate", "serialization_equal_ui_block"}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only dry-run for phase3 multiline serialization lifecycle bridge."
    )
    parser.add_argument("--multiline-jsonl", type=Path, default=DEFAULT_MULTILINE_JSONL)
    parser.add_argument("--phase3-jsonl", type=Path, default=DEFAULT_PHASE3_JSONL)
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def unescape_l10n(value: str | None) -> str:
    return str(value or "").replace('\\"', '"').replace("\\n", "\n")


def equal_after_allowed_serialization(output_text: str, confirmed_text: str) -> bool:
    return (
        canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
        or unescape_l10n(output_text) == unescape_l10n(confirmed_text)
    )


def int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def latest_segment_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs").fetchone()
    if row is None or row["id"] is None:
        raise SystemExit("missing segment_state_runs")
    return int(row["id"])


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    states: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(segment_ids), 800):
        chunk = segment_ids[offset : offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT *
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            """,
            [run_id, *chunk],
        ).fetchall()
        states.update({int(row["segment_id"]): dict(row) for row in rows})
    return states


def evaluate(row: dict[str, Any], phase3: dict[str, Any] | None, state: dict[str, Any] | None) -> dict[str, Any]:
    reasons: list[str] = []
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")

    if row.get("record_type") != "phase3_multiline_hold_row":
        reasons.append("not_phase3_multiline_hold_row")
    if row.get("classification") not in ALLOWED_CLASSES:
        reasons.append("classification_not_allowed")
    if row.get("bridge_candidate_later") is not True:
        reasons.append("not_bridge_candidate_later")
    if int_value(row.get("has_effect_or_debug_marker")) != 0:
        reasons.append("effect_or_debug_marker_present")
    if int_value(row.get("hold_or_architecture_later")) != 0:
        reasons.append("hold_or_architecture_later")
    if not equal_after_allowed_serialization(output_text, confirmed_text):
        reasons.append("serialization_equivalence_not_confirmed")

    if phase3 is None:
        reasons.append("missing_phase3_guard_row")
    else:
        if phase3.get("operational_bucket") != "multiline_hold_or_later_split":
            reasons.append("not_multiline_operational_bucket")
        if phase3.get("confirmation_level") != "human_confirmed":
            reasons.append("not_human_confirmed")
        if int_value(phase3.get("open_issue_count")) != 0:
            reasons.append("open_issue_count_not_0")
        if int_value(phase3.get("high_issue_count")) != 0:
            reasons.append("high_issue_count_not_0")

    if state is None:
        reasons.append("missing_segment_state_guard_row")
    else:
        if state.get("confirmation_level") != "human_confirmed":
            reasons.append("state_not_human_confirmed")
        if int_value(state.get("confirmed_matches_output")) != 1:
            reasons.append("confirmed_matches_output_not_1")
        if int_value(state.get("needs_output_apply")) != 0:
            reasons.append("needs_output_apply_not_0")
        if state.get("final_state") != "reopen_auto_confirmed_autofix":
            reasons.append("final_state_not_reopen_auto_confirmed_autofix")

    released = not reasons
    return {
        "source": SOURCE,
        "record_type": "phase3_multiline_serialization_bridge_dry_run_row",
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "classification": row.get("classification"),
        "token_surface": row.get("token_surface"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int_value(row.get("locked")),
        "output_newline_profile": row.get("output_newline_profile"),
        "confirmed_newline_profile": row.get("confirmed_newline_profile"),
        "canonical_l10n_equal": int_value(row.get("canonical_l10n_equal")),
        "unescaped_text_equal": int_value(row.get("unescaped_text_equal")),
        "has_ui_block_marker": int_value(row.get("has_ui_block_marker")),
        "has_effect_or_debug_marker": int_value(row.get("has_effect_or_debug_marker")),
        "phase3_open_issue_count": int_value((phase3 or {}).get("open_issue_count")),
        "phase3_high_issue_count": int_value((phase3 or {}).get("high_issue_count")),
        "segment_state_run_id": int_value((state or {}).get("run_id")),
        "state_confirmed_matches_output": int_value((state or {}).get("confirmed_matches_output")),
        "state_needs_output_apply": int_value((state or {}).get("needs_output_apply")),
        "state_final_state": (state or {}).get("final_state"),
        "state_group": (state or {}).get("state_group"),
        "dry_run_decision": "released" if released else "blocked",
        "block_reasons": reasons,
        "recommended_policy_name": POLICY_NAME,
        "recommended_policy_action": POLICY_ACTION,
        "recommended_final_state": FINAL_STATE,
        "candidate_generation_allowed": False,
        "apply_allowed": False,
        "lifecycle_run_allowed_now": False,
        "segment_state_allowed_now": False,
        "reindex_allowed": False,
        "production_full_allowed": False,
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    multiline_rows = read_jsonl(args.multiline_jsonl)
    phase3_by_id = {int(row["segment_id"]): row for row in read_jsonl(args.phase3_jsonl)}

    focus = [
        row
        for row in multiline_rows
        if row.get("classification") in ALLOWED_CLASSES and row.get("bridge_candidate_later") is True
    ]
    conn = db.connect()
    segment_state_run_id = args.segment_state_run_id or latest_segment_state_run_id(conn)
    state_by_id = fetch_states(conn, segment_state_run_id, [int(row["segment_id"]) for row in focus])
    records = [
        evaluate(row, phase3_by_id.get(int(row["segment_id"])), state_by_id.get(int(row["segment_id"])))
        for row in focus
    ]

    decisions = Counter(record["dry_run_decision"] for record in records)
    block_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for reason in record["block_reasons"] or ["released"]:
            block_counts[reason] += 1
            if len(samples[reason]) < 8:
                samples[reason].append(record)

    classification_counts = Counter(str(record["classification"]) for record in records)
    newline_counts = Counter(
        f"{record['output_newline_profile']} -> {record['confirmed_newline_profile']}" for record in records
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_multiline_serialization_bridge_dry_run",
        "input_multiline_jsonl": str(args.multiline_jsonl),
        "input_phase3_jsonl": str(args.phase3_jsonl),
        "segment_state_run_id": segment_state_run_id,
        "record_count": len(records),
        "expected_record_count": args.expected_count,
        "released_count": decisions.get("released", 0),
        "blocked_count": decisions.get("blocked", 0),
        "consumer_supported_now_count": decisions.get("released", 0),
        "classification_counts": dict(classification_counts.most_common()),
        "newline_profile_transition_counts": dict(newline_counts.most_common()),
        "confirmation_source_counts": dict(
            Counter(str(record["confirmation_source"]) for record in records).most_common(50)
        ),
        "confirmation_label_counts": dict(
            Counter(str(record["confirmation_label"]) for record in records).most_common(50)
        ),
        "block_reason_counts": dict(block_counts.most_common()),
        "samples_by_reason": dict(samples),
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "recommended_final_state": FINAL_STATE,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "If released_count equals record_count and blocked_count is 0, materialize only this lifecycle bridge, "
            "then run segment-state and a delta. Keep effect/debug multiline holds outside this cycle."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_multiline_serialization_bridge_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Phase3 multiline serialization bridge dry-run",
                f"record_count={summary['record_count']}",
                f"expected_record_count={summary['expected_record_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"consumer_supported_now_count={summary['consumer_supported_now_count']}",
                f"classification_counts={json.dumps(summary['classification_counts'], ensure_ascii=False, sort_keys=True)}",
                f"newline_profile_transition_counts={json.dumps(summary['newline_profile_transition_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
                "",
                "Recommendation:",
                summary["single_operational_recommendation"],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    if summary["record_count"] != args.expected_count:
        raise SystemExit(f"record count guard failed: {summary['record_count']} != {args.expected_count}")
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"consumer_supported_now_count={summary['consumer_supported_now_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
