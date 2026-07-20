from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import db
import package_version_snapshot as versions


RULE_VERSION = "materialize_package_version_v1"
BACKUP_ROOT = db.PROJECT_ROOT / "release_candidates" / "package_version_baselines"


def _regression_counts(summary: dict[str, Any]) -> tuple[int, int]:
    raw_count = int(summary.get("score_regressions") or 0)
    effective_value = summary.get("effective_package_score_regressions")
    effective_count = raw_count if effective_value is None else int(effective_value or 0)
    return raw_count, effective_count


def _latest_epoch(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM quality_epochs
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No quality epoch is available for materialization.")
    epoch = dict(row)
    if epoch.get("status") not in {"scored", "evaluated", "published"}:
        raise RuntimeError(
            f"Latest quality epoch {epoch['id']} is {epoch.get('status')}; score the epoch before materializing."
        )
    return epoch


def _latest_materialized_version(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, version_number, version_label, package_hash, file_count, frozen_at
        FROM package_versions
        WHERE status = 'materialized'
        ORDER BY version_number DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def _preflight(conn, old_root: Path, output_root: Path) -> dict[str, Any]:
    state = versions.latest_state(conn)
    epoch = _latest_epoch(conn)
    if int(state.get("id") or 0) < int(epoch.get("segment_state_run_id") or 0):
        raise RuntimeError("Latest segment-state predates the scored quality epoch.")
    if int(state.get("pending_count") or 0):
        raise RuntimeError(f"Materialization blocked: {state['pending_count']} pending segments.")
    if int(state.get("output_apply_pending_count") or 0):
        raise RuntimeError(
            f"Materialization blocked: {state['output_apply_pending_count']} output applies pending."
        )
    old_hash, old_files = versions.package_tree_hash(old_root)
    output_hash, output_files = versions.package_tree_hash(output_root)
    if output_files <= 0:
        raise RuntimeError("Output package is empty.")

    latest_version = _latest_materialized_version(conn)
    baseline_matches_output = (old_hash, old_files) == (output_hash, output_files)
    output_matches_latest = bool(
        latest_version
        and str(latest_version.get("package_hash") or "") == output_hash
        and int(latest_version.get("file_count") or 0) == output_files
    )
    if baseline_matches_output and output_matches_latest:
        current_version_number = int(latest_version.get("version_number") or 0)
        current_version_label = str(latest_version.get("version_label") or f"V{current_version_number}")
        return {
            "state": state,
            "epoch": epoch,
            "comparison": {"summary": {"changed_vs_old": 0, "needs_apply": 0}},
            "old_hash": old_hash,
            "old_files": old_files,
            "output_hash": output_hash,
            "output_files": output_files,
            "changed_count": 0,
            "raw_score_regression_count": 0,
            "effective_score_regression_count": 0,
            "summary": {"changed_vs_old": 0, "needs_apply": 0},
            "eligible": False,
            "reason": "no_package_delta",
            "message": (
                f"{current_version_label} já representa output/spanish; "
                "não há alterações no pacote para materializar."
            ),
            "current_version_id": int(latest_version.get("id") or 0),
            "current_version_number": current_version_number,
            "current_version_label": current_version_label,
        }
    if baseline_matches_output and latest_version:
        raise RuntimeError(
            "Baseline and output are identical but do not match the latest materialized package; "
            "reconcile package history before creating another version."
        )
    if (
        int(state.get("active_score_run_id") or 0) != int(epoch.get("old_score_run_id") or 0)
        or int(state.get("candidate_score_run_id") or 0) != int(epoch.get("output_score_run_id") or 0)
    ):
        raise RuntimeError("Latest segment-state is not pinned to the scored quality epoch scores.")
    comparison = versions.dashboard_backend._release_diff_review_payload(conn, int(state["id"]))
    summary = comparison.get("summary") or {}
    raw_regressions, effective_regressions = _regression_counts(summary)
    if effective_regressions:
        raise RuntimeError(
            f"Materialization blocked: {effective_regressions} effective score regressions remain "
            f"({raw_regressions} raw observations)."
        )
    if int(summary.get("needs_apply") or 0):
        raise RuntimeError(f"Materialization blocked: {summary['needs_apply']} changes still need apply.")
    return {
        "state": state,
        "epoch": epoch,
        "comparison": comparison,
        "old_hash": old_hash,
        "old_files": old_files,
        "output_hash": output_hash,
        "output_files": output_files,
        "changed_count": int(summary.get("changed_vs_old") or 0),
        "raw_score_regression_count": raw_regressions,
        "effective_score_regression_count": effective_regressions,
        "summary": summary,
        "eligible": True,
        "reason": "package_delta_ready",
    }


def _copy_verified(output_root: Path, old_root: Path, version_number: int) -> Path:
    staging = old_root.parent / f".{old_root.name}.materialize-v{version_number}.tmp"
    backup = BACKUP_ROOT / f"v{version_number}_previous_spanish_old"
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        raise RuntimeError(f"Version backup already exists: {backup}")
    shutil.copytree(output_root, staging, copy_function=shutil.copy2)
    staged_hash, staged_files = versions.package_tree_hash(staging)
    output_hash, output_files = versions.package_tree_hash(output_root)
    if (staged_hash, staged_files) != (output_hash, output_files):
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("Staged spanish_old copy does not match output/spanish.")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_root), str(backup))
    try:
        shutil.move(str(staging), str(old_root))
    except Exception:
        shutil.move(str(backup), str(old_root))
        raise
    return backup


def _run_index() -> None:
    process = subprocess.run(
        [sys.executable, "pipeline/index_source.py"],
        cwd=str(db.PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if process.returncode:
        tail = "\n".join((process.stderr or process.stdout or "").splitlines()[-30:])
        raise RuntimeError(f"Source reindex failed:\n{tail}")


def materialize(*, apply: bool) -> dict[str, Any]:
    settings = db.load_settings()
    old_root = db.project_path(settings["spanish_traduzido_old"])
    output_root = db.project_path(settings["output_spanish"])
    if not old_root.is_dir() or not output_root.is_dir():
        raise RuntimeError("spanish_old and output/spanish must both exist.")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        check = _preflight(conn, old_root, output_root)
        next_version = int(
            conn.execute("SELECT COALESCE(MAX(version_number), 0) + 1 FROM package_versions").fetchone()[0]
        )
        parent = conn.execute("SELECT * FROM package_versions ORDER BY version_number DESC LIMIT 1").fetchone()
        result = {
            "ok": True,
            "apply": apply,
            "rule_version": RULE_VERSION,
            "version_number": next_version,
            "parent_version_id": int(parent["id"]) if parent else None,
            "quality_epoch_id": int(check["epoch"]["id"]),
            "segment_state_run_id": int(check["state"]["id"]),
            "changed_count": check["changed_count"],
            "old_hash_before": check["old_hash"],
            "output_hash": check["output_hash"],
            "file_count": check["output_files"],
            "raw_score_regression_count": check["raw_score_regression_count"],
            "effective_score_regression_count": check["effective_score_regression_count"],
            "eligible": bool(check.get("eligible", True)),
            "reason": check.get("reason") or "package_delta_ready",
        }
        if check.get("message"):
            result["message"] = check["message"]
        for key in ("current_version_id", "current_version_number", "current_version_label"):
            if check.get(key) is not None:
                result[key] = check[key]
        if not result["eligible"]:
            if apply:
                raise RuntimeError(str(result.get("message") or "There are no package changes to materialize."))
            return result
        if not apply:
            return result

        score_run_id = int(check["epoch"]["output_score_run_id"])
        score = versions.score_run(conn, score_run_id)
        package_score = (check["summary"].get("package_score_comparison") or {})
        metadata = {
            "snapshot_schema": "package_versions_v2_materialized_baseline",
            "materialization_rule": RULE_VERSION,
            "quality_epoch_id": int(check["epoch"]["id"]),
            "segment_state_run_id": int(check["state"]["id"]),
            "previous_baseline_hash": check["old_hash"],
            "output_hash": check["output_hash"],
            "changed_vs_parent": check["changed_count"],
            "raw_score_regressions": check["raw_score_regression_count"],
            "effective_score_regressions": check["effective_score_regression_count"],
            "regressions": check["effective_score_regression_count"],
            "needs_apply": 0,
        }
        version_id = versions.insert_version(
            conn,
            version_number=next_version,
            version_label=f"Baseline materializada v{next_version}",
            package_name="output_spanish",
            package_role="materialized_stable_baseline",
            source_path=old_root,
            package_hash=check["output_hash"],
            file_count=check["output_files"],
            score_run_row=score,
            state=check["state"],
            parent_version_id=int(parent["id"]) if parent else None,
            change_cohort_score=package_score.get("weighted_avg_new_score"),
            change_cohort_delta=package_score.get("weighted_avg_delta"),
            changed_from_parent_count=check["changed_count"],
            metadata=metadata,
        )
        items = versions.insert_items(
            conn,
            version_id=version_id,
            package_name="output_spanish",
            score_run_id=score_run_id,
            state_run_id=int(check["state"]["id"]),
        )
        if parent:
            versions.insert_changes(
                conn,
                version_id=version_id,
                parent_version_id=int(parent["id"]),
                old_score_run_id=int(check["epoch"]["old_score_run_id"]),
                new_score_run_id=score_run_id,
            )
        conn.commit()

    backup: Path | None = None
    try:
        backup = _copy_verified(output_root, old_root, next_version)
        _run_index()
        final_hash, final_files = versions.package_tree_hash(old_root)
        if (final_hash, final_files) != (check["output_hash"], check["output_files"]):
            raise RuntimeError("Materialized spanish_old differs from output/spanish after reindex.")
        with db.connect(settings) as conn:
            remaining = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM source_segments source
                    JOIN output_segments output ON output.segment_id = source.id
                    WHERE source.is_active = 1
                      AND COALESCE(source.old_text, '') <> COALESCE(output.portuguese_text, '')
                    """
                ).fetchone()[0]
            )
            if remaining:
                raise RuntimeError(f"Database baseline still differs from output in {remaining} segments.")
            now = db.utc_now()
            conn.execute(
                "UPDATE package_versions SET status = 'materialized', frozen_at = ? WHERE id = ?",
                (now, version_id),
            )
            conn.execute(
                """
                UPDATE quality_epochs
                SET status = 'stale', invalidation_reason = 'baseline_materialized',
                    invalidated_at = ?, updated_at = ?
                WHERE status IN ('open', 'scored', 'evaluated', 'published')
                """,
                (now, now),
            )
            conn.commit()
        result.update(
            {
                "version_id": version_id,
                "item_count": items,
                "backup_path": backup.relative_to(db.PROJECT_ROOT).as_posix(),
                "old_hash_after": final_hash,
                "database_remaining_diff_count": 0,
                "status": "materialized",
            }
        )
        manifest = db.PROJECT_ROOT / "docs" / "package_versions" / f"materialized_v{next_version}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["manifest_path"] = str(manifest)
        return result
    except Exception:
        if backup and backup.exists():
            if old_root.exists():
                shutil.rmtree(old_root)
            shutil.move(str(backup), str(old_root))
            try:
                _run_index()
            except Exception:
                pass
        with db.connect(settings) as conn:
            conn.execute("DELETE FROM package_versions WHERE id = ? AND status = 'frozen'", (version_id,))
            conn.commit()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize output/spanish as the next stable spanish_old baseline.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(materialize(apply=args.apply), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
