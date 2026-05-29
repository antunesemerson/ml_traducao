from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "memory" / "translation_engine.sqlite"
LEARNING_STATUS_FILE = ROOT / "memory" / "learning_status.json"
PRODUCTION_RUN_STATUS_FILE = ROOT / "memory" / "production_run_status.json"
PRODUCTION_SNAPSHOT_ROOT = ROOT / "memory" / "production_snapshots"
PRODUCTION_RUN_LOCK = threading.Lock()
TRAINING_LOCK_FILES = (
    ROOT / "memory" / "training_in_progress.flag",
    ROOT / "memory" / "training_lock.json",
    ROOT / "memory" / "production_hold.json",
)


def _dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row else {}


def _one(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    return _dict(con.execute(sql, params).fetchone())


def _all(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = _one(
        con,
        """
        SELECT COUNT(*) AS total
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (name,),
    )
    return _int(row.get("total")) > 0


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _pct(part: Any, total: Any) -> float:
    total_num = _num(total)
    if total_num <= 0:
        return 0.0
    return round((_num(part) / total_num) * 100, 2)


def _short_model_name(version: str | None) -> str:
    if not version:
        return "sem modelo"
    if "_v" in version:
        return "v" + version.split("_v", 1)[1].split("_", 1)[0]
    return version


def _model_axis_label(row: dict[str, Any]) -> str:
    return f"#{row['id']} {_short_model_name(row.get('model_version'))}"


def _run_axis_label(run_id: Any) -> str:
    return str(_int(run_id))


def _latest_score(con: sqlite3.Connection, offset: int = 0, operational: bool = False) -> dict[str, Any]:
    where = "WHERE scored_count >= 10000" if operational else ""
    return _one(
        con,
        f"""
        SELECT *
        FROM ml_score_runs
        {where}
        ORDER BY id DESC
        LIMIT 1 OFFSET ?
        """,
        (offset,),
    )


def _active_model(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(
        con,
        """
        SELECT
          r.active_model_run_id,
          r.active_model_version,
          r.policy_version,
          r.promoted_at,
          r.reason,
          m.*
        FROM ml_model_registry r
        LEFT JOIN ml_model_runs m ON m.id = r.active_model_run_id
        WHERE r.model_kind = 'risk_action_classifier'
        LIMIT 1
        """,
    )


def _latest_model(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(
        con,
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind = 'risk_action_classifier'
        ORDER BY id DESC
        LIMIT 1
        """,
    )


def _latest_dataset(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(con, "SELECT * FROM ml_dataset_runs ORDER BY id DESC LIMIT 1")


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"read_error": str(exc), "path": str(path)}
    return parsed if isinstance(parsed, dict) else {"path": str(path), "read_error": "json_root_is_not_object"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _production_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _read_production_run_status() -> dict[str, Any]:
    return _read_json_file(PRODUCTION_RUN_STATUS_FILE)


def _write_production_run_status(status: dict[str, Any]) -> None:
    status["updated_at"] = _now_iso()
    PRODUCTION_RUN_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRODUCTION_RUN_STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _production_run_active() -> bool:
    status = _read_production_run_status()
    return status.get("status") in {"starting", "running"}


def _new_production_run_status(run_id: str) -> dict[str, Any]:
    stages = [
        {"id": "snapshot", "label": "Pre-run Snapshot", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "preflight_sync", "label": "Preflight Index Sync", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "segment_state_before", "label": "Segment State", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_general_dry_run", "label": "General Apply Dry-run", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_token_policy_dry_run", "label": "Token Policy Apply Dry-run", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_general_write", "label": "Write Regular Output", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_token_policy_write", "label": "Write Token-Policy Output", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_locked_override_write", "label": "Write Locked Manual Overrides", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "segment_state_after", "label": "Post-write Segment State", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "token_policy_after", "label": "Post-write Token Policy", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "composite_review_progress", "label": "Composite Review Progress", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "production_report", "label": "Production Report", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
    ]
    snapshot_path = PRODUCTION_SNAPSHOT_ROOT / run_id
    return {
        "run_id": run_id,
        "status": "starting",
        "mode": "full_production_apply",
        "apply_output": True,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "finished_at": "",
        "current_stage": "",
        "stages": stages,
        "log_path": str(ROOT / "logs" / f"{run_id}_production_run.log"),
        "report_path": str(ROOT / "reports" / f"{run_id}_production_run.txt"),
        "snapshot_path": str(snapshot_path),
        "snapshot_manifest_path": str(snapshot_path / "manifest.json"),
        "report_paths": [],
        "logs_tail": [],
        "message": "Full production run queued.",
    }


def _update_stage(run_status: dict[str, Any], stage_id: str, **updates: Any) -> None:
    for stage in run_status.get("stages", []):
        if stage.get("id") == stage_id:
            stage.update(updates)
            break


def _append_run_log(status: dict[str, Any], line: str) -> None:
    logs_tail = list(status.get("logs_tail") or [])
    logs_tail.append(line.rstrip())
    status["logs_tail"] = logs_tail[-120:]


def _update_stage_metric(run_status: dict[str, Any], stage_id: str, key: str, value: Any) -> None:
    for stage in run_status.get("stages", []):
        if stage.get("id") == stage_id:
            metrics = dict(stage.get("metrics") or {})
            metrics[key] = value
            stage["metrics"] = metrics
            break


def _parse_count_suffix(line: str) -> int | None:
    try:
        return int(line.rsplit(":", 1)[1].strip().replace(",", ""))
    except Exception:
        return None


def _path_size(path: Path, ignore_func=None) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        ignored = ignore_func(str(current), [child.name for child in children]) if ignore_func else set()
        for child in children:
            if child.name in ignored:
                continue
            if child.is_dir():
                stack.append(child)
            elif child.is_file():
                total += child.stat().st_size
    return total


def _snapshot_ignore(dir_path: str, names: list[str]) -> set[str]:
    ignored = {
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "dist",
        ".git",
    }
    path = Path(dir_path)
    if path.name == "memory":
        ignored.update(
            {
                "backups",
                "bkp banco",
                "production_snapshots",
                "translation_engine.sqlite",
                "translation_engine.sqlite-shm",
                "translation_engine.sqlite-wal",
            }
        )
        ignored.update(name for name in names if name.startswith("translation_engine_before_"))
    return {name for name in names if name in ignored}


def _copy_tree_snapshot(source: Path, target: Path) -> None:
    if source.exists():
        shutil.copytree(source, target, ignore=_snapshot_ignore, dirs_exist_ok=True)


def _sqlite_backup_snapshot(target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DEFAULT_DB)
    try:
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _create_production_snapshot(status: dict[str, Any], log_handle) -> None:
    stage_id = "snapshot"
    _update_stage(status, stage_id, status="running", started_at=_now_iso())
    status["status"] = "running"
    status["current_stage"] = stage_id
    status["message"] = "Creating pre-run snapshot"
    _write_production_run_status(status)

    snapshot_root = Path(status["snapshot_path"])
    if snapshot_root.exists():
        raise RuntimeError(f"Snapshot path already exists: {snapshot_root}")
    snapshot_root.mkdir(parents=True, exist_ok=False)

    sources = {
        "source": ROOT / "source",
        "output": ROOT / "output",
        "reports": ROOT / "reports",
        "logs": ROOT / "logs",
        "memory_light": ROOT / "memory",
    }
    optional_models = ROOT / "models"
    if optional_models.exists():
        sources["models"] = optional_models

    estimated_size = DEFAULT_DB.stat().st_size if DEFAULT_DB.exists() else 0
    estimated_size += sum(_path_size(path, _snapshot_ignore) for path in sources.values())
    free_space = shutil.disk_usage(snapshot_root).free
    required_space = int(estimated_size * 1.08)
    manifest: dict[str, Any] = {
        "run_id": status.get("run_id"),
        "created_at": _now_iso(),
        "snapshot_root": str(snapshot_root),
        "sqlite_backup": str(snapshot_root / "memory" / "translation_engine.sqlite"),
        "estimated_bytes": estimated_size,
        "required_bytes_with_margin": required_space,
        "free_bytes_before": free_space,
        "copied": [],
        "skipped": [
            "memory/backups",
            "memory/bkp banco",
            "memory/production_snapshots",
            "memory/translation_engine.sqlite-wal",
            "memory/translation_engine.sqlite-shm",
            "memory/translation_engine_before_*",
        ],
    }
    if free_space < required_space:
        manifest_path = snapshot_root / "manifest.json"
        manifest["error"] = "insufficient_free_space"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError(
            f"Insufficient disk space for production snapshot. Required {required_space} bytes, free {free_space} bytes."
        )

    log_handle.write("\n=== snapshot ===\n")
    log_handle.write(f"Snapshot root: {snapshot_root}\n")
    log_handle.write(f"Estimated bytes: {estimated_size}\n")
    log_handle.flush()
    _append_run_log(status, f"[snapshot] root: {snapshot_root}")
    _append_run_log(status, f"[snapshot] estimated bytes: {estimated_size}")

    _sqlite_backup_snapshot(snapshot_root / "memory" / "translation_engine.sqlite")
    manifest["copied"].append("memory/translation_engine.sqlite")
    _append_run_log(status, "[snapshot] SQLite backup completed")
    _write_production_run_status(status)

    for name, source_path in sources.items():
        target_name = "memory" if name == "memory_light" else name
        target_path = snapshot_root / target_name
        _copy_tree_snapshot(source_path, target_path)
        manifest["copied"].append(str(source_path.relative_to(ROOT)))
        _append_run_log(status, f"[snapshot] copied {source_path.relative_to(ROOT)}")
        _write_production_run_status(status)

    manifest_path = Path(status["snapshot_manifest_path"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _update_stage(status, stage_id, status="done", finished_at=_now_iso(), exit_code=0)
    status["message"] = "Pre-run snapshot completed"
    _write_production_run_status(status)


def _write_production_report(status: dict[str, Any]) -> None:
    report_path = Path(status["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "CK3 PT-BR production run report",
        f"Run id: {status.get('run_id')}",
        f"Mode: {status.get('mode')}",
        f"Apply output: {status.get('apply_output')}",
        f"Status: {status.get('status')}",
        f"Started at: {status.get('started_at')}",
        f"Finished at: {status.get('finished_at')}",
        f"Snapshot: {status.get('snapshot_path')}",
        f"Snapshot manifest: {status.get('snapshot_manifest_path')}",
        "",
        "Stages:",
    ]
    for stage in status.get("stages", []):
        lines.append(
            f"- {stage.get('id')}: {stage.get('status')} exit={stage.get('exit_code')} "
            f"started={stage.get('started_at')} finished={stage.get('finished_at')}"
        )
    lines.extend(
        [
            "",
            "Pipeline reports:",
            *[f"- {path}" for path in status.get("report_paths", [])],
            "",
            "Interpretation:",
            "- This production executor creates a snapshot before writing output.",
            "- Output writes are restricted to confirmed rows that pass the segment-token policy gate.",
            "- Remaining pending, low-confidence and token-policy items are reported for the learning front.",
            "",
            "Recent log tail:",
            *[f"- {line}" for line in (status.get("logs_tail") or [])[-40:]],
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_production_command(status: dict[str, Any], *, stage_id: str, command: list[str], log_handle) -> int:
    _update_stage(status, stage_id, status="running", started_at=_now_iso())
    status["status"] = "running"
    status["current_stage"] = stage_id
    status["message"] = f"Running {stage_id}"
    _write_production_run_status(status)
    log_handle.write(f"\n=== {stage_id} ===\n")
    log_handle.write(f"Command: {' '.join(command)}\n")
    log_handle.flush()

    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\n")
        log_handle.write(raw_line)
        if "Report:" in line:
            report_path = line.split("Report:", 1)[1].strip()
            if report_path and report_path not in status["report_paths"]:
                status["report_paths"].append(report_path)
        if "[apply_segment_state_updates]" in line:
            metric_map = {
                "Candidates inspected": "candidates",
                "Ready to apply": "ready",
                "Applied updates": "applied",
                "token_mismatch": "token_mismatch",
                "stale_token_policy_confirmed_hash": "stale_token_policy_confirmed_hash",
            }
            for label, key in metric_map.items():
                if label in line:
                    parsed = _parse_count_suffix(line)
                    if parsed is not None:
                        _update_stage_metric(status, stage_id, key, parsed)
        _append_run_log(status, line)
    exit_code = process.wait()
    log_handle.flush()
    _update_stage(
        status,
        stage_id,
        status="done" if exit_code == 0 else "failed",
        finished_at=_now_iso(),
        exit_code=exit_code,
    )
    _write_production_run_status(status)
    return exit_code


def _production_run_worker(run_id: str) -> None:
    status = _new_production_run_status(run_id)
    log_path = Path(status["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _write_production_run_status(status)
    commands = [
        ("preflight_sync", [sys.executable, "pipeline/main.py", "cycle"]),
        ("segment_state_before", [sys.executable, "pipeline/main.py", "segment-state"]),
        (
            "apply_general_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
            ],
        ),
        (
            "apply_token_policy_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
                "--segment-require-token-policy-decision",
            ],
        ),
        (
            "apply_general_write",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
                "--auto-apply",
            ],
        ),
        (
            "apply_token_policy_write",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
                "--segment-require-token-policy-decision",
                "--auto-apply",
            ],
        ),
        (
            "apply_locked_override_write",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-review-states",
                "human_locked,human_confirmed",
                "--segment-include-intentional-blank",
                "--segment-allow-locked-token-override",
                "--auto-apply",
            ],
        ),
        ("segment_state_after", [sys.executable, "pipeline/main.py", "segment-state"]),
        (
            "token_policy_after",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-token-policy",
                "--segment-include-auto-confirmed",
            ],
        ),
        ("composite_review_progress", [sys.executable, "pipeline/main.py", "ml-composite-review-progress"]),
    ]
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
            log_handle.write(f"CK3 PT-BR production run {run_id}\n")
            log_handle.write("Mode: full_production_apply\n")
            _create_production_snapshot(status, log_handle)
            for stage_id, command in commands:
                exit_code = _run_production_command(status, stage_id=stage_id, command=command, log_handle=log_handle)
                if exit_code != 0:
                    status["status"] = "failed"
                    status["message"] = f"Stage failed: {stage_id}"
                    break
            if status.get("status") != "failed":
                status["status"] = "completed"
                status["message"] = "Full production run completed."
            status["finished_at"] = _now_iso()
            _update_stage(status, "production_report", status="running", started_at=_now_iso())
            _write_production_report(status)
            _update_stage(status, "production_report", status="done", finished_at=_now_iso(), exit_code=0)
            _write_production_run_status(status)
    except Exception as exc:  # pragma: no cover - background production path
        current_stage = status.get("current_stage")
        if current_stage:
            _update_stage(status, current_stage, status="failed", finished_at=_now_iso(), exit_code=1)
        status["status"] = "failed"
        status["message"] = f"Production run failed: {exc}"
        status["finished_at"] = _now_iso()
        _append_run_log(status, status["message"])
        _write_production_report(status)
        _write_production_run_status(status)


def _start_production_run() -> dict[str, Any]:
    with PRODUCTION_RUN_LOCK:
        if _production_run_active():
            status = _read_production_run_status()
            return {"accepted": False, "status": "already_running", "run": status}
        run_id = _production_run_id()
        status = _new_production_run_status(run_id)
        _write_production_run_status(status)
        thread = threading.Thread(target=_production_run_worker, args=(run_id,), daemon=True)
        thread.start()
        return {"accepted": True, "status": "running", "run": status}


def _training_lock_payload(con: sqlite3.Connection) -> dict[str, Any]:
    file_locks = []
    for path in TRAINING_LOCK_FILES:
        if not path.exists():
            continue
        payload: dict[str, Any] = {"path": str(path), "reason": "lock_file_present"}
        if path.suffix == ".json":
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    payload.update(parsed)
            except Exception as exc:
                payload["read_error"] = str(exc)
        file_locks.append(payload)

    unfinished_model = {}
    if _table_exists(con, "ml_model_runs"):
        unfinished_model = _one(
            con,
            """
            SELECT id, model_kind, model_version, started_at, finished_at
            FROM ml_model_runs
            WHERE finished_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
        )

    locked = bool(file_locks or unfinished_model)
    if file_locks:
        reason = "training_or_learning_lock_file"
    elif unfinished_model:
        reason = "unfinished_model_run"
    else:
        reason = "released_for_production"
    return {
        "locked": locked,
        "reason": reason,
        "file_locks": file_locks,
        "unfinished_model_run": unfinished_model,
        "message": "Producao liberada" if not locked else "Producao bloqueada ate o chat de aprendizado liberar",
    }


def _learning_status_payload(con: sqlite3.Connection) -> dict[str, Any]:
    status = _read_json_file(LEARNING_STATUS_FILE)
    lock = _training_lock_payload(con)
    if not status:
        status = {
            "schema_version": 1,
            "owner": "",
            "status": "idle",
            "production_safe": not bool(lock.get("locked")),
            "objective": "No active learning cycle",
            "current_phase": "",
            "current_phase_label": "",
            "phase_total": 0,
            "phase_completed": 0,
            "phase_pending": 0,
            "progress_pct": 100.0 if not lock.get("locked") else 0.0,
            "phases": [],
            "last_report": "",
            "blockers": [],
            "warnings": [],
            "next_action": "Production may run" if not lock.get("locked") else "Wait for learning release",
        }
    status["lock"] = lock
    status["production_safe"] = bool(status.get("production_safe")) and not bool(lock.get("locked"))
    status["can_start_production"] = status["production_safe"]
    status["gate_message"] = (
        "Learning front released production"
        if status["can_start_production"]
        else "Production blocked by learning front"
    )
    return status


def _production_payload(con: sqlite3.Connection) -> dict[str, Any]:
    lifecycle = _lifecycle_payload(con)
    agents = _agents_payload(con)
    summary = lifecycle.get("summary") or {}
    output_application = lifecycle.get("outputApplication") or []
    output_apply = lifecycle.get("outputApply") or {}
    active_gate = agents.get("activeGate") or {}
    latest_apply_runs = output_apply.get("runs") or []
    last_apply = next((row for row in latest_apply_runs if _int(row.get("apply")) == 1), latest_apply_runs[0] if latest_apply_runs else {})
    lock = _training_lock_payload(con)
    learning_status = _learning_status_payload(con)

    active_segments = _int(summary.get("total_segments"))
    closed = _int(summary.get("closed_count"))
    pending = _int(summary.get("pending_count"))
    needs_apply = _int(summary.get("output_apply_pending_count"))
    applied_segments = sum(
        _int(row.get("total"))
        for row in output_application
        if row.get("apply_state") == "applied"
    )
    blocked_critical = _int(active_gate.get("invalid_release_count"))
    auto_apply_allowed = bool(_int(active_gate.get("auto_apply_allowed")))
    readiness_status = "blocked"
    recommended_action = "review_blockers"
    if not learning_status.get("can_start_production"):
        readiness_status = "learning_locked"
        recommended_action = "wait_learning_release"
    elif blocked_critical > 0 or auto_apply_allowed:
        readiness_status = "blocked"
        recommended_action = "review_governance"
    elif pending > 0:
        readiness_status = "ready_with_known_issues"
        recommended_action = "review_pending_or_run_game_test"
    else:
        readiness_status = "ready_for_game_test"
        recommended_action = "run_game_test"

    stages = [
        {
            "id": "source_audit",
            "label": "Source Audit",
            "status": "done",
            "total": active_segments,
            "completed": active_segments,
            "pending": 0,
            "last_report": "",
        },
        {
            "id": "mirror_check",
            "label": "Mirror Check",
            "status": "done" if active_segments else "pending",
            "total": active_segments,
            "completed": active_segments,
            "pending": 0,
            "last_report": "",
        },
        {
            "id": "indexing",
            "label": "Indexing",
            "status": "done" if active_segments else "pending",
            "total": active_segments,
            "completed": active_segments,
            "pending": 0,
            "last_report": "",
        },
        {
            "id": "memory_apply",
            "label": "Memory Apply",
            "status": "done",
            "total": active_segments,
            "completed": closed,
            "pending": pending,
            "last_report": "",
        },
        {
            "id": "deterministic_guards",
            "label": "Deterministic Guards",
            "status": "done" if blocked_critical == 0 else "blocked",
            "total": _int(active_gate.get("guarded_release_count")),
            "completed": _int(active_gate.get("guarded_release_count")) - blocked_critical,
            "pending": blocked_critical,
            "last_report": active_gate.get("latest_report_path") or "",
        },
        {
            "id": "ml_network",
            "label": "ML Network",
            "status": "done" if _int(active_gate.get("active_overlay_run_id")) else "pending",
            "total": _int(active_gate.get("guarded_release_count")),
            "completed": _int(active_gate.get("guarded_release_count")),
            "pending": 0,
            "last_report": active_gate.get("latest_report_path") or "",
        },
        {
            "id": "controlled_output_apply",
            "label": "Controlled Output Apply",
            "status": "blocked" if needs_apply else "done",
            "total": _int(last_apply.get("candidates_count")) or needs_apply + _int(last_apply.get("applied_count")),
            "completed": _int(last_apply.get("applied_count")),
            "pending": needs_apply,
            "last_report": last_apply.get("report_path") or "",
        },
        {
            "id": "final_validation",
            "label": "Final Validation",
            "status": "pending" if pending else "done",
            "total": active_segments,
            "completed": closed,
            "pending": pending,
            "last_report": "",
        },
        {
            "id": "release_report",
            "label": "Release Report",
            "status": "pending",
            "total": 1,
            "completed": 0,
            "pending": 1,
            "last_report": "",
        },
        {
            "id": "learning_handoff",
            "label": "Learning Handoff",
            "status": "pending" if pending else "done",
            "total": pending,
            "completed": 0 if pending else 1,
            "pending": pending,
            "last_report": "",
        },
    ]

    blockers = []
    if not learning_status.get("can_start_production"):
        blockers.append(
            {
                "type": "learning_lock",
                "count": 1,
                "message": learning_status.get("gate_message") or lock.get("message"),
            }
        )
    if needs_apply:
        blockers.append({"type": "token_policy", "count": needs_apply, "message": "Outputs confirmados ainda precisam de politica de token ou revalidacao"})
    if pending:
        blockers.append({"type": "operational_pending", "count": pending, "message": "Pendencias conhecidas para revisao, autofix ou aprendizado"})
    if blocked_critical:
        blockers.append({"type": "critical_gate", "count": blocked_critical, "message": "Gate ativo encontrou releases invalidos"})

    return {
        "status": "blocked" if not learning_status.get("can_start_production") else "idle",
        "active_run_id": (_read_production_run_status() or {}).get("run_id") if (_read_production_run_status() or {}).get("status") in {"starting", "running"} else None,
        "run": _read_production_run_status(),
        "lock": lock,
        "learning": {
            "status": learning_status.get("status"),
            "production_safe": learning_status.get("production_safe"),
            "can_start_production": learning_status.get("can_start_production"),
            "objective": learning_status.get("objective"),
            "current_phase": learning_status.get("current_phase"),
            "current_phase_label": learning_status.get("current_phase_label"),
            "progress_pct": learning_status.get("progress_pct"),
            "last_report": learning_status.get("last_report"),
            "next_action": learning_status.get("next_action"),
        },
        "source": {
            "changed": False,
            "last_index_at": summary.get("started_at") or "",
            "active_segments": active_segments,
        },
        "output": {
            "changed_files": _int(last_apply.get("files_touched_count")),
            "structure_valid": True,
            "needs_apply": needs_apply,
            "last_apply_count": _int(last_apply.get("applied_count")),
        },
        "gate": {
            "active_overlay_run_id": _int(active_gate.get("active_overlay_run_id")),
            "auto_apply_allowed": auto_apply_allowed,
            "invalid_releases": blocked_critical,
            "guarded_releases": _int(active_gate.get("guarded_release_count")),
        },
        "readiness": {
            "status": readiness_status,
            "closed_pct": _pct(closed, active_segments),
            "pending_operational": pending,
            "blocked_critical": blocked_critical,
            "recommended_action": recommended_action,
        },
        "summary": {
            "active_segments": active_segments,
            "closed_segments": closed,
            "closed_pct": _pct(closed, active_segments),
            "pending_operational": pending,
            "applied": applied_segments,
            "needs_apply": needs_apply,
            "valid_blank": _int(summary.get("blank_valid_count")),
            "intentional_blank": _int(summary.get("blank_intentional_count")),
            "blocked_critical": blocked_critical,
        },
        "stages": stages,
        "logs": [
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "stage": "production-status",
                "level": "info",
                "message": f"Estado de producao: {readiness_status}",
            }
        ],
        "blockers": blockers,
        "links": {
            "analytic_dashboard": "http://127.0.0.1:5173/#Operational",
            "managerial_dashboard": "http://127.0.0.1:5173/#Managerial",
            "latest_report": last_apply.get("report_path") or "",
        },
    }


def _dataset_negative_coverage(con: sqlite3.Connection, dataset_run_id: Any) -> float:
    row = _one(
        con,
        """
        SELECT negative_count, total_count
        FROM ml_dataset_runs
        WHERE id = ?
        """,
        (_int(dataset_run_id),),
    )
    return round(_pct(row.get("negative_count"), row.get("total_count")) / 100, 4)


def _policy_payload(con: sqlite3.Connection) -> dict[str, Any]:
    exists = _one(
        con,
        """
        SELECT COUNT(*) AS total
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('ml_policy_runs', 'ml_policy_items')
        """,
    )
    if _int(exists.get("total")) < 2:
        return {"available": False, "message": "Nenhuma politica operacional executada ainda"}

    summary = _one(
        con,
        """
        SELECT
          pr.id AS policy_run_id,
          pr.rule_version,
          pr.score_run_id,
          pr.model_run_id,
          pr.model_version,
          pr.scored_count,
          pr.active_auto_safe_count,
          pr.policy_auto_safe_count,
          pr.new_safe_count,
          pr.demoted_safe_count,
          pr.protect_active_safe,
          ROUND(100.0 * pr.active_auto_safe_count / NULLIF(pr.scored_count, 0), 2) AS active_auto_safe_pct,
          ROUND(100.0 * pr.policy_auto_safe_count / NULLIF(pr.scored_count, 0), 2) AS policy_auto_safe_pct,
          ROUND(100.0 * pr.new_safe_count / NULLIF(pr.scored_count, 0), 4) AS new_safe_pct,
          pr.started_at,
          pr.finished_at
        FROM ml_policy_runs pr
        ORDER BY pr.id DESC
        LIMIT 1
        """,
    )
    if not summary:
        return {"available": False, "message": "Nenhuma politica operacional executada ainda"}

    policy_run_id = _int(summary.get("policy_run_id"))
    comparison_rows = _all(
        con,
        """
        WITH score_counts AS (
          SELECT
            'score' AS source,
            msi.final_action AS action,
            COUNT(*) AS total
          FROM ml_policy_runs pr
          JOIN ml_score_items msi ON msi.run_id = pr.score_run_id
          WHERE pr.id = ?
          GROUP BY msi.final_action
        ),
        policy_counts AS (
          SELECT
            'policy' AS source,
            mpi.policy_action AS action,
            COUNT(*) AS total
          FROM ml_policy_items mpi
          WHERE mpi.run_id = ?
          GROUP BY mpi.policy_action
        )
        SELECT source, action, total
        FROM score_counts
        UNION ALL
        SELECT source, action, total
        FROM policy_counts
        ORDER BY source, action
        """,
        (policy_run_id, policy_run_id),
    )
    action_order = ["auto_safe", "needs_human", "needs_autofix", "blocked_structure"]
    comparison_by_action = {
        action: {"action": action, "score": 0, "policy": 0}
        for action in action_order
    }
    for row in comparison_rows:
        action = row.get("action")
        if action not in comparison_by_action:
            comparison_by_action[action] = {"action": action, "score": 0, "policy": 0}
        comparison_by_action[action][row["source"]] = _int(row.get("total"))
    comparison = [comparison_by_action[action] for action in comparison_by_action]

    group_gain = _all(
        con,
        """
        SELECT
          policy_group,
          COUNT(*) AS total_segments,
          SUM(CASE WHEN score_final_action = 'auto_safe' THEN 1 ELSE 0 END) AS score_auto_safe,
          SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
          SUM(new_safe) AS new_safe,
          SUM(CASE WHEN new_safe = 1 AND learned_positive = 1 THEN 1 ELSE 0 END) AS new_safe_learned_positive,
          SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS learned_negative_count,
          MIN(policy_threshold) AS min_threshold,
          MAX(policy_threshold) AS max_threshold,
          ROUND(100.0 * SUM(new_safe) / NULLIF(COUNT(*), 0), 4) AS new_safe_pct_in_group,
          ROUND(100.0 * SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS policy_auto_safe_pct
        FROM ml_policy_items
        WHERE run_id = ?
        GROUP BY policy_group
        ORDER BY new_safe DESC, policy_group
        LIMIT 12
        """,
        (policy_run_id,),
    )

    audit_items = _all(
        con,
        """
        SELECT
          mpi.segment_id,
          mpi.policy_group,
          mpi.relative_path,
          mpi.source_key,
          mpi.model_safe_probability,
          mpi.policy_threshold,
          mpi.policy_require_learned_positive,
          mpi.score_final_action,
          mpi.policy_action,
          mpi.learned_positive,
          mpi.learned_negative,
          msi.candidate_text,
          os.portuguese_text AS output_text,
          mpi.reasons_json
        FROM ml_policy_items mpi
        JOIN ml_score_items msi ON msi.id = mpi.score_item_id
        LEFT JOIN output_segments os ON os.segment_id = mpi.segment_id
        WHERE mpi.run_id = ?
          AND mpi.new_safe = 1
        ORDER BY mpi.policy_group, mpi.model_safe_probability DESC, mpi.segment_id
        LIMIT 120
        """,
        (policy_run_id,),
    )

    history = _all(
        con,
        """
        SELECT
          id AS policy_run_id,
          rule_version,
          score_run_id,
          model_version,
          scored_count,
          active_auto_safe_count,
          policy_auto_safe_count,
          new_safe_count,
          demoted_safe_count,
          ROUND(100.0 * policy_auto_safe_count / NULLIF(scored_count, 0), 2) AS policy_auto_safe_pct,
          started_at,
          finished_at
        FROM ml_policy_runs
        ORDER BY id ASC
        """,
    )

    return {
        "available": True,
        "summary": summary,
        "comparison": comparison,
        "groupGain": group_gain,
        "auditItems": audit_items,
        "history": history,
    }


def _lab_payload(con: sqlite3.Connection) -> dict[str, Any]:
    summary = _one(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT *
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_model AS (
          SELECT m.*
          FROM ml_model_runs m
          JOIN active_registry ar ON ar.active_model_run_id = m.id
        ),
        candidate_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM active_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_promotion AS (
          SELECT *
          FROM ml_model_promotions
          WHERE candidate_model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          cm.id AS candidate_model_run_id,
          cm.model_version AS candidate_model_version,
          cm.dataset_run_id AS candidate_dataset_run_id,
          cm.accuracy AS candidate_accuracy,
          cm.macro_f1 AS candidate_macro_f1,
          cm.false_safe_count AS candidate_false_safe_count,
          cm.false_safe_rate AS candidate_false_safe_rate,
          cm.safe_precision AS candidate_safe_precision,
          cm.safe_recall AS candidate_safe_recall,
          cm.safe_threshold AS candidate_safe_threshold,
          cs.id AS candidate_score_run_id,
          cs.scored_count AS candidate_scored_count,
          cs.final_auto_safe_count AS candidate_auto_safe_count,
          ROUND(100.0 * cs.final_auto_safe_count / NULLIF(cs.scored_count, 0), 2) AS candidate_auto_safe_pct,
          am.id AS active_model_run_id,
          am.model_version AS active_model_version,
          am.safe_precision AS active_safe_precision,
          am.safe_recall AS active_safe_recall,
          am.macro_f1 AS active_macro_f1,
          ast.id AS active_score_run_id,
          ast.scored_count AS active_scored_count,
          ast.final_auto_safe_count AS active_auto_safe_count,
          ROUND(100.0 * ast.final_auto_safe_count / NULLIF(ast.scored_count, 0), 2) AS active_auto_safe_pct,
          cp.id AS promotion_run_id,
          cp.decision AS promotion_decision,
          cp.policy_version AS promotion_policy_version,
          cp.reason AS promotion_reason,
          cp.created_at AS promotion_created_at
        FROM candidate_model cm
        CROSS JOIN active_model am
        LEFT JOIN candidate_score cs ON 1 = 1
        LEFT JOIN active_score ast ON 1 = 1
        LEFT JOIN candidate_promotion cp ON 1 = 1
        """,
    )
    if not summary:
        return {"available": False, "message": "Nenhum modelo experimental encontrado"}

    bars_rows = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          'active' AS model_role,
          id AS score_run_id,
          model_run_id,
          scored_count,
          final_auto_safe_count AS auto_safe,
          needs_human_count AS needs_human,
          needs_autofix_count AS needs_autofix,
          blocked_structure_count AS blocked_structure,
          deterministic_block_count AS deterministic_blocks,
          ROUND(100.0 * final_auto_safe_count / NULLIF(scored_count, 0), 2) AS auto_safe_pct
        FROM active_score
        UNION ALL
        SELECT
          'candidate' AS model_role,
          id AS score_run_id,
          model_run_id,
          scored_count,
          final_auto_safe_count AS auto_safe,
          needs_human_count AS needs_human,
          needs_autofix_count AS needs_autofix,
          blocked_structure_count AS blocked_structure,
          deterministic_block_count AS deterministic_blocks,
          ROUND(100.0 * final_auto_safe_count / NULLIF(scored_count, 0), 2) AS auto_safe_pct
        FROM candidate_score
        """,
    )
    role_rows = {row["model_role"]: row for row in bars_rows}
    action_comparison = []
    for key, label in [
        ("auto_safe", "Auto-safe"),
        ("needs_human", "Human"),
        ("needs_autofix", "Autofix"),
        ("blocked_structure", "Blocked"),
    ]:
        action_comparison.append(
            {
                "action": label,
                "active": _int(role_rows.get("active", {}).get(key)),
                "candidate": _int(role_rows.get("candidate", {}).get(key)),
            }
        )

    recent_models = _all(
        con,
        """
        WITH latest_score_by_model AS (
          SELECT model_run_id, MAX(id) AS latest_score_run_id
          FROM ml_score_runs
          GROUP BY model_run_id
        ),
        latest_promotion_by_model AS (
          SELECT candidate_model_run_id, MAX(id) AS latest_promotion_id
          FROM ml_model_promotions
          GROUP BY candidate_model_run_id
        )
        SELECT
          m.id AS model_run_id,
          m.model_version,
          m.dataset_run_id,
          m.safe_threshold,
          m.accuracy,
          m.macro_f1,
          m.false_safe_count,
          m.false_safe_rate,
          m.safe_precision,
          m.safe_recall,
          s.id AS latest_score_run_id,
          s.scored_count,
          s.final_auto_safe_count,
          ROUND(100.0 * s.final_auto_safe_count / NULLIF(s.scored_count, 0), 2) AS operational_auto_safe_pct,
          p.decision AS latest_promotion_decision,
          p.reason AS latest_promotion_reason,
          m.started_at,
          m.finished_at
        FROM ml_model_runs m
        LEFT JOIN latest_score_by_model lsm ON lsm.model_run_id = m.id
        LEFT JOIN ml_score_runs s ON s.id = lsm.latest_score_run_id
        LEFT JOIN latest_promotion_by_model lpm ON lpm.candidate_model_run_id = m.id
        LEFT JOIN ml_model_promotions p ON p.id = lpm.latest_promotion_id
        WHERE m.model_kind = 'risk_action_classifier'
        ORDER BY m.id DESC
        LIMIT 30
        """,
    )
    recent_models.reverse()

    candidate_distribution = _all(
        con,
        """
        WITH candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          final_action,
          risk_class,
          COUNT(*) AS total,
          ROUND(AVG(model_safe_probability), 4) AS avg_safe_probability,
          SUM(CASE WHEN token_status <> 'ok' THEN 1 ELSE 0 END) AS token_mismatch_count,
          SUM(CASE WHEN deterministic_blocked = 1 THEN 1 ELSE 0 END) AS deterministic_blocked_count
        FROM ml_score_items
        WHERE run_id = (SELECT id FROM candidate_score)
        GROUP BY final_action, risk_class
        ORDER BY total DESC
        """,
    )

    file_regressions = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        joined AS (
          SELECT
            a.segment_id,
            a.relative_path,
            a.source_key,
            a.final_action AS active_action,
            c.final_action AS candidate_action,
            c.model_safe_probability AS candidate_safe_probability
          FROM ml_score_items a
          JOIN ml_score_items c ON c.segment_id = a.segment_id
          WHERE a.run_id = (SELECT id FROM active_score)
            AND c.run_id = (SELECT id FROM candidate_score)
        )
        SELECT
          relative_path,
          COUNT(*) AS compared_segments,
          SUM(CASE WHEN active_action = 'auto_safe' AND candidate_action <> 'auto_safe' THEN 1 ELSE 0 END) AS operational_regressions,
          SUM(CASE WHEN active_action <> 'auto_safe' AND candidate_action = 'auto_safe' THEN 1 ELSE 0 END) AS candidate_recoveries,
          SUM(CASE WHEN active_action = 'auto_safe' AND candidate_action = 'auto_safe' THEN 1 ELSE 0 END) AS both_auto_safe,
          SUM(CASE WHEN active_action <> 'auto_safe' AND candidate_action <> 'auto_safe' THEN 1 ELSE 0 END) AS both_not_safe,
          ROUND(AVG(candidate_safe_probability), 4) AS avg_candidate_safe_probability
        FROM joined
        GROUP BY relative_path
        ORDER BY operational_regressions DESC, candidate_recoveries DESC
        LIMIT 50
        """,
    )

    regression_audit = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          c.segment_id,
          c.relative_path,
          c.source_key,
          a.final_action AS active_action,
          c.final_action AS candidate_action,
          c.model_safe_probability AS candidate_safe_probability,
          c.risk_class AS candidate_risk_class,
          c.token_status,
          c.issue_count,
          c.deterministic_blocked,
          c.candidate_text,
          c.reasons_json
        FROM ml_score_items a
        JOIN ml_score_items c ON c.segment_id = a.segment_id
        WHERE a.run_id = (SELECT id FROM active_score)
          AND c.run_id = (SELECT id FROM candidate_score)
          AND a.final_action = 'auto_safe'
          AND c.final_action <> 'auto_safe'
        ORDER BY c.model_safe_probability DESC, c.relative_path, c.source_key
        LIMIT 500
        """,
    )

    return {
        "available": True,
        "summary": summary,
        "actionComparison": action_comparison,
        "recentModels": recent_models,
        "candidateDistribution": candidate_distribution,
        "fileRegressions": file_regressions,
        "regressionAudit": regression_audit,
        "gaps": {
            "auto_safe_gap_count": _int(summary.get("active_auto_safe_count")) - _int(summary.get("candidate_auto_safe_count")),
            "auto_safe_gap_pct_points": round(_num(summary.get("active_auto_safe_pct")) - _num(summary.get("candidate_auto_safe_pct")), 2),
        },
    }


def _specialists_payload(con: sqlite3.Connection) -> dict[str, Any]:
    overview = _all(
        con,
        """
        WITH ranked AS (
          SELECT
            m.*,
            ROW_NUMBER() OVER (PARTITION BY m.model_kind ORDER BY m.id DESC) AS rn
          FROM ml_model_runs m
          WHERE m.model_kind LIKE 'specialist_%'
            AND m.finished_at IS NOT NULL
        ),
        latest_score AS (
          SELECT model_run_id, MAX(id) AS score_run_id
          FROM ml_score_runs
          WHERE finished_at IS NOT NULL
          GROUP BY model_run_id
        )
        SELECT
          r.model_kind,
          r.model_version,
          r.id AS model_run_id,
          r.dataset_run_id,
          r.training_examples,
          r.test_examples,
          r.safe_threshold,
          r.accuracy,
          r.macro_f1,
          r.safe_precision,
          r.safe_recall,
          r.false_safe_count,
          s.score_run_id,
          sr.scored_count,
          sr.final_auto_safe_count,
          ROUND(100.0 * sr.final_auto_safe_count / NULLIF(sr.scored_count, 0), 2) AS auto_safe_pct,
          r.finished_at,
          d.positive_count AS positive_examples,
          d.negative_count AS negative_examples
        FROM ranked r
        LEFT JOIN latest_score s ON s.model_run_id = r.id
        LEFT JOIN ml_score_runs sr ON sr.id = s.score_run_id
        LEFT JOIN ml_dataset_runs d ON d.id = r.dataset_run_id
        WHERE r.rn = 1
        ORDER BY r.model_kind
        """,
    )

    auditor_by_specialist = _all(
        con,
        """
        WITH general_score AS (
          SELECT r.id AS general_score_run_id
          FROM ml_score_runs r
          JOIN ml_model_runs m ON m.id = r.model_run_id
          WHERE m.model_kind = 'risk_action_classifier'
            AND r.finished_at IS NOT NULL
          ORDER BY r.id DESC
          LIMIT 1
        ),
        latest_specialist_score AS (
          SELECT
            m.model_kind,
            MAX(r.id) AS specialist_score_run_id
          FROM ml_score_runs r
          JOIN ml_model_runs m ON m.id = r.model_run_id
          WHERE m.model_kind LIKE 'specialist_%'
            AND r.finished_at IS NOT NULL
          GROUP BY m.model_kind
        ),
        joined AS (
          SELECT
            lss.model_kind,
            g.run_id AS general_score_run_id,
            s.run_id AS specialist_score_run_id,
            s.segment_id,
            s.relative_path,
            s.source_key,
            g.final_action AS general_action,
            s.final_action AS specialist_action,
            g.model_safe_probability AS general_probability,
            s.model_safe_probability AS specialist_probability
          FROM latest_specialist_score lss
          JOIN ml_score_items s ON s.run_id = lss.specialist_score_run_id
          JOIN ml_score_items g
            ON g.segment_id = s.segment_id
           AND g.run_id = (SELECT general_score_run_id FROM general_score)
        )
        SELECT
          model_kind,
          general_score_run_id,
          specialist_score_run_id,
          COUNT(*) AS compared,
          SUM(CASE WHEN general_action = 'auto_safe' AND specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS auto_safe_agree,
          SUM(CASE WHEN general_action <> 'auto_safe' AND specialist_action <> 'auto_safe' THEN 1 ELSE 0 END) AS needs_human_agree,
          SUM(CASE WHEN general_action <> 'auto_safe' AND specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS specialist_new_safe_review,
          SUM(CASE WHEN general_action = 'auto_safe' AND specialist_action <> 'auto_safe' THEN 1 ELSE 0 END) AS specialist_demoted_review,
          ROUND(
            100.0 * SUM(CASE WHEN general_action <> specialist_action THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0),
            2
          ) AS review_required_pct
        FROM joined
        GROUP BY model_kind, general_score_run_id, specialist_score_run_id
        ORDER BY specialist_new_safe_review DESC, specialist_demoted_review DESC
        """,
    )

    selected_kind = "specialist_title_names"
    if not any(row.get("model_kind") == selected_kind for row in auditor_by_specialist) and auditor_by_specialist:
        selected_kind = auditor_by_specialist[0]["model_kind"]

    auditor_queue = []
    if selected_kind:
        auditor_queue = _all(
            con,
            """
            WITH general_score AS (
              SELECT r.id AS general_score_run_id
              FROM ml_score_runs r
              JOIN ml_model_runs m ON m.id = r.model_run_id
              WHERE m.model_kind = 'risk_action_classifier'
                AND r.finished_at IS NOT NULL
              ORDER BY r.id DESC
              LIMIT 1
            ),
            specialist_score AS (
              SELECT r.id AS specialist_score_run_id
              FROM ml_score_runs r
              JOIN ml_model_runs m ON m.id = r.model_run_id
              WHERE m.model_kind = ?
                AND r.finished_at IS NOT NULL
              ORDER BY r.id DESC
              LIMIT 1
            )
            SELECT
              s.segment_id,
              s.relative_path,
              s.source_key,
              s.candidate_text,
              g.final_action AS general_action,
              s.final_action AS specialist_action,
              CASE
                WHEN g.final_action <> 'auto_safe' AND s.final_action = 'auto_safe' THEN 'specialist_new_safe_review'
                WHEN g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe' THEN 'specialist_demoted_review'
                ELSE 'agree'
              END AS auditor_action,
              g.model_safe_probability AS general_probability,
              s.model_safe_probability AS specialist_probability,
              s.token_status,
              s.issue_count,
              s.reasons_json
            FROM ml_score_items s
            JOIN ml_score_items g
              ON g.segment_id = s.segment_id
             AND g.run_id = (SELECT general_score_run_id FROM general_score)
            WHERE s.run_id = (SELECT specialist_score_run_id FROM specialist_score)
              AND (
                (g.final_action <> 'auto_safe' AND s.final_action = 'auto_safe')
                OR
                (g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe')
              )
            ORDER BY
              CASE WHEN g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe' THEN 0 ELSE 1 END,
              s.model_safe_probability DESC,
              s.segment_id
            LIMIT 300
            """,
            (selected_kind,),
        )

    learning_rows = _all(
        con,
        """
        SELECT
          focus_group,
          human_label,
          COUNT(*) AS total,
          SUM(CASE WHEN corrected_text IS NOT NULL AND TRIM(corrected_text) <> '' THEN 1 ELSE 0 END) AS corrected
        FROM local_learning_candidates
        WHERE queue_source = 'ml_specialist_auditor'
          AND human_label <> 'pending'
        GROUP BY focus_group, human_label
        ORDER BY focus_group, human_label
        """,
    )
    learning_by_label: dict[str, dict[str, Any]] = {}
    learning_by_focus: dict[str, dict[str, Any]] = {}
    for row in learning_rows:
        label = row["human_label"]
        focus = row["focus_group"] or "unknown"
        learning_by_label.setdefault(label, {"human_label": label, "total": 0, "corrected": 0})
        learning_by_label[label]["total"] += _int(row.get("total"))
        learning_by_label[label]["corrected"] += _int(row.get("corrected"))
        learning_by_focus.setdefault(focus, {"focus_group": focus, "total": 0, "corrected": 0})
        learning_by_focus[focus]["total"] += _int(row.get("total"))
        learning_by_focus[focus]["corrected"] += _int(row.get("corrected"))

    title_names_evolution = _all(
        con,
        """
        WITH scores AS (
          SELECT model_run_id, MAX(id) AS score_run_id
          FROM ml_score_runs
          WHERE finished_at IS NOT NULL
          GROUP BY model_run_id
        )
        SELECT
          m.id AS model_run_id,
          m.model_version,
          m.dataset_run_id,
          m.safe_threshold,
          m.macro_f1,
          m.safe_precision,
          m.safe_recall,
          m.false_safe_count,
          s.score_run_id,
          sr.scored_count,
          sr.final_auto_safe_count,
          ROUND(100.0 * sr.final_auto_safe_count / NULLIF(sr.scored_count, 0), 2) AS auto_safe_pct,
          m.finished_at
        FROM ml_model_runs m
        LEFT JOIN scores s ON s.model_run_id = m.id
        LEFT JOIN ml_score_runs sr ON sr.id = s.score_run_id
        WHERE m.model_kind = 'specialist_title_names'
          AND m.finished_at IS NOT NULL
        ORDER BY m.id ASC
        """,
    )

    selected_overview = next((row for row in overview if row.get("model_kind") == selected_kind), overview[0] if overview else {})
    selected_auditor = next((row for row in auditor_by_specialist if row.get("model_kind") == selected_kind), {})
    reviewed_total = sum(_int(row.get("total")) for row in learning_rows)
    correct_total = sum(_int(row.get("total")) for row in learning_rows if row.get("human_label") == "correct")
    minor_fix_total = sum(_int(row.get("total")) for row in learning_rows if row.get("human_label") == "minor_fix")
    semantic_error_total = sum(_int(row.get("total")) for row in learning_rows if row.get("human_label") == "semantic_error")
    corrected_total = sum(_int(row.get("corrected")) for row in learning_rows)

    return {
        "summary": {
            "specialists_total": len(overview),
            "specialists_with_score": sum(1 for row in overview if row.get("score_run_id") is not None),
            "specialists_active": 0,
            "specialist_false_safe": sum(_int(row.get("false_safe_count")) for row in overview),
            "selected_model_kind": selected_kind,
            "selected_auto_safe_count": _int(selected_overview.get("final_auto_safe_count")),
            "selected_auto_safe_pct": _num(selected_overview.get("auto_safe_pct")),
            "selected_score_run_id": selected_overview.get("score_run_id"),
            "auditor_review_required": _int(selected_auditor.get("specialist_new_safe_review")) + _int(selected_auditor.get("specialist_demoted_review")),
            "auditor_review_required_pct": _num(selected_auditor.get("review_required_pct")),
            "human_reviewed_total": reviewed_total,
        },
        "overview": overview,
        "coverageBySpecialist": overview,
        "auditorSummary": {
            "general_score_run_id": selected_auditor.get("general_score_run_id"),
            "specialist_score_run_id": selected_auditor.get("specialist_score_run_id"),
            "compared": _int(selected_auditor.get("compared")),
            "auto_safe_agree": _int(selected_auditor.get("auto_safe_agree")),
            "needs_human_agree": _int(selected_auditor.get("needs_human_agree")),
            "specialist_new_safe_review": _int(selected_auditor.get("specialist_new_safe_review")),
            "specialist_demoted_review": _int(selected_auditor.get("specialist_demoted_review")),
            "review_required_pct": _num(selected_auditor.get("review_required_pct")),
        },
        "auditorBySpecialist": auditor_by_specialist,
        "auditorQueue": auditor_queue,
        "learningSummary": {
            "reviewed_total": reviewed_total,
            "correct": correct_total,
            "minor_fix": minor_fix_total,
            "semantic_error": semantic_error_total,
            "acceptance_rate": round(_pct(correct_total + minor_fix_total, reviewed_total), 2),
            "corrected_text_total": corrected_total,
        },
        "learningByLabel": list(learning_by_label.values()),
        "learningByFocus": list(learning_by_focus.values()),
        "titleNamesEvolution": title_names_evolution,
        "specialists": overview,
        "groupComparison": auditor_by_specialist,
        "divergenceMatrix": [],
        "auditQueue": auditor_queue,
        "evolution": title_names_evolution,
    }


def _agents_payload(con: sqlite3.Connection) -> dict[str, Any]:
    required = [
        "ml_agent_registry",
        "ml_agent_routing_runs",
        "ml_agent_recommendations",
    ]
    if not all(_table_exists(con, table) for table in required):
        return {
            "available": False,
            "summary": {
                "agents_total": 0,
                "agents_operational": 0,
                "experimental_subagents": 0,
                "planned_subagents": 0,
                "latest_false_safe": 0,
                "ensemble_net_gain": 0,
                "recommendation_evidence": 0,
            },
            "topologyNodes": [],
            "topologyEdges": [],
            "registry": [],
            "health": [],
            "recommendations": [],
            "routingRuns": [],
            "routedItemsByAgent": [],
            "routingSamples": [],
            "ensembleImpact": [],
            "promotionReadiness": {},
            "experimentalContribution": [],
            "learningByAgent": [],
            "agentTimeline": [],
        }

    has_snapshots = _table_exists(con, "ml_specialist_policy_snapshots")
    summary = _one(
        con,
        """
        WITH latest_policy AS (
          SELECT *
          FROM ml_policy_runs
          WHERE finished_at IS NOT NULL
          ORDER BY id DESC
          LIMIT 1
        ),
        latest_reco_run AS (
          SELECT MAX(run_id) AS run_id
          FROM ml_agent_recommendations
        ),
        latest_agent_model AS (
          SELECT
            a.agent_key,
            a.status,
            a.operational_state,
            a.agent_type,
            COALESCE(m.false_safe_count, 0) AS false_safe_count
          FROM ml_agent_registry a
          LEFT JOIN ml_model_runs m
            ON m.model_kind = a.model_kind
           AND m.finished_at IS NOT NULL
           AND m.id = (
             SELECT MAX(m2.id)
             FROM ml_model_runs m2
             WHERE m2.model_kind = a.model_kind
               AND m2.finished_at IS NOT NULL
           )
        )
        SELECT
          (SELECT COUNT(*) FROM ml_agent_registry) AS agents_total,
          (
            SELECT COUNT(*)
            FROM ml_agent_registry
            WHERE status = 'active'
              AND operational_state IN ('authoritative', 'operational', 'dry_run')
          ) AS agents_operational,
          (
            SELECT COUNT(*)
            FROM ml_agent_registry
            WHERE status = 'active'
              AND operational_state = 'experimental'
              AND agent_type = 'subspecialist'
          ) AS experimental_subagents,
          (
            SELECT COUNT(*)
            FROM ml_agent_registry
            WHERE status = 'planned'
          ) AS planned_subagents,
          COALESCE((SELECT SUM(false_safe_count) FROM latest_agent_model), 0) AS latest_false_safe,
          COALESCE((
            SELECT SUM(false_safe_count)
            FROM latest_agent_model
            WHERE status = 'active'
              AND operational_state IN ('authoritative', 'operational', 'dry_run')
          ), 0) AS operational_false_safe,
          COALESCE((
            SELECT SUM(false_safe_count)
            FROM latest_agent_model
            WHERE status = 'active'
              AND operational_state = 'experimental'
          ), 0) AS experimental_false_safe,
          COALESCE((
            SELECT SUM(false_safe_count)
            FROM latest_agent_model
            WHERE status = 'planned'
          ), 0) AS planned_false_safe,
          COALESCE((SELECT policy_auto_safe_count - active_auto_safe_count FROM latest_policy), 0) AS ensemble_net_gain,
          COALESCE((
            SELECT SUM(evidence_count)
            FROM ml_agent_recommendations
            WHERE run_id = (SELECT run_id FROM latest_reco_run)
          ), 0) AS recommendation_evidence
        """,
    )

    active_gate = {}
    if _table_exists(con, "ml_composite_gate_registry"):
        active_gate = _one(
            con,
            """
            SELECT
              g.gate_key,
              g.coordinator_key,
              g.active_checkpoint_id,
              g.active_guarded_checkpoint_id,
              g.active_overlay_run_id,
              g.active_policy_run_id,
              g.operational_state,
              g.active_promotion_kind,
              g.auto_apply_allowed,
              g.promoted_at,
              g.promoted_by,
              c.promotion_status AS guarded_checkpoint_status,
              c.recommended_action AS guarded_recommended_action,
              c.ready_rule_count AS guarded_ready_rule_count,
              c.guarded_release_count,
              c.invalid_release_count,
              c.apply_allowed_count AS guarded_apply_allowed_count,
              c.blockers_json AS guarded_blockers_json,
              c.warnings_json AS guarded_warnings_json
            FROM ml_composite_gate_registry g
            LEFT JOIN ml_composite_guarded_overlay_checkpoints c
              ON c.id = g.active_guarded_checkpoint_id
            WHERE g.gate_key = 'segment_token_composite_review_gate'
            LIMIT 1
            """,
        )

    snapshot_cte = (
        """
        latest_snapshot AS (
          SELECT
            s.*,
            ROW_NUMBER() OVER (PARTITION BY s.specialist_key ORDER BY s.id DESC) AS rn
          FROM ml_specialist_policy_snapshots s
        ),
        """
        if has_snapshots
        else """
        latest_snapshot AS (
          SELECT
            NULL AS specialist_key,
            NULL AS status,
            NULL AS pending_real_count,
            NULL AS specialist_new_safe_count,
            NULL AS specialist_demoted_count,
            NULL AS rn
          WHERE 0
        ),
        """
    )
    registry = _all(
        con,
        f"""
        WITH latest_model AS (
          SELECT
            m.*,
            ROW_NUMBER() OVER (PARTITION BY m.model_kind ORDER BY m.id DESC) AS rn
          FROM ml_model_runs m
          WHERE m.finished_at IS NOT NULL
        ),
        latest_score AS (
          SELECT
            r.*,
            ROW_NUMBER() OVER (PARTITION BY r.model_run_id ORDER BY r.id DESC) AS rn
          FROM ml_score_runs r
          WHERE r.finished_at IS NOT NULL
        ),
        {snapshot_cte}
        base AS (
          SELECT
            a.agent_key,
            a.agent_type,
            a.parent_agent_key,
            a.status,
            a.operational_state,
            a.decision_role,
            a.model_kind,
            a.scope_group,
            a.scope_description,
            a.default_threshold,
            a.priority,
            a.dashboard_group,
            lm.id AS model_run_id,
            lm.dataset_run_id,
            lm.model_version,
            lm.safe_threshold,
            lm.macro_f1,
            lm.safe_precision,
            lm.safe_recall,
            lm.false_safe_count,
            ls.id AS score_run_id,
            ls.scored_count,
            ls.final_auto_safe_count,
            ROUND(100.0 * ls.final_auto_safe_count / NULLIF(ls.scored_count, 0), 2) AS auto_safe_pct,
            snap.status AS policy_status,
            snap.pending_real_count,
            snap.specialist_new_safe_count,
            snap.specialist_demoted_count
          FROM ml_agent_registry a
          LEFT JOIN latest_model lm ON lm.model_kind = a.model_kind AND lm.rn = 1
          LEFT JOIN latest_score ls ON ls.model_run_id = lm.id AND ls.rn = 1
          LEFT JOIN latest_snapshot snap ON snap.specialist_key = a.agent_key AND snap.rn = 1
        )
        SELECT *
        FROM base
        ORDER BY priority, dashboard_group, agent_key
        """,
    )

    for row in registry:
        state = row.get("operational_state")
        false_safe = _int(row.get("false_safe_count"))
        if state in {"authoritative", "operational", "dry_run"}:
            row["decision_authority"] = "decision_authorized"
        elif state == "experimental":
            row["decision_authority"] = "evidence_only"
        elif row.get("status") == "planned":
            row["decision_authority"] = "planned_only"
        else:
            row["decision_authority"] = "unknown"

        if row.get("model_kind") and not row.get("model_run_id"):
            row["health_status"] = "no_model_yet"
        elif false_safe > 0 and state == "experimental":
            row["health_status"] = "experimental_false_safe_watch"
        elif false_safe > 0:
            row["health_status"] = "blocked_false_safe"
        elif state == "experimental":
            row["health_status"] = "experimental_watch"
        else:
            row["health_status"] = "healthy"

    topology_nodes = [
        {
            "id": row.get("agent_key"),
            "parent": row.get("parent_agent_key"),
            "agent_type": row.get("agent_type"),
            "status": row.get("status"),
            "operational_state": row.get("operational_state"),
            "decision_role": row.get("decision_role"),
            "dashboard_group": row.get("dashboard_group"),
            "scope_group": row.get("scope_group"),
            "model_kind": row.get("model_kind"),
            "model_run_id": row.get("model_run_id"),
            "score_run_id": row.get("score_run_id"),
            "false_safe_count": _int(row.get("false_safe_count")),
            "auto_safe_pct": _num(row.get("auto_safe_pct")),
            "decision_authority": row.get("decision_authority"),
            "health_status": row.get("health_status"),
        }
        for row in registry
    ]
    topology_edges = [
        {"source": row.get("parent_agent_key"), "target": row.get("agent_key")}
        for row in registry
        if row.get("parent_agent_key")
    ]

    latest_reco_run = _one(con, "SELECT MAX(run_id) AS run_id FROM ml_agent_recommendations")
    recommendations = _all(
        con,
        """
        WITH latest_run AS (
          SELECT MAX(run_id) AS run_id
          FROM ml_agent_recommendations
        )
        SELECT
          proposed_agent_key,
          parent_agent_key,
          recommendation_type,
          status,
          reason,
          evidence_count,
          positive_count,
          negative_count,
          corrected_count,
          sample_segments_json,
          created_at
        FROM ml_agent_recommendations
        WHERE run_id = (SELECT run_id FROM latest_run)
        ORDER BY
          CASE WHEN negative_count > 0 THEN 0 ELSE 1 END,
          corrected_count DESC,
          evidence_count DESC,
          proposed_agent_key
        """,
    )
    for row in recommendations:
        try:
            samples = json.loads(row.get("sample_segments_json") or "[]")
        except json.JSONDecodeError:
            samples = []
        row["sample_count"] = len(samples) if isinstance(samples, list) else 0
        row["sample_preview"] = samples[:3] if isinstance(samples, list) else []

    ensemble_impact = []
    if _table_exists(con, "ml_policy_runs") and _table_exists(con, "ml_policy_items"):
        ensemble_impact = _all(
            con,
            """
            WITH latest_policy AS (
              SELECT id
              FROM ml_policy_runs
              WHERE finished_at IS NOT NULL
              ORDER BY id DESC
              LIMIT 1
            )
            SELECT
              policy_group,
              COUNT(*) AS rows_count,
              SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
              SUM(new_safe) AS new_safe,
              SUM(demoted_safe) AS demoted_safe,
              SUM(CASE WHEN learned_positive = 1 THEN 1 ELSE 0 END) AS learned_positive,
              SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS learned_negative
            FROM ml_policy_items
            WHERE run_id = (SELECT id FROM latest_policy)
            GROUP BY policy_group
            ORDER BY new_safe DESC, rows_count DESC
            LIMIT 20
            """,
        )

    routing_runs = _all(
        con,
        """
        SELECT
          id,
          rule_version,
          general_score_run_id,
          policy_run_id,
          coordinator_key,
          agents_considered_count,
          segments_scanned_count,
          routed_count,
          active_agent_covered_count,
          planned_agent_covered_count,
          recommendation_count,
          started_at,
          finished_at
        FROM ml_agent_routing_runs
        ORDER BY id ASC
        """,
    )

    promotion_readiness = _one(
        con,
        """
        WITH active_model AS (
          SELECT *
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
        ),
        active_score AS (
          SELECT sr.*
          FROM ml_score_runs sr
          JOIN active_model am ON am.active_model_run_id = sr.model_run_id
          WHERE sr.finished_at IS NOT NULL
          ORDER BY sr.id DESC
          LIMIT 1
        ),
        candidate_macro AS (
          SELECT m.*
          FROM ml_model_runs m
          WHERE m.model_kind = 'risk_action_classifier'
            AND m.finished_at IS NOT NULL
          ORDER BY m.id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT sr.*
          FROM ml_score_runs sr
          JOIN candidate_macro cm ON cm.id = sr.model_run_id
          WHERE sr.finished_at IS NOT NULL
          ORDER BY sr.id DESC
          LIMIT 1
        ),
        latest_ensemble AS (
          SELECT *
          FROM ml_policy_runs
          WHERE rule_version = 'ml_specialist_ensemble_policy_v1'
            AND finished_at IS NOT NULL
          ORDER BY id DESC
          LIMIT 1
        ),
        agent_summary AS (
          SELECT
            SUM(CASE WHEN status = 'active' AND operational_state = 'experimental' THEN 1 ELSE 0 END) AS experimental_agents,
            SUM(CASE WHEN status = 'active' AND operational_state IN ('operational', 'authoritative', 'dry_run') THEN 1 ELSE 0 END) AS operational_agents
          FROM ml_agent_registry
        )
        SELECT
          am.active_model_run_id,
          am.active_model_version,
          active_score.id AS active_score_run_id,
          active_score.scored_count AS active_scored_count,
          active_score.final_auto_safe_count AS active_auto_safe_count,
          ROUND(100.0 * active_score.final_auto_safe_count / NULLIF(active_score.scored_count, 0), 2) AS active_auto_safe_pct,
          cm.id AS candidate_model_run_id,
          cm.model_version AS candidate_model_version,
          cm.macro_f1 AS candidate_macro_f1,
          cm.false_safe_count AS candidate_false_safe_count,
          cm.safe_precision AS candidate_safe_precision,
          cm.safe_recall AS candidate_safe_recall,
          candidate_score.id AS candidate_score_run_id,
          candidate_score.final_auto_safe_count AS candidate_auto_safe_count,
          latest_ensemble.id AS ensemble_policy_run_id,
          latest_ensemble.policy_auto_safe_count AS ensemble_auto_safe_count,
          latest_ensemble.new_safe_count AS ensemble_new_safe_count,
          latest_ensemble.demoted_safe_count AS ensemble_demoted_safe_count,
          latest_ensemble.policy_auto_safe_count - latest_ensemble.active_auto_safe_count AS ensemble_gain_vs_candidate_macro,
          latest_ensemble.policy_auto_safe_count - active_score.final_auto_safe_count AS ensemble_gap_vs_active,
          ROUND(
            100.0 * (latest_ensemble.policy_auto_safe_count - active_score.final_auto_safe_count)
            / NULLIF(active_score.scored_count, 0),
            2
          ) AS ensemble_gap_vs_active_pct_points,
          agent_summary.operational_agents,
          agent_summary.experimental_agents,
          CASE
            WHEN COALESCE(cm.false_safe_count, 0) > 0 THEN 'not_ready_false_safe'
            WHEN latest_ensemble.policy_auto_safe_count < active_score.final_auto_safe_count THEN 'not_ready_coverage_gap'
            WHEN agent_summary.experimental_agents > 0 THEN 'watch_experimental_agents'
            ELSE 'ready_for_review'
          END AS promotion_readiness
        FROM active_model am
        LEFT JOIN active_score ON 1 = 1
        LEFT JOIN candidate_macro cm ON 1 = 1
        LEFT JOIN candidate_score ON 1 = 1
        LEFT JOIN latest_ensemble ON 1 = 1
        LEFT JOIN agent_summary ON 1 = 1
        """,
    )

    routed_items_by_agent = []
    routing_samples = []
    experimental_contribution = []
    if _table_exists(con, "ml_agent_routing_items"):
        routed_items_by_agent = _all(
            con,
            """
            WITH latest_run AS (
              SELECT MAX(id) AS run_id
              FROM ml_agent_routing_runs
            )
            SELECT
              route_agent_key,
              route_agent_type,
              route_status,
              COUNT(*) AS rows_count,
              SUM(CASE WHEN general_action = 'auto_safe' THEN 1 ELSE 0 END) AS general_auto_safe,
              SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
              SUM(CASE WHEN specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS specialist_auto_safe,
              SUM(CASE WHEN recommendation_key IS NOT NULL THEN 1 ELSE 0 END) AS recommendation_rows
            FROM ml_agent_routing_items
            WHERE run_id = (SELECT run_id FROM latest_run)
            GROUP BY route_agent_key, route_agent_type, route_status
            ORDER BY route_status, route_agent_key
            """,
        )
        experimental_contribution = _all(
            con,
            """
            WITH latest_run AS (
              SELECT MAX(id) AS run_id
              FROM ml_agent_routing_runs
            )
            SELECT
              route_agent_key AS agent_key,
              route_status,
              COUNT(*) AS sampled_rows,
              SUM(CASE WHEN specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS specialist_auto_safe,
              SUM(CASE WHEN general_action = 'auto_safe' THEN 1 ELSE 0 END) AS general_auto_safe,
              SUM(CASE WHEN specialist_action = 'auto_safe' AND COALESCE(general_action, '') <> 'auto_safe' THEN 1 ELSE 0 END) AS potential_new_safe,
              SUM(CASE WHEN COALESCE(specialist_action, '') <> 'auto_safe' AND general_action = 'auto_safe' THEN 1 ELSE 0 END) AS potential_demotions,
              SUM(CASE WHEN recommendation_key IS NOT NULL THEN 1 ELSE 0 END) AS recommendation_rows
            FROM ml_agent_routing_items
            WHERE run_id = (SELECT run_id FROM latest_run)
              AND route_status = 'experimental'
            GROUP BY route_agent_key, route_status
            ORDER BY potential_new_safe DESC, sampled_rows DESC
            """,
        )
        routing_samples = _all(
            con,
            """
            WITH latest_run AS (
              SELECT MAX(id) AS run_id
              FROM ml_agent_routing_runs
            )
            SELECT
              segment_id,
              relative_path,
              source_key,
              route_agent_key,
              route_status,
              ROUND(route_confidence, 4) AS route_confidence,
              general_action,
              policy_action,
              specialist_action,
              recommendation_key
            FROM ml_agent_routing_items
            WHERE run_id = (SELECT run_id FROM latest_run)
            ORDER BY route_status, route_agent_key, relative_path, source_key
            LIMIT 200
            """,
        )

    learning_by_agent = _all(
        con,
        """
        SELECT
          focus_group AS agent_key,
          queue_source,
          COUNT(*) AS reviewed_count,
          SUM(CASE WHEN human_label IN ('correct', 'contextual_exception') THEN 1 ELSE 0 END) AS positive_count,
          SUM(CASE WHEN human_label IN ('minor_fix', 'major_fix', 'semantic_error', 'residual_spanish', 'structure_error', 'token_mismatch', 'rejected', 'rejected_suggestion') THEN 1 ELSE 0 END) AS negative_or_fix_count,
          SUM(CASE WHEN corrected_text IS NOT NULL AND TRIM(corrected_text) <> '' THEN 1 ELSE 0 END) AS corrected_count,
          MAX(reviewed_at) AS latest_reviewed_at
        FROM local_learning_candidates
        WHERE local_status = 'reviewed_human'
          AND queue_source IN ('ml_specialist_auditor', 'ml_specialist_scope_review')
          AND focus_group IS NOT NULL
        GROUP BY focus_group, queue_source
        ORDER BY reviewed_count DESC, agent_key
        LIMIT 20
        """,
    )

    agent_timeline = [
        {
            "id": row.get("id"),
            "rule_version": row.get("rule_version"),
            "routed_count": _int(row.get("routed_count")),
            "active_agent_covered_count": _int(row.get("active_agent_covered_count")),
            "planned_agent_covered_count": _int(row.get("planned_agent_covered_count")),
            "recommendation_count": _int(row.get("recommendation_count")),
            "finished_at": row.get("finished_at"),
        }
        for row in routing_runs
    ]

    return {
        "available": True,
        "summary": {
            "agents_total": _int(summary.get("agents_total")),
            "agents_operational": _int(summary.get("agents_operational")),
            "experimental_subagents": _int(summary.get("experimental_subagents")),
            "planned_subagents": _int(summary.get("planned_subagents")),
            "latest_false_safe": _int(summary.get("latest_false_safe")),
            "latest_false_safe_total": _int(summary.get("latest_false_safe")),
            "operational_false_safe": _int(summary.get("operational_false_safe")),
            "experimental_false_safe": _int(summary.get("experimental_false_safe")),
            "planned_false_safe": _int(summary.get("planned_false_safe")),
            "ensemble_net_gain": _int(summary.get("ensemble_net_gain")),
            "recommendation_evidence": _int(summary.get("recommendation_evidence")),
            "latest_recommendation_run_id": latest_reco_run.get("run_id"),
            "active_gate_overlay_run_id": active_gate.get("active_overlay_run_id"),
            "active_guarded_checkpoint_id": active_gate.get("active_guarded_checkpoint_id"),
            "active_gate_auto_apply_allowed": _int(active_gate.get("auto_apply_allowed")),
            "active_gate_guarded_releases": _int(active_gate.get("guarded_release_count")),
            "active_gate_invalid_releases": _int(active_gate.get("invalid_release_count")),
            "active_gate_apply_allowed_count": _int(active_gate.get("guarded_apply_allowed_count")),
        },
        "activeGate": active_gate,
        "topologyNodes": topology_nodes,
        "topologyEdges": topology_edges,
        "registry": registry,
        "health": registry,
        "recommendations": recommendations,
        "routingRuns": routing_runs,
        "routedItemsByAgent": routed_items_by_agent,
        "routingSamples": routing_samples,
        "ensembleImpact": ensemble_impact,
        "promotionReadiness": promotion_readiness,
        "experimentalContribution": experimental_contribution,
        "learningByAgent": learning_by_agent,
        "agentTimeline": agent_timeline,
    }


def _lifecycle_payload(con: sqlite3.Connection) -> dict[str, Any]:
    def output_apply_payload() -> dict[str, Any]:
        if not (_table_exists(con, "segment_output_apply_runs") and _table_exists(con, "segment_output_apply_items")):
            return {
                "available": False,
                "summary": {},
                "runs": [],
                "evolution": [],
                "packageItems": [],
                "tokenBlocks": [],
            }

        summary = _one(
            con,
            """
            WITH latest_applied AS (
              SELECT id, applied_count, files_touched_count, backup_root, report_path, started_at
              FROM segment_output_apply_runs
              WHERE apply = 1
              ORDER BY started_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              COALESCE(SUM(CASE WHEN r.apply = 1 THEN r.applied_count ELSE 0 END), 0) AS total_applied,
              COALESCE((SELECT applied_count FROM latest_applied), 0) AS latest_applied_count,
              COALESCE(SUM(r.token_mismatch_count), 0) AS token_mismatch_count,
              COALESCE(SUM(CASE WHEN r.apply = 1 THEN r.files_touched_count ELSE 0 END), 0) AS files_touched_count,
              COALESCE(SUM(CASE WHEN r.apply = 0 THEN 1 ELSE 0 END), 0) AS dry_run_count,
              (SELECT id FROM latest_applied) AS latest_apply_run_id,
              (SELECT backup_root FROM latest_applied) AS latest_backup_root,
              (SELECT report_path FROM latest_applied) AS latest_report_path,
              (SELECT started_at FROM latest_applied) AS latest_started_at
            FROM segment_output_apply_runs r
            """,
        )
        runs = _all(
            con,
            """
            SELECT
              id AS apply_run_id,
              state_run_id,
              apply,
              rule_version,
              limit_count,
              path_filter,
              review_states,
              candidates_inspected,
              ready_count,
              applied_count,
              skipped_count,
              token_mismatch_count,
              files_touched_count,
              backup_root,
              report_path,
              started_at,
              finished_at
            FROM segment_output_apply_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 20
            """,
        )
        evolution = _all(
            con,
            """
            WITH state_runs AS (
              SELECT
                id,
                finished_at,
                output_apply_pending_count,
                pending_count,
                closed_count,
                total_segments
              FROM segment_state_runs
              WHERE total_segments > 1000
            ),
            apply_by_state AS (
              SELECT
                state_run_id,
                SUM(CASE WHEN apply = 1 THEN applied_count ELSE 0 END) AS applied_count,
                SUM(token_mismatch_count) AS token_mismatch_count
              FROM segment_output_apply_runs
              GROUP BY state_run_id
            )
            SELECT
              s.id AS state_run_id,
              s.finished_at,
              s.output_apply_pending_count,
              s.pending_count,
              s.closed_count,
              ROUND(100.0 * s.closed_count / NULLIF(s.total_segments, 0), 2) AS closed_pct,
              COALESCE(a.applied_count, 0) AS applied_from_this_state,
              COALESCE(a.token_mismatch_count, 0) AS token_mismatch_from_this_state
            FROM state_runs s
            LEFT JOIN apply_by_state a ON a.state_run_id = s.id
            ORDER BY s.finished_at ASC, s.id ASC
            """,
        )
        package_items = _all(
            con,
            """
            SELECT
              r.id AS apply_run_id,
              substr(i.relative_path, 1, instr(i.relative_path || '/', '/') - 1) AS package_name,
              i.review_state,
              COUNT(*) AS inspected_count,
              SUM(CASE WHEN i.applied = 1 THEN 1 ELSE 0 END) AS applied_count,
              SUM(CASE WHEN i.token_mismatch = 1 THEN 1 ELSE 0 END) AS token_mismatch_count
            FROM segment_output_apply_items i
            JOIN segment_output_apply_runs r ON r.id = i.run_id
            GROUP BY r.id, package_name, i.review_state
            ORDER BY r.id DESC, applied_count DESC, token_mismatch_count DESC
            LIMIT 200
            """,
        )
        token_blocks = _all(
            con,
            """
            SELECT
              i.run_id AS apply_run_id,
              i.segment_id,
              i.relative_path,
              i.source_line_number,
              i.source_key,
              i.final_state,
              i.review_state,
              i.result_status,
              i.reasons_json,
              r.report_path,
              i.created_at
            FROM segment_output_apply_items i
            JOIN segment_output_apply_runs r ON r.id = i.run_id
            WHERE i.token_mismatch = 1
            ORDER BY i.created_at DESC, i.relative_path, i.source_line_number
            LIMIT 300
            """,
        )
        return {
            "available": True,
            "summary": summary,
            "runs": runs,
            "evolution": evolution,
            "packageItems": package_items,
            "tokenBlocks": token_blocks,
        }

    def token_policy_payload() -> dict[str, Any]:
        def checkpoint_payload() -> dict[str, Any]:
            if not _table_exists(con, "ml_composite_checkpoints"):
                return {
                    "available": False,
                    "summary": {},
                    "runs": [],
                    "trend": [],
                    "statusDistribution": [],
                    "registry": {},
                    "promotions": [],
                    "activeQueue": {"summary": {}, "runs": [], "routes": [], "fullSummary": {}, "fullRoutes": []},
                    "reviewProgress": {"summary": {}, "routes": [], "trend": []},
                    "subpolicyDiagnostic": {"summary": {}, "items": [], "statusDistribution": [], "routeMatrix": [], "trend": []},
                }
            registry = _one(
                con,
                """
                SELECT
                  gate_key,
                  coordinator_key,
                  active_checkpoint_id,
                  active_overlay_run_id,
                  active_policy_run_id,
                  operational_state,
                  auto_apply_allowed,
                  promoted_at,
                  promoted_by,
                  reason,
                  metrics_json,
                  updated_at
                FROM ml_composite_gate_registry
                WHERE gate_key = 'segment_token_composite_review_gate'
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_gate_registry") else {}
            promotions = _all(
                con,
                """
                SELECT
                  id AS promotion_id,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  previous_checkpoint_id,
                  previous_overlay_run_id,
                  decision,
                  policy_version,
                  auto_apply_allowed,
                  reason,
                  blockers_json,
                  warnings_json,
                  metrics_json,
                  promoted_by,
                  created_at
                FROM ml_composite_gate_promotions
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
            ) if _table_exists(con, "ml_composite_gate_promotions") else []
            active_queue_runs = _all(
                con,
                """
                SELECT
                  id AS queue_run_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  source_mode,
                  route_filter_csv,
                  risk_filter_csv,
                  critical_only,
                  limit_count,
                  total_rows,
                  critical_rows,
                  high_rows,
                  medium_rows,
                  low_rows,
                  route_counts_json,
                  bucket_counts_json,
                  report_path,
                  csv_path,
                  jsonl_path,
                  decisions_template_path,
                  started_at,
                  finished_at
                FROM ml_composite_gate_queue_runs
                ORDER BY started_at DESC, id DESC
                LIMIT 20
                """,
            ) if _table_exists(con, "ml_composite_gate_queue_runs") else []
            active_queue_summary = active_queue_runs[0] if active_queue_runs else {}
            active_queue_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total
                FROM ml_composite_gate_queue_routes
                WHERE queue_run_id = ?
                ORDER BY total DESC, suggested_route
                """,
                (active_queue_summary["queue_run_id"],),
            ) if active_queue_summary and _table_exists(con, "ml_composite_gate_queue_routes") else []
            active_queue_full_summary = _one(
                con,
                """
                SELECT
                  id AS queue_run_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  source_mode,
                  route_filter_csv,
                  risk_filter_csv,
                  critical_only,
                  limit_count,
                  total_rows,
                  critical_rows,
                  high_rows,
                  medium_rows,
                  low_rows,
                  route_counts_json,
                  bucket_counts_json,
                  report_path,
                  csv_path,
                  jsonl_path,
                  decisions_template_path,
                  started_at,
                  finished_at
                FROM ml_composite_gate_queue_runs
                WHERE source_mode = 'active_composite_gate'
                  AND critical_only = 0
                  AND route_filter_csv IS NULL
                  AND risk_filter_csv IS NULL
                  AND limit_count IS NULL
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_gate_queue_runs") else {}
            active_queue_full_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total
                FROM ml_composite_gate_queue_routes
                WHERE queue_run_id = ?
                ORDER BY total DESC, suggested_route
                """,
                (active_queue_full_summary["queue_run_id"],),
            ) if active_queue_full_summary and _table_exists(con, "ml_composite_gate_queue_routes") else []
            review_progress_summary = _one(
                con,
                """
                SELECT
                  id AS snapshot_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  rejected_count,
                  fix_count,
                  needs_subpolicy_count,
                  manual_exception_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  report_path,
                  started_at,
                  finished_at
                FROM ml_composite_gate_review_snapshots
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_gate_review_snapshots") else {}
            review_progress_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  rejected_count,
                  fix_count,
                  needs_subpolicy_count,
                  manual_exception_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  latest_queue_run_id,
                  latest_queue_total_rows
                FROM ml_composite_gate_review_route_status
                WHERE snapshot_id = ?
                ORDER BY review_coverage_pct ASC, total_items DESC, suggested_route
                """,
                (review_progress_summary["snapshot_id"],),
            ) if review_progress_summary and _table_exists(con, "ml_composite_gate_review_route_status") else []
            review_progress_trend = _all(
                con,
                """
                SELECT
                  id AS snapshot_id,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  rejected_count,
                  fix_count,
                  needs_subpolicy_count,
                  manual_exception_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  started_at
                FROM ml_composite_gate_review_snapshots
                ORDER BY started_at ASC, id ASC
                LIMIT 80
                """,
            ) if _table_exists(con, "ml_composite_gate_review_snapshots") else []
            subpolicy_diagnostic_summary = _one(
                con,
                """
                SELECT
                  id AS diagnostic_run_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  total_items,
                  reviewed_items,
                  pending_items,
                  grouped_subpolicies,
                  design_candidate_count,
                  policy_candidate_count,
                  needs_more_review_count,
                  queue_review_candidate_count,
                  report_path,
                  csv_path,
                  json_path,
                  started_at,
                  finished_at
                FROM ml_composite_subpolicy_diagnostic_runs
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_subpolicy_diagnostic_runs") else {}
            subpolicy_diagnostic_items = _all(
                con,
                """
                SELECT
                  suggested_route,
                  token_subtype,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  accept_count,
                  keep_manual_exception_count,
                  reject_count,
                  needs_subpolicy_count,
                  fix_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  maturity_status,
                  confidence_band,
                  recommended_action,
                  sample_policy_item_ids_json,
                  sample_paths_json,
                  token_families_json
                FROM ml_composite_subpolicy_diagnostic_items
                WHERE run_id = ?
                ORDER BY
                  CASE maturity_status
                    WHEN 'policy_candidate_review' THEN 0
                    WHEN 'ready_to_design_subpolicy' THEN 1
                    WHEN 'negative_boundary_learned' THEN 2
                    WHEN 'conflicting_evidence' THEN 3
                    WHEN 'queued_waiting_review' THEN 4
                    WHEN 'needs_more_review' THEN 5
                    WHEN 'needs_queue' THEN 6
                    ELSE 9
                  END,
                  reviewed_items DESC,
                  total_items DESC,
                  suggested_route,
                  token_subtype
                LIMIT 120
                """,
                (subpolicy_diagnostic_summary["diagnostic_run_id"],),
            ) if subpolicy_diagnostic_summary and _table_exists(con, "ml_composite_subpolicy_diagnostic_items") else []
            subpolicy_diagnostic_status = _all(
                con,
                """
                SELECT
                  maturity_status,
                  confidence_band,
                  COUNT(*) AS total_groups,
                  SUM(total_items) AS total_items,
                  SUM(reviewed_items) AS reviewed_items,
                  SUM(pending_items) AS pending_items
                FROM ml_composite_subpolicy_diagnostic_items
                WHERE run_id = ?
                GROUP BY maturity_status, confidence_band
                ORDER BY total_groups DESC, maturity_status
                """,
                (subpolicy_diagnostic_summary["diagnostic_run_id"],),
            ) if subpolicy_diagnostic_summary and _table_exists(con, "ml_composite_subpolicy_diagnostic_items") else []
            subpolicy_diagnostic_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  maturity_status,
                  COUNT(*) AS total_groups,
                  SUM(total_items) AS total_items,
                  SUM(reviewed_items) AS reviewed_items,
                  SUM(needs_subpolicy_count) AS needs_subpolicy_count
                FROM ml_composite_subpolicy_diagnostic_items
                WHERE run_id = ?
                GROUP BY suggested_route, maturity_status
                ORDER BY suggested_route, total_groups DESC
                """,
                (subpolicy_diagnostic_summary["diagnostic_run_id"],),
            ) if subpolicy_diagnostic_summary and _table_exists(con, "ml_composite_subpolicy_diagnostic_items") else []
            subpolicy_diagnostic_trend = _all(
                con,
                """
                SELECT
                  id AS diagnostic_run_id,
                  grouped_subpolicies,
                  design_candidate_count,
                  policy_candidate_count,
                  needs_more_review_count,
                  queue_review_candidate_count,
                  reviewed_items,
                  pending_items,
                  started_at
                FROM ml_composite_subpolicy_diagnostic_runs
                ORDER BY started_at ASC, id ASC
                LIMIT 80
                """,
            ) if _table_exists(con, "ml_composite_subpolicy_diagnostic_runs") else []
            subpolicy_diagnostic_payload = {
                "summary": subpolicy_diagnostic_summary,
                "items": subpolicy_diagnostic_items,
                "statusDistribution": subpolicy_diagnostic_status,
                "routeMatrix": subpolicy_diagnostic_routes,
                "trend": subpolicy_diagnostic_trend,
            }
            runs = _all(
                con,
                """
                SELECT
                  id AS checkpoint_id,
                  rule_version,
                  checkpoint_name,
                  checkpoint_scope,
                  coordinator_key,
                  source_policy_run_id,
                  overlay_run_id,
                  source_state_run_id,
                  total_candidates,
                  base_critical_count,
                  overlay_critical_count,
                  released_critical_count,
                  critical_queue_count,
                  high_count,
                  medium_count,
                  low_count,
                  enabled_rule_count,
                  apply_allowed_count,
                  active_agent_count,
                  operational_agent_count,
                  experimental_agent_count,
                  planned_agent_count,
                  promotion_status,
                  recommended_action,
                  blockers_json,
                  warnings_json,
                  metrics_json,
                  report_path,
                  started_at,
                  finished_at
                FROM ml_composite_checkpoints
                ORDER BY started_at DESC, id DESC
                LIMIT 20
                """,
            )
            if not runs:
                return {
                    "available": True,
                    "summary": {},
                    "runs": [],
                    "trend": [],
                    "statusDistribution": [],
                    "registry": registry,
                    "promotions": promotions,
                    "activeQueue": {
                        "summary": active_queue_summary,
                        "runs": active_queue_runs,
                        "routes": active_queue_routes,
                        "fullSummary": active_queue_full_summary,
                        "fullRoutes": active_queue_full_routes,
                    },
                    "reviewProgress": {
                        "summary": review_progress_summary,
                        "routes": review_progress_routes,
                        "trend": review_progress_trend,
                    },
                    "subpolicyDiagnostic": subpolicy_diagnostic_payload,
                }
            trend = _all(
                con,
                """
                SELECT
                  id AS checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  base_critical_count,
                  overlay_critical_count,
                  released_critical_count,
                  critical_queue_count,
                  enabled_rule_count,
                  promotion_status,
                  started_at
                FROM ml_composite_checkpoints
                ORDER BY started_at ASC, id ASC
                LIMIT 80
                """,
            )
            status_distribution = _all(
                con,
                """
                SELECT
                  promotion_status,
                  recommended_action,
                  COUNT(*) AS total
                FROM ml_composite_checkpoints
                GROUP BY promotion_status, recommended_action
                ORDER BY total DESC, promotion_status
                """,
            )
            return {
                "available": True,
                "summary": runs[0],
                "runs": runs,
                "trend": trend,
                "statusDistribution": status_distribution,
                "registry": registry,
                "promotions": promotions,
                "activeQueue": {
                    "summary": active_queue_summary,
                    "runs": active_queue_runs,
                    "routes": active_queue_routes,
                    "fullSummary": active_queue_full_summary,
                    "fullRoutes": active_queue_full_routes,
                },
                "reviewProgress": {
                    "summary": review_progress_summary,
                    "routes": review_progress_routes,
                    "trend": review_progress_trend,
                },
                "subpolicyDiagnostic": subpolicy_diagnostic_payload,
            }

        def overlay_payload() -> dict[str, Any]:
            if not (
                _table_exists(con, "segment_token_policy_overlay_runs")
                and _table_exists(con, "segment_token_policy_overlay_items")
            ):
                return {
                    "available": False,
                    "summary": {},
                    "runs": [],
                    "riskComparison": [],
                    "actionDistribution": [],
                    "bucketDistribution": [],
                    "releasedByRule": [],
                    "releasedItems": [],
                    "remainingCritical": [],
                }

            runs = _all(
                con,
                """
                SELECT
                  id AS overlay_run_id,
                  rule_version,
                  source_policy_run_id,
                  source_state_run_id,
                  source_rule_version,
                  overlay_name,
                  min_evidence,
                  total_candidates,
                  original_critical_count,
                  overlay_critical_count,
                  released_critical_count,
                  remaining_blocked_count,
                  enabled_rule_count,
                  apply_allowed_count,
                  ROUND(100.0 * released_critical_count / NULLIF(original_critical_count, 0), 2) AS critical_release_pct,
                  report_path,
                  csv_path,
                  jsonl_path,
                  notes_json,
                  started_at,
                  finished_at
                FROM segment_token_policy_overlay_runs
                WHERE finished_at IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT 20
                """,
            )
            if not runs:
                return {
                    "available": True,
                    "summary": {},
                    "runs": [],
                    "riskComparison": [],
                    "actionDistribution": [],
                    "bucketDistribution": [],
                    "releasedByRule": [],
                    "releasedItems": [],
                    "remainingCritical": [],
                }

            overlay_run_id = runs[0]["overlay_run_id"]
            risk_comparison = _all(
                con,
                """
                SELECT
                  original_risk_level,
                  overlay_risk_level,
                  COUNT(*) AS total
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                GROUP BY original_risk_level, overlay_risk_level
                ORDER BY
                  CASE original_risk_level
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 9
                  END,
                  CASE overlay_risk_level
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 9
                  END
                """,
                (overlay_run_id,),
            )
            action_distribution = _all(
                con,
                """
                SELECT
                  overlay_action,
                  overlay_risk_level,
                  overlay_agent_key,
                  COUNT(*) AS total
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                GROUP BY overlay_action, overlay_risk_level, overlay_agent_key
                ORDER BY total DESC, overlay_action
                """,
                (overlay_run_id,),
            )
            bucket_distribution = _all(
                con,
                """
                SELECT
                  overlay_policy_bucket,
                  overlay_risk_level,
                  COUNT(*) AS total,
                  SUM(CASE WHEN would_release_critical = 1 THEN 1 ELSE 0 END) AS released_critical_count,
                  SUM(CASE WHEN apply_allowed = 1 THEN 1 ELSE 0 END) AS apply_allowed_count
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                GROUP BY overlay_policy_bucket, overlay_risk_level
                ORDER BY
                  CASE overlay_risk_level
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 9
                  END,
                  total DESC,
                  overlay_policy_bucket
                """,
                (overlay_run_id,),
            )
            released_by_rule = _all(
                con,
                """
                SELECT
                  COALESCE(NULLIF(rule_key, ''), 'unchanged') AS rule_key,
                  overlay_agent_key,
                  COUNT(*) AS total,
                  SUM(CASE WHEN would_release_critical = 1 THEN 1 ELSE 0 END) AS released_critical_count
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                  AND would_release_critical = 1
                GROUP BY COALESCE(NULLIF(rule_key, ''), 'unchanged'), overlay_agent_key
                ORDER BY released_critical_count DESC, rule_key
                """,
                (overlay_run_id,),
            )
            released_items = _all(
                con,
                """
                SELECT
                  source_policy_item_id,
                  segment_id,
                  relative_path,
                  source_line_number,
                  source_key,
                  original_policy_bucket,
                  original_risk_level,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  overlay_action,
                  overlay_agent_key,
                  decision,
                  rule_key,
                  reasons_json
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                  AND would_release_critical = 1
                ORDER BY rule_key, relative_path, source_line_number
                LIMIT 200
                """,
                (overlay_run_id,),
            )
            remaining_critical = _all(
                con,
                """
                SELECT
                  source_policy_item_id,
                  segment_id,
                  relative_path,
                  source_line_number,
                  source_key,
                  original_policy_bucket,
                  original_risk_level,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  overlay_action,
                  overlay_agent_key,
                  decision,
                  rule_key,
                  reasons_json
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                  AND overlay_risk_level = 'critical'
                ORDER BY overlay_policy_bucket, relative_path, source_line_number
                LIMIT 200
                """,
                (overlay_run_id,),
            )

            return {
                "available": True,
                "summary": runs[0],
                "runs": runs,
                "riskComparison": risk_comparison,
                "actionDistribution": action_distribution,
                "bucketDistribution": bucket_distribution,
                "releasedByRule": released_by_rule,
                "releasedItems": released_items,
                "remainingCritical": remaining_critical,
            }

        def decision_payload() -> dict[str, Any]:
            if not (_table_exists(con, "segment_token_policy_decision_runs") and _table_exists(con, "segment_token_policy_decisions")):
                return {
                    "available": False,
                    "summary": {},
                    "runs": [],
                    "byBucket": [],
                    "coverage": [],
                }

            decision_runs = _all(
                con,
                """
                SELECT
                  id AS decision_run_id,
                  policy_run_id,
                  source_report,
                  decisions_path,
                  total_decisions,
                  approved_count,
                  rejected_count,
                  fix_count,
                  skipped_count,
                  report_path,
                  started_at,
                  finished_at
                FROM segment_token_policy_decision_runs
                WHERE finished_at IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT 20
                """,
            )
            latest_policy_id = decision_runs[0]["policy_run_id"] if decision_runs else None
            if latest_policy_id is None:
                return {
                    "available": True,
                    "summary": {},
                    "runs": decision_runs,
                    "byBucket": [],
                    "coverage": [],
                }

            summary = _one(
                con,
                """
                SELECT
                  ? AS policy_run_id,
                  COUNT(*) AS total_decisions,
                  SUM(CASE WHEN approved_for_apply = 1 THEN 1 ELSE 0 END) AS approved_for_apply,
                  SUM(CASE WHEN decision = 'accept_policy_candidate' THEN 1 ELSE 0 END) AS accepted_policy,
                  SUM(CASE WHEN decision = 'keep_manual_exception_only' THEN 1 ELSE 0 END) AS manual_exceptions,
                  SUM(CASE WHEN decision = 'reject_policy_candidate' THEN 1 ELSE 0 END) AS rejected_policy,
                  SUM(CASE WHEN decision IN ('fix_confirmed_text', 'encoding_cleanup_required', 'manual_token_rewrite_required') THEN 1 ELSE 0 END) AS needs_fix,
                  SUM(CASE WHEN decision = 'needs_subpolicy' THEN 1 ELSE 0 END) AS needs_subpolicy,
                  SUM(CASE WHEN approved_for_apply = 1 AND risk_level = 'critical' THEN 1 ELSE 0 END) AS critical_approved_for_apply
                FROM segment_token_policy_decisions
                WHERE policy_run_id = ?
                """,
                (latest_policy_id, latest_policy_id),
            )
            by_bucket = _all(
                con,
                """
                SELECT
                  policy_bucket,
                  risk_level,
                  decision,
                  approved_for_apply,
                  COUNT(*) AS total
                FROM segment_token_policy_decisions
                WHERE policy_run_id = ?
                GROUP BY policy_bucket, risk_level, decision, approved_for_apply
                ORDER BY total DESC, policy_bucket, decision
                """,
                (latest_policy_id,),
            )
            coverage = _all(
                con,
                """
                WITH policy_items AS (
                  SELECT id, policy_bucket, risk_level
                  FROM segment_token_policy_items
                  WHERE run_id = ?
                )
                SELECT
                  p.policy_bucket,
                  p.risk_level,
                  COUNT(*) AS policy_items,
                  COUNT(d.id) AS reviewed_items,
                  SUM(CASE WHEN d.approved_for_apply = 1 THEN 1 ELSE 0 END) AS approved_for_apply,
                  ROUND(100.0 * COUNT(d.id) / NULLIF(COUNT(*), 0), 2) AS review_coverage_pct
                FROM policy_items p
                LEFT JOIN segment_token_policy_decisions d
                  ON d.policy_run_id = ?
                 AND d.policy_item_id = p.id
                GROUP BY p.policy_bucket, p.risk_level
                ORDER BY review_coverage_pct DESC, policy_items DESC
                LIMIT 200
                """,
                (latest_policy_id, latest_policy_id),
            )
            return {
                "available": True,
                "summary": summary,
                "runs": decision_runs,
                "byBucket": by_bucket,
                "coverage": coverage,
            }

        if not (_table_exists(con, "segment_token_policy_runs") and _table_exists(con, "segment_token_policy_items")):
            return {
                "available": False,
                "summary": {},
                "runs": [],
                "bucketDistribution": [],
                "packageBuckets": [],
                "reviewQueue": [],
                "decisions": decision_payload(),
                "overlay": overlay_payload(),
                "checkpoints": checkpoint_payload(),
            }

        runs = _all(
            con,
            """
            SELECT
              id AS token_policy_run_id,
              rule_version,
              state_run_id,
              total_candidates,
              critical_count,
              high_count,
              medium_count,
              low_count,
              manual_review_count,
              policy_candidate_count,
              blocked_count,
              report_path,
              csv_path,
              jsonl_path,
              started_at,
              finished_at
            FROM segment_token_policy_runs
            WHERE finished_at IS NOT NULL
            ORDER BY finished_at DESC, id DESC
            LIMIT 20
            """,
        )
        summary = runs[0] if runs else {}
        bucket_distribution = _all(
            con,
            """
            WITH latest AS (
              SELECT id
              FROM segment_token_policy_runs
              WHERE finished_at IS NOT NULL
                AND total_candidates > 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              policy_bucket,
              risk_level,
              review_state,
              COUNT(*) AS total,
              SUM(CASE WHEN auto_apply_allowed = 1 THEN 1 ELSE 0 END) AS auto_apply_allowed_count,
              SUM(CASE WHEN needs_human_review = 1 THEN 1 ELSE 0 END) AS needs_human_review_count
            FROM segment_token_policy_items
            WHERE run_id = (SELECT id FROM latest)
            GROUP BY policy_bucket, risk_level, review_state
            ORDER BY
              CASE risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
              END,
              total DESC,
              policy_bucket
            """,
        )
        package_buckets = _all(
            con,
            """
            WITH latest AS (
              SELECT id
              FROM segment_token_policy_runs
              WHERE finished_at IS NOT NULL
                AND total_candidates > 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              substr(relative_path, 1, instr(relative_path || '/', '/') - 1) AS package_name,
              policy_bucket,
              risk_level,
              COUNT(*) AS total
            FROM segment_token_policy_items
            WHERE run_id = (SELECT id FROM latest)
            GROUP BY package_name, policy_bucket, risk_level
            ORDER BY total DESC, package_name, policy_bucket
            LIMIT 200
            """,
        )
        review_queue = _all(
            con,
            """
            WITH latest AS (
              SELECT id
              FROM segment_token_policy_runs
              WHERE finished_at IS NOT NULL
                AND total_candidates > 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            ),
            latest_confirmation AS (
              SELECT *
              FROM (
                SELECT
                  sc.*,
                  ROW_NUMBER() OVER (PARTITION BY sc.segment_id ORDER BY sc.updated_at DESC, sc.id DESC) AS rn
                FROM segment_confirmations sc
              )
              WHERE rn = 1
            )
            SELECT
              i.id AS policy_item_id,
              i.segment_id,
              i.relative_path,
              i.source_line_number,
              i.source_key,
              i.review_state,
              i.diff_kind,
              i.policy_bucket,
              i.risk_level,
              i.recommendation,
              i.missing_tokens_json,
              i.extra_tokens_json,
              i.issue_flags_json,
              s.english_text,
              s.spanish_text,
              s.old_text,
              o.portuguese_text AS output_text,
              sc.confirmed_text
            FROM segment_token_policy_items i
            JOIN source_segments s ON s.id = i.segment_id
            LEFT JOIN output_segments o ON o.segment_id = i.segment_id
            LEFT JOIN latest_confirmation sc ON sc.segment_id = i.segment_id
            WHERE i.run_id = (SELECT id FROM latest)
            ORDER BY
              CASE i.risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
              END,
              i.policy_bucket,
              i.relative_path,
              i.source_line_number
            LIMIT 300
            """,
        )
        return {
            "available": True,
            "summary": summary,
            "runs": runs,
            "bucketDistribution": bucket_distribution,
            "packageBuckets": package_buckets,
            "reviewQueue": review_queue,
            "decisions": decision_payload(),
            "overlay": overlay_payload(),
            "checkpoints": checkpoint_payload(),
        }

    if not (_table_exists(con, "segment_state_runs") and _table_exists(con, "segment_state_items")):
        return {
            "available": False,
            "summary": {},
            "stateDistribution": [],
            "outputApplication": [],
            "packageBacklog": [],
            "applyQueue": [],
            "reopenQueue": [],
            "outputApply": output_apply_payload(),
            "tokenPolicy": token_policy_payload(),
        }

    summary = _one(
        con,
        """
        WITH latest AS (
          SELECT id
          FROM segment_state_runs
          WHERE total_segments > 1000
          ORDER BY finished_at DESC, id DESC
          LIMIT 1
        )
        SELECT
          r.id AS run_id,
          r.rule_version,
          r.active_score_run_id,
          r.candidate_score_run_id,
          r.policy_run_id,
          r.total_segments,
          r.closed_count,
          r.pending_count,
          ROUND(100.0 * r.closed_count / NULLIF(r.total_segments, 0), 2) AS closed_pct,
          ROUND(100.0 * r.pending_count / NULLIF(r.total_segments, 0), 2) AS pending_pct,
          r.output_apply_pending_count,
          r.blank_valid_count,
          r.reopen_count,
          r.finished_at
        FROM segment_state_runs r
        WHERE r.id = (SELECT id FROM latest)
        """,
    )
    run_id = summary.get("run_id")
    if not run_id:
        return {
            "available": False,
            "summary": {},
            "stateDistribution": [],
            "outputApplication": [],
            "packageBacklog": [],
            "applyQueue": [],
            "reopenQueue": [],
            "outputApply": output_apply_payload(),
            "tokenPolicy": token_policy_payload(),
        }

    state_distribution = _all(
        con,
        """
        SELECT
          state_group,
          final_state,
          COUNT(*) AS total,
          ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_total
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY state_group, final_state
        ORDER BY state_group, total DESC
        """,
        (run_id,),
    )
    output_application = _all(
        con,
        """
        SELECT
          output_state,
          apply_state,
          review_state,
          COUNT(*) AS total
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY output_state, apply_state, review_state
        ORDER BY total DESC
        """,
        (run_id,),
    )
    package_backlog = _all(
        con,
        """
        SELECT
          substr(relative_path, 1, instr(relative_path || '/', '/') - 1) AS package_name,
          COUNT(*) AS total,
          SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) AS closed_count,
          SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS needs_apply_count,
          SUM(CASE WHEN needs_reopen = 1 THEN 1 ELSE 0 END) AS reopen_count,
          ROUND(100.0 * SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS closed_pct
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY package_name
        ORDER BY pending_count DESC, needs_apply_count DESC, package_name
        LIMIT 40
        """,
        (run_id,),
    )
    apply_queue = _all(
        con,
        """
        SELECT
          segment_id,
          relative_path,
          source_line_number,
          source_key,
          final_state,
          review_state,
          priority_score,
          reasons_json
        FROM segment_state_items
        WHERE run_id = ?
          AND needs_output_apply = 1
        ORDER BY priority_score DESC, relative_path, source_line_number
        LIMIT 200
        """,
        (run_id,),
    )
    reopen_queue = _all(
        con,
        """
        SELECT
          segment_id,
          relative_path,
          source_line_number,
          source_key,
          final_state,
          review_state,
          active_action,
          candidate_action,
          policy_action,
          priority_score,
          reasons_json
        FROM segment_state_items
        WHERE run_id = ?
          AND needs_reopen = 1
        ORDER BY priority_score DESC, relative_path, source_line_number
        LIMIT 200
        """,
        (run_id,),
    )

    grouped = [
        {"name": "Consolidado", "value": _int(summary.get("closed_count")), "group": "closed"},
        {"name": "Pendente", "value": _int(summary.get("pending_count")), "group": "pending"},
    ]
    return {
        "available": True,
        "summary": summary,
        "groupDistribution": grouped,
        "stateDistribution": state_distribution,
        "outputApplication": output_application,
        "packageBacklog": package_backlog,
        "applyQueue": apply_queue,
        "reopenQueue": reopen_queue,
        "outputApply": output_apply_payload(),
        "tokenPolicy": token_policy_payload(),
    }


def _dashboard_payload(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        segment_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS active_segments,
              SUM(CASE WHEN has_english = 1 THEN 1 ELSE 0 END) AS with_english,
              SUM(CASE WHEN has_old = 1 THEN 1 ELSE 0 END) AS with_old
            FROM source_segments
            WHERE is_active = 1
            """,
        )
        output_counts = _one(
            con,
            """
            SELECT
              SUM(CASE WHEN COALESCE(o.portuguese_text, '') <> '' THEN 1 ELSE 0 END) AS with_output,
              SUM(CASE WHEN COALESCE(o.portuguese_text, '') = '' THEN 1 ELSE 0 END) AS without_output
            FROM source_segments s
            LEFT JOIN output_segments o ON o.segment_id = s.id
            WHERE s.is_active = 1
            """,
        )
        confirmation_counts = _one(
            con,
            """
            SELECT
              COUNT(DISTINCT sc.segment_id) AS total,
              COUNT(DISTINCT CASE WHEN sc.locked = 1 THEN sc.segment_id END) AS locked,
              COUNT(DISTINCT CASE WHEN sc.locked = 1 AND sc.confirmation_level IN ('human_confirmed', 'human') THEN sc.segment_id END) AS locked_human,
              COUNT(DISTINCT CASE WHEN sc.confirmation_level = 'auto_confirmed' THEN sc.segment_id END) AS auto_confirmed,
              COUNT(DISTINCT CASE WHEN sc.locked = 0 AND sc.confirmation_level IN ('human_confirmed', 'human') THEN sc.segment_id END) AS human_unlocked
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            """,
        )
        review_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN human_label = 'pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN human_label <> 'pending' THEN 1 ELSE 0 END) AS reviewed
            FROM local_learning_candidates
            """,
        )
        issue_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN severity IN ('high', 'critical') THEN 1 ELSE 0 END) AS high
            FROM issues
            """,
        )

        latest_score = _latest_score(con, operational=True) or _latest_score(con)
        previous_score = _latest_score(con, 1, operational=True) or _latest_score(con, 1)
        active_model = _active_model(con)
        latest_model = _latest_model(con)
        latest_dataset = _latest_dataset(con)

        scored = _int(latest_score.get("scored_count"))
        auto_safe = _int(latest_score.get("final_auto_safe_count"))
        pending = (
            _int(latest_score.get("needs_human_count"))
            + _int(latest_score.get("needs_autofix_count"))
            + _int(latest_score.get("blocked_structure_count"))
        )
        auto_safe_pct = _pct(auto_safe, scored)
        previous_auto_safe_pct = _pct(
            previous_score.get("final_auto_safe_count"),
            previous_score.get("scored_count"),
        )
        latest_score_id = _int(latest_score.get("id"))

        model_rows = _all(
            con,
            """
            SELECT id, model_version, macro_f1, safe_precision, safe_recall, false_safe_count, predicted_safe_count, started_at
            FROM ml_model_runs
            WHERE model_kind = 'risk_action_classifier'
            ORDER BY id DESC
            LIMIT 12
            """,
        )
        model_rows.reverse()
        ml_trend = [
            {
                "model": _run_axis_label(row["id"]),
                "modelVersion": row.get("model_version"),
                "modelLabel": _short_model_name(row.get("model_version")),
                "runId": row["id"],
                "macroF1": round(_num(row.get("macro_f1")), 4),
                "safePrecision": round(_num(row.get("safe_precision")), 4),
                "safeRecall": round(_num(row.get("safe_recall")), 4),
                "falseSafe": _int(row.get("false_safe_count")),
                "predictedSafe": _int(row.get("predicted_safe_count")),
            }
            for row in model_rows
        ]
        latest_by_model: dict[str, dict[str, Any]] = {}
        for row in ml_trend:
            key = row.get("modelLabel") or row.get("modelVersion") or str(row.get("runId"))
            if key not in latest_by_model or _int(row.get("runId")) > _int(latest_by_model[key].get("runId")):
                latest_by_model[key] = row
        ml_trend_by_model = [
            {
                **row,
                "model": row.get("modelLabel") or row.get("model"),
            }
            for row in sorted(latest_by_model.values(), key=lambda item: _int(item.get("runId")))
        ]

        score_rows = _all(
            con,
            """
            SELECT id, model_run_id, scored_count, final_auto_safe_count, needs_human_count, needs_autofix_count, blocked_structure_count, started_at
            FROM ml_score_runs
            WHERE scored_count >= 10000
            ORDER BY id DESC
            LIMIT 10
            """,
        )
        score_rows.reverse()
        score_model_rows = _all(
            con,
            """
            SELECT id, model_version, macro_f1
            FROM ml_model_runs
            WHERE id IN (
              SELECT DISTINCT model_run_id
              FROM ml_score_runs
              WHERE scored_count >= 10000
            )
            """,
        )
        score_model_by_id = {row["id"]: row for row in score_model_rows}
        quality_trend = []
        for row in score_rows:
            score_pending = _int(row.get("needs_human_count")) + _int(row.get("needs_autofix_count")) + _int(row.get("blocked_structure_count"))
            current_model = score_model_by_id.get(_int(row.get("model_run_id")), {})
            quality_index = round(_num(current_model.get("macro_f1")) * 100, 2) if current_model else 0
            quality_trend.append(
                {
                    "run": f"R{row['id']}",
                    "runLabel": _run_axis_label(row["id"]),
                    "modelRunId": row.get("model_run_id"),
                    "modelVersion": current_model.get("model_version"),
                    "qualityIndex": quality_index,
                    "autoSafe": _pct(row.get("final_auto_safe_count"), row.get("scored_count")),
                    "pending": score_pending,
                }
            )
        latest_score_by_model: dict[str, dict[str, Any]] = {}
        for row in quality_trend:
            key = _short_model_name(row.get("modelVersion")) or str(row.get("modelRunId"))
            if key not in latest_score_by_model or _int(row.get("modelRunId")) > _int(latest_score_by_model[key].get("modelRunId")):
                latest_score_by_model[key] = row
        quality_trend_by_model = [
            {
                **row,
                "runLabel": _short_model_name(row.get("modelVersion")),
            }
            for row in sorted(latest_score_by_model.values(), key=lambda item: _int(item.get("modelRunId")))
        ]

        dataset_composition = [
            {"label": "Positivos", "value": _int(latest_dataset.get("positive_count"))},
            {"label": "Negativos", "value": _int(latest_dataset.get("negative_count"))},
            {"label": "Neutros", "value": _int(latest_dataset.get("neutral_count"))},
            {"label": "Strong +", "value": _int(latest_dataset.get("strong_positive_count"))},
            {"label": "Strong -", "value": _int(latest_dataset.get("strong_negative_count"))},
        ]

        segment_distribution = [
            {"name": "Auto-safe", "value": auto_safe, "color": "#10b981"},
            {"name": "Revisao humana", "value": _int(latest_score.get("needs_human_count")), "color": "#f59e0b"},
            {"name": "Autofix", "value": _int(latest_score.get("needs_autofix_count")), "color": "#3b82f6"},
            {"name": "Bloqueio estrutural", "value": _int(latest_score.get("blocked_structure_count")), "color": "#ef4444"},
        ]

        pipeline_status = [
            {"status": "Sem output", "count": _int(output_counts.get("without_output"))},
            {
                "status": "Output nao confirmado",
                "count": max(_int(output_counts.get("with_output")) - _int(confirmation_counts.get("total")), 0),
            },
            {"status": "Confirmado automatico", "count": _int(confirmation_counts.get("auto_confirmed"))},
            {"status": "Confirmado humano", "count": _int(confirmation_counts.get("human_unlocked"))},
            {"status": "Locked humano", "count": _int(confirmation_counts.get("locked_human"))},
        ]

        package_backlog = _all(
            con,
            """
            SELECT
              relative_path AS file,
              SUM(CASE WHEN final_action <> 'auto_safe' THEN 1 ELSE 0 END) AS pending
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY relative_path
            HAVING pending > 0
            ORDER BY pending DESC
            LIMIT 8
            """,
            (latest_score_id,),
        )

        human_reviews = _all(
            con,
            """
            SELECT
              DATE(COALESCE(reviewed_at, updated_at, created_at)) AS day,
              SUM(CASE WHEN human_label = 'correct' THEN 1 ELSE 0 END) AS correct,
              SUM(CASE WHEN human_label = 'minor_fix' THEN 1 ELSE 0 END) AS minorFix,
              SUM(CASE WHEN human_label = 'semantic_error' THEN 1 ELSE 0 END) AS semanticError,
              SUM(CASE WHEN human_label = 'residual_spanish' THEN 1 ELSE 0 END) AS residualSpanish
            FROM local_learning_candidates
            WHERE human_label <> 'pending'
            GROUP BY DATE(COALESCE(reviewed_at, updated_at, created_at))
            ORDER BY day DESC
            LIMIT 7
            """,
        )
        human_reviews.reverse()

        promotion_rows = _all(
            con,
            """
            SELECT
              p.id,
              p.candidate_model_run_id,
              COALESCE(m.model_version, 'modelo ' || p.candidate_model_run_id) AS model_version,
              p.decision,
              COALESCE(m.false_safe_count, 0) AS false_safe_count,
              COALESCE(m.safe_recall, 0) AS safe_recall,
              COALESCE(m.safe_precision, 0) AS safe_precision
            FROM ml_model_promotions p
            LEFT JOIN ml_model_runs m ON m.id = p.candidate_model_run_id
            ORDER BY p.id DESC
            LIMIT 10
            """,
        )
        promotion_rows.reverse()
        promotion_timeline = [
            {
                "model": _run_axis_label(row["candidate_model_run_id"]),
                "modelVersion": row["model_version"],
                "decision": "Promovido" if row["decision"] == "promote" else "Rejeitado",
                "risk": _int(row["false_safe_count"]),
                "holdoutCoverage": round(_num(row.get("safe_recall")) * 100, 2),
                "safePrecision": round(_num(row.get("safe_precision")) * 100, 2),
            }
            for row in promotion_rows
        ]

        block_reasons = _all(
            con,
            """
            SELECT issue_type AS reason, COUNT(*) AS count
            FROM issues
            GROUP BY issue_type
            ORDER BY count DESC
            LIMIT 8
            """,
        )
        if not block_reasons:
            block_reasons = _all(
                con,
                """
                SELECT reason, SUM(count) AS count
                FROM (
                  SELECT 'Bloqueio estrutural' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND final_action = 'blocked_structure'
                  UNION ALL
                  SELECT 'Bloqueio determinístico' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND deterministic_blocked = 1
                  UNION ALL
                  SELECT 'Token diferente de ok' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND COALESCE(token_status, 'ok') <> 'ok'
                )
                GROUP BY reason
                HAVING count > 0
                ORDER BY count DESC
                """,
                (latest_score_id, latest_score_id, latest_score_id),
            )

        confirmation_sources = _all(
            con,
            """
            SELECT
              COALESCE(confirmation_source, confirmation_level, 'desconhecido') AS name,
              COUNT(*) AS value
            FROM segment_confirmations
            GROUP BY COALESCE(confirmation_source, confirmation_level, 'desconhecido')
            ORDER BY value DESC
            LIMIT 6
            """,
        )
        palette = ["#10b981", "#8b5cf6", "#3b82f6", "#f59e0b", "#14b8a6", "#ef4444"]
        for index, row in enumerate(confirmation_sources):
            row["color"] = palette[index % len(palette)]

        model_total_count = _int(
            _one(
                con,
                "SELECT COUNT(*) AS total FROM ml_model_runs WHERE model_kind = 'risk_action_classifier'",
            ).get("total")
        )
        promoted_count = _int(
            _one(
                con,
                """
                WITH latest_decision AS (
                  SELECT p.*
                  FROM ml_model_promotions p
                  JOIN (
                    SELECT candidate_model_run_id, MAX(id) AS latest_id
                    FROM ml_model_promotions
                    GROUP BY candidate_model_run_id
                  ) latest ON latest.latest_id = p.id
                )
                SELECT COUNT(*) AS total
                FROM latest_decision
                WHERE decision = 'promote'
                """,
            ).get("total")
        )
        rejected_count = _int(
            _one(
                con,
                """
                WITH latest_decision AS (
                  SELECT p.*
                  FROM ml_model_promotions p
                  JOIN (
                    SELECT candidate_model_run_id, MAX(id) AS latest_id
                    FROM ml_model_promotions
                    GROUP BY candidate_model_run_id
                  ) latest ON latest.latest_id = p.id
                )
                SELECT COUNT(*) AS total
                FROM latest_decision
                WHERE decision <> 'promote'
                """,
            ).get("total")
        )
        evaluated_model_count = _int(
            _one(
                con,
                "SELECT COUNT(DISTINCT candidate_model_run_id) AS total FROM ml_model_promotions",
            ).get("total")
        )
        unevaluated_model_count = max(model_total_count - evaluated_model_count, 0)

        active_precision = _num(active_model.get("safe_precision"))
        latest_precision = _num(latest_model.get("safe_precision"))
        candidate_delta = latest_precision - active_precision
        model_comparison = [
            {"metric": "Accuracy", "current": round(_num(active_model.get("accuracy")), 4), "candidate": round(_num(latest_model.get("accuracy")), 4), "format": "percent"},
            {"metric": "Macro F1", "current": round(_num(active_model.get("macro_f1")), 4), "candidate": round(_num(latest_model.get("macro_f1")), 4)},
            {"metric": "Safe Precision", "current": round(active_precision, 4), "candidate": round(latest_precision, 4)},
            {"metric": "Holdout Coverage", "current": round(_num(active_model.get("safe_recall")), 4), "candidate": round(_num(latest_model.get("safe_recall")), 4), "format": "percent"},
            {"metric": "False Safe", "current": _int(active_model.get("false_safe_count")), "candidate": _int(latest_model.get("false_safe_count"))},
            {"metric": "Predicted Safe", "current": _int(active_model.get("predicted_safe_count")), "candidate": _int(latest_model.get("predicted_safe_count"))},
        ]

        return {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "databasePath": str(db_path),
            "cockpit": {
                "kpis": {
                    "activeSegments": _int(segment_counts.get("active_segments")),
                    "outputCoverage": _pct(output_counts.get("with_output"), segment_counts.get("active_segments")),
                    "autoSafeEfficiency": auto_safe_pct,
                    "autoSafeDelta": round(auto_safe_pct - previous_auto_safe_pct, 2),
                    "pendingReview": pending,
                },
                "qualityTrend": quality_trend,
                "qualityTrendByModel": quality_trend_by_model,
                "segmentDistribution": segment_distribution,
                "status": {
                    "activeModel": active_model.get("active_model_version"),
                    "activeModelRunId": active_model.get("active_model_run_id"),
                    "latestScoreRunId": latest_score.get("id"),
                    "latestDatasetRunId": latest_dataset.get("id"),
                },
            },
            "mlPerformance": {
                "kpis": {
                    "activeModelShort": _short_model_name(active_model.get("active_model_version")),
                    "activeModel": active_model.get("active_model_version"),
                    "macroF1": round(_num(active_model.get("macro_f1")), 4),
                    "safePrecision": round(active_precision, 4),
                    "holdoutCoverage": round(_num(active_model.get("safe_recall")), 4),
                    "negativeCoverage": _pct(latest_dataset.get("negative_count"), latest_dataset.get("total_count")),
                },
                "mlTrend": ml_trend,
                "mlTrendByModel": ml_trend_by_model,
                "datasetComposition": dataset_composition,
                "modelComparison": model_comparison,
                "candidateDecision": "promote" if latest_model.get("id") == active_model.get("active_model_run_id") else "review",
                "candidateDeltaSafePrecision": round(candidate_delta, 4),
            },
            "pipeline": {
                "kpis": {
                    "segmentsTotal": _int(segment_counts.get("active_segments")),
                    "withOutput": _int(output_counts.get("with_output")),
                    "withoutOutput": _int(output_counts.get("without_output")),
                    "lockedHuman": _int(confirmation_counts.get("locked_human")),
                    "confirmed": _int(confirmation_counts.get("total")),
                    "pendingReview": _int(review_counts.get("pending")),
                    "structuralIssues": _int(issue_counts.get("high")),
                    "autofix": _int(latest_score.get("needs_autofix_count")),
                },
                "pipelineStatus": pipeline_status,
                "funnelData": [
                    {"step": "Source", "value": _int(segment_counts.get("active_segments"))},
                    {"step": "Output", "value": _int(output_counts.get("with_output"))},
                    {"step": "Analisado", "value": _int(_one(con, "SELECT COUNT(DISTINCT segment_id) AS total FROM segment_analysis").get("total"))},
                    {"step": "Scored ML", "value": scored},
                    {"step": "Confirmado", "value": _int(confirmation_counts.get("total"))},
                    {"step": "Locked", "value": _int(confirmation_counts.get("locked"))},
                ],
                "packageBacklog": package_backlog,
                "humanReviews": human_reviews,
            },
            "governance": {
                "kpis": {
                    "lockedHuman": _int(confirmation_counts.get("locked_human")),
                    "blockedStructure": _int(latest_score.get("blocked_structure_count")),
                    "tokenIssues": _int(_one(con, "SELECT COUNT(*) AS total FROM issues WHERE issue_type LIKE '%token%' OR issue_type LIKE '%placeholder%'").get("total")),
                    "falseSafeHoldout": _int(active_model.get("false_safe_count")),
                    "totalModels": model_total_count,
                    "rejectedModels": rejected_count,
                    "lastPromotion": _short_model_name(active_model.get("active_model_version")),
                },
                "promotionTimeline": promotion_timeline,
                "blockReasons": block_reasons,
                "confirmationSources": confirmation_sources,
                "policy": [
                    {"title": "Threshold seguro atual", "value": f"{round(_num(active_model.get('safe_threshold')) * 100, 1)}%"},
                    {"title": "Promocao de modelo", "value": "False safe precisa ser 0"},
                    {"title": "Locked humano", "value": "Nunca sobrescrever automaticamente"},
                    {"title": "Estrutura e tokens", "value": "Prioridade sobre ML"},
                ],
                "modelSnapshot": {
                    "active": {
                        "label": "Modelo ativo/promovido",
                        "runId": active_model.get("active_model_run_id"),
                        "version": active_model.get("active_model_version"),
                        "accuracy": round(_num(active_model.get("accuracy")), 4),
                        "macroF1": round(_num(active_model.get("macro_f1")), 4),
                        "safePrecision": round(active_precision, 4),
                        "safeRecall": round(_num(active_model.get("safe_recall")), 4),
                        "holdoutCoverage": round(_num(active_model.get("safe_recall")), 4),
                        "negativeCoverage": _dataset_negative_coverage(con, active_model.get("dataset_run_id")),
                    },
                    "latest": {
                        "label": "Último modelo treinado",
                        "runId": latest_model.get("id"),
                        "version": latest_model.get("model_version"),
                        "accuracy": round(_num(latest_model.get("accuracy")), 4),
                        "macroF1": round(_num(latest_model.get("macro_f1")), 4),
                        "safePrecision": round(latest_precision, 4),
                        "safeRecall": round(_num(latest_model.get("safe_recall")), 4),
                        "holdoutCoverage": round(_num(latest_model.get("safe_recall")), 4),
                        "negativeCoverage": _dataset_negative_coverage(con, latest_model.get("dataset_run_id")),
                    },
                },
            },
            "policy": _policy_payload(con),
            "lab": _lab_payload(con),
            "specialists": _specialists_payload(con),
            "agents": _agents_payload(con),
            "lifecycle": _lifecycle_payload(con),
            "learning": _learning_status_payload(con),
            "production": _production_payload(con),
        }
    finally:
        con.close()


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB

    def _send_html(self, status: int, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("", "/", "/api"):
            self._send_html(
                200,
                """
                <!doctype html>
                <html lang="pt-BR">
                  <head>
                    <meta charset="utf-8" />
                    <title>CK3 PT-BR Dashboard API</title>
                    <style>
                      body { font-family: system-ui, sans-serif; margin: 40px; line-height: 1.5; }
                      code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
                    </style>
                  </head>
                  <body>
                    <h1>CK3 PT-BR Dashboard API</h1>
                    <p>Este servidor é apenas o backend de dados.</p>
                    <p>Abra o dashboard visual em <code>http://127.0.0.1:5173</code> depois de iniciar o frontend.</p>
                    <p>Endpoint JSON: <a href="/api/dashboard">/api/dashboard</a></p>
                    <p>Health check: <a href="/api/health">/api/health</a></p>
                  </body>
                </html>
                """,
            )
            return
        if path == "/api/health":
            self._send_json(200, {"ok": self.db_path.exists(), "databasePath": str(self.db_path)})
            return
        if path == "/api/dashboard":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                self._send_json(200, _dashboard_payload(self.db_path))
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/api/production/status":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                con = sqlite3.connect(self.db_path)
                con.row_factory = sqlite3.Row
                try:
                    self._send_json(200, {"production": _production_payload(con)})
                finally:
                    con.close()
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path in ("/api/production/runs/latest", "/api/production/run/latest"):
            self._send_json(200, {"run": _read_production_run_status()})
            return
        if path == "/api/learning/status":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                con = sqlite3.connect(self.db_path)
                con.row_factory = sqlite3.Row
                try:
                    self._send_json(200, {"learning": _learning_status_payload(con)})
                finally:
                    con.close()
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/production/start":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                learning = _learning_status_payload(con)
                lock = learning.get("lock") or _training_lock_payload(con)
                if not learning.get("can_start_production"):
                    self._send_json(
                        423,
                        {
                            "accepted": False,
                            "status": "blocked",
                            "lock": lock,
                            "learning": learning,
                        },
                    )
                    return
                result = _start_production_run()
                self._send_json(202 if result.get("accepted") else 409, result)
            finally:
                con.close()
            return
        self._send_json(404, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[dashboard] {self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CK3 PT-BR dashboard read-only API.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to translation_engine.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    DashboardHandler.db_path = Path(args.db).resolve()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard API: http://{args.host}:{args.port}/api/dashboard")
    print(f"SQLite: {DashboardHandler.db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
