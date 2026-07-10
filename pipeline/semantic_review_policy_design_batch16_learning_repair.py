from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import apply_local_learning_feedback as feedback
import db


RULE_VERSION = "semantic_review_policy_design_batch16_learning_repair_v1"
TARGET_CANDIDATE_IDS = (23586, 23589)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_rows(conn) -> list[Any]:
    placeholders = ",".join("?" for _ in TARGET_CANDIDATE_IDS)
    return conn.execute(
        f"""
        SELECT
            ll.id,
            ll.run_id,
            ll.segment_id,
            ll.human_label,
            ll.suggested_text,
            ll.corrected_text,
            ll.origin,
            ll.match_type,
            ll.suggestion_status,
            ll.token_status,
            ll.source_language,
            ll.queue_source,
            ll.focus_group,
            ll.reasons_json,
            ll.learned_at,
            ll.confirmation_synced_at,
            c.confirmed_text,
            c.locked,
            o.portuguese_text AS output_text
        FROM local_learning_candidates ll
        LEFT JOIN segment_confirmations c ON c.segment_id = ll.segment_id
        LEFT JOIN output_segments o ON o.segment_id = ll.segment_id
        WHERE ll.id IN ({placeholders})
        ORDER BY ll.id
        """,
        TARGET_CANDIDATE_IDS,
    ).fetchall()


def evaluate(row) -> dict[str, Any]:
    candidate_text = feedback.confirmation_text(row)
    reasons: list[str] = []
    if row["human_label"] != "correct":
        reasons.append("human_label_not_correct")
    if not row["learned_at"]:
        reasons.append("not_learned")
    if int(row["locked"] or 0) != 1:
        reasons.append("confirmation_not_locked")
    if not candidate_text:
        reasons.append("missing_candidate_confirmation_text")
    if candidate_text and row["confirmed_text"] == candidate_text:
        reasons.append("candidate_matches_locked_confirmation")
    if row["output_text"] == row["confirmed_text"]:
        reasons.append("output_already_matches_locked_confirmation")
    return {
        "candidate_id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "segment_id": int(row["segment_id"]),
        "status": "ready" if not reasons else "blocked",
        "block_reasons": reasons,
        "old_human_label": row["human_label"],
        "new_human_label": "superseded_by_human_correction",
        "old_local_status": None,
        "new_local_status": "blocked_locked_confirmation_mismatch",
        "candidate_text": candidate_text,
        "locked_confirmed_text": row["confirmed_text"],
        "output_text": row["output_text"],
        "pattern_keys": feedback.pattern_keys(row),
    }


def apply_repair(conn, records: list[dict[str, Any]]) -> Counter[str]:
    timestamp = now()
    decrements: Counter[str] = Counter()
    for record in records:
        for key in record["pattern_keys"]:
            decrements[key] += 1
        conn.execute(
            """
            UPDATE local_learning_candidates
            SET
                human_label = 'superseded_by_human_correction',
                local_status = 'blocked_locked_confirmation_mismatch',
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, record["candidate_id"]),
        )
    for key, decrement in decrements.items():
        stat = conn.execute(
            """
            SELECT *
            FROM local_learning_pattern_stats
            WHERE pattern_key = ?
            """,
            (key,),
        ).fetchone()
        if not stat:
            continue
        positive = max(int(stat["positive_count"] or 0) - decrement, 0)
        near_positive = int(stat["near_positive_count"] or 0)
        partial = int(stat["partial_count"] or 0)
        negative = int(stat["negative_count"] or 0)
        harmful = int(stat["harmful_count"] or 0)
        total = positive + near_positive + partial + negative + harmful
        conn.execute(
            """
            UPDATE local_learning_pattern_stats
            SET
                positive_count = ?,
                total_count = ?,
                weight_adjustment = ?,
                last_label = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                positive,
                total,
                feedback.compute_weight_adjustment(positive, near_positive, partial, negative, harmful),
                "superseded_by_human_correction",
                timestamp,
                stat["id"],
            ),
        )
    return decrements


def write_reports(records: list[dict[str, Any]], mode: str, decrements: Counter[str]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_policy_design_batch16_learning_repair_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    ready_count = sum(1 for record in records if record["status"] == "ready")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": now(),
        "mode": mode,
        "target_candidate_ids": list(TARGET_CANDIDATE_IDS),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "repaired_count": ready_count if mode == "apply" and blocked_count == 0 else 0,
        "pattern_stat_decrements": dict(sorted(decrements.items())),
        "source_changed": False,
        "output_changed": False,
        "runs_segment_state": False,
        "runs_lifecycle": False,
        "records": records,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic review policy design batch16 learning repair",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"ready_count={ready_count}",
        f"blocked_count={blocked_count}",
        f"repaired_count={summary['repaired_count']}",
        "source_changed=false",
        "output_changed=false",
        "runs_segment_state=false",
        "runs_lifecycle=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn)
        records = [evaluate(row) for row in rows]
        if len(records) != len(TARGET_CANDIDATE_IDS):
            raise SystemExit("missing target candidates")
        blocked_count = sum(1 for record in records if record["status"] == "blocked")
        decrements: Counter[str] = Counter()
        if args.apply:
            if blocked_count:
                write_reports(records, mode, decrements)
                raise SystemExit("apply blocked")
            decrements = apply_repair(conn, records)
            conn.commit()
        txt_path, jsonl_path, summary_path = write_reports(records, mode, decrements)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"ready_count={sum(1 for record in records if record['status'] == 'ready')}")
    print(f"blocked_count={blocked_count}")
    print(f"repaired_count={sum(1 for record in records if record['status'] == 'ready') if args.apply else 0}")


if __name__ == "__main__":
    main()
