from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import db


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def persist_snapshot(
    conn: sqlite3.Connection,
    *,
    source_rule_version: str,
    score_run_id: int,
    records: list[dict[str, Any]],
    eligible_lane: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the exact shadow handoff used by evidence generation.

    Reports are deliberately absent from this contract. Re-running an identical
    snapshot reuses the same run id and refreshes its rows atomically.
    """
    if not source_rule_version.strip():
        raise ValueError("source_rule_version is required")
    normalized_records = sorted(records, key=lambda item: int(item.get("segment_id") or 0))
    if any(int(item.get("score_run_id") or 0) != int(score_run_id) for item in normalized_records):
        raise RuntimeError("Shadow snapshot mixes score runs.")
    payload_hash = hashlib.sha256(_canonical_json(normalized_records).encode("utf-8")).hexdigest()
    snapshot_key = hashlib.sha256(
        f"{source_rule_version}|{int(score_run_id)}|{payload_hash}".encode("utf-8")
    ).hexdigest()
    now = db.utc_now()
    eligible_count = sum(1 for item in normalized_records if item.get("lane") == eligible_lane)
    conn.execute(
        """
        INSERT INTO ml_quality_shadow_runs (
          snapshot_key, source_rule_version, score_run_id, record_count,
          eligible_count, status, metadata_json, started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        ON CONFLICT(snapshot_key) DO UPDATE SET
          record_count = excluded.record_count,
          eligible_count = excluded.eligible_count,
          status = 'completed',
          metadata_json = excluded.metadata_json,
          finished_at = excluded.finished_at,
          updated_at = excluded.updated_at
        """,
        (
            snapshot_key,
            source_rule_version,
            int(score_run_id),
            len(normalized_records),
            eligible_count,
            _canonical_json(metadata or {}),
            now,
            now,
            now,
        ),
    )
    run = conn.execute(
        "SELECT id FROM ml_quality_shadow_runs WHERE snapshot_key = ?",
        (snapshot_key,),
    ).fetchone()
    if not run:
        raise RuntimeError("Failed to materialize quality shadow snapshot.")
    run_id = int(run["id"] if isinstance(run, sqlite3.Row) else run[0])
    conn.execute("DELETE FROM ml_quality_shadow_items WHERE run_id = ?", (run_id,))
    conn.executemany(
        """
        INSERT INTO ml_quality_shadow_items (
          run_id, segment_id, lane, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                int(item["segment_id"]),
                str(item.get("lane") or "unclassified"),
                _canonical_json(item),
                now,
            )
            for item in normalized_records
        ],
    )
    conn.commit()
    return {
        "shadow_run_id": run_id,
        "shadow_source": f"db:ml_quality_shadow_runs/{run_id}",
        "record_count": len(normalized_records),
        "eligible_count": eligible_count,
        "snapshot_key": snapshot_key,
    }


def load_snapshot(
    conn: sqlite3.Connection,
    *,
    source_rule_version: str,
    run_id: int | None = None,
    eligible_lane: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if run_id is None:
        run = conn.execute(
            """
            SELECT * FROM ml_quality_shadow_runs
            WHERE source_rule_version = ? AND status = 'completed'
            ORDER BY id DESC
            LIMIT 1
            """,
            (source_rule_version,),
        ).fetchone()
    else:
        run = conn.execute(
            """
            SELECT * FROM ml_quality_shadow_runs
            WHERE id = ? AND source_rule_version = ? AND status = 'completed'
            """,
            (int(run_id), source_rule_version),
        ).fetchone()
    if not run:
        target = f"run {run_id}" if run_id is not None else "latest run"
        raise RuntimeError(f"No completed database shadow snapshot for {source_rule_version} ({target}).")
    run_payload = dict(run)
    parameters: list[Any] = [int(run_payload["id"])]
    lane_filter = ""
    if eligible_lane is not None:
        lane_filter = " AND lane = ?"
        parameters.append(eligible_lane)
    items = conn.execute(
        f"""
        SELECT payload_json
        FROM ml_quality_shadow_items
        WHERE run_id = ?{lane_filter}
        ORDER BY segment_id, id
        """,
        parameters,
    ).fetchall()
    rows = [json.loads(item["payload_json"] if isinstance(item, sqlite3.Row) else item[0]) for item in items]
    expected_count = (
        int(run_payload.get("eligible_count") or 0)
        if eligible_lane is not None
        else int(run_payload.get("record_count") or 0)
    )
    if len(rows) != expected_count:
        raise RuntimeError(
            f"Database shadow snapshot {run_payload['id']} is incomplete: expected {expected_count}, found {len(rows)}."
        )
    return run_payload, rows
