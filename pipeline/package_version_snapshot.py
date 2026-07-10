from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import db


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_ROOT = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

import backend as dashboard_backend  # noqa: E402


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def package_tree_hash(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*.yml") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(db.file_hash(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), len(files)


def scalar(conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def latest_state(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed operational segment-state run was found.")
    return dict(row)


def score_run(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise RuntimeError(f"Score run {run_id} was not found.")
    return dict(row)


def existing_version(conn, version_number: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM package_versions WHERE version_number = ?",
        (version_number,),
    ).fetchone()
    return dict(row) if row else None


def insert_version(
    conn,
    *,
    version_number: int,
    version_label: str,
    package_name: str,
    package_role: str,
    source_path: Path,
    package_hash: str,
    file_count: int,
    score_run_row: dict[str, Any],
    state: dict[str, Any],
    parent_version_id: int | None,
    change_cohort_score: float | None,
    change_cohort_delta: float | None,
    changed_from_parent_count: int,
    metadata: dict[str, Any],
) -> int:
    version_existing = existing_version(conn, version_number)
    if version_existing:
        if version_existing["package_hash"] != package_hash:
            raise RuntimeError(
                f"Package v{version_number} already exists with a different hash. "
                "Version numbers are immutable."
            )
        return int(version_existing["id"])

    score_run_id = int(score_run_row["id"])
    full_average_score = scalar(
        conn,
        """
        SELECT AVG(score.model_safe_probability)
        FROM ml_score_items score
        JOIN source_segments source ON source.id = score.segment_id
        WHERE score.run_id = ? AND source.is_active = 1
          AND score.model_safe_probability IS NOT NULL
        """,
        (score_run_id,),
    )
    measured_score_count = int(
        scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM ml_score_items score
            JOIN source_segments source ON source.id = score.segment_id
            WHERE score.run_id = ? AND source.is_active = 1
              AND score.model_safe_probability IS NOT NULL
            """,
            (score_run_id,),
        )
        or 0
    )
    segment_count = int(
        scalar(conn, "SELECT COUNT(*) FROM source_segments WHERE is_active = 1") or 0
    )
    frozen_at = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO package_versions (
            version_number, version_label, package_name, package_role,
            parent_version_id, source_path, package_hash, file_count,
            segment_count, measured_score_count, full_average_score,
            change_cohort_score, change_cohort_delta, changed_from_parent_count,
            segment_state_run_id, score_run_id, score_rule_version,
            closed_count, pending_count, needs_output_apply_count,
            status, metadata_json, frozen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'frozen', ?, ?)
        """,
        (
            version_number,
            version_label,
            package_name,
            package_role,
            parent_version_id,
            str(source_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            package_hash,
            file_count,
            segment_count,
            measured_score_count,
            full_average_score,
            change_cohort_score,
            change_cohort_delta,
            changed_from_parent_count,
            int(state["id"]),
            score_run_id,
            score_run_row.get("rule_version"),
            int(state.get("closed_count") or 0),
            int(state.get("pending_count") or 0),
            int(state.get("output_apply_pending_count") or 0),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            frozen_at,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(
    conn,
    *,
    version_id: int,
    package_name: str,
    score_run_id: int,
    state_run_id: int,
) -> int:
    if scalar(conn, "SELECT COUNT(*) FROM package_version_items WHERE version_id = ?", (version_id,)):
        return int(
            scalar(conn, "SELECT COUNT(*) FROM package_version_items WHERE version_id = ?", (version_id,))
            or 0
        )

    is_output = package_name == "output_spanish"
    text_hash = "output.portuguese_hash" if is_output else "source.old_hash"
    output_join = "LEFT JOIN output_segments output ON output.segment_id = source.id" if is_output else ""
    state_columns = (
        "state.final_state, state.state_group, COALESCE(state.is_closed, 0), "
        "COALESCE(state.needs_output_apply, 0)"
        if is_output
        else "NULL, NULL, 0, 0"
    )
    state_join = (
        "LEFT JOIN segment_state_items state ON state.run_id = ? AND state.segment_id = source.id"
        if is_output
        else ""
    )
    parameters: list[Any] = [version_id, score_run_id]
    if is_output:
        parameters.append(state_run_id)
    parameters.append(utc_now())
    conn.execute(
        f"""
        INSERT INTO package_version_items (
            version_id, segment_id, relative_path, source_key,
            source_line_number, text_hash, score, score_action,
            risk_class, token_status, final_state, state_group,
            is_closed, needs_output_apply, confirmation_locked, created_at
        )
        SELECT
            ?, source.id, source.relative_path, source.source_key,
            source.source_line_number, {text_hash}, score.model_safe_probability,
            score.final_action, score.risk_class, score.token_status,
            {state_columns}, COALESCE(score.locked, 0), ?
        FROM source_segments source
        {output_join}
        LEFT JOIN ml_score_items score ON score.run_id = ? AND score.segment_id = source.id
        {state_join}
        WHERE source.is_active = 1
        """,
        tuple([parameters[0], parameters[-1], parameters[1], *parameters[2:-1]]),
    )
    return int(
        scalar(conn, "SELECT COUNT(*) FROM package_version_items WHERE version_id = ?", (version_id,))
        or 0
    )


def insert_changes(
    conn,
    *,
    version_id: int,
    parent_version_id: int,
    old_score_run_id: int,
    new_score_run_id: int,
) -> int:
    if scalar(conn, "SELECT COUNT(*) FROM package_version_changes WHERE version_id = ?", (version_id,)):
        return int(
            scalar(conn, "SELECT COUNT(*) FROM package_version_changes WHERE version_id = ?", (version_id,))
            or 0
        )
    conn.execute(
        """
        INSERT INTO package_version_changes (
            version_id, parent_version_id, segment_id, relative_path,
            source_key, source_line_number, previous_text, current_text,
            previous_text_hash, current_text_hash, previous_score,
            current_score, score_delta, created_at
        )
        SELECT
            ?, ?, source.id, source.relative_path, source.source_key,
            source.source_line_number, source.old_text, output.portuguese_text,
            source.old_hash, output.portuguese_hash,
            old_score.model_safe_probability, new_score.model_safe_probability,
            CASE
                WHEN old_score.model_safe_probability IS NULL OR new_score.model_safe_probability IS NULL THEN NULL
                ELSE new_score.model_safe_probability - old_score.model_safe_probability
            END,
            ?
        FROM source_segments source
        JOIN output_segments output ON output.segment_id = source.id
        LEFT JOIN ml_score_items old_score
          ON old_score.run_id = ? AND old_score.segment_id = source.id
        LEFT JOIN ml_score_items new_score
          ON new_score.run_id = ? AND new_score.segment_id = source.id
        WHERE source.is_active = 1
          AND COALESCE(output.portuguese_text, '') <> COALESCE(source.old_text, '')
        """,
        (
            version_id,
            parent_version_id,
            utc_now(),
            old_score_run_id,
            new_score_run_id,
        ),
    )
    return int(
        scalar(conn, "SELECT COUNT(*) FROM package_version_changes WHERE version_id = ?", (version_id,))
        or 0
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze immutable package versions in the project database.")
    parser.add_argument("--old-version", type=int, default=1)
    parser.add_argument("--output-version", type=int, default=2)
    parser.add_argument("--old-label", default="Primeira baseline estável")
    parser.add_argument("--output-label", default="Segundo pacote validado")
    parser.add_argument("--notes", default="Baseline congelada antes da próxima atualização dos sources.")
    args = parser.parse_args()

    settings = db.load_settings()
    old_root = db.project_path(settings["spanish_traduzido_old"])
    output_root = db.project_path(settings["output_spanish"])
    if not old_root.is_dir() or not output_root.is_dir():
        raise RuntimeError("Both spanish_old and output/spanish must exist before freezing versions.")

    print("[package-version] Hashing package trees")
    old_hash, old_file_count = package_tree_hash(old_root)
    output_hash, output_file_count = package_tree_hash(output_root)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        state = latest_state(conn)
        old_score_run_id = int(state.get("active_score_run_id") or 0)
        new_score_run_id = int(state.get("candidate_score_run_id") or 0)
        if not old_score_run_id or not new_score_run_id:
            raise RuntimeError("Latest segment-state does not identify both old and candidate score runs.")
        old_score = score_run(conn, old_score_run_id)
        new_score = score_run(conn, new_score_run_id)

        print("[package-version] Calculating current comparison summary")
        comparison = dashboard_backend._release_diff_review_payload(conn, int(state["id"]))
        summary = comparison.get("summary") or {}
        package_score = summary.get("package_score_comparison") or {}
        changed_count = int(summary.get("changed_vs_old") or 0)
        common_metadata = {
            "snapshot_schema": "package_versions_v1",
            "segment_state_run_id": int(state["id"]),
            "old_score_run_id": old_score_run_id,
            "new_score_run_id": new_score_run_id,
            "changed_vs_old": changed_count,
            "package_ready_changes": int(summary.get("package_diff_count") or 0),
            "promotions": int(summary.get("promotions_vs_old") or 0),
            "regressions": int(summary.get("score_regressions") or 0),
            "needs_apply": int(summary.get("needs_apply") or 0),
            "notes": args.notes,
        }

        old_version_id = insert_version(
            conn,
            version_number=args.old_version,
            version_label=args.old_label,
            package_name="spanish_old",
            package_role="stable_baseline",
            source_path=old_root,
            package_hash=old_hash,
            file_count=old_file_count,
            score_run_row=old_score,
            state=state,
            parent_version_id=None,
            change_cohort_score=package_score.get("weighted_avg_old_score"),
            change_cohort_delta=None,
            changed_from_parent_count=0,
            metadata={**common_metadata, "comparison_role": "old"},
        )
        output_version_id = insert_version(
            conn,
            version_number=args.output_version,
            version_label=args.output_label,
            package_name="output_spanish",
            package_role="validated_output",
            source_path=output_root,
            package_hash=output_hash,
            file_count=output_file_count,
            score_run_row=new_score,
            state=state,
            parent_version_id=old_version_id,
            change_cohort_score=package_score.get("weighted_avg_new_score"),
            change_cohort_delta=package_score.get("weighted_avg_delta"),
            changed_from_parent_count=changed_count,
            metadata={**common_metadata, "comparison_role": "output"},
        )

        old_items = insert_items(
            conn,
            version_id=old_version_id,
            package_name="spanish_old",
            score_run_id=old_score_run_id,
            state_run_id=int(state["id"]),
        )
        output_items = insert_items(
            conn,
            version_id=output_version_id,
            package_name="output_spanish",
            score_run_id=new_score_run_id,
            state_run_id=int(state["id"]),
        )
        changes = insert_changes(
            conn,
            version_id=output_version_id,
            parent_version_id=old_version_id,
            old_score_run_id=old_score_run_id,
            new_score_run_id=new_score_run_id,
        )
        conn.commit()

        versions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM package_versions WHERE version_number IN (?, ?) ORDER BY version_number",
                (args.old_version, args.output_version),
            ).fetchall()
        ]

    manifest = {
        "created_at": utc_now(),
        "segment_state_run_id": int(state["id"]),
        "versions": versions,
        "item_counts": {
            str(args.old_version): old_items,
            str(args.output_version): output_items,
        },
        "version_change_count": changes,
    }
    manifest_dir = PROJECT_ROOT / "docs" / "package_versions"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"package_versions_v{args.old_version}_v{args.output_version}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[package-version] Frozen v{args.old_version}: {old_items} segments, {old_file_count} files")
    print(f"[package-version] Frozen v{args.output_version}: {output_items} segments, {output_file_count} files")
    print(f"[package-version] Changes v{args.old_version} -> v{args.output_version}: {changes}")
    print(f"[package-version] Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
