from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_parameter_modifier_batch2_readonly_sample_v1"
INPUT_PATTERN = "*_culture_religion_pending_readonly_triage.jsonl"
PREVIOUS_SAMPLE_PATTERN = "*_culture_religion_parameter_modifier_readonly_sample.jsonl"
TARGET_LANE = "parameter_or_modifier_label"
MAX_SAMPLE = 24
SAMPLE_PER_BUCKET = 6
KNOWN_HOLD_SEGMENTS = {
    21002,
    21003,
    21004,
    239966,
    239970,
    240178,
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


def preliminary_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("current_output_text") or "")
    key = str(row.get("source_key") or "")
    token_count = int(row.get("token_count") or 0)
    if int(row["segment_id"]) in KNOWN_HOLD_SEGMENTS or "$knight_culture_player_plural$" in text:
        return "hold_context", "$knight_culture_player_plural$ remains context-risk"
    if "homens" in text or "mulheres" in text:
        return "hold_context", "visible gender/plural wording needs context"
    if "Pode promulgar a" in text and "GetLaw(" in text:
        return "likely_positive_pattern", "same law-unlock pattern already human-confirmed"
    if "tem bônus adicionais" in text or "concedem mais" in text:
        return "likely_positive_pattern", "same parameter wording family already human-confirmed"
    if token_count >= 5:
        return "needs_human_review", "dense protected-token surface"
    if any(marker in key for marker in ("doctrine", "opinion", "knight", "trait", "parameter")):
        return "human_review", "domain parameter text needs confirmation before learning"
    return "human_review", "short parameter text needs confirmation before learning"


def choose_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[bucket(row)].append(row)

    selected: list[dict[str, Any]] = []
    preferred = [
        "holding_building_parameter",
        "trait_parameter",
        "law_unlock_parameter",
        "opinion_parameter",
        "religion_doctrine_parameter",
        "martial_knight_parameter",
        "general_parameter",
    ]
    ordered = preferred + sorted(set(groups) - set(preferred))
    for group_key in ordered:
        for row in groups.get(group_key, [])[:SAMPLE_PER_BUCKET]:
            selected.append(row)
            if len(selected) >= MAX_SAMPLE:
                return selected
    return selected


def build_records(rows: list[dict[str, Any]], state_by_segment: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        state = state_by_segment.get(segment_id, {})
        decision, rationale = preliminary_decision(row)
        records.append(
            {
                **row,
                "latest_final_state": state.get("final_state"),
                "latest_state_group": state.get("state_group"),
                "latest_review_state": state.get("review_state"),
                "latest_apply_state": state.get("apply_state"),
                "latest_needs_output_apply": int(state.get("needs_output_apply") or 0),
                "latest_confirmed_matches_output": int(state.get("confirmed_matches_output") or 0),
                "confirmation_level": state.get("confirmation_level"),
                "confirmation_source": state.get("confirmation_source"),
                "confirmation_locked": int(state.get("locked") or 0),
                "learning_status": state.get("learning_status"),
                "learning_run_id": state.get("learning_run_id"),
                "learned_at": state.get("learned_at"),
                "confirmation_synced_at": state.get("confirmation_synced_at"),
                "sample_bucket": bucket(row),
                "preliminary_decision": decision,
                "preliminary_rationale": rationale,
            }
        )
    return records


def write_outputs(
    input_path: Path,
    previous_sample_path: Path,
    latest_run_id: int,
    records: list[dict[str, Any]],
    stats: dict[str, Any],
) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_batch2_readonly_sample"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    bucket_counts = Counter(record["sample_bucket"] for record in records)
    decision_counts = Counter(record["preliminary_decision"] for record in records)
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_triage_jsonl": str(input_path),
        "previous_sample_jsonl": str(previous_sample_path),
        "latest_segment_state_run_id": latest_run_id,
        "target_lane": TARGET_LANE,
        "sample_count": len(records),
        "bucket_counts": dict(bucket_counts),
        "preliminary_decision_counts": dict(decision_counts),
        "stats": stats,
        "read_only": True,
        "candidate_generation_executed": False,
        "apply_executed": False,
        "recommended_next_step": "review_batch2_with_user_then_ingest_only_confirmed_learning_signals",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "culture/religion parameter modifier batch2 read-only sample",
        f"source={RULE_VERSION}",
        f"input_triage_jsonl={input_path}",
        f"previous_sample_jsonl={previous_sample_path}",
        f"latest_segment_state_run_id={latest_run_id}",
        f"target_lane={TARGET_LANE}",
        "",
        "stats:",
        *[f"- {key}: {value}" for key, value in stats.items()],
        "",
        "preliminary_decision_counts:",
        *[f"- {key}: {value}" for key, value in decision_counts.most_common()],
        "",
        "bucket_counts:",
        *[f"- {key}: {value}" for key, value in bucket_counts.most_common()],
        "",
        "sample:",
    ]
    for record in records:
        lines.extend(
            [
                f"- {record['segment_id']} | {record['sample_bucket']} | {record['preliminary_decision']}",
                f"  key: {record['source_key']}",
                f"  text: {record['current_output_text']}",
                f"  english: {record['english_text']}",
                f"  rationale: {record['preliminary_rationale']}",
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
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_path(INPUT_PATTERN)
    previous_sample_path = latest_path(PREVIOUS_SAMPLE_PATTERN)
    rows = [row for row in read_jsonl(input_path) if row.get("lane") == TARGET_LANE]
    previous_ids = {int(row["segment_id"]) for row in read_jsonl(previous_sample_path)}
    segment_ids = [int(row["segment_id"]) for row in rows]

    with connect_readonly() as conn:
        run_id = latest_state_run_id(conn)
        state_by_segment = state_snapshot(conn, segment_ids, run_id)

    eligible: list[dict[str, Any]] = []
    excluded_counts: Counter[str] = Counter()
    for row in rows:
        segment_id = int(row["segment_id"])
        state = state_by_segment.get(segment_id, {})
        if segment_id in previous_ids:
            excluded_counts["previous_sample"] += 1
            continue
        if state.get("state_group") != "pending":
            excluded_counts["not_pending_latest_state"] += 1
            continue
        if int(state.get("needs_output_apply") or 0) != 0:
            excluded_counts["needs_output_apply"] += 1
            continue
        if state.get("confirmation_level") in {"human_confirmed", "human"} or int(state.get("locked") or 0) == 1:
            excluded_counts["already_confirmed_or_locked"] += 1
            continue
        if state.get("learning_status") in {"learned", "applied", "synced"} or state.get("learned_at") or state.get("confirmation_synced_at"):
            excluded_counts["already_learned"] += 1
            continue
        eligible.append(row)

    sample = choose_sample(eligible)
    records = build_records(sample, state_by_segment)
    stats = {
        "total_lane_count": len(rows),
        "previous_sample_count": len(previous_ids),
        "eligible_after_exclusions": len(eligible),
        **{f"excluded_{key}": value for key, value in sorted(excluded_counts.items())},
    }
    txt_path, jsonl_path, summary_path = write_outputs(input_path, previous_sample_path, run_id, records, stats)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"latest_segment_state_run_id={run_id}")
    print("stats=" + json.dumps(stats, ensure_ascii=False, sort_keys=True))
    print("decision_counts=" + json.dumps(dict(Counter(r["preliminary_decision"] for r in records)), ensure_ascii=False, sort_keys=True))
    print("candidate_generation_executed=False")
    print("apply_executed=False")


if __name__ == "__main__":
    main()
