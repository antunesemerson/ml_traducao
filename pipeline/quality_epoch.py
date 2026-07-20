from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import db
import source_tree_snapshot


RULE_VERSION = "quality_epoch_v1"
TREE_HASH_CACHE_FILE = db.PROJECT_ROOT / "memory" / "quality_epoch_tree_hash_cache.json"
SCORING_CONTRACT_FILES = (
    "config/settings.json",
    "pipeline/ml_score_segments.py",
    "pipeline/ml_group_threshold_policy.py",
    "pipeline/local_quality_validator.py",
)


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tree_manifest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(item for item in root.rglob("*.yml") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def _load_tree_cache() -> dict[str, Any]:
    if not TREE_HASH_CACHE_FILE.exists():
        return {"version": 1, "trees": {}}
    try:
        payload = json.loads(TREE_HASH_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "trees": {}}
    payload.setdefault("version", 1)
    payload.setdefault("trees", {})
    return payload


def _save_tree_cache(payload: dict[str, Any]) -> None:
    TREE_HASH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = TREE_HASH_CACHE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(TREE_HASH_CACHE_FILE)


def _cached_tree_summary(root: Path, cache_key: str) -> dict[str, Any]:
    manifest_hash, file_count = _tree_manifest(root)
    cache = _load_tree_cache()
    cached = cache["trees"].get(cache_key) or {}
    if cached.get("manifest_hash") == manifest_hash and cached.get("tree_hash"):
        return {
            "tree_hash": cached["tree_hash"],
            "file_count": int(cached.get("file_count") or file_count),
            "manifest_hash": manifest_hash,
            "cache_hit": True,
        }
    summary, _ = source_tree_snapshot.inspect_tree(root)
    cache["trees"][cache_key] = {
        "root": str(root),
        "manifest_hash": manifest_hash,
        "tree_hash": summary["tree_hash"],
        "file_count": summary["file_count"],
        "updated_at": db.utc_now(),
    }
    _save_tree_cache(cache)
    return {
        **summary,
        "manifest_hash": manifest_hash,
        "cache_hit": False,
    }


def _latest_model(conn) -> dict[str, Any]:
    registry_exists = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ml_model_registry'"
        ).fetchone()
    )
    if registry_exists:
        active = conn.execute(
            """
            SELECT run.id, run.rule_version, run.model_version, run.model_path,
                   run.dataset_run_id, run.safe_threshold, run.finished_at
            FROM ml_model_registry registry
            JOIN ml_model_runs run ON run.id = registry.active_model_run_id
            WHERE registry.model_kind = 'risk_action_classifier'
              AND run.finished_at IS NOT NULL
              AND run.model_path IS NOT NULL
            LIMIT 1
            """
        ).fetchone()
        if active:
            return {**dict(active), "model_selection_source": "registry_active"}
    row = conn.execute(
        """
        SELECT id, rule_version, model_version, model_path, dataset_run_id,
               safe_threshold, finished_at
        FROM ml_model_runs
        WHERE finished_at IS NOT NULL
          AND model_path IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No finished ML model is available for a quality epoch.")
    return {**dict(row), "model_selection_source": "latest_finished_fallback"}


def _contract_hash(model: dict[str, Any]) -> str:
    files: list[dict[str, Any]] = []
    for relative in SCORING_CONTRACT_FILES:
        path = db.PROJECT_ROOT / relative
        files.append(
            {
                "path": relative,
                "sha256": db.file_hash(path) if path.exists() else "missing",
            }
        )
    model_path = db.project_path(str(model.get("model_path") or ""))
    return _json_hash(
        {
            "rule_version": RULE_VERSION,
            "model_run_id": int(model["id"]),
            "model_rule_version": model.get("rule_version"),
            "model_version": model.get("model_version"),
            "model_sha256": db.file_hash(model_path) if model_path.is_file() else "missing",
            "safe_threshold": model.get("safe_threshold"),
            "files": files,
        }
    )


def current_fingerprint(conn, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or db.load_settings()
    model = _latest_model(conn)
    english_summary = _cached_tree_summary(db.project_path(settings["english_source"]), "english_source")
    spanish_summary = _cached_tree_summary(db.project_path(settings["spanish_source"]), "spanish_source")
    snapshot_row = conn.execute(
        """
        SELECT *
        FROM source_tree_snapshots
        WHERE english_tree_hash = ? AND spanish_tree_hash = ?
        LIMIT 1
        """,
        (english_summary["tree_hash"], spanish_summary["tree_hash"]),
    ).fetchone()
    if not snapshot_row:
        raise RuntimeError("The current source trees were not snapshotted before opening the quality epoch.")
    snapshot = dict(snapshot_row)
    baseline_summary = _cached_tree_summary(
        db.project_path(settings["spanish_traduzido_old"]),
        "spanish_traduzido_old",
    )
    output_summary = _cached_tree_summary(db.project_path(settings["output_spanish"]), "output_spanish")
    baseline_tree_hash = str(baseline_summary["tree_hash"])
    output_tree_hash = str(output_summary["tree_hash"])
    scoring_contract_hash = _contract_hash(model)
    data = {
        "source_snapshot_id": int(snapshot["id"]),
        "english_tree_hash": snapshot.get("english_tree_hash"),
        "spanish_tree_hash": snapshot.get("spanish_tree_hash"),
        "baseline_tree_hash": baseline_tree_hash,
        "output_tree_hash": output_tree_hash,
    }
    data_snapshot_hash = _json_hash(data)
    epoch_key = _json_hash(
        {
            "scoring_contract_hash": scoring_contract_hash,
            "data_snapshot_hash": data_snapshot_hash,
        }
    )
    return {
        **data,
        "epoch_key": epoch_key,
        "scoring_contract_hash": scoring_contract_hash,
        "data_snapshot_hash": data_snapshot_hash,
        "model_run_id": int(model["id"]),
        "model_version": model.get("model_version"),
        "model_rule_version": model.get("rule_version"),
        "model_selection_source": model.get("model_selection_source"),
    }


def _score_plan_for_fingerprint(fingerprint: dict[str, Any]) -> dict[str, Any]:
    baseline_tree_hash = str(fingerprint.get("baseline_tree_hash") or "")
    output_tree_hash = str(fingerprint.get("output_tree_hash") or "")
    identical_package_trees = bool(
        baseline_tree_hash
        and output_tree_hash
        and baseline_tree_hash == output_tree_hash
    )
    if identical_package_trees:
        return {
            "required_sources": ["output"],
            "source_aliases": {"old": "output"},
            "identical_package_trees": True,
        }
    return {
        "required_sources": ["old", "output"],
        "source_aliases": {},
        "identical_package_trees": False,
    }


def _score_run_ids_finished(conn, run_ids: Iterable[Any]) -> bool:
    unique_run_ids = sorted({int(run_id) for run_id in run_ids if run_id})
    if not unique_run_ids:
        return False
    placeholders = ", ".join("?" for _ in unique_run_ids)
    finished_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM ml_score_runs "
            f"WHERE id IN ({placeholders}) AND finished_at IS NOT NULL",
            tuple(unique_run_ids),
        ).fetchone()[0]
    )
    return finished_count == len(unique_run_ids)


def _table_exists(conn, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    )


def _event(
    conn,
    epoch_id: int | None,
    event_type: str,
    source: str,
    *,
    mutation_scope: str = "metadata",
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO quality_epoch_events (
            epoch_id, event_type, event_source, mutation_scope,
            details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            epoch_id,
            event_type,
            source,
            mutation_scope,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            db.utc_now(),
        ),
    )


def open_epoch(*, source: str = "diagnostic") -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        fingerprint = current_fingerprint(conn, settings)
        row = conn.execute(
            "SELECT * FROM quality_epochs WHERE epoch_key = ? LIMIT 1",
            (fingerprint["epoch_key"],),
        ).fetchone()
        now = db.utc_now()
        if row:
            epoch = dict(row)
            score_rows_exist = _score_run_ids_finished(
                conn,
                (epoch.get("old_score_run_id"), epoch.get("output_score_run_id")),
            )
            needs_scoring = not (
                score_rows_exist and epoch.get("status") in {"scored", "evaluated", "published"}
            )
            score_plan = _score_plan_for_fingerprint(fingerprint)
            if not needs_scoring:
                score_plan = {
                    **score_plan,
                    "required_sources": [],
                    "attached_run_ids": {
                        "old": int(epoch["old_score_run_id"]),
                        "output": int(epoch["output_score_run_id"]),
                    },
                }
            _event(
                conn,
                int(epoch["id"]),
                "epoch_reused" if not needs_scoring else "epoch_reopened",
                source,
                details={"previous_status": epoch.get("status"), "score_plan": score_plan},
            )
            if needs_scoring:
                conn.execute(
                    """
                    UPDATE quality_epochs
                    SET status = 'open', invalidation_reason = NULL,
                        invalidated_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, epoch["id"]),
                )
            conn.commit()
            return {
                "epoch_id": int(epoch["id"]),
                "epoch_key": fingerprint["epoch_key"],
                "status": "open" if needs_scoring else epoch.get("status"),
                "needs_scoring": needs_scoring,
                "reused": not needs_scoring,
                "score_plan": score_plan,
                "fingerprint": fingerprint,
            }

        cursor = conn.execute(
            """
            INSERT INTO quality_epochs (
                epoch_key, scoring_contract_hash, data_snapshot_hash,
                source_snapshot_id, english_tree_hash, spanish_tree_hash,
                baseline_tree_hash, output_tree_hash,
                model_run_id, model_version, scoring_rule_version,
                status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                fingerprint["epoch_key"],
                fingerprint["scoring_contract_hash"],
                fingerprint["data_snapshot_hash"],
                fingerprint["source_snapshot_id"],
                fingerprint["english_tree_hash"],
                fingerprint["spanish_tree_hash"],
                fingerprint["baseline_tree_hash"],
                fingerprint["output_tree_hash"],
                fingerprint["model_run_id"],
                fingerprint["model_version"],
                fingerprint["model_rule_version"],
                json.dumps({"opened_by": source}, ensure_ascii=False),
                now,
                now,
            ),
        )
        epoch_id = int(cursor.lastrowid)
        conn.execute(
            """
            UPDATE quality_epochs
            SET status = 'stale', invalidation_reason = 'new_fingerprint',
                invalidated_at = ?, updated_at = ?
            WHERE id <> ? AND status IN ('open', 'scored', 'evaluated')
            """,
            (now, now, epoch_id),
        )
        _event(conn, epoch_id, "epoch_opened", source, mutation_scope="quality_contract")
        conn.commit()
        score_plan = _score_plan_for_fingerprint(fingerprint)
        return {
            "epoch_id": epoch_id,
            "epoch_key": fingerprint["epoch_key"],
            "status": "open",
            "needs_scoring": True,
            "reused": False,
            "score_plan": score_plan,
            "fingerprint": fingerprint,
        }


def _score_run_for_tree(
    conn,
    source: str,
    tree_hash: str,
    source_snapshot_id: int,
    model_run_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE candidate_text_source = ?
          AND candidate_tree_hash = ?
          AND source_snapshot_id = ?
          AND model_run_id = ?
          AND finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (source, tree_hash, source_snapshot_id, model_run_id),
    ).fetchone()
    if not row:
        raise RuntimeError(f"No finished {source} score run matches the current quality epoch.")
    return dict(row)


def finalize_scoring(*, source: str = "diagnostic") -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        fingerprint = current_fingerprint(conn, settings)
        epoch_row = conn.execute(
            "SELECT * FROM quality_epochs WHERE epoch_key = ? LIMIT 1",
            (fingerprint["epoch_key"],),
        ).fetchone()
        if not epoch_row:
            raise RuntimeError("The current quality epoch was not opened before scoring.")
        epoch = dict(epoch_row)
        output_run = _score_run_for_tree(
            conn,
            "output",
            fingerprint["output_tree_hash"],
            fingerprint["source_snapshot_id"],
            fingerprint["model_run_id"],
        )
        shared_score_run = (
            fingerprint["baseline_tree_hash"] == fingerprint["output_tree_hash"]
        )
        old_run = (
            output_run
            if shared_score_run
            else _score_run_for_tree(
                conn,
                "old",
                fingerprint["baseline_tree_hash"],
                fingerprint["source_snapshot_id"],
                fingerprint["model_run_id"],
            )
        )
        comparable = all(
            old_run.get(key) == output_run.get(key)
            for key in ("rule_version", "model_run_id", "model_version", "source_snapshot_id")
        )
        comparable = comparable and int(old_run["model_run_id"]) == int(fingerprint["model_run_id"])
        if not comparable:
            raise RuntimeError("Old and output score runs do not share the same scoring contract.")
        policy = conn.execute(
            """
            SELECT id
            FROM ml_policy_runs
            WHERE score_run_id = ? AND finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (output_run["id"],),
        ).fetchone()
        if not policy:
            raise RuntimeError("No finished score policy matches the output score run.")
        state = conn.execute(
            "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        now = db.utc_now()
        conn.execute(
            """
            UPDATE quality_epochs
            SET old_score_run_id = ?, output_score_run_id = ?, policy_run_id = ?,
                segment_state_run_id = ?, scoring_rule_version = ?,
                model_run_id = ?, model_version = ?, status = 'scored',
                scored_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                old_run["id"],
                output_run["id"],
                policy["id"],
                int(state["id"]) if state else None,
                output_run.get("rule_version"),
                output_run.get("model_run_id"),
                output_run.get("model_version"),
                now,
                now,
                epoch["id"],
            ),
        )
        _event(
            conn,
            int(epoch["id"]),
            "epoch_scored",
            source,
            mutation_scope="scores",
            details={
                "old_score_run_id": old_run["id"],
                "output_score_run_id": output_run["id"],
                "policy_run_id": policy["id"],
                "shared_score_run": shared_score_run,
            },
        )
        conn.commit()
        return {
            "epoch_id": int(epoch["id"]),
            "status": "scored",
            "old_score_run_id": int(old_run["id"]),
            "output_score_run_id": int(output_run["id"]),
            "policy_run_id": int(policy["id"]),
            "shared_score_run": shared_score_run,
        }


def validate_current(required_statuses: Iterable[str]) -> dict[str, Any]:
    statuses = {item.strip() for item in required_statuses if item.strip()}
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        fingerprint = current_fingerprint(conn, settings)
        row = conn.execute(
            "SELECT * FROM quality_epochs WHERE epoch_key = ? LIMIT 1",
            (fingerprint["epoch_key"],),
        ).fetchone()
        if not row:
            raise RuntimeError("No quality epoch matches the current source, baseline, output and scoring contract.")
        epoch = dict(row)
        if statuses and epoch.get("status") not in statuses:
            raise RuntimeError(
                f"Quality epoch {epoch['id']} has status {epoch.get('status')}; expected {sorted(statuses)}."
            )
        return {
            "epoch_id": int(epoch["id"]),
            "epoch_key": epoch["epoch_key"],
            "status": epoch["status"],
            "old_score_run_id": epoch.get("old_score_run_id"),
            "output_score_run_id": epoch.get("output_score_run_id"),
            "policy_run_id": epoch.get("policy_run_id"),
            "segment_state_run_id": epoch.get("segment_state_run_id"),
            "fingerprint_current": True,
        }


def _finished_segment_state_run_id(
    conn: sqlite3.Connection,
    requested_run_id: int | None = None,
) -> int | None:
    if requested_run_id is None:
        row = conn.execute(
            "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM segment_state_runs WHERE id = ? AND finished_at IS NOT NULL",
            (requested_run_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"Segment-state run {requested_run_id} does not exist or is not finished."
            )
    return int(row["id"]) if row else None


def attach_segment_state(
    *,
    source: str,
    epoch_id: int | None = None,
    segment_state_run_id: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if epoch_id is None:
            fingerprint = current_fingerprint(conn, settings)
            row = conn.execute(
                "SELECT * FROM quality_epochs WHERE epoch_key = ? LIMIT 1",
                (fingerprint["epoch_key"],),
            ).fetchone()
            if not row:
                raise RuntimeError("No quality epoch matches the current quality contract.")
        else:
            row = conn.execute(
                "SELECT * FROM quality_epochs WHERE id = ?",
                (epoch_id,),
            ).fetchone()
            if not row:
                raise RuntimeError(f"Quality epoch {epoch_id} does not exist.")
        epoch = dict(row)
        resolved_run_id = _finished_segment_state_run_id(conn, segment_state_run_id)
        if resolved_run_id is None:
            raise RuntimeError("No finished segment-state run is available for the quality epoch.")
        previous_run_id = epoch.get("segment_state_run_id")
        now = db.utc_now()
        conn.execute(
            "UPDATE quality_epochs SET segment_state_run_id = ?, updated_at = ? WHERE id = ?",
            (resolved_run_id, now, epoch["id"]),
        )
        _event(
            conn,
            int(epoch["id"]),
            "epoch_segment_state_attached",
            source,
            mutation_scope="metadata",
            details={
                "previous_segment_state_run_id": previous_run_id,
                "segment_state_run_id": resolved_run_id,
            },
        )
        conn.commit()
        return {
            "epoch_id": int(epoch["id"]),
            "epoch_key": epoch["epoch_key"],
            "status": epoch["status"],
            "previous_segment_state_run_id": previous_run_id,
            "segment_state_run_id": resolved_run_id,
        }


def close_noop_epoch(
    *,
    source: str,
    epoch_id: int | None = None,
    segment_state_run_id: int | None = None,
) -> dict[str, Any]:
    """Evaluate a scored epoch when baseline and output are identical.

    The operation is idempotent and skips normal changed epochs. A no-op epoch
    closes only after discovery and calibration policy persisted that there is
    no pairwise change cohort to evaluate.
    """

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        fingerprint = current_fingerprint(conn, settings)
        if epoch_id is None:
            row = conn.execute(
                "SELECT * FROM quality_epochs WHERE epoch_key = ? LIMIT 1",
                (fingerprint["epoch_key"],),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM quality_epochs WHERE id = ?",
                (epoch_id,),
            ).fetchone()
        if not row:
            raise RuntimeError("No quality epoch is available for no-op closure.")

        epoch = dict(row)
        result = {
            "epoch_id": int(epoch["id"]),
            "epoch_key": epoch["epoch_key"],
            "status": epoch.get("status"),
            "noop_evaluated": False,
            "reused": False,
        }
        if epoch.get("epoch_key") != fingerprint.get("epoch_key"):
            raise RuntimeError(
                f"Quality epoch {epoch['id']} does not match the current quality fingerprint."
            )
        if epoch.get("status") == "published":
            return {**result, "reason": "already_published", "reused": True}
        if epoch.get("status") not in {"scored", "evaluated"}:
            return {**result, "reason": f"epoch_status_{epoch.get('status') or 'missing'}"}

        baseline_tree_hash = str(epoch.get("baseline_tree_hash") or "")
        output_tree_hash = str(epoch.get("output_tree_hash") or "")
        if not baseline_tree_hash or baseline_tree_hash != output_tree_hash:
            return {**result, "reason": "package_trees_differ"}
        if epoch.get("old_score_run_id") != epoch.get("output_score_run_id"):
            return {**result, "reason": "score_runs_not_shared"}

        discovery = None
        if _table_exists(conn, "ml_quality_pattern_discovery_runs"):
            discovery = conn.execute(
                """
                SELECT id, status
                FROM ml_quality_pattern_discovery_runs
                WHERE quality_epoch_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (epoch["id"],),
            ).fetchone()
        if not discovery or discovery["status"] != "completed":
            return {**result, "reason": "discovery_not_completed"}

        calibration = None
        if _table_exists(conn, "ml_pairwise_calibration_policy_decisions"):
            calibration = conn.execute(
                """
                SELECT id, decision, candidate_count
                FROM ml_pairwise_calibration_policy_decisions
                WHERE quality_epoch_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (epoch["id"],),
            ).fetchone()
        if not calibration:
            return {**result, "reason": "calibration_policy_missing"}
        if int(calibration["candidate_count"] or 0) != 0:
            return {
                **result,
                "reason": "pairwise_candidates_present",
                "candidate_count": int(calibration["candidate_count"] or 0),
            }
        if str(calibration["decision"] or "") != "skip":
            return {
                **result,
                "reason": "calibration_not_skipped",
                "calibration_decision": calibration["decision"],
            }

        requested_state_run_id = (
            segment_state_run_id
            if segment_state_run_id is not None
            else epoch.get("segment_state_run_id")
        )
        resolved_run_id = _finished_segment_state_run_id(conn, requested_state_run_id)
        if resolved_run_id is None:
            raise RuntimeError(
                f"Quality epoch {epoch['id']} cannot close as no-op without a finished segment-state run."
            )
        if epoch.get("status") == "evaluated":
            return {
                **result,
                "status": "evaluated",
                "noop_evaluated": True,
                "reused": True,
                "reason": "already_evaluated",
                "segment_state_run_id": resolved_run_id,
            }

        now = db.utc_now()
        details = {
            "baseline_tree_hash": baseline_tree_hash,
            "output_tree_hash": output_tree_hash,
            "shared_score_run_id": int(epoch["output_score_run_id"]),
            "segment_state_run_id": resolved_run_id,
            "discovery_run_id": int(discovery["id"]),
            "calibration_policy_decision_id": int(calibration["id"]),
            "candidate_count": 0,
        }
        conn.execute(
            """
            UPDATE quality_epochs
            SET status = 'evaluated', evaluated_at = ?,
                segment_state_run_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, resolved_run_id, now, epoch["id"]),
        )
        _event(
            conn,
            int(epoch["id"]),
            "epoch_noop_evaluated",
            source,
            mutation_scope="lifecycle",
            details=details,
        )
        conn.commit()
        return {
            **result,
            "status": "evaluated",
            "noop_evaluated": True,
            "reason": "identical_package_no_candidates",
            "segment_state_run_id": resolved_run_id,
            "details": details,
        }


def mark_status(
    status: str,
    *,
    source: str,
    epoch_id: int | None = None,
    segment_state_run_id: int | None = None,
) -> dict[str, Any]:
    allowed = {"evaluated", "published", "stale"}
    if status not in allowed:
        raise ValueError(f"Unsupported quality epoch status: {status}")
    required = {"scored", "evaluated"} if status == "evaluated" else {"evaluated"}
    if epoch_id is None:
        current = validate_current(required)
    else:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM quality_epochs WHERE id = ?", (epoch_id,)).fetchone()
            if not row:
                raise RuntimeError(f"Quality epoch {epoch_id} does not exist.")
            epoch = dict(row)
            if epoch.get("status") not in required:
                raise RuntimeError(
                    f"Quality epoch {epoch_id} has status {epoch.get('status')}; expected {sorted(required)}."
                )
            current = {
                "epoch_id": int(epoch["id"]),
                "epoch_key": epoch["epoch_key"],
                "status": epoch["status"],
                "old_score_run_id": epoch.get("old_score_run_id"),
                "output_score_run_id": epoch.get("output_score_run_id"),
                "policy_run_id": epoch.get("policy_run_id"),
                "segment_state_run_id": epoch.get("segment_state_run_id"),
                "fingerprint_current": False,
            }
    now = db.utc_now()
    column = "evaluated_at" if status == "evaluated" else "published_at" if status == "published" else "invalidated_at"
    with db.connect() as conn:
        resolved_run_id = current.get("segment_state_run_id")
        event_details: dict[str, Any] = {}
        if status in {"evaluated", "published"}:
            resolved_run_id = _finished_segment_state_run_id(conn, segment_state_run_id)
            if resolved_run_id is None:
                raise RuntimeError(
                    f"Quality epoch {current['epoch_id']} cannot be marked {status} without a finished segment-state run."
                )
            event_details = {
                "previous_segment_state_run_id": current.get("segment_state_run_id"),
                "segment_state_run_id": resolved_run_id,
            }
            conn.execute(
                f"""
                UPDATE quality_epochs
                SET status = ?, {column} = ?, segment_state_run_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, resolved_run_id, now, current["epoch_id"]),
            )
        else:
            conn.execute(
                f"UPDATE quality_epochs SET status = ?, {column} = ?, updated_at = ? WHERE id = ?",
                (status, now, now, current["epoch_id"]),
            )
        _event(
            conn,
            current["epoch_id"],
            f"epoch_{status}",
            source,
            mutation_scope="lifecycle",
            details=event_details,
        )
        conn.commit()
    return {**current, "status": status, "segment_state_run_id": resolved_run_id}


def latest_epoch() -> dict[str, Any]:
    with db.connect() as conn:
        db.ensure_database(conn)
        row = conn.execute("SELECT * FROM quality_epochs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else {}


def _emit(payload: dict[str, Any]) -> None:
    print("[quality_epoch] " + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage immutable quality/scoring epochs.")
    sub = parser.add_subparsers(dest="command", required=True)
    open_parser = sub.add_parser("open")
    open_parser.add_argument("--source", default="diagnostic")
    finalize_parser = sub.add_parser("finalize-scoring")
    finalize_parser.add_argument("--source", default="diagnostic")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--require-status", default="scored,evaluated")
    mark_parser = sub.add_parser("mark")
    mark_parser.add_argument("status", choices=("evaluated", "published", "stale"))
    mark_parser.add_argument("--source", default="pipeline")
    mark_parser.add_argument("--epoch-id", type=int, default=None)
    mark_parser.add_argument("--segment-state-run-id", type=int, default=None)
    attach_parser = sub.add_parser("attach-state")
    attach_parser.add_argument("--source", default="pipeline")
    attach_parser.add_argument("--epoch-id", type=int, default=None)
    attach_parser.add_argument("--segment-state-run-id", type=int, default=None)
    close_noop_parser = sub.add_parser("close-noop")
    close_noop_parser.add_argument("--source", default="diagnostic")
    close_noop_parser.add_argument("--epoch-id", type=int, default=None)
    close_noop_parser.add_argument("--segment-state-run-id", type=int, default=None)
    sub.add_parser("latest")
    args = parser.parse_args()
    try:
        if args.command == "open":
            payload = open_epoch(source=args.source)
        elif args.command == "finalize-scoring":
            payload = finalize_scoring(source=args.source)
        elif args.command == "validate":
            payload = validate_current(args.require_status.split(","))
        elif args.command == "mark":
            payload = mark_status(
                args.status,
                source=args.source,
                epoch_id=args.epoch_id,
                segment_state_run_id=args.segment_state_run_id,
            )
        elif args.command == "attach-state":
            payload = attach_segment_state(
                source=args.source,
                epoch_id=args.epoch_id,
                segment_state_run_id=args.segment_state_run_id,
            )
        elif args.command == "close-noop":
            payload = close_noop_epoch(
                source=args.source,
                epoch_id=args.epoch_id,
                segment_state_run_id=args.segment_state_run_id,
            )
        else:
            payload = latest_epoch()
        _emit(payload)
        return 0
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
