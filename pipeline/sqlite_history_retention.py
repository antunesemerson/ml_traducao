from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "memory" / "translation_engine.sqlite"
POLICY_VERSION = "sqlite_history_retention_v1"
CONFIRMATION_PREFIX = "PRUNE"
COMPACTION_CONFIRMATION_PREFIX = "COMPACT"

SCORE_SCOPE = "ml_score_items"
STATE_SCOPE = "segment_state_items"

RETENTION_RUNS_TABLE = "history_retention_runs"
RETENTION_SCOPES_TABLE = "history_retention_scopes"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")')}


def _database_stats(conn: sqlite3.Connection, database_path: Path | None) -> dict[str, int]:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    database_bytes = (
        int(database_path.stat().st_size)
        if database_path is not None and database_path.exists()
        else page_size * page_count
    )
    wal_bytes = 0
    shm_bytes = 0
    if database_path is not None:
        wal_path = Path(f"{database_path}-wal")
        shm_path = Path(f"{database_path}-shm")
        wal_bytes = int(wal_path.stat().st_size) if wal_path.exists() else 0
        shm_bytes = int(shm_path.stat().st_size) if shm_path.exists() else 0
    return {
        "database_bytes": database_bytes,
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "total_storage_bytes": database_bytes + wal_bytes + shm_bytes,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": freelist_count,
        "reusable_bytes": page_size * freelist_count,
    }


def _add_reason(
    protected: set[int],
    reasons: dict[int, set[str]],
    run_ids: Iterable[Any],
    reason: str,
) -> None:
    for raw_run_id in run_ids:
        if raw_run_id is None:
            continue
        run_id = int(raw_run_id)
        protected.add(run_id)
        reasons[run_id].add(reason)


def _referencing_run_ids(
    conn: sqlite3.Connection,
    target_table: str,
    *,
    item_table: str,
) -> list[tuple[str, set[int]]]:
    """Return parent-run references without scanning the very large item tables."""
    result: list[tuple[str, set[int]]] = []
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    for table_name in tables:
        if table_name == item_table or table_name.endswith("_items"):
            continue
        for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{table_name}")'):
            if str(foreign_key[2]) != target_table:
                continue
            column_name = str(foreign_key[3])
            run_ids = {
                int(row[0])
                for row in conn.execute(
                    f'SELECT DISTINCT "{column_name}" FROM "{table_name}" '
                    f'WHERE "{column_name}" IS NOT NULL'
                )
            }
            if run_ids:
                result.append((f"reference:{table_name}.{column_name}", run_ids))
    return result


def _protect_latest_by_lane(
    rows: list[sqlite3.Row],
    protected: set[int],
    reasons: dict[int, set[str]],
    keep_latest_per_lane: int,
) -> None:
    by_lane: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        if not row["finished_at"]:
            continue
        lane = str(row["candidate_text_source"] or "legacy")
        by_lane[lane].append(int(row["id"]))
    for lane, run_ids in by_lane.items():
        _add_reason(
            protected,
            reasons,
            sorted(run_ids, reverse=True)[:keep_latest_per_lane],
            f"recent_lane:{lane}",
        )


def _already_pruned_run_ids(conn: sqlite3.Connection, scope: str) -> set[int]:
    if not _table_exists(conn, RETENTION_SCOPES_TABLE):
        return set()
    return {
        int(row[0])
        for row in conn.execute(
            f"SELECT source_run_id FROM {RETENTION_SCOPES_TABLE} WHERE scope = ?",
            (scope,),
        )
    }


def _reason_counts(reasons: dict[int, set[str]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for run_reasons in reasons.values():
        for reason in run_reasons:
            counts[reason] += 1
    return dict(sorted(counts.items()))


def _score_plan(
    conn: sqlite3.Connection,
    *,
    keep_latest_per_lane: int,
) -> dict[str, Any]:
    if not (_table_exists(conn, "ml_score_runs") and _table_exists(conn, SCORE_SCOPE)):
        return {
            "scope": SCORE_SCOPE,
            "candidate_run_ids": [],
            "protected_run_ids": [],
            "estimated_detail_items": 0,
            "protected_detail_items": 0,
            "already_pruned_runs": 0,
            "protection_reason_counts": {},
        }

    rows = list(
        conn.execute(
            """
            SELECT id, candidate_text_source, scored_count, finished_at
            FROM ml_score_runs
            ORDER BY id
            """
        )
    )
    protected: set[int] = set()
    reasons: dict[int, set[str]] = defaultdict(set)
    for reason, run_ids in _referencing_run_ids(
        conn,
        "ml_score_runs",
        item_table=SCORE_SCOPE,
    ):
        _add_reason(protected, reasons, run_ids, reason)
    _add_reason(
        protected,
        reasons,
        (row["id"] for row in rows if not row["finished_at"]),
        "unfinished",
    )
    _protect_latest_by_lane(
        rows,
        protected,
        reasons,
        keep_latest_per_lane,
    )

    pruned = _already_pruned_run_ids(conn, SCORE_SCOPE)
    by_id = {int(row["id"]): row for row in rows}
    candidate_ids = sorted(set(by_id) - protected - pruned)
    return {
        "scope": SCORE_SCOPE,
        "candidate_run_ids": candidate_ids,
        "protected_run_ids": sorted(protected),
        "estimated_detail_items": sum(
            int(by_id[run_id]["scored_count"] or 0) for run_id in candidate_ids
        ),
        "protected_detail_items": sum(
            int(by_id[run_id]["scored_count"] or 0)
            for run_id in protected
            if run_id in by_id
        ),
        "already_pruned_runs": len(pruned),
        "protection_reason_counts": _reason_counts(reasons),
    }


def _state_plan(
    conn: sqlite3.Connection,
    *,
    keep_latest_state_runs: int,
) -> dict[str, Any]:
    if not (_table_exists(conn, "segment_state_runs") and _table_exists(conn, STATE_SCOPE)):
        return {
            "scope": STATE_SCOPE,
            "candidate_run_ids": [],
            "protected_run_ids": [],
            "estimated_detail_items": 0,
            "protected_detail_items": 0,
            "already_pruned_runs": 0,
            "protection_reason_counts": {},
        }

    rows = list(
        conn.execute(
            """
            SELECT id, total_segments, finished_at
            FROM segment_state_runs
            ORDER BY id
            """
        )
    )
    protected: set[int] = set()
    reasons: dict[int, set[str]] = defaultdict(set)
    for reason, run_ids in _referencing_run_ids(
        conn,
        "segment_state_runs",
        item_table=STATE_SCOPE,
    ):
        _add_reason(protected, reasons, run_ids, reason)
    _add_reason(
        protected,
        reasons,
        (row["id"] for row in rows if not row["finished_at"]),
        "unfinished",
    )
    finished_ids = [int(row["id"]) for row in rows if row["finished_at"]]
    _add_reason(
        protected,
        reasons,
        sorted(finished_ids, reverse=True)[:keep_latest_state_runs],
        "recent_state",
    )

    pruned = _already_pruned_run_ids(conn, STATE_SCOPE)
    by_id = {int(row["id"]): row for row in rows}
    candidate_ids = sorted(set(by_id) - protected - pruned)
    return {
        "scope": STATE_SCOPE,
        "candidate_run_ids": candidate_ids,
        "protected_run_ids": sorted(protected),
        "estimated_detail_items": sum(
            int(by_id[run_id]["total_segments"] or 0) for run_id in candidate_ids
        ),
        "protected_detail_items": sum(
            int(by_id[run_id]["total_segments"] or 0)
            for run_id in protected
            if run_id in by_id
        ),
        "already_pruned_runs": len(pruned),
        "protection_reason_counts": _reason_counts(reasons),
    }


def build_retention_plan(
    conn: sqlite3.Connection,
    *,
    database_path: Path | None = None,
    keep_latest_per_lane: int = 3,
    keep_latest_state_runs: int = 5,
) -> dict[str, Any]:
    if keep_latest_per_lane < 1 or keep_latest_state_runs < 1:
        raise ValueError("Retention counts must be at least 1.")
    score = _score_plan(conn, keep_latest_per_lane=keep_latest_per_lane)
    state = _state_plan(conn, keep_latest_state_runs=keep_latest_state_runs)
    payload: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "database_path": str(database_path.resolve()) if database_path is not None else ":memory:",
        "keep_latest_per_lane": keep_latest_per_lane,
        "keep_latest_state_runs": keep_latest_state_runs,
        "database": _database_stats(conn, database_path),
        "scopes": {
            SCORE_SCOPE: score,
            STATE_SCOPE: state,
        },
        "candidate_runs": len(score["candidate_run_ids"]) + len(state["candidate_run_ids"]),
        "estimated_detail_items": int(score["estimated_detail_items"])
        + int(state["estimated_detail_items"]),
    }
    hash_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
    payload["plan_hash"] = plan_hash
    payload["execution_token"] = f"{CONFIRMATION_PREFIX}-{plan_hash[:12].upper()}"
    return payload


def summarize_retention_plan(plan: dict[str, Any]) -> dict[str, Any]:
    scopes: dict[str, Any] = {}
    for scope_name, scope in plan["scopes"].items():
        scopes[scope_name] = {
            "candidate_runs": len(scope["candidate_run_ids"]),
            "protected_runs": len(scope["protected_run_ids"]),
            "estimated_detail_items": int(scope["estimated_detail_items"]),
            "protected_detail_items": int(scope["protected_detail_items"]),
            "already_pruned_runs": int(scope["already_pruned_runs"]),
            "candidate_run_range": (
                [min(scope["candidate_run_ids"]), max(scope["candidate_run_ids"])]
                if scope["candidate_run_ids"]
                else []
            ),
        }
    return {
        "policy_version": plan["policy_version"],
        "database_path": plan["database_path"],
        "database": plan["database"],
        "keep_latest_per_lane": plan["keep_latest_per_lane"],
        "keep_latest_state_runs": plan["keep_latest_state_runs"],
        "scopes": scopes,
        "candidate_runs": plan["candidate_runs"],
        "estimated_detail_items": plan["estimated_detail_items"],
        "plan_hash": plan["plan_hash"],
        "execution_token": plan["execution_token"],
    }


def build_compaction_plan(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    stats = _database_stats(conn, database_path)
    output_parent = output_path.resolve().parent
    free_bytes = int(shutil.disk_usage(output_parent).free) if output_parent.exists() else 0
    estimated_compacted_bytes = max(
        int(stats["database_bytes"]) - int(stats["reusable_bytes"]),
        int(stats["page_size"]),
    )
    safety_margin = max(2 * 1024**3, int(estimated_compacted_bytes * 0.05))
    required_free_bytes = estimated_compacted_bytes + safety_margin
    payload: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "database_path": str(database_path.resolve()),
        "output_path": str(output_path.resolve()),
        "database": stats,
        "estimated_compacted_bytes": estimated_compacted_bytes,
        "required_free_bytes": required_free_bytes,
        "available_free_bytes": free_bytes,
        "output_exists": output_path.exists(),
        "can_compact": (
            output_parent.exists()
            and not output_path.exists()
            and free_bytes >= required_free_bytes
        ),
    }
    hash_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
    payload["plan_hash"] = plan_hash
    payload["execution_token"] = (
        f"{COMPACTION_CONFIRMATION_PREFIX}-{plan_hash[:12].upper()}"
    )
    return payload


def ensure_retention_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RETENTION_RUNS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_version TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            status TEXT NOT NULL,
            deleted_detail_items INTEGER NOT NULL DEFAULT 0,
            cascaded_changes INTEGER NOT NULL DEFAULT 0,
            database_bytes_before INTEGER NOT NULL DEFAULT 0,
            reusable_bytes_before INTEGER NOT NULL DEFAULT 0,
            database_bytes_after INTEGER,
            reusable_bytes_after INTEGER,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error_text TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RETENTION_SCOPES_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            retention_run_id INTEGER NOT NULL,
            scope TEXT NOT NULL,
            source_run_id INTEGER NOT NULL,
            estimated_item_count INTEGER NOT NULL DEFAULT 0,
            deleted_item_count INTEGER NOT NULL DEFAULT 0,
            cascaded_changes INTEGER NOT NULL DEFAULT 0,
            pruned_at TEXT NOT NULL,
            FOREIGN KEY(retention_run_id) REFERENCES {RETENTION_RUNS_TABLE}(id) ON DELETE CASCADE,
            UNIQUE(scope, source_run_id)
        )
        """
    )
    database_stats = _database_stats(conn, None)
    conn.execute(
        f"""
        UPDATE {RETENTION_RUNS_TABLE}
        SET status = 'interrupted', finished_at = ?,
            deleted_detail_items = COALESCE((
                SELECT SUM(scope.deleted_item_count)
                FROM {RETENTION_SCOPES_TABLE} scope
                WHERE scope.retention_run_id = {RETENTION_RUNS_TABLE}.id
            ), 0),
            cascaded_changes = COALESCE((
                SELECT SUM(scope.cascaded_changes)
                FROM {RETENTION_SCOPES_TABLE} scope
                WHERE scope.retention_run_id = {RETENTION_RUNS_TABLE}.id
            ), 0),
            database_bytes_after = ?, reusable_bytes_after = ?,
            error_text = COALESCE(
                error_text,
                'Previous maintenance process ended before finalization; committed per-run deletions were audited and preserved.'
            )
        WHERE status = 'running'
        """,
        (
            utc_now(),
            int(database_stats["database_bytes"]),
            int(database_stats["reusable_bytes"]),
        ),
    )
    conn.commit()


def _index_covers_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    for index_row in conn.execute(f'PRAGMA index_list("{table_name}")'):
        index_name = str(index_row[1])
        index_columns = [
            str(row[2]) for row in conn.execute(f'PRAGMA index_info("{index_name}")')
        ]
        if index_columns and index_columns[0] == column_name:
            return True
    return False


def ensure_retention_delete_indexes(
    conn: sqlite3.Connection,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[str]:
    """Index child foreign keys so parent-detail deletion is not quadratic."""
    created: list[str] = []
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    for table_name in tables:
        for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{table_name}")'):
            target_table = str(foreign_key[2])
            if target_table not in {SCORE_SCOPE, STATE_SCOPE}:
                continue
            column_name = str(foreign_key[3])
            if _index_covers_column(conn, table_name, column_name):
                continue
            digest = hashlib.sha256(
                f"{table_name}.{column_name}".encode("utf-8")
            ).hexdigest()[:16]
            index_name = f"idx_retention_fk_{digest}"
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "create_foreign_key_index",
                        "table": table_name,
                        "column": column_name,
                        "index": index_name,
                    }
                )
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f'ON "{table_name}" ("{column_name}")'
            )
            conn.commit()
            created.append(index_name)
    return created


def _assert_no_active_pipeline(project_root: Path) -> None:
    for lock_name in ("training_in_progress.flag", "training_lock.json", "production_hold.json"):
        lock_path = project_root / "memory" / lock_name
        if lock_path.exists():
            raise RuntimeError(f"Maintenance blocked by active lock: {lock_path}")
    status_path = project_root / "memory" / "production_run_status.json"
    if not status_path.exists():
        return
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    status = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status in {"starting", "running", "in_progress", "queued"}:
        raise RuntimeError(f"Maintenance blocked by production status: {status}")


def apply_retention_plan(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    *,
    confirmation_token: str,
    project_root: Path = PROJECT_ROOT,
    scopes: Iterable[str] | None = None,
    max_runs: int | None = None,
    max_detail_items: int | None = None,
    checkpoint_every: int = 10,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    expected_token = str(plan["execution_token"])
    if confirmation_token != expected_token:
        raise ValueError(f"Invalid confirmation token; expected {expected_token}.")
    if max_runs is not None and max_runs < 1:
        raise ValueError("max_runs must be at least 1 when provided.")
    if max_detail_items is not None and max_detail_items < 1:
        raise ValueError("max_detail_items must be at least 1 when provided.")
    selected_scopes = tuple(
        (SCORE_SCOPE, STATE_SCOPE) if scopes is None else scopes
    )
    if not selected_scopes:
        raise ValueError("At least one retention scope must be selected.")
    invalid_scopes = sorted(set(selected_scopes) - {SCORE_SCOPE, STATE_SCOPE})
    if invalid_scopes:
        raise ValueError(f"Invalid retention scopes: {', '.join(invalid_scopes)}")
    selected_scopes = tuple(dict.fromkeys(selected_scopes))
    _assert_no_active_pipeline(project_root)
    ensure_retention_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 300000")
    conn.execute("PRAGMA secure_delete = OFF")
    created_indexes = ensure_retention_delete_indexes(
        conn,
        progress_callback=progress_callback,
    )

    before = dict(plan["database"])
    started_at = utc_now()
    audit_plan = dict(plan)
    audit_plan["execution"] = {
        "selected_scopes": list(selected_scopes),
        "max_runs": max_runs,
        "max_detail_items": max_detail_items,
        "checkpoint_every": checkpoint_every,
    }
    cursor = conn.execute(
        f"""
        INSERT INTO {RETENTION_RUNS_TABLE} (
            policy_version, plan_hash, plan_json, status,
            database_bytes_before, reusable_bytes_before, started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?)
        """,
        (
            POLICY_VERSION,
            plan["plan_hash"],
            json.dumps(audit_plan, ensure_ascii=False, sort_keys=True),
            int(before["database_bytes"]),
            int(before["reusable_bytes"]),
            started_at,
        ),
    )
    retention_run_id = int(cursor.lastrowid)
    conn.commit()

    deleted_total = 0
    cascaded_total = 0
    processed_runs = 0
    stopped_by_limit = False
    stop_reason = ""
    total_candidate_runs = sum(
        len(plan["scopes"][scope]["candidate_run_ids"])
        for scope in selected_scopes
    )
    try:
        for scope in selected_scopes:
            scope_plan = plan["scopes"][scope]
            parent_table = "ml_score_runs" if scope == SCORE_SCOPE else "segment_state_runs"
            count_column = "scored_count" if scope == SCORE_SCOPE else "total_segments"
            for source_run_id in scope_plan["candidate_run_ids"]:
                if max_runs is not None and processed_runs >= max_runs:
                    stopped_by_limit = True
                    stop_reason = "max_runs"
                    break
                row = conn.execute(
                    f"SELECT {count_column} FROM {parent_table} WHERE id = ?",
                    (source_run_id,),
                ).fetchone()
                estimated_count = int(row[0] or 0) if row else 0
                if (
                    max_detail_items is not None
                    and deleted_total + estimated_count > max_detail_items
                ):
                    stopped_by_limit = True
                    stop_reason = "max_detail_items"
                    break
                changes_before = conn.total_changes
                conn.execute("BEGIN IMMEDIATE")
                delete_cursor = conn.execute(
                    f"DELETE FROM {scope} WHERE run_id = ?",
                    (source_run_id,),
                )
                deleted_count = max(int(delete_cursor.rowcount), 0)
                changes_after_delete = conn.total_changes
                cascaded_changes = max(changes_after_delete - changes_before - deleted_count, 0)
                conn.execute(
                    f"""
                    INSERT INTO {RETENTION_SCOPES_TABLE} (
                        retention_run_id, scope, source_run_id,
                        estimated_item_count, deleted_item_count,
                        cascaded_changes, pruned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        retention_run_id,
                        scope,
                        source_run_id,
                        estimated_count,
                        deleted_count,
                        cascaded_changes,
                        utc_now(),
                    ),
                )
                conn.commit()
                deleted_total += deleted_count
                cascaded_total += cascaded_changes
                processed_runs += 1
                if progress_callback is not None:
                    progress_callback(
                        {
                            "scope": scope,
                            "source_run_id": source_run_id,
                            "processed_runs": processed_runs,
                            "total_candidate_runs": total_candidate_runs,
                            "deleted_in_run": deleted_count,
                            "deleted_total": deleted_total,
                        }
                    )
                if checkpoint_every > 0 and processed_runs % checkpoint_every == 0:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if stopped_by_limit:
                break

        checkpoint = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        after = _database_stats(conn, None)
        final_status = "partial" if stopped_by_limit else "completed"
        conn.execute(
            f"""
            UPDATE {RETENTION_RUNS_TABLE}
            SET status = ?, deleted_detail_items = ?, cascaded_changes = ?,
                database_bytes_after = ?, reusable_bytes_after = ?, finished_at = ?
            WHERE id = ?
            """,
            (
                final_status,
                deleted_total,
                cascaded_total,
                int(after["database_bytes"]),
                int(after["reusable_bytes"]),
                utc_now(),
                retention_run_id,
            ),
        )
        conn.commit()
        return {
            "retention_run_id": retention_run_id,
            "status": final_status,
            "processed_runs": processed_runs,
            "deleted_detail_items": deleted_total,
            "cascaded_changes": cascaded_total,
            "selected_scopes": list(selected_scopes),
            "stop_reason": stop_reason or None,
            "checkpoint": checkpoint,
            "created_foreign_key_indexes": created_indexes,
            "database_before": before,
            "database_after": after,
        }
    except Exception as exc:
        conn.rollback()
        conn.execute(
            f"""
            UPDATE {RETENTION_RUNS_TABLE}
            SET status = 'failed', deleted_detail_items = ?, cascaded_changes = ?,
                finished_at = ?, error_text = ?
            WHERE id = ?
            """,
            (deleted_total, cascaded_total, utc_now(), str(exc), retention_run_id),
        )
        conn.commit()
        raise


def compact_database(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    *,
    confirmation_token: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    expected_token = str(plan["execution_token"])
    if confirmation_token != expected_token:
        raise ValueError(f"Invalid confirmation token; expected {expected_token}.")
    if not plan["can_compact"]:
        raise RuntimeError(
            "Compaction preflight failed: the output already exists or free disk is insufficient."
        )
    _assert_no_active_pipeline(project_root)
    output_path = Path(plan["output_path"])
    if output_path.exists():
        raise FileExistsError(f"Compaction output already exists: {output_path}")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    quoted_output = str(output_path).replace("'", "''")
    conn.execute(f"VACUUM INTO '{quoted_output}'")

    validation = sqlite3.connect(
        f"file:{output_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=300,
    )
    try:
        quick_check = str(validation.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_issues = int(
            validation.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0]
        )
        output_pages = int(validation.execute("PRAGMA page_count").fetchone()[0])
        output_page_size = int(validation.execute("PRAGMA page_size").fetchone()[0])
    finally:
        validation.close()
    if quick_check.lower() != "ok" or foreign_key_issues:
        raise RuntimeError(
            f"Compacted database validation failed: quick_check={quick_check}, "
            f"foreign_key_issues={foreign_key_issues}."
        )
    return {
        "status": "validated",
        "source_path": plan["database_path"],
        "output_path": str(output_path),
        "output_bytes": int(output_path.stat().st_size),
        "output_pages": output_pages,
        "output_page_size": output_page_size,
        "quick_check": quick_check,
        "foreign_key_issues": foreign_key_issues,
        "source_was_replaced": False,
    }


def _connect(database_path: Path, *, writable: bool) -> sqlite3.Connection:
    if writable:
        conn = sqlite3.connect(database_path, timeout=300)
    else:
        conn = sqlite3.connect(f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 300000")
    if writable:
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        conn.execute("PRAGMA query_only = ON")
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or apply conservative SQLite history retention. Dry-run is the default."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--keep-latest-per-lane", type=int, default=3)
    parser.add_argument("--keep-latest-state-runs", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Apply only this many parent runs, useful for a controlled pilot.",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "score", "state"),
        default="all",
        help="Restrict retention to score details or segment-state details.",
    )
    parser.add_argument(
        "--max-detail-items",
        type=int,
        default=None,
        help="Stop before a parent run would exceed this detail-item budget.",
    )
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument(
        "--compact-into",
        type=Path,
        default=None,
        help="Plan a VACUUM INTO copy; with its COMPACT token, create and validate it.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include every protected and candidate run id in the printed plan.",
    )
    args = parser.parse_args()

    database_path = args.db.resolve()
    if not database_path.exists():
        raise SystemExit(f"Database not found: {database_path}")
    compact_requested = args.compact_into is not None
    conn = _connect(database_path, writable=args.apply or compact_requested)
    try:
        if compact_requested:
            output_path = args.compact_into.resolve()
            compaction_plan = build_compaction_plan(
                conn,
                database_path=database_path,
                output_path=output_path,
            )
            print(
                "[sqlite_history_compaction] "
                + json.dumps(compaction_plan, ensure_ascii=False)
            )
            if not args.confirm:
                return
            result = compact_database(
                conn,
                compaction_plan,
                confirmation_token=args.confirm,
            )
            print(
                "[sqlite_history_compaction] "
                + json.dumps(result, ensure_ascii=False)
            )
            return
        plan = build_retention_plan(
            conn,
            database_path=database_path,
            keep_latest_per_lane=args.keep_latest_per_lane,
            keep_latest_state_runs=args.keep_latest_state_runs,
        )
        printed_plan = plan if args.verbose else summarize_retention_plan(plan)
        print(
            "[sqlite_history_retention] " + json.dumps(printed_plan, ensure_ascii=False),
            flush=True,
        )
        if not args.apply:
            return
        selected_scopes = {
            "all": (SCORE_SCOPE, STATE_SCOPE),
            "score": (SCORE_SCOPE,),
            "state": (STATE_SCOPE,),
        }[args.scope]
        result = apply_retention_plan(
            conn,
            plan,
            confirmation_token=args.confirm,
            scopes=selected_scopes,
            max_runs=args.max_runs,
            max_detail_items=args.max_detail_items,
            checkpoint_every=args.checkpoint_every,
            progress_callback=lambda progress: print(
                "[sqlite_history_retention_progress] "
                + json.dumps(progress, ensure_ascii=False),
                flush=True,
            ),
        )
        print("[sqlite_history_retention] " + json.dumps(result, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
