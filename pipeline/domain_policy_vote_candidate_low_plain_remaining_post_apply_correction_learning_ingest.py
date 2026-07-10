from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "domain_policy_vote_candidate_low_plain_remaining_post_apply_correction_learning_ingest_v1"
DECISIONS_PATH = Path("reports/20260629_111743_502978_domain_policy_vote_candidate_low_plain_remaining_decisions_consolidated.jsonl")
APPLY_POST_VALIDATION_PATH = Path("reports/20260629_112359_808377_domain_policy_vote_candidate_low_plain_remaining_apply_post_validation_summary.json")
QUEUE_SOURCE = "domain-policy-vote-candidate-low-plain-remaining-post-apply"
ORIGIN = "human_confirmed_domain_policy_vote_candidate_low_plain_remaining_post_apply"
MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_low_plain_remaining_post_apply_correction"
REVIEWER = "user_human_review_domain_policy_vote_candidate_low_plain_remaining"
FOCUS_GROUP = "domain_policy_vote_candidate_low_plain_remaining"
SEGMENT_STATE_RUN_ID = 493


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_corrections() -> dict[int, str]:
    corrections: dict[int, str] = {}
    with DECISIONS_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("human_decision") == "approve_correction":
                segment_id = int(record["segment_id"])
                corrected = str(record.get("corrected_text") or "")
                if not corrected:
                    raise SystemExit(f"missing corrected_text for {segment_id}")
                corrections[segment_id] = corrected
    return dict(sorted(corrections.items()))


def load_post_validation_ok_ids() -> set[int]:
    payload = json.loads(APPLY_POST_VALIDATION_PATH.read_text(encoding="utf-8"))
    return {int(record["segment_id"]) for record in payload["post_validation"] if record["status"] == "ok"}


def fetch_context(conn, segment_id: int):
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            c.confirmed_text,
            c.locked,
            state.state_group,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id = ?
        """,
        (SEGMENT_STATE_RUN_ID, segment_id),
    ).fetchone()


def fetch_snapshot(conn, segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_confirmations WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_state_items": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_state_items WHERE run_id = ? AND segment_id IN ({placeholders}) ORDER BY segment_id",
                (SEGMENT_STATE_RUN_ID, *segment_ids),
            )
        ],
    }


def existing_candidate(conn, segment_id: int, suggested_hash: str):
    return conn.execute(
        """
        SELECT id
        FROM local_learning_candidates
        WHERE segment_id = ?
          AND suggested_hash = ?
          AND queue_source = ?
          AND origin = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (segment_id, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()


def create_run(conn, timestamp: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode, limit_count, auto_confidence_threshold, candidate_count,
            high_confidence_count, pending_human_count, status, notes,
            started_at, finished_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            QUEUE_SOURCE,
            0,
            1.0,
            0,
            0,
            0,
            "completed",
            f"{RULE_VERSION}; post_apply_output_equals_corrected; no_source_output_write",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def validate_context(context, corrected: str, post_validation_ok_ids: set[int]) -> list[str]:
    if context is None:
        return ["missing_segment"]
    reasons: list[str] = []
    segment_id = int(context["segment_id"])
    current = str(context["current_output_text"] or "")
    if segment_id not in post_validation_ok_ids:
        reasons.append("missing_apply_post_validation_ok")
    if context["state_group"] != "pending" or context["final_state"] != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_segment_state")
    if int(context["needs_output_apply"] or 0) != 0:
        reasons.append("needs_output_apply_not_zero")
    if int(context["confirmed_matches_output"] or 0) != 1:
        reasons.append("confirmed_matches_output_not_one")
    if int(context["locked"] or 0) == 1:
        reasons.append("confirmation_already_locked")
    if not current:
        reasons.append("missing_current_output_text")
    if current != corrected:
        reasons.append("post_apply_output_not_equal_corrected")
    return reasons


def insert_candidate(conn, run_id: int, context, corrected: str, timestamp: str) -> int:
    reasons = [
        RULE_VERSION,
        "domain_policy_vote_candidate_low_plain_remaining",
        "human_confirmed_in_chat",
        "protected_apply_post_validated",
        "output_equals_corrected_text",
        "no_source_write",
        "no_output_write",
    ]
    cursor = conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id, feedback_id, suggestion_id, segment_id, relative_path,
            source_key, source_line_number, english_text, spanish_text, old_text,
            current_output_text, suggested_text, suggested_hash, source_language,
            origin, match_type, match_score, token_status, suggestion_status,
            local_confidence_score, local_status, human_label, corrected_text,
            reason, reviewer, reviewed_at, reasons_json, created_at, updated_at,
            queue_source, focus_group
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(context["segment_id"]),
            context["relative_path"],
            context["source_key"],
            context["source_line_number"],
            context["english_text"],
            context["spanish_text"],
            context["old_text"],
            context["current_output_text"],
            corrected,
            sha256_text(corrected),
            "human_post_apply_correction",
            ORIGIN,
            MATCH_TYPE_CORRECTION,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved post-apply correction signal",
            REVIEWER,
            timestamp,
            json.dumps(reasons, ensure_ascii=True),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            FOCUS_GROUP,
        ),
    )
    return int(cursor.lastrowid)


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], before_snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_low_plain_remaining_post_apply_correction_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain policy vote candidate low plain remaining post-apply correction learning ingest",
        f"rule_version={RULE_VERSION}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        "source_changed=false",
        "output_changed=false",
        "production_full_recommended_now=false",
        f"next_action={summary['next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    corrections = load_corrections()
    post_validation_ok_ids = load_post_validation_ok_ids()
    segment_ids = sorted(corrections)
    timestamp = now()
    records: list[dict[str, Any]] = []
    inserted = 0
    skipped = 0
    blocked = 0
    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        before_snapshot = fetch_snapshot(conn, segment_ids)
        run_id = create_run(conn, timestamp)
        for segment_id, corrected in corrections.items():
            context = fetch_context(conn, segment_id)
            reasons = validate_context(context, corrected, post_validation_ok_ids)
            candidate_id = None
            status = "blocked" if reasons else "inserted"
            if not reasons:
                existing = existing_candidate(conn, segment_id, sha256_text(corrected))
                if existing:
                    status = "skipped_existing"
                    skipped += 1
                else:
                    candidate_id = insert_candidate(conn, run_id, context, corrected, timestamp)
                    inserted += 1
            else:
                blocked += 1
            records.append(
                {
                    "segment_id": segment_id,
                    "status": status,
                    "kind": "post_apply_correction",
                    "candidate_id": candidate_id,
                    "block_reasons": reasons,
                }
            )
        conn.execute(
            """
            UPDATE local_learning_runs
            SET limit_count = ?,
                candidate_count = ?,
                high_confidence_count = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                inserted + skipped + blocked,
                inserted,
                inserted,
                f"{RULE_VERSION}; skipped_existing={skipped}; blocked={blocked}; no_source_output_write",
                timestamp,
                run_id,
            ),
        )
        conn.commit()
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "run_id": run_id,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "inserted_count": inserted,
        "inserted_correction_count": inserted,
        "blocked_count": blocked,
        "skipped_existing_count": skipped,
        "approved_correction_segment_ids": segment_ids,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "records": records,
        "next_action": "learn_feedback_then_lifecycle_self_service_dry_run",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"before_snapshot={snapshot_path}")
    print(f"run_id={run_id}")
    print(f"inserted_count={inserted}")
    print(f"inserted_correction_count={inserted}")
    print(f"blocked_count={blocked}")
    print(f"skipped_existing_count={skipped}")
    print("source_changed=false")
    print("output_changed=false")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
