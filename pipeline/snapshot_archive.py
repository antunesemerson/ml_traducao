from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEMORY_DIR = PROJECT_ROOT / "memory"
DEFAULT_SNAPSHOTS_ROOT = MEMORY_DIR / "production_snapshots"
DEFAULT_ARCHIVES_ROOT = MEMORY_DIR / "production_snapshot_archives"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.2f} GB"
    if num_bytes >= 1024**2:
        return f"{num_bytes / 1024**2:.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.2f} KB"
    return f"{num_bytes} B"


def resolve_child(root: Path, child_name: str) -> Path:
    if Path(child_name).name != child_name:
        raise ValueError(f"Snapshot id must be a direct directory name: {child_name!r}")
    root_resolved = root.resolve()
    child = (root_resolved / child_name).resolve()
    if child.parent != root_resolved:
        raise ValueError(f"Refusing path outside snapshots root: {child}")
    return child


def iter_files(path: Path):
    for child in path.iterdir():
        if child.is_dir():
            yield from iter_files(child)
        elif child.is_file():
            yield child


def directory_size(path: Path) -> tuple[int, int]:
    total = 0
    files = 0
    if not path.exists():
        return total, files
    for file_path in iter_files(path):
        try:
            total += file_path.stat().st_size
            files += 1
        except OSError:
            continue
    return total, files


@dataclass(frozen=True)
class SnapshotInfo:
    name: str
    path: Path
    size_bytes: int
    file_count: int
    has_manifest: bool
    has_archive: bool
    archive_path: Path


def load_snapshots(snapshots_root: Path, archives_root: Path) -> list[SnapshotInfo]:
    if not snapshots_root.exists():
        return []
    rows: list[SnapshotInfo] = []
    for child in sorted(snapshots_root.iterdir(), key=lambda p: p.name, reverse=True):
        if not child.is_dir():
            continue
        size_bytes, file_count = directory_size(child)
        archive_path = archives_root / f"{child.name}.zip"
        rows.append(
            SnapshotInfo(
                name=child.name,
                path=child,
                size_bytes=size_bytes,
                file_count=file_count,
                has_manifest=(child / "manifest.json").is_file(),
                has_archive=archive_path.is_file(),
                archive_path=archive_path,
            )
        )
    return rows


def print_snapshot_list(snapshots_root: Path, archives_root: Path) -> int:
    rows = load_snapshots(snapshots_root, archives_root)
    if not rows:
        print(f"No snapshots found in {snapshots_root}")
        return 0

    print("Production snapshots")
    print(f"Snapshots root: {snapshots_root}")
    print(f"Archives root:  {archives_root}")
    print()
    for row in rows:
        status: list[str] = []
        status.append("manifest" if row.has_manifest else "no-manifest")
        status.append("archived" if row.has_archive else "not-archived")
        print(
            f"- {row.name} | {format_size(row.size_bytes)} | "
            f"{row.file_count} files | {', '.join(status)}"
        )
    total = sum(row.size_bytes for row in rows)
    print()
    print(f"Total open snapshot size: {format_size(total)}")
    return 0


def verify_archive(archive_path: Path, expected_snapshot_id: str | None = None) -> bool:
    if not archive_path.is_file():
        print(f"[verify] Archive not found: {archive_path}")
        return False
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            bad_file = zf.testzip()
            if bad_file:
                print(f"[verify] Corrupt file inside archive: {bad_file}")
                return False
            names = set(zf.namelist())
            manifests = [name for name in names if name.endswith("/manifest.json")]
            if not manifests:
                print("[verify] No manifest.json found inside archive")
                return False
            if expected_snapshot_id:
                expected_manifest = f"{expected_snapshot_id}/manifest.json"
                if expected_manifest not in names:
                    print(f"[verify] Expected manifest not found: {expected_manifest}")
                    return False
                with zf.open(expected_manifest) as handle:
                    try:
                        json.load(handle)
                    except json.JSONDecodeError:
                        print(f"[verify] Manifest is not valid JSON: {expected_manifest}")
                        return False
    except zipfile.BadZipFile:
        print(f"[verify] Bad zip file: {archive_path}")
        return False
    print(f"[verify] OK: {archive_path}")
    return True


def rmtree_with_readonly_retry(path: Path) -> None:
    def handle_remove_error(function, failed_path, exc_info) -> None:
        del exc_info
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            function(failed_path)
        except Exception as exc:  # pragma: no cover - platform-specific cleanup guard.
            raise exc

    shutil.rmtree(path, onexc=handle_remove_error)


def delete_verified_original(snapshot_id: str, snapshots_root: Path, archives_root: Path) -> int:
    snapshot_path = resolve_child(snapshots_root, snapshot_id)
    archive_path = archives_root / f"{snapshot_id}.zip"
    if not snapshot_path.is_dir():
        print(f"[delete-original] Snapshot folder not found: {snapshot_path}")
        return 0
    if not verify_archive(archive_path, snapshot_id):
        print("[delete-original] Verification failed. Original snapshot was kept.")
        return 1
    print(f"[delete-original] Deleting verified original snapshot: {snapshot_path}")
    rmtree_with_readonly_retry(snapshot_path)
    print("[delete-original] Original snapshot deleted.")
    return 0


def archive_snapshot(
    snapshot_id: str,
    snapshots_root: Path,
    archives_root: Path,
    *,
    compression_level: int,
    force: bool,
    delete_original: bool,
) -> int:
    snapshot_path = resolve_child(snapshots_root, snapshot_id)
    if not snapshot_path.is_dir():
        print(f"[archive] Snapshot not found: {snapshot_path}")
        return 1
    manifest_path = snapshot_path / "manifest.json"
    if not manifest_path.is_file():
        print(f"[archive] Refusing to archive snapshot without manifest: {snapshot_path}")
        return 1

    archives_root.mkdir(parents=True, exist_ok=True)
    archive_path = archives_root / f"{snapshot_id}.zip"
    if archive_path.exists() and not force:
        print(f"[archive] Archive already exists: {archive_path}")
        print("[archive] Use --force to replace it.")
        return 1
    if archive_path.exists() and force:
        archive_path.unlink()

    files = list(iter_files(snapshot_path))
    total_bytes = sum(path.stat().st_size for path in files)
    print(f"[archive] Snapshot: {snapshot_id}")
    print(f"[archive] Files: {len(files)}")
    print(f"[archive] Input size: {format_size(total_bytes)}")
    print(f"[archive] Output: {archive_path}")

    written = 0
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=compression_level,
        allowZip64=True,
    ) as zf:
        for index, file_path in enumerate(files, start=1):
            arcname = Path(snapshot_id) / file_path.relative_to(snapshot_path)
            zf.write(file_path, arcname.as_posix())
            written += file_path.stat().st_size
            if index % 1000 == 0 or index == len(files):
                print(
                    f"[archive] {index}/{len(files)} files | "
                    f"{format_size(written)} / {format_size(total_bytes)}"
                )

    archive_size = archive_path.stat().st_size
    ratio = archive_size / total_bytes if total_bytes else 0
    print(f"[archive] Archive size: {format_size(archive_size)} ({ratio:.1%} of original)")

    if not verify_archive(archive_path, snapshot_id):
        print("[archive] Verification failed. Original snapshot was kept.")
        return 1

    if delete_original:
        # Last guard: resolve again immediately before deletion.
        return delete_verified_original(snapshot_id, snapshots_root, archives_root)
    else:
        print("[archive] Original snapshot kept. Use --delete-original only after reviewing the zip.")
    return 0


def delete_incremental_backups(backups_root: Path, *, execute: bool) -> int:
    backups_root = backups_root.resolve()
    expected = (MEMORY_DIR / "backups").resolve()
    if backups_root != expected:
        print(f"[backups] Refusing non-default backups root: {backups_root}")
        return 1
    if not backups_root.exists():
        print(f"[backups] No incremental backups directory found: {backups_root}")
        return 0
    total_bytes, file_count = directory_size(backups_root)
    print(f"[backups] Directory: {backups_root}")
    print(f"[backups] Files: {file_count}")
    print(f"[backups] Size: {format_size(total_bytes)}")
    if not execute:
        print("[backups] Dry-run only. Re-run with --execute to delete.")
        return 0
    rmtree_with_readonly_retry(backups_root)
    print("[backups] Incremental backups deleted.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive and verify full production snapshots safely.",
    )
    parser.add_argument(
        "--snapshots-root",
        default=str(DEFAULT_SNAPSHOTS_ROOT),
        help="Directory containing open production snapshot folders.",
    )
    parser.add_argument(
        "--archives-root",
        default=str(DEFAULT_ARCHIVES_ROOT),
        help="Directory where snapshot .zip archives are written.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List open snapshots and archive status.")

    archive_parser = subparsers.add_parser("archive", help="Archive one snapshot to ZIP64.")
    archive_parser.add_argument("snapshot_id", help="Snapshot directory name, for example 20260530_111458.")
    archive_parser.add_argument("--compression-level", type=int, default=6, choices=range(0, 10))
    archive_parser.add_argument("--force", action="store_true", help="Replace an existing zip archive.")
    archive_parser.add_argument(
        "--delete-original",
        action="store_true",
        help="Delete the snapshot folder after ZIP verification succeeds.",
    )

    verify_parser = subparsers.add_parser("verify", help="Verify one snapshot archive.")
    verify_parser.add_argument("archive", help="Path to a snapshot .zip archive.")
    verify_parser.add_argument("--snapshot-id", help="Expected snapshot id inside the archive.")

    delete_original_parser = subparsers.add_parser(
        "delete-original",
        help="Verify an existing archive and delete its open snapshot folder.",
    )
    delete_original_parser.add_argument(
        "snapshot_id",
        help="Snapshot directory name whose archive already exists.",
    )

    backups_parser = subparsers.add_parser(
        "delete-incremental-backups",
        help="Delete memory/backups after a dry-run summary.",
    )
    backups_parser.add_argument("--execute", action="store_true", help="Actually delete memory/backups.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    snapshots_root = Path(args.snapshots_root)
    archives_root = Path(args.archives_root)

    print(f"[snapshot_archive] Started at {now_iso()}")
    if args.command == "list":
        return print_snapshot_list(snapshots_root, archives_root)
    if args.command == "archive":
        return archive_snapshot(
            args.snapshot_id,
            snapshots_root,
            archives_root,
            compression_level=args.compression_level,
            force=args.force,
            delete_original=args.delete_original,
        )
    if args.command == "verify":
        ok = verify_archive(Path(args.archive), args.snapshot_id)
        return 0 if ok else 1
    if args.command == "delete-original":
        return delete_verified_original(args.snapshot_id, snapshots_root, archives_root)
    if args.command == "delete-incremental-backups":
        return delete_incremental_backups(MEMORY_DIR / "backups", execute=args.execute)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
