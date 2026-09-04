from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import db
import source_tree_snapshot


RULE_VERSION = "source_update_sync_v1"
STATE_FILE = db.PROJECT_ROOT / "memory" / "source_update_sync_state.json"
DEFAULT_REUSE_SECONDS = 300


def _reusable_verification(max_age_seconds: int) -> dict[str, Any] | None:
    if max_age_seconds <= 0 or not STATE_FILE.exists():
        return None
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    checked_at_epoch = float(state.get("checked_at_epoch") or 0)
    if time.time() - checked_at_epoch > max_age_seconds:
        return None
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, dict) or not int(snapshot.get("snapshot_id") or 0):
        return None
    settings = db.load_settings()
    with db.connect(settings) as conn:
        latest = conn.execute(
            "SELECT id FROM source_tree_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        snapshot_id = int(snapshot["snapshot_id"])
        if not latest or int(latest["id"]) != snapshot_id:
            return None
        if not source_tree_snapshot.source_index_matches_snapshot(conn, snapshot_id):
            return None
    return state


def _write_verification_state(payload: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "rule_version": RULE_VERSION,
        "checked_at_epoch": time.time(),
        "snapshot": payload["snapshot"],
        "index_updated": bool(payload.get("index_updated")),
    }
    temporary = Path(str(STATE_FILE) + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def sync_source_update(
    *,
    label: str,
    game_version: str | None = None,
    metadata: dict[str, Any] | None = None,
    force: bool = False,
    reuse_within_seconds: int = DEFAULT_REUSE_SECONDS,
) -> dict[str, Any]:
    reusable = None if force else _reusable_verification(reuse_within_seconds)
    if reusable:
        return {
            "rule_version": RULE_VERSION,
            "snapshot": reusable["snapshot"],
            "verification_reused": True,
            "index_current_before": True,
            "index_updated": False,
            "index_current_after": True,
            "process": {
                "executed": False,
                "exit_code": 0,
                "stdout_tail": [],
                "stderr_tail": [],
            },
            "output_writes": 0,
            "score_writes": 0,
        }
    snapshot = source_tree_snapshot.create_snapshot(
        label=label,
        game_version=game_version,
        metadata={
            "source": RULE_VERSION,
            **(metadata or {}),
        },
    )
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        index_current_before = source_tree_snapshot.source_index_matches_snapshot(
            conn,
            int(snapshot["snapshot_id"]),
        )

    process_payload: dict[str, Any] = {
        "executed": False,
        "exit_code": 0,
        "stdout_tail": [],
        "stderr_tail": [],
    }
    if not index_current_before:
        process = subprocess.run(
            [sys.executable, "pipeline/main.py", "setup"],
            cwd=str(db.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        stdout_lines = (process.stdout or "").splitlines()
        stderr_lines = (process.stderr or "").splitlines()
        process_payload = {
            "executed": True,
            "exit_code": process.returncode,
            "stdout_tail": stdout_lines[-12:],
            "stderr_tail": stderr_lines[-12:],
        }
        if process.returncode != 0:
            detail = "\n".join(stderr_lines[-8:] or stdout_lines[-8:])
            raise RuntimeError(detail or "Source index synchronization failed.")

    with db.connect(settings) as conn:
        index_current_after = source_tree_snapshot.source_index_matches_snapshot(
            conn,
            int(snapshot["snapshot_id"]),
        )
    if not index_current_after:
        raise RuntimeError("Source snapshot and indexed manifest still differ after synchronization.")

    payload = {
        "rule_version": RULE_VERSION,
        "snapshot": snapshot,
        "verification_reused": False,
        "index_current_before": index_current_before,
        "index_updated": not index_current_before,
        "index_current_after": index_current_after,
        "process": process_payload,
        "output_writes": 0,
        "score_writes": 0,
    }
    _write_verification_state(payload)
    return payload


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Capture the source tree and reindex only when its manifest changed."
    )
    parser.add_argument("--label", default="automatic_source_update_sync")
    parser.add_argument("--game-version", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reuse-within-seconds", type=int, default=DEFAULT_REUSE_SECONDS)
    args = parser.parse_args()
    payload = sync_source_update(
        label=args.label,
        game_version=args.game_version,
        metadata={"consumer": "pipeline"},
        force=args.force,
        reuse_within_seconds=max(0, args.reuse_within_seconds),
    )
    print(f"[source-update-sync] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")
    return payload


if __name__ == "__main__":
    main()
