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
    "learn_local": "local_learning_cycle",
    "learn_feedback": "apply_local_learning_feedback",
    "confirmations": "segment_confirmation_report",
    "auto_validate": "auto_validate_segments",
    "learned_report": "learned_validation_report",
    "learned_apply": "apply_learned_validation",
    "learned_autofix": "apply_learned_autofix",
    "mojibake_audit": "audit_mojibake_confirmations",
    "mojibake_context": "build_mojibake_context_queue",
    "auto_validate_names": "auto_validate_names",
    "visual_rules": "apply_visual_residue_rules",
    "triage_positive_core": "triage_positive_core",
    "batch_audit": "batch_audit_confirmed_packages",
    "package_priority": "package_priority_report",
    "pending_diagnostic": "pending_diagnostic",
    "bulk_confirm_likely": "bulk_confirm_likely",
    "bulk_mechanical_autofix": "bulk_mechanical_autofix",
    "offline_proposals": "offline_residual_proposals",
    "offline_apply": "apply_offline_proposals",
    "offline_review": "build_offline_review_queue",
    "focus_queue": "build_package_focus_queue",
    "closure_queue": "build_finalization_queue",
    "finalize_nicknames": "finalize_nicknames",
    "package_autofix": "package_autofix",
    "name_rejections": "report_name_rejections",
    "name_disagreements": "review_name_disagreements",
    "name_queue": "build_name_equivalence_queue",
    "name_apply": "apply_name_equivalences",
    "title_apply": "apply_title_review_queue",
    "dynasty_prefixes": "report_dynasty_prefixes",
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
        choices=[
            "setup",
            "cycle",
            "learn-local",
            "learn-feedback",
            "confirmations",
            "auto-validate",
            "learned-report",
            "learned-apply",
            "learned-autofix",
            "mojibake-audit",
            "mojibake-context",
            "inline-literals",
            "auto-validate-names",
            "visual-rules",
            "relationship-rules",
            "triage-positive-core",
            "batch-audit",
            "package-priority",
            "pending-diagnostic",
            "bulk-confirm-likely",
            "bulk-confirm-trusted-safe",
            "bulk-mechanical-autofix",
            "composite-autofix",
            "curated-fixes",
            "human-assisted-offline",
            "offline-proposals",
            "offline-apply",
            "offline-review",
            "focus-queue",
            "closure-queue",
            "finalize-nicknames",
            "package-autofix",
            "name-queue",
            "name-apply",
            "title-name-rules",
            "title-queue",
            "title-apply",
            "apply",
            "full",
        ],
        help="Pipeline mode. Common modes: setup, cycle, confirmations, learned-report, learned-apply, learned-autofix, auto-validate, package-priority, package-autofix, apply and full.",
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
        "--skip-apply",
        action="store_true",
        help="During full, run learning without rewriting output files.",
    )
    parser.add_argument(
        "--learn-limit",
        type=int,
        default=None,
        help="During learn-local, number of local learning candidates to queue.",
    )
    parser.add_argument(
        "--learn-auto-confidence",
        type=float,
        default=None,
        help="During learn-local, confidence threshold used only to mark high-confidence preview rows.",
    )
    parser.add_argument(
        "--learn-source",
        choices=["pending", "positive", "review-light"],
        default=None,
        help="During learn-local, use pending suggestions, positive source samples, or review-light residue samples.",
    )
    parser.add_argument(
        "--learn-focus",
        choices=["all", "core", "titles", "world", "ui", "events"],
        default=None,
        help="During learn-local, restrict the queue to a priority corpus group.",
    )
    parser.add_argument(
        "--auto-limit",
        type=int,
        default=None,
        help="During auto-validate, maximum candidates inspected per source.",
    )
    parser.add_argument(
        "--auto-min-score",
        type=float,
        default=None,
        help="During auto-validate, minimum score for auto confirmation.",
    )
    parser.add_argument(
        "--auto-path-like",
        default=None,
        help="During auto-validate, optional SQL LIKE filter for source relative_path, for example gui/%%.",
    )
    parser.add_argument(
        "--auto-offset",
        type=int,
        default=0,
        help="During inline-literals, skip this many candidate rows before inspecting.",
    )
    parser.add_argument(
        "--auto-focus",
        choices=["common", "concepts", "pronouns"],
        default=None,
        help="During inline-literals, use a focused residue term group.",
    )
    parser.add_argument(
        "--auto-term",
        action="append",
        default=None,
        help="During inline-literals, add a focused search term. Can be used more than once.",
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="During auto-validate/learned-apply, write auto_confirmed rows. Default is report only.",
    )
    parser.add_argument(
        "--offline-run-id",
        type=int,
        default=None,
        help="During offline-apply, offline_proposal_runs id. Default is latest.",
    )
    parser.add_argument(
        "--offline-include-literal-changed",
        action="store_true",
        help="During offline-apply, allow proposals that changed translatable literals inside CK3 tokens.",
    )
    parser.add_argument(
        "--offline-reason-like",
        default=None,
        help="During offline-review, only queue proposals whose reasons_json contains this text.",
    )
    parser.add_argument(
        "--offline-issue-code",
        default=None,
        help="During offline-review, only queue proposals whose issues_json contains this issue code.",
    )
    parser.add_argument(
        "--offline-proposal-source",
        default=None,
        help="During offline-review, only queue proposals from this proposal_source.",
    )
    parser.add_argument(
        "--closure-bucket",
        default=None,
        help="During closure-queue, filter the top segment preview to one closure bucket.",
    )
    parser.add_argument(
        "--mojibake-kind",
        default=None,
        help="During mojibake-context, filter preview by residue kind.",
    )
    parser.add_argument(
        "--learned-run-id",
        type=int,
        default=None,
        help="During learned-apply, learned_validation_runs id. Default is latest.",
    )
    parser.add_argument(
        "--learned-actions",
        default=None,
        help="During learned-apply, comma-separated actions to promote, for example auto_safe.",
    )
    parser.add_argument(
        "--learned-max-words",
        type=int,
        default=None,
        help="During learned-apply, only promote rows with word_count at or below this value.",
    )
    parser.add_argument(
        "--learned-exclude-audit-flags",
        action="store_true",
        help="During learned-apply, skip rows marked as technical_row or sensitive_path.",
    )
    parser.add_argument(
        "--learned-exclude-path-like",
        action="append",
        default=[],
        help="During learned-apply, SQL LIKE relative_path pattern to exclude. May be repeated.",
    )
    parser.add_argument(
        "--learned-require-source-match",
        action="store_true",
        help="During learned-apply, require candidate_text, english_text, and spanish_text to be identical.",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=None,
        help="During visual-rules, maximum rows inspected before writing a report.",
    )
    parser.add_argument(
        "--start-after",
        type=int,
        default=None,
        help="During visual-rules, only inspect source segment ids greater than this value.",
    )
    parser.add_argument(
        "--triage-batch-size",
        type=int,
        default=50,
        help="During triage-positive-core, number of candidates fetched per internal run.",
    )
    parser.add_argument(
        "--triage-review-limit",
        type=int,
        default=80,
        help="During triage-positive-core, maximum review candidates listed in the report.",
    )
    parser.add_argument(
        "--batch-audit-apply",
        action="store_true",
        help="During batch-audit, promote clean low/medium-risk packages. Default is dry-run only.",
    )
    parser.add_argument(
        "--batch-audit-max-hits",
        type=int,
        default=12,
        help="During batch-audit, maximum hit samples kept per package.",
    )
    parser.add_argument(
        "--focus-limit",
        type=int,
        default=100,
        help="During focus-queue, maximum packages in the high-impact focus group.",
    )
    parser.add_argument(
        "--package-limit",
        type=int,
        default=10,
        help="During package-autofix, inspect this many high-impact packages.",
    )
    parser.add_argument(
        "--bulk-max-words",
        type=int,
        default=30,
        help="During bulk-confirm-likely, maximum visible word count to auto-confirm.",
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

        should_check_index = args.mode not in {
            "learn-feedback",
            "learn-local",
            "confirmations",
            "learned-report",
            "learned-apply",
            "learned-autofix",
            "mojibake-audit",
            "mojibake-context",
            "inline-literals",
            "auto-validate",
            "auto-validate-names",
            "visual-rules",
            "relationship-rules",
            "triage-positive-core",
            "batch-audit",
            "package-priority",
            "pending-diagnostic",
            "bulk-confirm-likely",
            "bulk-confirm-trusted-safe",
            "bulk-mechanical-autofix",
            "offline-proposals",
            "offline-apply",
            "offline-review",
            "focus-queue",
            "closure-queue",
            "finalize-nicknames",
            "package-autofix",
            "name-queue",
            "name-apply",
            "title-queue",
            "title-apply",
        }
        should_run_index = should_check_index
        if should_check_index and not args.force_index:
            index_current, changes = source_index_is_current(settings)
            should_run_index = not index_current
            if index_current:
                print("[main] Index is current; skipping index_source")
                report_lines.append("- index: skipped, file hashes unchanged")
            else:
                print(f"[main] Index is stale; detected {len(changes)} change(s)")
                report_lines.append(f"- index: stale, detected {len(changes)} change(s)")
                report_lines.extend(f"  - {change}" for change in changes[:20])
        elif not should_check_index:
            print(f"[main] Index check skipped for {args.mode}")
            report_lines.append(f"- index: skipped for {args.mode}")

        if should_run_index:
            run_stage_with_log("index", log_lines)
            report_lines.append("- index: executed")
            run_stage_with_log("inline", log_lines)
            report_lines.append("- inline: executed")

        if args.mode in {"cycle", "full"}:
            if not should_run_index:
                run_stage_with_log("inline", log_lines)
                report_lines.append("- inline: executed")
            run_cycle_stages(log_lines, report_lines)

        if args.mode == "learn-local":
            import local_learning_cycle

            print("[main] Running stage: learn_local (local_learning_cycle.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    local_learning_cycle.main(
                        limit=args.learn_limit,
                        auto_confidence_threshold=args.learn_auto_confidence,
                        queue_source=args.learn_source,
                        focus_group=args.learn_focus,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- learn_local: executed")

        if args.mode == "learn-feedback":
            run_stage_with_log("learn_feedback", log_lines)
            report_lines.append("- learn_feedback: executed")

        if args.mode == "confirmations":
            run_stage_with_log("confirmations", log_lines)
            report_lines.append("- confirmations: executed")

        if args.mode == "mojibake-context":
            import build_mojibake_context_queue

            print("[main] Running stage: mojibake_context (build_mojibake_context_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    build_mojibake_context_queue.main(
                        limit=args.auto_limit or 80,
                        kind=args.mojibake_kind,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- mojibake_context: executed")

        if args.mode == "learned-report":
            import learned_validation_report

            print("[main] Running stage: learned_report (learned_validation_report.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    learned_validation_report.main(
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- learned_report: executed")

        if args.mode == "learned-apply":
            import apply_learned_validation

            print("[main] Running stage: learned_apply (apply_learned_validation.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_learned_validation.main(
                        run_id=args.learned_run_id,
                        actions_value=args.learned_actions,
                        min_score=args.auto_min_score or 0.95,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        max_words=args.learned_max_words,
                        exclude_audit_flags=args.learned_exclude_audit_flags,
                        exclude_path_like=tuple(args.learned_exclude_path_like or ()),
                        require_source_match=args.learned_require_source_match,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- learned_apply: executed, apply={args.auto_apply}")

        if args.mode == "learned-autofix":
            import apply_learned_autofix

            print("[main] Running stage: learned_autofix (apply_learned_autofix.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_learned_autofix.main(
                        run_id=args.learned_run_id,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- learned_autofix: executed, apply={args.auto_apply}")

        if args.mode == "mojibake-audit":
            import audit_mojibake_confirmations

            print("[main] Running stage: mojibake_audit (audit_mojibake_confirmations.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    audit_mojibake_confirmations.main(
                        apply=args.auto_apply,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- mojibake_audit: executed, apply={args.auto_apply}")

        if args.mode == "inline-literals":
            import apply_inline_literal_fixes

            print("[main] Running stage: inline_literals (apply_inline_literal_fixes.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_inline_literal_fixes.main(
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        offset=args.auto_offset,
                        focus=args.auto_focus,
                        terms=args.auto_term,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- inline_literals: executed, apply={args.auto_apply}")

        if args.mode == "auto-validate":
            import auto_validate_segments

            print("[main] Running stage: auto_validate (auto_validate_segments.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_validate_segments.main(
                        limit=args.auto_limit,
                        min_score=args.auto_min_score,
                        apply=args.auto_apply,
                        path_like=args.auto_path_like,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- auto_validate: executed, apply={args.auto_apply}")

        if args.mode == "auto-validate-names":
            import auto_validate_names

            print("[main] Running stage: auto_validate_names (auto_validate_names.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_validate_names.main(
                        limit=args.auto_limit,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- auto_validate_names: executed, apply={args.auto_apply}")

        if args.mode == "visual-rules":
            import apply_visual_residue_rules

            print("[main] Running stage: visual_rules (apply_visual_residue_rules.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_visual_residue_rules.main(
                        limit=args.auto_limit,
                        apply=args.auto_apply,
                        scan_limit=args.scan_limit,
                        start_after=args.start_after,
                        path_like=args.auto_path_like,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- visual_rules: executed, apply={args.auto_apply}")

        if args.mode == "relationship-rules":
            import apply_relationship_reason_rules

            print("[main] Running stage: relationship_rules (apply_relationship_reason_rules.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_relationship_reason_rules.main(
                        limit=args.auto_limit,
                        apply=args.auto_apply,
                        scan_limit=args.scan_limit,
                        start_after=args.start_after,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- relationship_rules: executed, apply={args.auto_apply}")

        if args.mode == "triage-positive-core":
            import triage_positive_core

            print("[main] Running stage: triage_positive_core (triage_positive_core.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    triage_positive_core.main(
                        limit=args.auto_limit or 500,
                        batch_size=args.triage_batch_size,
                        review_limit=args.triage_review_limit,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- triage_positive_core: executed, limit={args.auto_limit or 500}"
            )

        if args.mode == "batch-audit":
            import batch_audit_confirmed_packages

            print("[main] Running stage: batch_audit (batch_audit_confirmed_packages.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    batch_audit_confirmed_packages.main(
                        apply=args.batch_audit_apply,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        max_hits=args.batch_audit_max_hits,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- batch_audit: executed, apply={args.batch_audit_apply}"
            )

        if args.mode == "package-priority":
            import package_priority_report

            print("[main] Running stage: package_priority (package_priority_report.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    package_priority_report.main(limit=args.auto_limit)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- package_priority: executed")

        if args.mode == "pending-diagnostic":
            import pending_diagnostic

            print("[main] Running stage: pending_diagnostic (pending_diagnostic.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    pending_diagnostic.main(
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- pending_diagnostic: executed")

        if args.mode == "bulk-confirm-likely":
            import bulk_confirm_likely

            print("[main] Running stage: bulk_confirm_likely (bulk_confirm_likely.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    bulk_confirm_likely.main(
                        limit=args.auto_limit,
                        max_words=args.bulk_max_words,
                        min_score=args.auto_min_score or 0.94,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- bulk_confirm_likely: executed, apply={args.auto_apply}")

        if args.mode == "bulk-confirm-trusted-safe":
            import bulk_confirm_trusted_safe

            print("[main] Running stage: bulk_confirm_trusted_safe (bulk_confirm_trusted_safe.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    bulk_confirm_trusted_safe.main(
                        limit=args.auto_limit,
                        min_analysis_score=args.auto_min_score or 0.99,
                        max_words=args.bulk_max_words,
                        exclude_path_like=tuple(args.learned_exclude_path_like or ()),
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- bulk_confirm_trusted_safe: executed, apply={args.auto_apply}")

        if args.mode == "bulk-mechanical-autofix":
            import bulk_mechanical_autofix

            print("[main] Running stage: bulk_mechanical_autofix (bulk_mechanical_autofix.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    bulk_mechanical_autofix.main(
                        limit=args.auto_limit,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- bulk_mechanical_autofix: executed, apply={args.auto_apply}")

        if args.mode == "composite-autofix":
            import apply_composite_autofix

            print("[main] Running stage: composite_autofix (apply_composite_autofix.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_composite_autofix.main(
                        run_id=args.learned_run_id,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- composite_autofix: executed, apply={args.auto_apply}")

        if args.mode == "curated-fixes":
            import apply_curated_residual_fixes

            print("[main] Running stage: curated_fixes (apply_curated_residual_fixes.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_curated_residual_fixes.main(apply=args.auto_apply)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- curated_fixes: executed, apply={args.auto_apply}")

        if args.mode == "human-assisted-offline":
            import apply_human_assisted_offline

            print("[main] Running stage: human_assisted_offline (apply_human_assisted_offline.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_human_assisted_offline.main(
                        run_id=args.offline_run_id,
                        min_score=args.auto_min_score or 0.80,
                        limit=args.auto_limit,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- human_assisted_offline: executed, apply={args.auto_apply}")

        if args.mode == "offline-proposals":
            import offline_residual_proposals

            print("[main] Running stage: offline_proposals (offline_residual_proposals.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    offline_residual_proposals.main(
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- offline_proposals: executed")

        if args.mode == "offline-apply":
            import apply_offline_proposals

            print("[main] Running stage: offline_apply (apply_offline_proposals.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_offline_proposals.main(
                        run_id=args.offline_run_id,
                        min_score=args.auto_min_score or 0.90,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        include_literal_changed=args.offline_include_literal_changed,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- offline_apply: executed, apply={args.auto_apply}")

        if args.mode == "offline-review":
            import build_offline_review_queue

            print("[main] Running stage: offline_review (build_offline_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    build_offline_review_queue.main(
                        offline_run_id=args.offline_run_id,
                        limit=args.auto_limit or 50,
                        path_like=args.auto_path_like,
                        reason_like=args.offline_reason_like,
                        issue_code=args.offline_issue_code,
                        proposal_source=args.offline_proposal_source,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- offline_review: executed")

        if args.mode == "focus-queue":
            import build_package_focus_queue

            print("[main] Running stage: focus_queue (build_package_focus_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    build_package_focus_queue.main(
                        focus_limit=args.focus_limit,
                        inspect_limit=args.auto_limit or 300,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- focus_queue: executed, focus_limit={args.focus_limit}")

        if args.mode == "closure-queue":
            import build_finalization_queue

            print("[main] Running stage: closure_queue (build_finalization_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    build_finalization_queue.main(
                        limit=args.auto_limit,
                        bucket=args.closure_bucket,
                        top_limit=args.focus_limit or 40,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- closure_queue: executed")

        if args.mode == "finalize-nicknames":
            import finalize_nicknames

            print("[main] Running stage: finalize_nicknames (finalize_nicknames.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    finalize_nicknames.main(limit=args.auto_limit, apply=args.auto_apply)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- finalize_nicknames: executed, apply={args.auto_apply}")

        if args.mode == "package-autofix":
            import package_autofix

            print("[main] Running stage: package_autofix (package_autofix.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    package_autofix.main(
                        package_limit=args.package_limit,
                        segment_limit=args.auto_limit or 1000,
                        path_like=args.auto_path_like,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- package_autofix: executed, package_limit={args.package_limit}, apply={args.auto_apply}"
            )

        if args.mode == "name-queue":
            import build_name_equivalence_queue

            print("[main] Running stage: name_queue (build_name_equivalence_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    build_name_equivalence_queue.main(limit=args.auto_limit)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- name_queue: executed")

        if args.mode == "name-apply":
            import apply_name_equivalences

            print("[main] Running stage: name_apply (apply_name_equivalences.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_name_equivalences.main(limit=args.auto_limit, apply=args.auto_apply)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- name_apply: executed, apply={args.auto_apply}")

        if args.mode == "title-name-rules":
            import apply_title_name_rules

            print("[main] Running stage: title_name_rules (apply_title_name_rules.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_title_name_rules.main(limit=args.auto_limit, apply=args.auto_apply)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- title_name_rules: executed, apply={args.auto_apply}")

        if args.mode == "title-queue":
            import build_title_review_queue

            print("[main] Running stage: title_queue (build_title_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    build_title_review_queue.main(limit=args.auto_limit)
            finally:
                captured = buffer.getvalue().splitlines()
                log_lines.extend(captured)
            report_lines.append("- title_queue: executed")

        if args.mode == "title-apply":
            import apply_title_review_queue

            print("[main] Running stage: title_apply (apply_title_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_title_review_queue.main(limit=args.auto_limit, apply=args.auto_apply)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- title_apply: executed, apply={args.auto_apply}")

        if args.skip_apply and args.mode in {"apply", "full"}:
            print("[main] Apply skipped by --skip-apply")
            report_lines.append("- apply: skipped by --skip-apply")
        elif args.mode in {"apply", "full"}:
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
                        only_locked_human=False,
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
