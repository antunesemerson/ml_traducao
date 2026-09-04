from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "source_tree_snapshot_v2_cached_hashes"


def inspect_tree(
    root: Path,
    cached_files: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    cached_files = cached_files or {}
    for path in sorted(item for item in root.rglob("*.yml") if item.is_file()):
        relative_path = path.relative_to(root).as_posix()
        stat = path.stat()
        cached = cached_files.get(relative_path) or {}
        if (
            int(cached.get("size_bytes") or -1) == stat.st_size
            and int(cached.get("source_mtime_ns") or -1) == stat.st_mtime_ns
            and cached.get("file_hash")
        ):
            file_hash = str(cached["file_hash"])
        else:
            file_hash = db.file_hash(path)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
        records.append(
            {
                "relative_path": relative_path,
                "file_hash": file_hash,
                "size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            }
        )
    return (
        {
            "root": str(root.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
            "tree_hash": digest.hexdigest(),
            "file_count": len(records),
            "total_bytes": sum(record["size_bytes"] for record in records),
        },
        records,
    )


def source_index_matches_snapshot(conn, snapshot_id: int) -> bool:
    """Check the indexed English/Spanish manifests without rereading source files."""
    snapshot_rows = conn.execute(
        """
        SELECT source_kind, relative_path, file_hash
        FROM source_tree_snapshot_files
        WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchall()
    indexed_rows = conn.execute(
        """
        SELECT package_name, relative_path, file_hash
        FROM files
        WHERE package_name IN ('english_source', 'spanish_source')
        """
    ).fetchall()
    source_kind_by_package = {
        "english_source": "english",
        "spanish_source": "spanish",
    }
    snapshot_manifest = {
        (str(row["source_kind"]), str(row["relative_path"])): str(row["file_hash"] or "")
        for row in snapshot_rows
    }
    indexed_manifest = {
        (
            source_kind_by_package[str(row["package_name"])],
            str(row["relative_path"]),
        ): str(row["file_hash"] or "")
        for row in indexed_rows
    }
    return snapshot_manifest == indexed_manifest


def create_snapshot(
    *,
    label: str,
    game_version: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    english_root = db.project_path(settings["english_source"])
    spanish_root = db.project_path(settings["spanish_source"])
    created_at = db.utc_now()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        latest = conn.execute(
            "SELECT id FROM source_tree_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cached_by_kind: dict[str, dict[str, dict[str, Any]]] = {
            "english": {},
            "spanish": {},
        }
        if latest:
            for row in conn.execute(
                """
                SELECT source_kind, relative_path, file_hash, size_bytes, source_mtime_ns
                FROM source_tree_snapshot_files
                WHERE snapshot_id = ?
                """,
                (int(latest["id"]),),
            ).fetchall():
                cached_by_kind.setdefault(str(row["source_kind"]), {})[
                    str(row["relative_path"])
                ] = dict(row)
        english, english_files = inspect_tree(
            english_root,
            cached_by_kind.get("english"),
        )
        spanish, spanish_files = inspect_tree(
            spanish_root,
            cached_by_kind.get("spanish"),
        )
        existing = conn.execute(
            """
            SELECT *
            FROM source_tree_snapshots
            WHERE english_tree_hash = ? AND spanish_tree_hash = ?
            LIMIT 1
            """,
            (english["tree_hash"], spanish["tree_hash"]),
        ).fetchone()
        if existing:
            snapshot_id = int(existing["id"])
            reused = True
        else:
            cursor = conn.execute(
                """
                INSERT INTO source_tree_snapshots (
                    snapshot_label, game_version,
                    english_tree_hash, spanish_tree_hash,
                    english_file_count, spanish_file_count,
                    english_total_bytes, spanish_total_bytes,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    label,
                    game_version,
                    english["tree_hash"],
                    spanish["tree_hash"],
                    english["file_count"],
                    spanish["file_count"],
                    english["total_bytes"],
                    spanish["total_bytes"],
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            reused = False
            rows = []
            for source_kind, file_records in (
                ("english", english_files),
                ("spanish", spanish_files),
            ):
                rows.extend(
                    (
                        snapshot_id,
                        source_kind,
                        record["relative_path"],
                        record["file_hash"],
                        record["size_bytes"],
                        record["source_mtime_ns"],
                        created_at,
                    )
                    for record in file_records
                )
            conn.executemany(
                """
                INSERT INTO source_tree_snapshot_files (
                    snapshot_id, source_kind, relative_path, file_hash,
                    size_bytes, source_mtime_ns, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        conn.commit()

    return {
        "rule_version": RULE_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_label": label,
        "game_version": game_version,
        "reused": reused,
        "english": english,
        "spanish": spanish,
        "created_at": created_at,
    }


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Freeze exact English and Spanish game source fingerprints.")
    parser.add_argument("--label", default="v3_current_game_sources")
    parser.add_argument("--game-version", default=None)
    parser.add_argument("--notes", default="Current game sources captured before V3 scoring and discovery.")
    args = parser.parse_args()
    result = create_snapshot(
        label=args.label,
        game_version=args.game_version,
        metadata={"notes": args.notes},
    )
    print("[source-tree-snapshot] Snapshot ready")
    print(f"[source-tree-snapshot] ID: {result['snapshot_id']}")
    print(f"[source-tree-snapshot] Reused: {result['reused']}")
    print(
        "[source-tree-snapshot] English: "
        f"{result['english']['file_count']} files, {result['english']['tree_hash']}"
    )
    print(
        "[source-tree-snapshot] Spanish: "
        f"{result['spanish']['file_count']} files, {result['spanish']['tree_hash']}"
    )
    return result


if __name__ == "__main__":
    main()
