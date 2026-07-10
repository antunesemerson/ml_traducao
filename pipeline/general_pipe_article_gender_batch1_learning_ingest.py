from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "general_pipe_article_gender_batch1_learning_ingest_v1"
QUEUE_SOURCE = "general-pipe-article-gender-human-review-batch1"
ORIGIN = "human_confirmed_general_pipe_article_gender_batch1"
MATCH_TYPE_CORRECTION = "general_pipe_article_gender_human_correction"
MATCH_TYPE_ALREADY_OK = "general_pipe_article_gender_human_confirmed_already_ok"
REVIEWER = "user_human_review_general_pipe_article_gender_batch1"
FOCUS_GROUP = "general_pipe_article_gender_batch1"

APPROVED_ROWS: dict[int, dict[str, str]] = {
    20403: {"decision": "already_ok"},
    20630: {"decision": "already_ok"},
    20762: {
        "decision": "correction_needed",
        "suggested_text": "Sua [dynasty|lE] não possui o [dynasty_perk|lE] $glory_legacy_2_name$",
    },
    21019: {"decision": "already_ok"},
    60209: {"decision": "already_ok"},
    60278: {
        "decision": "correction_needed",
        "suggested_text": "Mais provável de atrair [characters|lE] estrangeiros que sejam [knight|lE]",
    },
    230843: {
        "decision": "correction_needed",
        "suggested_text": "[GetVassalStance( 'glory_hound' ).GetTextIcon][GetVassalStance( 'glory_hound' ).GetName] Aprova a [victory|lE] ofensiva",
    },
    230844: {"decision": "already_ok"},
    230845: {"decision": "already_ok"},
    239969: {"decision": "already_ok"},
}

HOLD_ROWS: dict[int, str] = {
    21002: "needs_more_context_knight_culture_player_plural",
    21003: "needs_more_context_knight_culture_player_plural",
    21004: "needs_more_context_knight_culture_player_plural",
    239966: "needs_more_context_knight_culture_player_plural",
    239970: "needs_more_context_knight_culture_player_plural",
    240178: "needs_more_context_knight_culture_player_plural",
}


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


def latest_reassessment() -> Path:
    matches = sorted(
        reports_dir().glob("*_post_acclaimed_unlock_article_gender_policy_reassessment.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing post_acclaimed_unlock_article_gender_policy_reassessment jsonl")
    return matches[0]


def read_review_rows(path: Path) -> dict[int, dict[str, Any]]:
    wanted = set(APPROVED_ROWS) | set(HOLD_ROWS)
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            segment_id = int(row.get("segment_id") or 0)
            if segment_id in wanted:
                rows[segment_id] = row
    missing = sorted(wanted - set(rows))
    if missing:
        raise SystemExit(f"missing reviewed rows in reassessment: {missing}")
    return rows


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
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (segment_id,),
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
    }


def token_guard(review_row: dict[str, Any], current: str, suggested: str) -> bool:
    tokens = [str(token) for token in review_row.get("pipe_tokens") or []]
    return all(token in current and token in suggested for token in tokens)


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


def create_run(conn, inserted_count: int, skipped_count: int, blocked_count: int, hold_count: int, timestamp: str) -> int:
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
            inserted_count + skipped_count + blocked_count + hold_count,
            1.0,
            inserted_count,
            inserted_count,
            hold_count,
            "completed",
            f"{RULE_VERSION}; skipped_existing={skipped_count}; blocked={blocked_count}; holds={hold_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(
    conn,
    run_id: int,
    context,
    review_row: dict[str, Any],
    suggested: str,
    match_type: str,
    timestamp: str,
) -> int:
    reasons = [
        "human_confirmed_general_pipe_article_gender_batch1",
        f"review_decision:{review_row.get('review_decision')}",
        f"policy_decision:{review_row.get('policy_decision')}",
        "pipe_tokens_preserved_by_guard",
        "no_source_write",
        "no_output_write",
        "approved_in_chat",
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
            suggested,
            sha256_text(suggested),
            "human_correction" if match_type == MATCH_TYPE_CORRECTION else "human_already_ok",
            ORIGIN,
            match_type,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved general pipe article/gender learning signal",
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
    base = reports_dir() / f"{stamp()}_general_pipe_article_gender_batch1_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "general pipe article/gender batch1 learning ingest",
        f"rule_version={RULE_VERSION}",
        f"input_reassessment={summary['input_reassessment']}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"inserted_corrections={summary['inserted_corrections']}",
        f"inserted_already_ok={summary['inserted_already_ok']}",
        f"hold_count={summary['hold_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    reassessment_path = latest_reassessment()
    review_rows = read_review_rows(reassessment_path)
    timestamp = now()
    records: list[dict[str, Any]] = []
    plan: list[tuple[int, dict[str, Any], Any, str, str]] = []
    skipped_existing_count = 0
    blocked_count = 0

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        segment_ids = sorted(set(APPROVED_ROWS) | set(HOLD_ROWS))
        before_snapshot = fetch_snapshot(conn, segment_ids)

        for segment_id in segment_ids:
            review_row = review_rows[segment_id]
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context"})
                continue
            current = str(context["current_output_text"] or "")
            if segment_id in HOLD_ROWS:
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "hold_not_ingested",
                        "hold_reason": HOLD_ROWS[segment_id],
                        "current_output_text": current,
                    }
                )
                continue

            approved = APPROVED_ROWS[segment_id]
            suggested = str(approved.get("suggested_text") or current)
            match_type = MATCH_TYPE_CORRECTION if approved["decision"] == "correction_needed" else MATCH_TYPE_ALREADY_OK
            if not token_guard(review_row, current, suggested):
                blocked_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "blocked_token_guard",
                        "current_output_text": current,
                        "suggested_text": suggested,
                        "pipe_tokens": review_row.get("pipe_tokens") or [],
                    }
                )
                continue

            suggested_hash = sha256_text(suggested)
            existing = existing_candidate(conn, segment_id, suggested_hash)
            if existing:
                skipped_existing_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "skipped_existing",
                        "existing_candidate_id": int(existing["id"]),
                        "decision": approved["decision"],
                    }
                )
                continue

            plan.append((segment_id, review_row, context, suggested, match_type))

        run_id = create_run(conn, len(plan), skipped_existing_count, blocked_count, len(HOLD_ROWS), timestamp)
        inserted_corrections = 0
        inserted_already_ok = 0
        for segment_id, review_row, context, suggested, match_type in plan:
            candidate_id = insert_candidate(conn, run_id, context, review_row, suggested, match_type, timestamp)
            if match_type == MATCH_TYPE_CORRECTION:
                inserted_corrections += 1
            else:
                inserted_already_ok += 1
            records.append(
                {
                    "segment_id": segment_id,
                    "candidate_id": candidate_id,
                    "status": "inserted",
                    "decision": "correction_needed" if match_type == MATCH_TYPE_CORRECTION else "already_ok",
                    "match_type": match_type,
                    "current_output_text": context["current_output_text"],
                    "suggested_text": suggested,
                }
            )
        conn.commit()

    summary = {
        "rule_version": RULE_VERSION,
        "input_reassessment": str(reassessment_path),
        "run_id": run_id,
        "reviewed_count": len(APPROVED_ROWS) + len(HOLD_ROWS),
        "approved_learning_count": len(APPROVED_ROWS),
        "inserted_count": len(plan),
        "inserted_corrections": inserted_corrections,
        "inserted_already_ok": inserted_already_ok,
        "hold_count": len(HOLD_ROWS),
        "hold_segments": sorted(HOLD_ROWS),
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "false_safe_risk_count": 0,
        "source_changed": False,
        "output_changed": False,
        "next_action": "apply_local_learning_feedback_for_general_pipe_article_gender_batch1",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"run_id={run_id}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"inserted_corrections={inserted_corrections}")
    print(f"inserted_already_ok={inserted_already_ok}")
    print(f"hold_count={summary['hold_count']}")
    print(f"blocked_count={blocked_count}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")


if __name__ == "__main__":
    main()
