from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_parameter_modifier_remaining_status_v1"
INPUT_PATTERN = "*_culture_religion_pending_readonly_triage.jsonl"
TARGET_LANE = "parameter_or_modifier_label"
PREVIOUS_SAMPLE_PATTERNS = [
    "*_culture_religion_parameter_modifier_readonly_sample.jsonl",
    "*_culture_religion_parameter_modifier_batch2_readonly_sample.jsonl",
    "*_culture_religion_parameter_modifier_batch4_readonly_sample.jsonl",
    "*_culture_religion_parameter_modifier_batch5_readonly_sample.jsonl",
    "*_culture_religion_parameter_modifier_batch6_readonly_sample.jsonl",
]
KNOWN_HOLD_SEGMENTS = {
    20333,
    20392,
    20692,
    20721,
    21002,
    21003,
    21004,
    21426,
    21349,
    21350,
    239966,
    239970,
    239972,
    240136,
    240140,
    240155,
    240178,
    240221,
    240012,
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_path(pattern: str) -> Path:
    matches = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise SystemExit(f"missing report for pattern {pattern}")
    return matches[0]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def previous_sample_ids() -> tuple[set[int], list[str]]:
    segment_ids: set[int] = set()
    paths: list[str] = []
    for pattern in PREVIOUS_SAMPLE_PATTERNS:
        for path in sorted(reports_dir().glob(pattern), key=lambda item: item.stat().st_mtime):
            paths.append(str(path))
            for row in read_jsonl(path):
                segment_id = int(row.get("segment_id") or 0)
                if segment_id:
                    segment_ids.add(segment_id)
    return segment_ids, paths


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def latest_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS run_id FROM segment_state_runs").fetchone()
    if not row or row["run_id"] is None:
        raise SystemExit("missing segment_state_runs")
    return int(row["run_id"])


def state_snapshot(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            state.segment_id,
            state.final_state,
            state.state_group,
            state.review_state,
            state.apply_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.needs_human,
            conf.confirmation_level,
            conf.confirmation_source,
            conf.locked,
            learned.local_status AS learning_status,
            learned.run_id AS learning_run_id,
            learned.learned_at AS learned_at,
            learned.confirmation_synced_at AS confirmation_synced_at
        FROM segment_state_items state
        LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
        LEFT JOIN (
            SELECT
                segment_id,
                MAX(run_id) AS run_id,
                MAX(local_status) AS local_status,
                MAX(learned_at) AS learned_at,
                MAX(confirmation_synced_at) AS confirmation_synced_at
            FROM local_learning_candidates
            GROUP BY segment_id
        ) learned ON learned.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def bucket(row: dict[str, Any]) -> str:
    key = str(row.get("source_key") or "")
    text = str(row.get("current_output_text") or "")
    if "$knight_culture_player_plural$" in text:
        return "hold_context_knight_culture"
    if "can_enact" in key or "succession_enabled" in key or "inheritance" in key:
        return "law_unlock_parameter"
    if "trait" in key or "GetTrait" in text:
        return "trait_parameter"
    if "building" in key or "holding" in key or "holdings" in text:
        return "holding_building_parameter"
    if "opinion" in key or "opinion" in text:
        return "opinion_parameter"
    if "knight" in key or "commander" in key or "martial" in key:
        return "martial_knight_parameter"
    if "doctrine" in key or str(row.get("relative_path") or "").startswith("religion/"):
        return "religion_doctrine_parameter"
    return "general_parameter"


def risk_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("current_output_text") or "")
    token_count = int(row.get("token_count") or 0)
    segment_id = int(row["segment_id"])
    if segment_id in KNOWN_HOLD_SEGMENTS:
        return "known_hold", "known hold/correction candidate"
    if "$knight_culture_player_plural$" in text:
        return "known_hold", "$knight_culture_player_plural$ context-risk"
    if "Concept(" in text:
        return "hold_context", "Concept token content needs explicit review"
    if "revocar" in text or "gobierno" in text:
        return "semantic_or_language_correction", "visible Spanish residual/correction needed"
    if "homens" in text or "mulheres" in text:
        return "hold_context", "visible gender/plural wording needs context"
    if token_count >= 6:
        return "needs_human_review", "dense protected-token surface"
    return "human_review", "domain parameter text needs confirmation before learning"


def classify(row: dict[str, Any], state: dict[str, Any], previous_ids: set[int]) -> tuple[str, str]:
    segment_id = int(row["segment_id"])
    if segment_id in KNOWN_HOLD_SEGMENTS:
        return "excluded_known_hold", "known hold/correction candidate"
    if segment_id in previous_ids:
        return "excluded_previous_sample", "already shown in prior sample"
    if state.get("state_group") != "pending":
        return "excluded_not_pending_latest_state", str(state.get("final_state"))
    if int(state.get("needs_output_apply") or 0) != 0:
        return "excluded_needs_output_apply", "requires output apply"
    if state.get("confirmation_level") in {"human_confirmed", "human"} or int(state.get("locked") or 0) == 1:
        return "excluded_already_confirmed_or_locked", str(state.get("confirmation_level"))
    if state.get("learning_status") in {"learned", "applied", "synced"} or state.get("learned_at") or state.get("confirmation_synced_at"):
        return "excluded_already_learned", str(state.get("learning_run_id"))
    return "eligible", "pending and not previously sampled/held/learned"


def short(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def main() -> None:
    input_path = latest_path(INPUT_PATTERN)
    rows = [row for row in read_jsonl(input_path) if row.get("lane") == TARGET_LANE]
    previous_ids, previous_paths = previous_sample_ids()
    segment_ids = [int(row["segment_id"]) for row in rows]

    with connect_readonly() as conn:
        run_id = latest_state_run_id(conn)
        state_by_segment = state_snapshot(conn, segment_ids, run_id)

    records: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    eligible_bucket_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    eligible_by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        segment_id = int(row["segment_id"])
        state = state_by_segment.get(segment_id, {})
        status, status_reason = classify(row, state, previous_ids)
        row_bucket = bucket(row)
        decision, decision_reason = risk_decision(row)
        status_counts[status] += 1
        bucket_counts[row_bucket] += 1
        if status == "eligible":
            eligible_bucket_counts[row_bucket] += 1
            risk_counts[decision] += 1
            if len(eligible_by_bucket[row_bucket]) < 8:
                eligible_by_bucket[row_bucket].append(
                    {
                        "segment_id": segment_id,
                        "source_key": row.get("source_key"),
                        "bucket": row_bucket,
                        "risk_decision": decision,
                        "risk_reason": decision_reason,
                        "token_count": row.get("token_count"),
                        "text": short(row.get("current_output_text")),
                        "english": short(row.get("english_text")),
                    }
                )
        records.append(
            {
                "segment_id": segment_id,
                "source_key": row.get("source_key"),
                "bucket": row_bucket,
                "status": status,
                "status_reason": status_reason,
                "risk_decision": decision,
                "risk_reason": decision_reason,
                "latest_final_state": state.get("final_state"),
                "latest_state_group": state.get("state_group"),
                "latest_needs_output_apply": int(state.get("needs_output_apply") or 0),
                "confirmation_level": state.get("confirmation_level"),
                "confirmation_locked": int(state.get("locked") or 0),
                "learning_run_id": state.get("learning_run_id"),
            }
        )

    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_remaining_status"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_triage_jsonl": str(input_path),
        "latest_segment_state_run_id": run_id,
        "target_lane": TARGET_LANE,
        "total_lane_count": len(rows),
        "previous_sample_count": len(previous_ids),
        "known_hold_count": len(KNOWN_HOLD_SEGMENTS),
        "status_counts": dict(status_counts),
        "bucket_counts": dict(bucket_counts),
        "eligible_bucket_counts": dict(eligible_bucket_counts),
        "eligible_risk_counts": dict(risk_counts),
        "eligible_examples_by_bucket": eligible_by_bucket,
        "previous_sample_jsonl_paths": previous_paths,
        "read_only": True,
        "candidate_generation_executed": False,
        "apply_executed": False,
        "recommended_next_step": "continue_sampling_only_if_human_review_capacity_exists_else_shift_to_hold_corrections",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "culture/religion parameter modifier remaining status",
        f"source={RULE_VERSION}",
        f"input_triage_jsonl={input_path}",
        f"latest_segment_state_run_id={run_id}",
        f"target_lane={TARGET_LANE}",
        f"total_lane_count={len(rows)}",
        "",
        "status_counts:",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "eligible_bucket_counts:",
        *[f"- {key}: {value}" for key, value in eligible_bucket_counts.most_common()],
        "",
        "eligible_risk_counts:",
        *[f"- {key}: {value}" for key, value in risk_counts.most_common()],
        "",
        "eligible_examples_by_bucket:",
    ]
    for key, examples in sorted(eligible_by_bucket.items()):
        lines.append(f"- {key}:")
        for example in examples:
            lines.extend(
                [
                    f"  - {example['segment_id']} | {example['risk_decision']} | {example['source_key']}",
                    f"    text: {example['text']}",
                ]
            )
    lines.extend(
        [
            "",
            "execution_flags:",
            "- read_only=true",
            "- candidate_generation_executed=false",
            "- apply_executed=false",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"latest_segment_state_run_id={run_id}")
    print("status_counts=" + json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True))
    print("eligible_bucket_counts=" + json.dumps(dict(eligible_bucket_counts), ensure_ascii=False, sort_keys=True))
    print("eligible_risk_counts=" + json.dumps(dict(risk_counts), ensure_ascii=False, sort_keys=True))
    print("candidate_generation_executed=False")
    print("apply_executed=False")


if __name__ == "__main__":
    main()
