from __future__ import annotations

import argparse
import importlib
from datetime import datetime
from pathlib import Path

import db


STAGES = {
    "db": "db",
    "index": "index_source",
    "analyze": "analyze_segments",
    "memory": "build_translation_memory",
    "suggest": "suggest_translations",
    "evaluate": "evaluate_suggestions",
    "apply": "apply_safe_output_updates",
}


def package_roots(settings: dict) -> dict[str, Path]:
    return {
        "spanish_source": db.project_path(settings["spanish_source"]),
        "english_source": db.project_path(settings["english_source"]),
        "spanish_old": db.project_path(settings["spanish_traduzido_old"]),
        "output_spanish": db.project_path(settings["output_spanish"]),
    }


def current_file_manifest(settings: dict) -> dict[tuple[str, str], str]:
    manifest: dict[tuple[str, str], str] = {}
    for package_name, root in package_roots(settings).items():
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.yml")):
            relative_path = path.relative_to(root).as_posix()
            if package_name == "english_source":
                relative_path = relative_path.replace("_l_english.yml", "_l_spanish.yml")
            manifest[(package_name, relative_path)] = db.file_hash(path)
    return manifest


def indexed_file_manifest(settings: dict) -> dict[tuple[str, str], str]:
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = conn.execute(
            """
            SELECT package_name, relative_path, file_hash
            FROM files
            WHERE package_name IN ('spanish_source', 'english_source', 'spanish_old', 'output_spanish')
            """
        ).fetchall()
    manifest: dict[tuple[str, str], str] = {}
    for row in rows:
        relative_path = row["relative_path"]
        if row["package_name"] == "english_source":
            relative_path = relative_path.replace("_l_english.yml", "_l_spanish.yml")
        manifest[(row["package_name"], relative_path)] = row["file_hash"]
    return manifest


def source_index_is_current(settings: dict) -> tuple[bool, list[str]]:
    print("[main] Checking whether source index is current")
    current = current_file_manifest(settings)
    indexed = indexed_file_manifest(settings)
    changes: list[str] = []

    current_keys = set(current)
    indexed_keys = set(indexed)
    for key in sorted(current_keys - indexed_keys):
        changes.append(f"new file: {key[0]}:{key[1]}")
    for key in sorted(indexed_keys - current_keys):
        changes.append(f"missing file: {key[0]}:{key[1]}")
    for key in sorted(current_keys & indexed_keys):
        if current[key] != indexed[key]:
            changes.append(f"changed file: {key[0]}:{key[1]}")
            if len(changes) >= 20:
                break

    return len(changes) == 0, changes


def run_stage(stage_name: str) -> None:
    module_name = STAGES[stage_name]
    print(f"[main] Running stage: {stage_name} ({module_name}.py)")
    module = importlib.import_module(module_name)
    module.main()


def write_main_report(settings: dict, mode: str, lines: list[str], started_at: datetime) -> None:
    elapsed = datetime.now() - started_at
    report_lines = [
        "Pipeline main report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Mode: {mode}",
        "",
        *lines,
    ]
    report_path = db.write_report(settings, f"main_{mode}", report_lines)
    print(f"[main] Report: {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CK3 localization ML pipeline.")
    parser.add_argument(
        "mode",
        nargs="?",
        default="cycle",
        choices=["setup", "cycle", "apply", "full"],
        help="setup: db+index; cycle: setup+learning+suggestions; apply: rewrite output; full: cycle+apply",
    )
    parser.add_argument(
        "--force-index",
        action="store_true",
        help="Run index_source even when file hashes look unchanged.",
    )
    parser.add_argument(
        "--apply-include-safe-pending",
        action="store_true",
        help="During apply/full, also apply safe suggestions that are still pending review.",
    )
    parser.add_argument(
        "--apply-no-backup",
        action="store_true",
        help="During apply/full, skip output backups.",
    )
    args = parser.parse_args()

    started_at = datetime.now()
    settings = db.load_settings()
    report_lines: list[str] = []

    print(f"[main] Starting pipeline mode: {args.mode}")
    run_stage("db")
    report_lines.append("- db: executed")

    should_run_index = True
    if not args.force_index:
        index_current, changes = source_index_is_current(settings)
        should_run_index = not index_current
        if index_current:
            print("[main] Index is current; skipping index_source")
            report_lines.append("- index: skipped, file hashes unchanged")
        else:
            print(f"[main] Index is stale; detected {len(changes)} change(s)")
            report_lines.append(f"- index: stale, detected {len(changes)} change(s)")
            report_lines.extend(f"  - {change}" for change in changes[:20])

    if should_run_index:
        run_stage("index")
        report_lines.append("- index: executed")

    if args.mode in {"cycle", "full"}:
        for stage in ["analyze", "memory", "suggest", "evaluate"]:
            run_stage(stage)
            report_lines.append(f"- {stage}: executed")

    if args.mode in {"apply", "full"}:
        import apply_safe_output_updates

        print("[main] Running stage: apply (apply_safe_output_updates.py)")
        apply_safe_output_updates.main(
            include_safe_pending=args.apply_include_safe_pending,
            create_backup=not args.apply_no_backup,
        )
        report_lines.append("- apply: executed")

    write_main_report(settings, args.mode, report_lines, started_at)
    print("[main] Done")


if __name__ == "__main__":
    main()
