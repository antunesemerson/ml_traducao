from __future__ import annotations

import argparse
import importlib
import io
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

import db


STAGES = {
    "db": "db",
    "index": "index_source",
    "inline": "index_inline_fragments",
    "analyze": "analyze_segments",
    "memory": "build_translation_memory",
    "suggest": "suggest_translations",
    "evaluate": "evaluate_suggestions",
    "api_validate": "validate_suggestions_api",
    "api_apply": "apply_api_reviews",
    "apply": "apply_safe_output_updates",
}


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


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


def run_stage_with_log(stage_name: str, captured_lines: list[str]) -> None:
    buffer = io.StringIO()
    tee_stdout = Tee(sys.stdout, buffer)
    tee_stderr = Tee(sys.stderr, buffer)
    try:
        with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
            run_stage(stage_name)
    except Exception:
        traceback.print_exc(file=buffer)
        output = buffer.getvalue()
        captured_lines.extend(output.splitlines())
        raise

    output = buffer.getvalue()
    captured_lines.extend(output.splitlines())


def run_cycle_stages(log_lines: list[str], report_lines: list[str]) -> None:
    for stage in ["analyze", "memory", "suggest", "evaluate"]:
        run_stage_with_log(stage, log_lines)
        report_lines.append(f"- {stage}: executed")


def run_api_flow_with_log(
    log_lines: list[str],
    report_lines: list[str],
    limit: int,
    min_confidence: float,
    concurrency: int,
) -> None:
    import apply_api_reviews
    import validate_suggestions_api

    print("[main] Running stage: api_validate (validate_suggestions_api.py)")
    buffer = io.StringIO()
    tee_stdout = Tee(sys.stdout, buffer)
    tee_stderr = Tee(sys.stderr, buffer)
    try:
        with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
            validate_suggestions_api.main(limit=limit, concurrency=concurrency)
    except Exception:
        traceback.print_exc(file=buffer)
        output = buffer.getvalue()
        log_lines.extend(output.splitlines())
        raise
    output = buffer.getvalue()
    log_lines.extend(output.splitlines())
    report_lines.append(f"- api_validate: executed, limit {limit}, concurrency {concurrency}")

    print("[main] Running stage: api_apply (apply_api_reviews.py)")
    buffer = io.StringIO()
    tee_stdout = Tee(sys.stdout, buffer)
    tee_stderr = Tee(sys.stderr, buffer)
    try:
        with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
            apply_api_reviews.main(
                min_confidence=min_confidence,
                include_approved=False,
                mark_auto_ready=True,
                reviewer="api_auto",
            )
    except Exception:
        traceback.print_exc(file=buffer)
        output = buffer.getvalue()
        log_lines.extend(output.splitlines())
        raise
    output = buffer.getvalue()
    log_lines.extend(output.splitlines())
    report_lines.append(f"- api_apply: executed, min confidence {min_confidence}")


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
        choices=["setup", "cycle", "apply", "full", "full-api"],
        help="setup: db+index; cycle: setup+learning+suggestions; apply: rewrite output; full: cycle+apply; full-api: cycle+api+cycle+apply",
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
    parser.add_argument(
        "--bootstrap-old",
        action="store_true",
        help="During apply/full, initialize output/spanish from spanish_old before incremental cycles.",
    )
    parser.add_argument(
        "--api-limit",
        type=int,
        default=200,
        help="During full-api, maximum pending suggestions reviewed by API.",
    )
    parser.add_argument(
        "--api-min-confidence",
        type=float,
        default=None,
        help="During full-api, minimum API confidence promoted automatically.",
    )
    parser.add_argument(
        "--api-concurrency",
        type=int,
        default=None,
        help="During full-api, number of concurrent API reviews.",
    )
    parser.add_argument(
        "--skip-apply",
        action="store_true",
        help="During full/full-api, run learning and API promotion without rewriting output files.",
    )
    args = parser.parse_args()

    started_at = datetime.now()
    settings = db.load_settings()
    report_lines: list[str] = []
    log_lines: list[str] = [
        "Pipeline execution log",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Mode: {args.mode}",
        "",
    ]

    print(f"[main] Starting pipeline mode: {args.mode}")
    try:
        run_stage_with_log("db", log_lines)
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
            run_stage_with_log("index", log_lines)
            report_lines.append("- index: executed")
            run_stage_with_log("inline", log_lines)
            report_lines.append("- inline: executed")

        if args.mode in {"cycle", "full", "full-api"}:
            if not should_run_index:
                run_stage_with_log("inline", log_lines)
                report_lines.append("- inline: executed")
            run_cycle_stages(log_lines, report_lines)

        if args.mode == "full-api":
            api_min_confidence = (
                args.api_min_confidence
                if args.api_min_confidence is not None
                else float(settings.get("api_review", {}).get("auto_apply_min_confidence", 0.97))
            )
            api_concurrency = (
                args.api_concurrency
                if args.api_concurrency is not None
                else int(settings.get("api_review", {}).get("concurrency", 4))
            )
            run_api_flow_with_log(
                log_lines,
                report_lines,
                args.api_limit,
                api_min_confidence,
                api_concurrency,
            )
            run_cycle_stages(log_lines, report_lines)

        if args.skip_apply and args.mode in {"apply", "full", "full-api"}:
            print("[main] Apply skipped by --skip-apply")
            report_lines.append("- apply: skipped by --skip-apply")
        elif args.mode in {"apply", "full", "full-api"}:
            import apply_safe_output_updates

            print("[main] Running stage: apply (apply_safe_output_updates.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_safe_output_updates.main(
                        include_safe_pending=args.apply_include_safe_pending,
                        create_backup=not args.apply_no_backup,
                        bootstrap_old=args.bootstrap_old,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- apply: executed")

            index_current, changes = source_index_is_current(settings)
            if index_current:
                print("[main] Post-apply index is current; skipping refresh")
                report_lines.append("- post-apply refresh: skipped, file hashes unchanged")
            else:
                print(f"[main] Post-apply refresh detected {len(changes)} changed file(s)")
                report_lines.append(f"- post-apply refresh: detected {len(changes)} changed file(s)")
                report_lines.extend(f"  - {change}" for change in changes[:20])
                for stage in ["index", "inline", "analyze", "memory", "suggest", "evaluate"]:
                    run_stage_with_log(stage, log_lines)
                    report_lines.append(f"- post-apply {stage}: executed")

        write_main_report(settings, args.mode, report_lines, started_at)
        print("[main] Done")
    finally:
        log_path = db.write_log(settings, f"main_{args.mode}", log_lines)
        print(f"[main] Log: {log_path}")


if __name__ == "__main__":
    main()
