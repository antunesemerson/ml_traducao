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
    "ml_baseline": "ml_baseline_report",
    "ml_dataset": "ml_build_dataset",
    "ml_train_risk": "ml_train_risk",
    "ml_promote_model": "ml_promote_model",
    "ml_export_model": "ml_export_model",
    "ml_coverage_scan": "ml_coverage_scan",
    "ml_group_candidate_queue": "ml_group_candidate_queue",
    "ml_group_threshold_policy": "ml_group_threshold_policy",
    "ml_policy_audit_queue": "ml_policy_audit_queue",
    "ml_contrast_review_queue": "ml_contrast_review_queue",
    "ml_holdout_eval": "ml_holdout_eval",
    "ml_holdout_review_queue": "ml_holdout_review_queue",
    "ml_progress": "ml_progress_report",
    "ml_threshold_sweep": "ml_threshold_sweep",
    "ml_score": "ml_score_segments",
    "ml_score_audit": "ml_score_audit",
    "ml_score_regression_queue": "ml_score_regression_queue",
    "ml_specialist_policy": "ml_specialist_policy",
    "ml_specialist_score": "ml_specialist_score",
    "ml_specialist_ensemble_policy": "ml_specialist_ensemble_policy",
    "ml_agent_architecture": "ml_agent_architecture",
    "ml_agent_audit_queue": "ml_agent_audit_queue",
    "learning_network_diagnostic": "learning_network_diagnostic",
    "ml_composite_review_progress": "ml_composite_review_progress",
    "issue_review_ingest": "issue_review_ingest",
    "issue_review_gender_boundary_checkpoint": "issue_review_gender_boundary_checkpoint",
    "issue_review_gender_boundary_lifecycle": "issue_review_gender_boundary_lifecycle_policy",
    "issue_review_trigger_gender_role_surface": "issue_review_trigger_gender_role_surface",
    "issue_review_trigger_gender_role_checkpoint": "issue_review_trigger_gender_role_checkpoint",
    "issue_review_trigger_gender_role_lifecycle": "issue_review_trigger_gender_role_lifecycle_policy",
    "issue_partial_coverage": "issue_partial_coverage_report",
    "issue_title_policy_routes": "issue_title_policy_route_diagnostic",
    "issue_semantic_short_label_pair_checkpoint": "issue_semantic_short_label_pair_checkpoint",
    "issue_composition_queue": "issue_composition_queue",
    "issue_multiagent_composition_queue": "issue_multiagent_composition_queue",
    "ml_composite_review_ingest": "ml_composite_review_ingest",
    "ml_composite_queue_backfill": "ml_composite_queue_backfill",
    "ml_composite_next_queue_cycle": "ml_composite_next_queue_cycle",
    "ml_composite_subpolicy_diagnostic": "ml_composite_subpolicy_diagnostic",
    "ml_composite_subpolicy_evidence_queue": "ml_composite_subpolicy_evidence_queue",
    "ml_composite_subpolicy_promotion_audit": "ml_composite_subpolicy_promotion_audit",
    "ml_composite_subpolicy_promotion_queue": "ml_composite_subpolicy_promotion_queue",
    "ml_composite_subpolicy_guarded_overlay": "ml_composite_subpolicy_guarded_overlay",
    "ml_composite_guarded_overlay_checkpoint": "ml_composite_guarded_overlay_checkpoint",
    "ml_composite_guarded_overlay_shadow_queue": "ml_composite_guarded_overlay_shadow_queue",
    "ml_composite_guarded_overlay_shadow_decisions": "ml_composite_guarded_overlay_shadow_decisions",
    "auto_confirmation_text_shadow_policy": "auto_confirmation_reopen_text_shadow_policy",
    "auto_confirmation_text_policy_checkpoint": "auto_confirmation_reopen_text_policy_checkpoint",
    "auto_confirmation_text_lifecycle_policy": "auto_confirmation_reopen_text_lifecycle_policy",
    "auto_confirmation_text_boundary_policy": "auto_confirmation_reopen_text_boundary_policy",
    "auto_confirmation_text_boundary_repair_queue": "auto_confirmation_reopen_text_boundary_repair_queue",
    "auto_confirmation_text_boundary_repair_shadow_policy": "auto_confirmation_reopen_text_boundary_repair_shadow_policy",
    "auto_confirmation_text_boundary_repair_checkpoint": "auto_confirmation_reopen_text_boundary_repair_checkpoint",
    "auto_confirmation_text_boundary_repair_lifecycle_policy": "auto_confirmation_reopen_text_boundary_repair_lifecycle_policy",
    "auto_confirmation_text_boundary_repair_production_audit": "auto_confirmation_reopen_text_boundary_repair_production_audit",
    "auto_confirmation_text_boundary_token_policy_bridge": "auto_confirmation_reopen_text_boundary_token_policy_bridge",
    "auto_confirmation_text_boundary_token_subpolicy_shadow": "auto_confirmation_reopen_text_boundary_token_subpolicy_shadow",
    "auto_confirmation_text_boundary_token_subpolicy_checkpoint": "auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint",
    "auto_confirmation_text_boundary_token_subpolicy_lifecycle_policy": "auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy",
    "auto_confirmation_text_boundary_token_subpolicy_production_audit": "auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit",
    "controlled_token_subpolicy_apply": "controlled_token_subpolicy_apply",
    "auto_confirmation_weak_boundary_queue": "auto_confirmation_reopen_weak_boundary_queue",
    "segment_state": "segment_state_snapshot",
    "segment_apply": "apply_segment_state_updates",
    "segment_token_queue": "segment_token_mismatch_queue",
    "segment_token_policy": "segment_token_policy",
    "segment_token_policy_decisions": "segment_token_policy_decisions",
    "segment_token_policy_decision_rebase": "segment_token_policy_decision_rebase",
    "learn_local": "local_learning_cycle",
    "learn_feedback": "apply_local_learning_feedback",
    "confirmations": "segment_confirmation_report",
    "auto_validate": "auto_validate_segments",
    "learned_report": "learned_validation_report",
    "learned_apply": "apply_learned_validation",
    "learned_autofix": "apply_learned_autofix",
    "mojibake_audit": "audit_mojibake_confirmations",
    "gender_token_audit": "audit_gender_tokens",
    "micro_review_queue": "build_micro_review_queue",
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
    previous_argv = sys.argv[:]
    try:
        sys.argv = [f"{module_name}.py"]
        module.main()
    finally:
        sys.argv = previous_argv


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
            "ml-baseline",
            "ml-dataset",
            "ml-train-risk",
            "ml-promote-model",
            "ml-export-model",
            "ml-coverage-scan",
            "ml-group-candidate-queue",
            "ml-group-threshold-policy",
            "ml-policy-audit-queue",
            "ml-contrast-review-queue",
            "ml-holdout-eval",
            "ml-holdout-review-queue",
            "ml-progress",
            "ml-threshold-sweep",
            "ml-specialist-models",
            "ml-specialist-auditor",
            "ml-specialist-policy",
            "ml-specialist-score",
            "ml-specialist-ensemble-policy",
            "ml-specialist-frontier-queue",
            "ml-agent-architecture",
            "ml-agent-audit-queue",
            "learning-network-diagnostic",
            "issue-ledger",
            "issue-review-queue",
            "issue-review-subcluster",
            "issue-review-ingest",
            "issue-review-short-label-positive-release",
            "issue-review-short-label-positive-checkpoint",
            "issue-review-gender-subpolicy-shadow",
            "issue-review-gender-boundary-checkpoint",
            "issue-review-gender-boundary-lifecycle",
            "issue-review-trigger-gender-role-surface",
            "issue-review-trigger-gender-role-checkpoint",
            "issue-review-trigger-gender-role-lifecycle",
            "issue-partial-coverage",
            "issue-title-policy-route-queue",
            "issue-composition-queue",
            "issue-multiagent-composition-queue",
            "ml-composite-checkpoint",
            "ml-composite-promote",
            "ml-composite-review-progress",
            "ml-composite-review-ingest",
            "ml-composite-queue-backfill",
            "ml-composite-next-queue-cycle",
            "ml-composite-subpolicy-diagnostic",
            "ml-composite-subpolicy-evidence-queue",
            "ml-composite-subpolicy-promotion-audit",
            "ml-composite-subpolicy-promotion-queue",
            "ml-composite-subpolicy-guarded-overlay",
            "ml-composite-guarded-overlay-checkpoint",
            "ml-composite-guarded-overlay-promote",
            "ml-composite-guarded-overlay-shadow-queue",
            "ml-composite-guarded-overlay-shadow-decisions",
            "auto-confirmation-text-shadow-policy",
            "auto-confirmation-text-policy-checkpoint",
            "auto-confirmation-text-lifecycle-policy",
            "auto-confirmation-text-boundary-policy",
            "auto-confirmation-text-boundary-repair-queue",
            "auto-confirmation-text-boundary-repair-shadow-policy",
            "auto-confirmation-text-boundary-repair-checkpoint",
            "auto-confirmation-text-boundary-repair-lifecycle-policy",
            "auto-confirmation-text-boundary-repair-production-audit",
            "auto-confirmation-text-boundary-token-policy-bridge",
            "auto-confirmation-text-boundary-token-subpolicy-shadow",
            "auto-confirmation-text-boundary-token-subpolicy-checkpoint",
            "auto-confirmation-text-boundary-token-subpolicy-lifecycle-policy",
            "auto-confirmation-text-boundary-token-subpolicy-production-audit",
            "controlled-token-subpolicy-apply",
            "same-token-boundary-repair-apply",
            "select-cstring-governed-bridge-apply",
            "auto-confirmation-weak-boundary-queue",
            "segment-state",
            "segment-apply",
            "segment-token-queue",
            "segment-token-policy",
            "segment-token-tutorial-concept-policy",
            "segment-token-tutorial-concept-promotion",
            "segment-token-tutorial-concept-candidate-policy",
            "segment-token-gender-subpolicy",
            "segment-token-gender-split-subpolicy",
            "segment-token-gender-split-evidence-queue",
            "segment-token-gender-simple-evidence-queue",
            "segment-token-gender-split-evidence-ingest",
            "segment-token-gender-split-promotion-audit",
            "segment-token-gender-split-guarded-policy",
            "segment-token-gender-split-coordinator-dry-run",
            "segment-token-gender-split-bridge-dry-run",
            "segment-token-policy-overlay",
            "segment-token-overlay-queue",
            "segment-token-overlay-text-decisions",
            "segment-token-overlay-structural-decisions",
            "segment-token-policy-queue",
            "segment-token-policy-decisions",
            "segment-token-policy-decision-rebase",
            "segment-token-composite-bridge",
            "segment-token-confirmation-fixes",
            "ml-score",
            "ml-score-audit",
            "ml-score-regression-queue",
            "auto-validate",
            "learned-report",
            "learned-apply",
            "learned-autofix",
            "mojibake-audit",
            "mojibake-strict",
            "mojibake-curated-decisions",
            "gender-token-audit",
            "micro-review-queue",
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
        "--segment-review-states",
        default=None,
        help="During segment-apply, comma-separated review states. Default: human_locked,human_confirmed.",
    )
    parser.add_argument(
        "--segment-include-auto-confirmed",
        action="store_true",
        help="During segment-apply, also inspect auto_confirmed pending apply rows.",
    )
    parser.add_argument(
        "--segment-include-intentional-blank",
        action="store_true",
        help="During segment-apply, allow trusted locked human confirmations whose confirmed text is intentionally blank.",
    )
    parser.add_argument(
        "--segment-allow-locked-token-override",
        action="store_true",
        help="During segment-apply, allow human_locked rows to override token mismatch validation.",
    )
    parser.add_argument(
        "--segment-require-token-policy-decision",
        action="store_true",
        help="During segment-apply, only apply token mismatches with an approved segment token policy decision.",
    )
    parser.add_argument(
        "--segment-allow-token-policy-decision",
        action="store_true",
        help="During segment-apply, allow approved token policy decisions only for rows that need them.",
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
        "--segment-state-report",
        action="store_true",
        help="During segment-state, generate the optional report after committing the authoritative database snapshot.",
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
        "--issue-agent-key",
        default="micro_short_label_style",
        help="During issue-review-queue, select ledger items for this microagent.",
    )
    parser.add_argument(
        "--issue-family",
        default=None,
        help="During issue-review-queue, optional issue_family filter.",
    )
    parser.add_argument(
        "--issue-kind",
        default=None,
        help="During issue-review-queue, optional issue_kind filter.",
    )
    parser.add_argument(
        "--issue-active-action",
        default=None,
        help="During issue-review-queue, optional active_action filter.",
    )
    parser.add_argument(
        "--issue-candidate-action",
        default=None,
        help="During issue-review-queue, optional candidate_action filter.",
    )
    parser.add_argument(
        "--issue-policy-action",
        default=None,
        help="During issue-review-queue, optional policy_action filter.",
    )
    parser.add_argument(
        "--issue-path-like",
        default=None,
        help="During issue-review-queue, optional SQL LIKE filter for relative_path.",
    )
    parser.add_argument(
        "--issue-queue-per-bucket",
        type=int,
        default=20,
        help="During issue-review-queue, maximum rows selected per stratification bucket.",
    )
    parser.add_argument(
        "--issue-queue-include-existing",
        action="store_true",
        help="During issue-review-queue, allow ledger items already emitted in previous issue queues.",
    )
    parser.add_argument(
        "--issue-review-decisions",
        default=None,
        help="During issue-review-ingest/subcluster, JSON/JSONL/CSV file with reviewed issue queue decisions.",
    )
    parser.add_argument(
        "--issue-review-queue-jsonl",
        default=None,
        help="During issue-review-subcluster, source issue review queue JSONL file.",
    )
    parser.add_argument(
        "--issue-review-queue-run-id",
        type=int,
        default=None,
        help="During issue-review-ingest, queue run id used to resolve ledger_item_id rows. Default is read from rows.",
    )
    parser.add_argument(
        "--issue-review-decision-run-id",
        type=int,
        default=None,
        help="During issue-review-short-label-positive-release, inspect a specific issue review decision run. Default is latest for the agent.",
    )
    parser.add_argument(
        "--issue-short-label-release-run-id",
        type=int,
        default=None,
        help="During issue-review-short-label-positive-checkpoint, inspect a specific short-label release run. Default is latest.",
    )
    parser.add_argument(
        "--issue-short-label-checkpoint-min-released",
        type=int,
        default=1,
        help="During issue-review-short-label-positive-checkpoint, minimum shadow released rows required.",
    )
    parser.add_argument(
        "--issue-short-label-checkpoint-max-blocked",
        type=int,
        default=0,
        help="During issue-review-short-label-positive-checkpoint, maximum release blocked rows allowed.",
    )
    parser.add_argument(
        "--issue-gender-shadow-run-id",
        type=int,
        default=None,
        help="During issue-review-gender-boundary-checkpoint, inspect a specific gender shadow run. Default is latest.",
    )
    parser.add_argument(
        "--issue-gender-boundary-min",
        type=int,
        default=1,
        help="During issue-review-gender-boundary-checkpoint, minimum boundary rows required.",
    )
    parser.add_argument(
        "--issue-gender-boundary-max-blocked",
        type=int,
        default=0,
        help="During issue-review-gender-boundary-checkpoint, maximum shadow blocked rows allowed.",
    )
    parser.add_argument(
        "--issue-gender-boundary-checkpoint-run-id",
        type=int,
        default=None,
        help="During issue-review-gender-boundary-lifecycle, inspect a specific gender boundary checkpoint. Default is latest.",
    )
    parser.add_argument(
        "--issue-gender-boundary-lifecycle-status",
        choices=["shadow"],
        default="shadow",
        help="During issue-review-gender-boundary-lifecycle, lifecycle status. This is intentionally shadow-only.",
    )
    parser.add_argument(
        "--issue-gender-boundary-lifecycle-expected-total",
        type=int,
        default=None,
        help="During issue-review-gender-boundary-lifecycle, expected lifecycle item count.",
    )
    parser.add_argument(
        "--issue-trigger-gender-role-shadow-run-id",
        type=int,
        default=None,
        help="During issue-review-trigger-gender-role-checkpoint, inspect a specific trigger gender-role shadow run. Default is latest.",
    )
    parser.add_argument(
        "--issue-trigger-gender-role-checkpoint-min-ready",
        type=int,
        default=1,
        help="During issue-review-trigger-gender-role-checkpoint, minimum ready rows required.",
    )
    parser.add_argument(
        "--issue-trigger-gender-role-checkpoint-max-blocked",
        type=int,
        default=0,
        help="During issue-review-trigger-gender-role-checkpoint, maximum shadow blocked rows allowed.",
    )
    parser.add_argument(
        "--issue-trigger-gender-role-lifecycle-checkpoint-run-id",
        type=int,
        default=None,
        help="During issue-review-trigger-gender-role-lifecycle, inspect a specific trigger gender-role checkpoint. Default is latest.",
    )
    parser.add_argument(
        "--issue-trigger-gender-role-lifecycle-status",
        choices=["shadow"],
        default="shadow",
        help="During issue-review-trigger-gender-role-lifecycle, lifecycle status. This is intentionally shadow-only.",
    )
    parser.add_argument(
        "--issue-trigger-gender-role-lifecycle-expected-total",
        type=int,
        default=None,
        help="During issue-review-trigger-gender-role-lifecycle, expected lifecycle item count.",
    )
    parser.add_argument(
        "--issue-partial-coverage-ledger-run-id",
        type=int,
        default=None,
        help="During issue-partial-coverage, inspect a specific issue ledger run. Default is latest.",
    )
    parser.add_argument(
        "--issue-ledger-run-id",
        type=int,
        default=None,
        help="During issue-ledger based diagnostics/queues, inspect a specific issue ledger run. Default is latest.",
    )
    parser.add_argument(
        "--issue-title-route-lane",
        default="landed_title_adjectives",
        help="During issue-title-policy-route-queue, select the title-policy route lane.",
    )
    parser.add_argument(
        "--issue-title-route-per-prefix",
        type=int,
        default=60,
        help="During issue-title-policy-route-queue, maximum selected rows per title key prefix.",
    )
    parser.add_argument(
        "--issue-composition-partial-run-id",
        type=int,
        default=None,
        help="During issue-composition-queue, inspect a specific partial coverage run. Default is latest.",
    )
    parser.add_argument(
        "--issue-composition-scope",
        choices=["partial", "full", "partial_or_reviewed", "not_uncovered"],
        default="partial",
        help="During issue-composition-queue, choose which partial coverage segment state to queue from.",
    )
    parser.add_argument(
        "--issue-composition-min-issues",
        type=int,
        default=2,
        help="During issue-multiagent-composition-queue, minimum open issue count per segment.",
    )
    parser.add_argument(
        "--issue-composition-min-mature-families",
        type=int,
        default=2,
        help="During issue-multiagent-composition-queue, minimum families with checkpoint evidence.",
    )
    parser.add_argument(
        "--issue-composition-min-mature-ratio",
        type=float,
        default=0.75,
        help="During issue-multiagent-composition-queue, minimum ratio of open families with checkpoint evidence.",
    )
    parser.add_argument(
        "--issue-composition-agent-key",
        default="composition_coordinator_v1",
        help="During issue-multiagent-composition-queue, coordinator agent key recorded in the queue.",
    )
    parser.add_argument(
        "--issue-review-source-report",
        default=None,
        help="During issue-review-ingest, optional source queue/report path for audit traceability.",
    )
    parser.add_argument(
        "--issue-reviewer",
        default=None,
        help="During issue-review-ingest, reviewer name stored when rows do not specify one.",
    )
    parser.add_argument(
        "--segment-ids",
        default=None,
        help="During segment-apply, comma-separated segment ids to apply exactly.",
    )
    parser.add_argument(
        "--token-policy-run-id",
        type=int,
        default=None,
        help="During segment-token-policy-queue, inspect a specific segment_token_policy_runs id. Default is latest.",
    )
    parser.add_argument(
        "--token-overlay-run-id",
        type=int,
        default=None,
        help="During segment-token-overlay-* modes, inspect a specific segment_token_policy_overlay_runs id. Default is latest.",
    )
    parser.add_argument(
        "--use-active-composite-gate",
        action="store_true",
        help="During segment-token-overlay-* modes, use the promoted active composite gate instead of the latest experimental overlay.",
    )
    parser.add_argument(
        "--token-overlay-all",
        action="store_true",
        help="During segment-token-overlay-queue, include all overlay rows instead of critical rows only.",
    )
    parser.add_argument(
        "--token-overlay-route",
        default=None,
        help="During segment-token-overlay-queue, comma-separated suggested_route filter.",
    )
    parser.add_argument(
        "--token-overlay-risk",
        default=None,
        help="During segment-token-overlay-queue, comma-separated overlay risk filter.",
    )
    parser.add_argument(
        "--token-overlay-limit",
        type=int,
        default=None,
        help="During segment-token-overlay-queue, cap rows after filters.",
    )
    parser.add_argument(
        "--token-overlay-skip-reviewed",
        action="store_true",
        help="During segment-token-overlay-queue, exclude items already reviewed in token policy decisions.",
    )
    parser.add_argument(
        "--token-overlay-skip-queued",
        action="store_true",
        help="During segment-token-overlay-queue, exclude items already emitted in active gate queues.",
    )
    parser.add_argument(
        "--token-policy-per-bucket",
        type=int,
        default=25,
        help="During segment-token-policy-queue, maximum rows selected per policy bucket.",
    )
    parser.add_argument(
        "--token-policy-buckets",
        default=None,
        help="During segment-token-policy-queue, comma-separated policy_bucket filter.",
    )
    parser.add_argument(
        "--token-policy-risks",
        default=None,
        help="During segment-token-policy-queue, comma-separated risk_level filter.",
    )
    parser.add_argument(
        "--token-policy-pending-apply-only",
        action="store_true",
        help="During segment-token-policy-queue, include only policy items still marked needs_apply in segment state.",
    )
    parser.add_argument(
        "--token-policy-skip-apply-approved",
        action="store_true",
        help="During segment-token-policy-queue, exclude policy items already approved for apply in current token policy decisions.",
    )
    parser.add_argument(
        "--token-policy-undecided-only",
        action="store_true",
        help="During gender-token learning modes, include only policy items without any token policy decision.",
    )
    parser.add_argument(
        "--token-policy-decisions",
        default=None,
        help="During segment-token-policy-decisions, JSON/JSONL/CSV file with reviewed token policy decisions.",
    )
    parser.add_argument(
        "--token-policy-source-report",
        default=None,
        help="During segment-token-policy-decisions, optional source queue/report path for audit traceability.",
    )
    parser.add_argument(
        "--token-policy-reviewer",
        default=None,
        help="During segment-token-policy-decisions, reviewer name stored on decisions when rows do not specify one.",
    )
    parser.add_argument(
        "--token-subpolicy-statuses",
        default=None,
        help="During segment-token-gender-subpolicy, comma-separated subpolicy_status filter.",
    )
    parser.add_argument(
        "--token-gender-subtypes",
        default=None,
        help="During segment-token-gender-subpolicy, comma-separated gender_subtype filter.",
    )
    parser.add_argument(
        "--token-split-agents",
        default=None,
        help="During segment-token-gender-split-* modes, comma-separated split_agent filter.",
    )
    parser.add_argument(
        "--token-split-maturity",
        default=None,
        help="During segment-token-gender-split-* modes, comma-separated split_maturity filter.",
    )
    parser.add_argument(
        "--token-split-evidence-decisions",
        default=None,
        help="During segment-token-gender-split-evidence-ingest, JSON/JSONL/CSV with evidence labels.",
    )
    parser.add_argument(
        "--token-split-evidence-source-queue",
        default=None,
        help="During segment-token-gender-split-evidence-ingest, source evidence queue JSONL.",
    )
    parser.add_argument(
        "--token-split-min-positive",
        type=int,
        default=3,
        help="During segment-token-gender-split-promotion-audit, positives required for guarded review.",
    )
    parser.add_argument(
        "--token-split-audit-run-id",
        type=int,
        default=None,
        help="During segment-token-gender-split-guarded-policy, inspect a specific gender split promotion audit run.",
    )
    parser.add_argument(
        "--token-split-guarded-run-id",
        type=int,
        default=None,
        help="During segment-token-gender-split-coordinator-dry-run, inspect a specific guarded policy run.",
    )
    parser.add_argument(
        "--composite-review-decisions",
        action="append",
        default=None,
        help="During ml-composite-review-ingest, reviewed JSONL path. Can be used more than once. Defaults to discovered reports/*_reviewed.jsonl.",
    )
    parser.add_argument(
        "--composite-review-reviewer",
        default="composite_gate_review",
        help="During ml-composite-review-ingest, reviewer name stored when applying validated decisions.",
    )
    parser.add_argument(
        "--composite-queue-batch-size",
        type=int,
        default=25,
        help="During ml-composite-next-queue-cycle, rows generated per selected route.",
    )
    parser.add_argument(
        "--composite-queue-max-routes",
        type=int,
        default=4,
        help="During ml-composite-next-queue-cycle, maximum routes selected for the cycle.",
    )
    parser.add_argument(
        "--composite-queue-routes",
        default=None,
        help="During ml-composite-next-queue-cycle, comma-separated suggested_route filter.",
    )
    parser.add_argument(
        "--composite-queue-plan-only",
        action="store_true",
        help="During ml-composite-next-queue-cycle, write only the plan report without generating queue files.",
    )
    parser.add_argument(
        "--composite-subpolicy-min-evidence",
        type=int,
        default=10,
        help="During ml-composite-subpolicy-diagnostic, reviewed rows needed to mark a subtype as ready for subpolicy design.",
    )
    parser.add_argument(
        "--composite-subpolicy-min-positive",
        type=int,
        default=5,
        help="During ml-composite-subpolicy-diagnostic, positive decisions needed for guarded policy candidate review.",
    )
    parser.add_argument(
        "--composite-subpolicy-run-id",
        type=int,
        default=None,
        help="During ml-composite-subpolicy-evidence-queue, diagnostic run id. Default is latest.",
    )
    parser.add_argument(
        "--composite-subpolicy-statuses",
        default="ready_to_design_subpolicy",
        help="During ml-composite-subpolicy-evidence-queue, comma-separated maturity statuses to queue.",
    )
    parser.add_argument(
        "--composite-subpolicy-routes",
        default=None,
        help="During ml-composite-subpolicy-evidence-queue, comma-separated route filter.",
    )
    parser.add_argument(
        "--composite-subpolicy-subtypes",
        default=None,
        help="During ml-composite-subpolicy-evidence-queue, comma-separated token_subtype filter.",
    )
    parser.add_argument(
        "--composite-subpolicy-max-groups",
        type=int,
        default=4,
        help="During ml-composite-subpolicy-evidence-queue, maximum diagnostic groups to queue.",
    )
    parser.add_argument(
        "--composite-subpolicy-limit-per-group",
        type=int,
        default=12,
        help="During ml-composite-subpolicy-evidence-queue, maximum rows selected per diagnostic group.",
    )
    parser.add_argument(
        "--composite-subpolicy-include-reviewed",
        action="store_true",
        help="During ml-composite-subpolicy-evidence-queue, allow already reviewed policy items to be requeued.",
    )
    parser.add_argument(
        "--composite-subpolicy-include-queued",
        action="store_true",
        help="During ml-composite-subpolicy-evidence-queue, allow already queued policy items to be requeued.",
    )
    parser.add_argument(
        "--composite-subpolicy-plan-only",
        action="store_true",
        help="During ml-composite-subpolicy-evidence-queue, write files without recording queue metadata.",
    )
    parser.add_argument(
        "--composite-subpolicy-promotion-statuses",
        default="policy_candidate_review,conflicting_evidence",
        help="During ml-composite-subpolicy-promotion-audit, comma-separated maturity statuses to audit.",
    )
    parser.add_argument(
        "--composite-promotion-audit-run-id",
        type=int,
        default=None,
        help="During ml-composite-subpolicy-promotion-queue, promotion audit run id. Default is latest.",
    )
    parser.add_argument(
        "--composite-promotion-queue-statuses",
        default="needs_more_positive_evidence,split_required_conflicting_evidence",
        help="During ml-composite-subpolicy-promotion-queue, comma-separated promotion_status values to queue.",
    )
    parser.add_argument(
        "--composite-promotion-rule-keys",
        default=None,
        help="During ml-composite-subpolicy-promotion-queue, comma-separated narrow rule_key filter.",
    )
    parser.add_argument(
        "--composite-promotion-skip-queued",
        action="store_true",
        help="During ml-composite-subpolicy-promotion-queue, skip items already present in an active gate queue.",
    )
    parser.add_argument(
        "--composite-guarded-overlay-statuses",
        default="ready_for_guarded_policy_review",
        help="During ml-composite-subpolicy-guarded-overlay, comma-separated promotion_status values to enable.",
    )
    parser.add_argument(
        "--composite-guarded-overlay-run-id",
        type=int,
        default=None,
        help="During ml-composite-guarded-overlay-checkpoint, inspect a specific guarded overlay run. Default is latest.",
    )
    parser.add_argument(
        "--composite-guarded-min-releases",
        type=int,
        default=50,
        help="During ml-composite-guarded-overlay-checkpoint, target release sample before shadow validation.",
    )
    parser.add_argument(
        "--composite-guarded-min-rules",
        type=int,
        default=3,
        help="During ml-composite-guarded-overlay-checkpoint, minimum ready rules required for the checkpoint.",
    )
    parser.add_argument(
        "--composite-guarded-checkpoint-id",
        type=int,
        default=None,
        help="During guarded overlay shadow queue/promote, inspect a specific guarded checkpoint. Default is latest ready checkpoint.",
    )
    parser.add_argument(
        "--composite-shadow-priority",
        choices=["all", "hygiene", "clean"],
        default="all",
        help="During ml-composite-guarded-overlay-shadow-queue, restrict rows by shadow priority.",
    )
    parser.add_argument(
        "--composite-shadow-limit",
        type=int,
        default=None,
        help="During ml-composite-guarded-overlay-shadow-queue, cap selected rows after priority ordering.",
    )
    parser.add_argument(
        "--composite-shadow-release-scope",
        choices=["new", "inherited", "all"],
        default="new",
        help="During ml-composite-guarded-overlay-shadow-queue, choose new, inherited, or all guarded releases.",
    )
    parser.add_argument(
        "--composite-shadow-plan-only",
        action="store_true",
        help="During ml-composite-guarded-overlay-shadow-queue, write files without recording queue metadata.",
    )
    parser.add_argument(
        "--composite-shadow-skip-reviewed",
        action="store_true",
        help="During ml-composite-guarded-overlay-shadow-queue, exclude policy items that already have token policy decisions.",
    )
    parser.add_argument(
        "--composite-shadow-queue-run-id",
        type=int,
        default=None,
        help="During ml-composite-guarded-overlay-shadow-decisions, draft decisions for a specific shadow queue run.",
    )
    parser.add_argument(
        "--text-diagnostic-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-shadow-policy, inspect a specific text diagnostic run. Default is latest.",
    )
    parser.add_argument(
        "--text-shadow-policy",
        choices=[
            "hold_court_aptitude_tooltip",
            "short_tooltip_artifact_honor_reason",
            "short_ui_relation_score",
            "weak_auto_static_token_only",
        ],
        default="hold_court_aptitude_tooltip",
        help="During auto-confirmation-text-shadow-policy, choose the shadow-only text subpolicy.",
    )
    parser.add_argument(
        "--text-shadow-policy-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-policy-checkpoint, checkpoint a specific text shadow policy run. Default is latest eligible run.",
    )
    parser.add_argument(
        "--text-checkpoint-policy",
        choices=["weak_auto_static_token_only"],
        default="weak_auto_static_token_only",
        help="During auto-confirmation-text-policy-checkpoint, choose the governed checkpoint policy.",
    )
    parser.add_argument(
        "--text-checkpoint-min-ready",
        type=int,
        default=100,
        help="During auto-confirmation-text-policy-checkpoint, require at least this many shadow-ready rows.",
    )
    parser.add_argument(
        "--text-checkpoint-max-blocked",
        type=int,
        default=0,
        help="During auto-confirmation-text-policy-checkpoint, allow at most this many shadow-blocked rows.",
    )
    parser.add_argument(
        "--text-checkpoint-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-lifecycle-policy, release a specific guarded checkpoint. Default is latest eligible checkpoint.",
    )
    parser.add_argument(
        "--text-lifecycle-status",
        choices=["shadow", "active"],
        default="active",
        help="During auto-confirmation-text-lifecycle-policy, create a shadow or active lifecycle release.",
    )
    parser.add_argument(
        "--text-boundary-policy",
        choices=[
            "all",
            "unclassified_negative_boundary",
            "weak_auto_custom_loc_es_helper",
            "weak_auto_embedded_glossary_visible_label",
            "weak_auto_embedded_select_cstring_spanish_literal",
            "weak_auto_visible_copula_token_form",
            "weak_auto_visible_possessive_connector_loss",
            "weak_auto_visible_runtime_spanish_verb",
            "weak_auto_visible_semantic_sentence_loss",
            "weak_auto_visible_sentence_collapse",
        ],
        default="all",
        help="During auto-confirmation-text-boundary-policy, choose the weak-auto negative boundary to classify.",
    )
    parser.add_argument(
        "--text-boundary-policy-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-queue, inspect a specific boundary policy run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-repair-scope",
        choices=["all", "same-token", "token-change"],
        default="all",
        help="During auto-confirmation-text-boundary-repair-queue, choose same-token, token-change, or all repair candidates.",
    )
    parser.add_argument(
        "--text-boundary-repair-limit",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-queue, cap selected rows.",
    )
    parser.add_argument(
        "--text-boundary-repair-include-existing",
        action="store_true",
        help="During auto-confirmation-text-boundary-repair-queue, include boundary items already queued before.",
    )
    parser.add_argument(
        "--text-boundary-repair-queue-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-shadow-policy, inspect a specific same-token repair queue run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-repair-shadow-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-checkpoint, inspect a specific repair shadow run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-repair-min-ready",
        type=int,
        default=1,
        help="During auto-confirmation-text-boundary-repair-checkpoint, minimum shadow-ready repairs required.",
    )
    parser.add_argument(
        "--text-boundary-repair-max-blocked",
        type=int,
        default=2,
        help="During auto-confirmation-text-boundary-repair-checkpoint, maximum blocked rows allowed in the shadow run.",
    )
    parser.add_argument(
        "--text-boundary-repair-checkpoint-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-lifecycle-policy, inspect a specific repair checkpoint. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-repair-lifecycle-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-production-audit, inspect a specific lifecycle run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-repair-state-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-repair-production-audit, compare against a specific segment-state run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-token-bridge-limit",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-token-policy-bridge, cap selected token-change repair rows.",
    )
    parser.add_argument(
        "--text-boundary-token-bridge-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-token-subpolicy-shadow, inspect a specific bridge run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy",
        default="select_cstring_invariant_ptbr_verb",
        help="During auto-confirmation-text-boundary-token-subpolicy-shadow, choose the shadow token subpolicy.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-token-subpolicy-checkpoint, inspect a specific subpolicy shadow run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-min-ready",
        type=int,
        default=1,
        help="During auto-confirmation-text-boundary-token-subpolicy-checkpoint, minimum shadow-ready rows required.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-max-blocked",
        type=int,
        default=0,
        help="During auto-confirmation-text-boundary-token-subpolicy-checkpoint, maximum blocked rows allowed.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-checkpoint-run-ids",
        default=None,
        help="During auto-confirmation-text-boundary-token-subpolicy-lifecycle-policy, comma-separated checkpoint run IDs. Default is latest ready checkpoint per subpolicy.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-lifecycle-min-checkpoints",
        type=int,
        default=4,
        help="During auto-confirmation-text-boundary-token-subpolicy-lifecycle-policy, minimum checkpoint count required.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-lifecycle-expected-total",
        type=int,
        default=44,
        help="During auto-confirmation-text-boundary-token-subpolicy-lifecycle-policy, expected total lifecycle items.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-lifecycle-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-token-subpolicy-production-audit, inspect a specific lifecycle run. Default is latest.",
    )
    parser.add_argument(
        "--text-boundary-token-subpolicy-state-run-id",
        type=int,
        default=None,
        help="During auto-confirmation-text-boundary-token-subpolicy-production-audit, compare against a specific segment-state run. Default is latest.",
    )
    parser.add_argument(
        "--controlled-token-subpolicy-audit-run-id",
        type=int,
        default=None,
        help="During controlled-token-subpolicy-apply, consume a specific production audit run. Default is latest.",
    )
    parser.add_argument(
        "--controlled-token-subpolicy-reaudit",
        "--reaudit",
        action="store_true",
        help="During controlled-token-subpolicy-apply, report actual post-write closure without writing output.",
    )
    parser.add_argument(
        "--same-token-boundary-repair-audit-run-id",
        type=int,
        default=None,
        help="During same-token-boundary-repair-apply, consume a specific production audit run. Default is latest ready.",
    )
    parser.add_argument(
        "--same-token-boundary-repair-reaudit",
        action="store_true",
        help="During same-token-boundary-repair-apply, report actual post-write closure without writing output.",
    )
    parser.add_argument(
        "--select-cstring-bridge-proposal-run-id",
        type=int,
        default=None,
        help="During select-cstring-governed-bridge-apply, consume a specific governed bridge proposal run. Default is latest ready.",
    )
    parser.add_argument(
        "--select-cstring-bridge-reaudit",
        action="store_true",
        help="During select-cstring-governed-bridge-apply, report actual post-write closure without writing output.",
    )
    parser.add_argument(
        "--weak-boundary",
        choices=["custom_loc", "embedded_literal", "source_visible"],
        default="source_visible",
        help="During auto-confirmation-weak-boundary-queue, choose the weak-auto boundary to queue.",
    )
    parser.add_argument(
        "--weak-boundary-limit",
        type=int,
        default=0,
        help="During auto-confirmation-weak-boundary-queue, cap selected rows. Use 0 for all candidates.",
    )
    parser.add_argument(
        "--weak-boundary-include-existing",
        action="store_true",
        help="During auto-confirmation-weak-boundary-queue, include rows already queued/reviewed for the same boundary.",
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
    parser.add_argument(
        "--ml-include-locked",
        action="store_true",
        help="During ml-score, include locked human confirmations in the scoring sample.",
    )
    parser.add_argument(
        "--ml-candidate-text-source",
        choices=["effective", "old", "output"],
        default="effective",
        help="During ml-score, select the exact package text to score.",
    )
    parser.add_argument(
        "--ml-feature-set",
        choices=["legacy_v1", "structural_v2", "structural_v3", "language_v4"],
        default="legacy_v1",
        help="During ML train/holdout experiments, choose the feature extraction version.",
    )
    parser.add_argument(
        "--ml-train-strategy",
        choices=["balanced_v1", "dedup_weighted_v2"],
        default="balanced_v1",
        help="During ML train/holdout experiments, choose sampling and weighting strategy.",
    )
    parser.add_argument(
        "--ml-safe-multiplier",
        type=int,
        default=5,
        help="During ML train/holdout experiments, positive-to-risky sampling multiplier.",
    )
    parser.add_argument(
        "--ml-thresholds",
        default=None,
        help="During ml-threshold-sweep, comma-separated thresholds, for example 0.90,0.92,0.95.",
    )
    parser.add_argument(
        "--ml-split-mode",
        choices=["file", "stratified"],
        default="file",
        help="During ml-threshold-sweep, choose file holdout or stratified example holdout.",
    )
    parser.add_argument(
        "--ml-specialist",
        choices=[
            "all",
            "titles",
            "religion",
            "title_subspecialists",
            "religion_subspecialists",
            "title_promising_subspecialists",
            "religion_promising_subspecialists",
            "all_with_title_subspecialists",
            "all_with_religion_subspecialists",
            "operational_title_religion_v1",
            "title_names",
            "title_adjectives",
            "title_prefixes",
            "title_rank_subspecialists",
            "title_baronies",
            "title_counties",
            "title_duchies",
            "title_kingdoms",
            "title_empires",
            "title_cultural_names",
            "culture_title_labels",
            "religion_bosnian_terms",
            "religion_sufri",
            "religion_possessive_gods",
            "religion_preserved_terms",
            "religion_divine_realm_contextual_boundary",
            "religion_dab_qhuas_terms",
            "religion_preserved_subspecialists",
            "select_cstring_ep3_laamp_roles",
            "select_cstring_ep3_laamp_spanish_role_boundary",
            "select_cstring_ep3_laamp_role_correction_candidate",
            "select_cstring_ep3_laamp_subspecialists",
            "all_with_token_gate_subspecialists",
        ],
        default="all",
        help="During ml-specialist-models, choose which specialist to train.",
    )
    parser.add_argument(
        "--ml-specialist-dataset-only",
        action="store_true",
        help="During ml-specialist-models, build specialist dataset(s) without training models.",
    )
    parser.add_argument(
        "--ml-groups",
        default=None,
        help="During ml-coverage-scan, comma-separated name=SQLLIKE groups.",
    )
    parser.add_argument(
        "--policy-audit-focus",
        choices=["all", "new_safe", "demoted_safe"],
        default="all",
        help="During ml-policy-audit-queue, choose policy rows to queue.",
    )
    parser.add_argument(
        "--ml-score-batch-size",
        type=int,
        default=5000,
        help="During ml-score, process and commit this many segments per batch.",
    )
    parser.add_argument(
        "--ml-model-run-id",
        type=int,
        default=None,
        help="During compatible ML modes, evaluate a specific model run id instead of the active model.",
    )
    parser.add_argument(
        "--ml-resume-run-id",
        type=int,
        default=None,
        help="During ml-score, resume an exact unfinished score run after validating its contract.",
    )
    parser.add_argument(
        "--ml-active-score-run-id",
        type=int,
        default=None,
        help="During ml-score-regression-queue, compare against this active score run id.",
    )
    parser.add_argument(
        "--ml-candidate-score-run-id",
        type=int,
        default=None,
        help="During ml-score-regression-queue, review regressions from this candidate score run id.",
    )
    parser.add_argument(
        "--ml-per-path-limit",
        type=int,
        default=20,
        help="During ml-score-regression-queue, cap selected rows per localization file.",
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
            "ml-baseline",
            "ml-dataset",
            "ml-train-risk",
            "ml-promote-model",
            "ml-export-model",
            "ml-coverage-scan",
            "ml-group-candidate-queue",
            "ml-group-threshold-policy",
            "ml-policy-audit-queue",
            "ml-contrast-review-queue",
            "ml-holdout-eval",
            "ml-holdout-review-queue",
            "ml-progress",
            "ml-threshold-sweep",
            "ml-specialist-models",
            "ml-specialist-auditor",
            "ml-specialist-policy",
            "ml-specialist-score",
            "ml-specialist-ensemble-policy",
            "ml-specialist-frontier-queue",
            "ml-agent-architecture",
            "ml-agent-audit-queue",
            "learning-network-diagnostic",
            "issue-ledger",
            "issue-review-queue",
            "issue-review-subcluster",
            "issue-review-ingest",
            "issue-review-short-label-positive-release",
            "issue-review-short-label-positive-checkpoint",
            "issue-review-gender-subpolicy-shadow",
            "issue-review-gender-boundary-checkpoint",
            "issue-review-gender-boundary-lifecycle",
            "issue-review-trigger-gender-role-surface",
            "issue-review-trigger-gender-role-checkpoint",
            "issue-review-trigger-gender-role-lifecycle",
            "issue-partial-coverage",
            "issue-title-policy-route-queue",
            "issue-composition-queue",
            "ml-composite-checkpoint",
            "ml-composite-promote",
            "ml-composite-review-progress",
            "ml-composite-review-ingest",
            "ml-composite-queue-backfill",
            "ml-composite-next-queue-cycle",
            "ml-composite-subpolicy-diagnostic",
            "ml-composite-subpolicy-evidence-queue",
            "ml-composite-subpolicy-promotion-audit",
            "ml-composite-subpolicy-promotion-queue",
            "ml-composite-subpolicy-guarded-overlay",
            "ml-composite-guarded-overlay-checkpoint",
            "ml-composite-guarded-overlay-promote",
            "ml-composite-guarded-overlay-shadow-queue",
            "ml-composite-guarded-overlay-shadow-decisions",
            "auto-confirmation-text-shadow-policy",
            "auto-confirmation-text-policy-checkpoint",
            "auto-confirmation-text-lifecycle-policy",
            "auto-confirmation-text-boundary-policy",
            "auto-confirmation-text-boundary-repair-queue",
            "auto-confirmation-text-boundary-repair-shadow-policy",
            "auto-confirmation-text-boundary-repair-checkpoint",
            "auto-confirmation-text-boundary-repair-lifecycle-policy",
            "auto-confirmation-text-boundary-repair-production-audit",
            "auto-confirmation-text-boundary-token-policy-bridge",
            "auto-confirmation-text-boundary-token-subpolicy-shadow",
            "auto-confirmation-text-boundary-token-subpolicy-checkpoint",
            "auto-confirmation-text-boundary-token-subpolicy-lifecycle-policy",
            "auto-confirmation-text-boundary-token-subpolicy-production-audit",
            "controlled-token-subpolicy-apply",
            "same-token-boundary-repair-apply",
            "select-cstring-governed-bridge-apply",
            "auto-confirmation-weak-boundary-queue",
            "segment-state",
            "segment-apply",
            "segment-token-queue",
            "segment-token-policy",
            "segment-token-tutorial-concept-policy",
            "segment-token-tutorial-concept-promotion",
            "segment-token-tutorial-concept-candidate-policy",
            "segment-token-gender-subpolicy",
            "segment-token-gender-split-subpolicy",
            "segment-token-gender-split-evidence-queue",
            "segment-token-gender-simple-evidence-queue",
            "segment-token-gender-split-evidence-ingest",
            "segment-token-gender-split-promotion-audit",
            "segment-token-gender-split-guarded-policy",
            "segment-token-gender-split-coordinator-dry-run",
            "segment-token-gender-split-bridge-dry-run",
            "segment-token-policy-overlay",
            "segment-token-overlay-queue",
            "segment-token-overlay-text-decisions",
            "segment-token-overlay-structural-decisions",
            "segment-token-policy-queue",
            "segment-token-policy-decisions",
            "segment-token-policy-decision-rebase",
            "segment-token-composite-bridge",
            "segment-token-confirmation-fixes",
            "ml-score",
            "ml-score-audit",
            "ml-score-regression-queue",
            "learned-report",
            "learned-apply",
            "learned-autofix",
            "mojibake-audit",
            "mojibake-strict",
            "mojibake-curated-decisions",
            "gender-token-audit",
            "micro-review-queue",
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

        if args.mode == "ml-baseline":
            run_stage_with_log("ml_baseline", log_lines)
            report_lines.append("- ml_baseline: executed")

        if args.mode == "ml-dataset":
            import ml_build_dataset

            print("[main] Running stage: ml_dataset (ml_build_dataset.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_build_dataset.main(limit=args.auto_limit)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_dataset: executed")

        if args.mode == "ml-train-risk":
            import ml_train_risk

            print("[main] Running stage: ml_train_risk (ml_train_risk.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_train_risk.main(
                        safe_threshold=args.auto_min_score or 0.90,
                        safe_multiplier=args.ml_safe_multiplier,
                        max_examples=args.auto_limit,
                        feature_set=args.ml_feature_set,
                        train_strategy=args.ml_train_strategy,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_train_risk: executed")

        if args.mode == "ml-promote-model":
            import ml_promote_model

            print("[main] Running stage: ml_promote_model (ml_promote_model.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_promote_model.main(
                        model_run_id=args.ml_model_run_id,
                        apply=args.auto_apply,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- ml_promote_model: executed, apply={args.auto_apply}")

        if args.mode == "ml-export-model":
            import ml_export_model

            print("[main] Running stage: ml_export_model (ml_export_model.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_export_model.main()
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_export_model: executed")

        if args.mode == "ml-coverage-scan":
            import ml_coverage_scan

            print("[main] Running stage: ml_coverage_scan (ml_coverage_scan.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_coverage_scan.main(
                        limit_per_group=args.auto_limit or 1000,
                        threshold=args.auto_min_score,
                        include_locked=args.ml_include_locked,
                        groups_value=args.ml_groups,
                        model_run_id=args.ml_model_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_coverage_scan: executed")

        if args.mode == "ml-group-candidate-queue":
            import ml_group_candidate_queue

            print("[main] Running stage: ml_group_candidate_queue (ml_group_candidate_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_group_candidate_queue.main(
                        proposed_threshold=args.auto_min_score or 0.93,
                        limit_per_group=args.auto_limit or 2000,
                        groups_value=args.ml_groups,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_group_candidate_queue: executed")

        if args.mode == "ml-group-threshold-policy":
            import ml_group_threshold_policy

            print("[main] Running stage: ml_group_threshold_policy (ml_group_threshold_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_group_threshold_policy.main(
                        score_run_id=args.ml_active_score_run_id,
                        sample_limit=args.auto_limit or 60,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_group_threshold_policy: executed")

        if args.mode == "ml-policy-audit-queue":
            import ml_policy_audit_queue

            print("[main] Running stage: ml_policy_audit_queue (ml_policy_audit_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_policy_audit_queue.main(
                        focus=args.policy_audit_focus,
                        groups_value=args.ml_groups,
                        limit=args.auto_limit or 200,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_policy_audit_queue: executed")

        if args.mode == "ml-contrast-review-queue":
            import ml_contrast_review_queue

            print("[main] Running stage: ml_contrast_review_queue (ml_contrast_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_contrast_review_queue.main(
                        limit=args.auto_limit or 40,
                        batch_size=args.triage_batch_size or 20,
                        mode=args.ml_groups or "risk",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_contrast_review_queue: executed")

        if args.mode == "ml-holdout-eval":
            import ml_holdout_eval

            print("[main] Running stage: ml_holdout_eval (ml_holdout_eval.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_holdout_eval.main(
                        safe_threshold=args.auto_min_score or 0.90,
                        safe_multiplier=args.ml_safe_multiplier,
                        sample_limit=args.auto_limit or 20,
                        feature_set=args.ml_feature_set,
                        train_strategy=args.ml_train_strategy,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_holdout_eval: executed")

        if args.mode == "ml-holdout-review-queue":
            import ml_holdout_review_queue

            print("[main] Running stage: ml_holdout_review_queue (ml_holdout_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_holdout_review_queue.main(
                        safe_threshold=args.auto_min_score or 0.90,
                        safe_multiplier=args.ml_safe_multiplier,
                        sample_limit=args.auto_limit or 40,
                        feature_set=args.ml_feature_set,
                        train_strategy=args.ml_train_strategy,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_holdout_review_queue: executed")

        if args.mode == "ml-progress":
            run_stage_with_log("ml_progress", log_lines)
            report_lines.append("- ml_progress: executed")

        if args.mode == "ml-threshold-sweep":
            import ml_threshold_sweep

            print("[main] Running stage: ml_threshold_sweep (ml_threshold_sweep.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_threshold_sweep.main(
                        thresholds_value=args.ml_thresholds,
                        feature_set=args.ml_feature_set,
                        safe_multiplier=args.ml_safe_multiplier,
                        train_strategy=args.ml_train_strategy,
                        split_mode=args.ml_split_mode,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_threshold_sweep: executed")

        if args.mode == "ml-specialist-models":
            import ml_specialist_models

            print("[main] Running stage: ml_specialist_models (ml_specialist_models.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_specialist_models.main(
                        specialist=args.ml_specialist,
                        safe_threshold=args.auto_min_score,
                        safe_multiplier=None if args.ml_safe_multiplier == 5 else args.ml_safe_multiplier,
                        feature_set=None if args.ml_feature_set == "legacy_v1" else args.ml_feature_set,
                        train_strategy=None if args.ml_train_strategy == "balanced_v1" else args.ml_train_strategy,
                        dataset_only=args.ml_specialist_dataset_only,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_specialist_models: executed")

        if args.mode == "ml-specialist-auditor":
            import ml_specialist_auditor

            print("[main] Running stage: ml_specialist_auditor (ml_specialist_auditor.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_specialist_auditor.main(
                        general_score_run_id=args.ml_active_score_run_id,
                        specialist_score_run_id=args.ml_candidate_score_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_specialist_auditor: executed")

        if args.mode == "ml-specialist-policy":
            import ml_specialist_policy

            print("[main] Running stage: ml_specialist_policy (ml_specialist_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_specialist_policy.main(
                        general_score_run_id=args.ml_active_score_run_id,
                        specialist=args.ml_specialist,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_specialist_policy: executed")

        if args.mode == "ml-specialist-score":
            import ml_specialist_score

            print("[main] Running stage: ml_specialist_score (ml_specialist_score.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_specialist_score.main(
                        specialist=args.ml_specialist,
                        model_run_id=args.ml_model_run_id,
                        limit=args.auto_limit,
                        batch_size=args.ml_score_batch_size,
                        include_locked=args.ml_include_locked,
                        safe_threshold=args.auto_min_score,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_specialist_score: executed")

        if args.mode == "ml-specialist-ensemble-policy":
            import ml_specialist_ensemble_policy

            print("[main] Running stage: ml_specialist_ensemble_policy (ml_specialist_ensemble_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_specialist_ensemble_policy.main(
                        general_score_run_id=args.ml_active_score_run_id,
                        sample_limit=args.auto_limit or 40,
                        specialist=args.ml_specialist,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_specialist_ensemble_policy: executed")

        if args.mode == "ml-specialist-frontier-queue":
            import ml_specialist_frontier_queue

            print("[main] Running stage: ml_specialist_frontier_queue (ml_specialist_frontier_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_specialist_frontier_queue.main(
                        specialist=args.ml_specialist,
                        policy_run_id=None,
                        limit=args.auto_limit or 60,
                        batch_size=args.triage_batch_size or 20,
                        min_score=args.auto_min_score if args.auto_min_score is not None else 0.70,
                        per_specialist_limit=args.ml_per_path_limit,
                        per_path_limit=args.ml_per_path_limit,
                        output=None,
                        include_reviewed=args.ml_include_locked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_specialist_frontier_queue: executed")

        if args.mode == "ml-agent-architecture":
            import ml_agent_architecture

            print("[main] Running stage: ml_agent_architecture (ml_agent_architecture.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_agent_architecture.main()
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_agent_architecture: executed")

        if args.mode == "ml-agent-audit-queue":
            import ml_agent_audit_queue

            print("[main] Running stage: ml_agent_audit_queue (ml_agent_audit_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_agent_audit_queue.main(
                        specialist=args.ml_specialist,
                        routing_run_id=None,
                        limit=args.auto_limit or 60,
                        batch_size=args.triage_batch_size or 20,
                        output=None,
                        include_reviewed=args.ml_include_locked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_agent_audit_queue: executed")

        if args.mode == "learning-network-diagnostic":
            run_stage_with_log("learning_network_diagnostic", log_lines)
            report_lines.append("- learning_network_diagnostic: executed")

        if args.mode == "issue-ledger":
            import issue_ledger

            print("[main] Running stage: issue_ledger (issue_ledger.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_ledger.main(limit=args.auto_limit)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_ledger: executed")

        if args.mode == "issue-review-queue":
            import issue_review_queue

            print("[main] Running stage: issue_review_queue (issue_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_queue.main(
                        agent_key=args.issue_agent_key,
                        issue_family=args.issue_family,
                        issue_kind=args.issue_kind,
                        active_action=args.issue_active_action,
                        candidate_action=args.issue_candidate_action,
                        policy_action=args.issue_policy_action,
                        path_like=args.issue_path_like,
                        limit=args.auto_limit,
                        per_bucket=args.issue_queue_per_bucket,
                        include_existing=args.issue_queue_include_existing,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_queue: executed")

        if args.mode == "issue-review-subcluster":
            import issue_review_subcluster

            if not args.issue_review_queue_jsonl:
                raise RuntimeError("--issue-review-queue-jsonl is required for issue-review-subcluster.")
            if not args.issue_review_decisions:
                raise RuntimeError("--issue-review-decisions is required for issue-review-subcluster.")
            print("[main] Running stage: issue_review_subcluster (issue_review_subcluster.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_subcluster.main(
                        queue_jsonl=args.issue_review_queue_jsonl,
                        decisions_jsonl=args.issue_review_decisions,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_subcluster: executed")

        if args.mode == "issue-review-ingest":
            import issue_review_ingest

            if not args.issue_review_decisions:
                raise RuntimeError("--issue-review-decisions is required for issue-review-ingest.")
            print("[main] Running stage: issue_review_ingest (issue_review_ingest.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_ingest.main(
                        decisions_path=args.issue_review_decisions,
                        queue_run_id=args.issue_review_queue_run_id,
                        source_report=args.issue_review_source_report,
                        reviewer=args.issue_reviewer,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_ingest: executed")

        if args.mode == "issue-review-short-label-positive-release":
            import issue_review_short_label_positive_release

            print("[main] Running stage: issue_review_short_label_positive_release (issue_review_short_label_positive_release.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_short_label_positive_release.main(
                        decision_run_id=args.issue_review_decision_run_id,
                        queue_run_id=args.issue_review_queue_run_id,
                        agent_key=args.issue_agent_key,
                        policy_status="shadow",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_short_label_positive_release: executed")

        if args.mode == "issue-review-short-label-positive-checkpoint":
            import issue_review_short_label_positive_checkpoint

            print("[main] Running stage: issue_review_short_label_positive_checkpoint (issue_review_short_label_positive_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_short_label_positive_checkpoint.main(
                        release_run_id=args.issue_short_label_release_run_id,
                        min_released_required=args.issue_short_label_checkpoint_min_released,
                        max_blocked_allowed=args.issue_short_label_checkpoint_max_blocked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_short_label_positive_checkpoint: executed")

        if args.mode == "issue-review-gender-subpolicy-shadow":
            import issue_review_gender_subpolicy_shadow

            print("[main] Running stage: issue_review_gender_subpolicy_shadow (issue_review_gender_subpolicy_shadow.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_gender_subpolicy_shadow.main(
                        decision_run_id=args.issue_review_decision_run_id,
                        queue_run_id=args.issue_review_queue_run_id,
                        agent_key=args.issue_agent_key,
                        policy_status="shadow",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_gender_subpolicy_shadow: executed")

        if args.mode == "issue-review-gender-boundary-checkpoint":
            import issue_review_gender_boundary_checkpoint

            print("[main] Running stage: issue_review_gender_boundary_checkpoint (issue_review_gender_boundary_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_gender_boundary_checkpoint.main(
                        shadow_run_id=args.issue_gender_shadow_run_id,
                        min_boundary_required=args.issue_gender_boundary_min,
                        max_blocked_allowed=args.issue_gender_boundary_max_blocked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_gender_boundary_checkpoint: executed")

        if args.mode == "issue-review-gender-boundary-lifecycle":
            import issue_review_gender_boundary_lifecycle_policy

            print("[main] Running stage: issue_review_gender_boundary_lifecycle_policy (issue_review_gender_boundary_lifecycle_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_gender_boundary_lifecycle_policy.main(
                        checkpoint_run_id=args.issue_gender_boundary_checkpoint_run_id,
                        policy_status=args.issue_gender_boundary_lifecycle_status,
                        expected_total=args.issue_gender_boundary_lifecycle_expected_total,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_gender_boundary_lifecycle_policy: executed")

        if args.mode == "issue-review-trigger-gender-role-surface":
            import issue_review_trigger_gender_role_surface

            print("[main] Running stage: issue_review_trigger_gender_role_surface (issue_review_trigger_gender_role_surface.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_trigger_gender_role_surface.main(
                        decision_run_id=args.issue_review_decision_run_id,
                        queue_run_id=args.issue_review_queue_run_id,
                        source_agent_key=args.issue_agent_key,
                        policy_status="shadow",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_trigger_gender_role_surface: executed")

        if args.mode == "issue-review-trigger-gender-role-checkpoint":
            import issue_review_trigger_gender_role_checkpoint

            print("[main] Running stage: issue_review_trigger_gender_role_checkpoint (issue_review_trigger_gender_role_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_trigger_gender_role_checkpoint.main(
                        shadow_run_id=args.issue_trigger_gender_role_shadow_run_id,
                        min_ready_required=args.issue_trigger_gender_role_checkpoint_min_ready,
                        max_blocked_allowed=args.issue_trigger_gender_role_checkpoint_max_blocked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_trigger_gender_role_checkpoint: executed")

        if args.mode == "issue-review-trigger-gender-role-lifecycle":
            import issue_review_trigger_gender_role_lifecycle_policy

            print("[main] Running stage: issue_review_trigger_gender_role_lifecycle_policy (issue_review_trigger_gender_role_lifecycle_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_review_trigger_gender_role_lifecycle_policy.main(
                        checkpoint_run_id=args.issue_trigger_gender_role_lifecycle_checkpoint_run_id,
                        policy_status=args.issue_trigger_gender_role_lifecycle_status,
                        expected_total=args.issue_trigger_gender_role_lifecycle_expected_total,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_review_trigger_gender_role_lifecycle_policy: executed")

        if args.mode == "issue-partial-coverage":
            import issue_partial_coverage_report

            print("[main] Running stage: issue_partial_coverage_report (issue_partial_coverage_report.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_partial_coverage_report.main(
                        ledger_run_id=args.issue_partial_coverage_ledger_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_partial_coverage_report: executed")

        if args.mode == "issue-title-policy-route-queue":
            import issue_title_policy_route_standard_review_queue

            print("[main] Running stage: issue_title_policy_route_standard_review_queue (issue_title_policy_route_standard_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_title_policy_route_standard_review_queue.main(
                        ledger_run_id=args.issue_ledger_run_id,
                        lane=args.issue_title_route_lane,
                        limit=args.auto_limit,
                        per_prefix=args.issue_title_route_per_prefix,
                        include_existing=args.issue_queue_include_existing,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_title_policy_route_standard_review_queue: executed")

        if args.mode == "issue-composition-queue":
            import issue_composition_queue

            print("[main] Running stage: issue_composition_queue (issue_composition_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_composition_queue.main(
                        agent_key=args.issue_agent_key,
                        issue_family=args.issue_family,
                        partial_run_id=args.issue_composition_partial_run_id,
                        scope=args.issue_composition_scope,
                        limit=args.auto_limit,
                        per_bucket=args.issue_queue_per_bucket,
                        include_existing=args.issue_queue_include_existing,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_composition_queue: executed")

        if args.mode == "issue-multiagent-composition-queue":
            import issue_multiagent_composition_queue

            print("[main] Running stage: issue_multiagent_composition_queue (issue_multiagent_composition_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    issue_multiagent_composition_queue.main(
                        partial_run_id=args.issue_composition_partial_run_id,
                        agent_key=args.issue_composition_agent_key,
                        limit=args.auto_limit,
                        per_bucket=args.issue_queue_per_bucket,
                        min_issues=args.issue_composition_min_issues,
                        min_mature_families=args.issue_composition_min_mature_families,
                        min_mature_ratio=args.issue_composition_min_mature_ratio,
                        include_existing=args.issue_queue_include_existing,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- issue_multiagent_composition_queue: executed")

        if args.mode == "ml-composite-checkpoint":
            import ml_composite_checkpoint

            print("[main] Running stage: ml_composite_checkpoint (ml_composite_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_checkpoint.main()
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_checkpoint: executed")

        if args.mode == "ml-composite-promote":
            import ml_composite_promote

            print("[main] Running stage: ml_composite_promote (ml_composite_promote.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_promote.main(promoted_by="codex")
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_promote: executed")

        if args.mode == "ml-composite-review-progress":
            import ml_composite_review_progress

            print("[main] Running stage: ml_composite_review_progress (ml_composite_review_progress.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_review_progress.main()
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_review_progress: executed")

        if args.mode == "ml-composite-review-ingest":
            import ml_composite_review_ingest

            print("[main] Running stage: ml_composite_review_ingest (ml_composite_review_ingest.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_review_ingest.main(
                        decisions=args.composite_review_decisions,
                        apply=args.auto_apply,
                        reviewer=args.composite_review_reviewer,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- ml_composite_review_ingest: executed, apply={args.auto_apply}")

        if args.mode == "ml-composite-queue-backfill":
            import ml_composite_queue_backfill

            print("[main] Running stage: ml_composite_queue_backfill (ml_composite_queue_backfill.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_queue_backfill.main()
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_queue_backfill: executed")

        if args.mode == "ml-composite-next-queue-cycle":
            import ml_composite_next_queue_cycle

            print("[main] Running stage: ml_composite_next_queue_cycle (ml_composite_next_queue_cycle.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_next_queue_cycle.main(
                        batch_size=args.composite_queue_batch_size,
                        max_routes=args.composite_queue_max_routes,
                        route_filter_value=args.composite_queue_routes,
                        plan_only=args.composite_queue_plan_only,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- ml_composite_next_queue_cycle: executed, plan_only={args.composite_queue_plan_only}"
            )

        if args.mode == "ml-composite-subpolicy-diagnostic":
            import ml_composite_subpolicy_diagnostic

            print("[main] Running stage: ml_composite_subpolicy_diagnostic (ml_composite_subpolicy_diagnostic.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_subpolicy_diagnostic.main(
                        min_evidence=args.composite_subpolicy_min_evidence,
                        min_positive=args.composite_subpolicy_min_positive,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_subpolicy_diagnostic: executed")

        if args.mode == "ml-composite-subpolicy-evidence-queue":
            import ml_composite_subpolicy_evidence_queue

            print("[main] Running stage: ml_composite_subpolicy_evidence_queue (ml_composite_subpolicy_evidence_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_subpolicy_evidence_queue.main(
                        diagnostic_run_id=args.composite_subpolicy_run_id,
                        statuses_value=args.composite_subpolicy_statuses,
                        routes_value=args.composite_subpolicy_routes,
                        subtypes_value=args.composite_subpolicy_subtypes,
                        max_groups=args.composite_subpolicy_max_groups,
                        limit_per_group=args.composite_subpolicy_limit_per_group,
                        include_reviewed=args.composite_subpolicy_include_reviewed,
                        include_queued=args.composite_subpolicy_include_queued,
                        plan_only=args.composite_subpolicy_plan_only,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- ml_composite_subpolicy_evidence_queue: executed, plan_only={args.composite_subpolicy_plan_only}"
            )

        if args.mode == "ml-composite-subpolicy-promotion-audit":
            import ml_composite_subpolicy_promotion_audit

            print("[main] Running stage: ml_composite_subpolicy_promotion_audit (ml_composite_subpolicy_promotion_audit.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_subpolicy_promotion_audit.main(
                        diagnostic_run_id=args.composite_subpolicy_run_id,
                        statuses_value=args.composite_subpolicy_promotion_statuses,
                        routes_value=args.composite_subpolicy_routes,
                        subtypes_value=args.composite_subpolicy_subtypes,
                        min_accept=args.composite_subpolicy_min_positive,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_subpolicy_promotion_audit: executed")

        if args.mode == "ml-composite-subpolicy-promotion-queue":
            import ml_composite_subpolicy_promotion_queue

            print("[main] Running stage: ml_composite_subpolicy_promotion_queue (ml_composite_subpolicy_promotion_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_subpolicy_promotion_queue.main(
                        audit_run_id=args.composite_promotion_audit_run_id,
                        statuses_value=args.composite_promotion_queue_statuses,
                        routes_value=args.composite_subpolicy_routes,
                        subtypes_value=args.composite_subpolicy_subtypes,
                        rule_keys_value=args.composite_promotion_rule_keys,
                        max_rules=args.composite_subpolicy_max_groups,
                        limit_per_rule=args.composite_subpolicy_limit_per_group,
                        include_reviewed=args.composite_subpolicy_include_reviewed,
                        include_queued=not args.composite_promotion_skip_queued,
                        plan_only=args.composite_subpolicy_plan_only,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- ml_composite_subpolicy_promotion_queue: executed, plan_only={args.composite_subpolicy_plan_only}"
            )

        if args.mode == "ml-composite-subpolicy-guarded-overlay":
            import ml_composite_subpolicy_guarded_overlay

            print("[main] Running stage: ml_composite_subpolicy_guarded_overlay (ml_composite_subpolicy_guarded_overlay.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_subpolicy_guarded_overlay.main(
                        audit_run_id=args.composite_promotion_audit_run_id,
                        statuses_value=args.composite_guarded_overlay_statuses,
                        rule_keys_value=args.composite_promotion_rule_keys,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_subpolicy_guarded_overlay: executed")

        if args.mode == "ml-composite-guarded-overlay-checkpoint":
            import ml_composite_guarded_overlay_checkpoint

            print("[main] Running stage: ml_composite_guarded_overlay_checkpoint (ml_composite_guarded_overlay_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_guarded_overlay_checkpoint.main(
                        overlay_run_id=args.composite_guarded_overlay_run_id,
                        min_releases=args.composite_guarded_min_releases,
                        min_rules=args.composite_guarded_min_rules,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_guarded_overlay_checkpoint: executed")

        if args.mode == "ml-composite-guarded-overlay-promote":
            import ml_composite_guarded_overlay_promote

            print("[main] Running stage: ml_composite_guarded_overlay_promote (ml_composite_guarded_overlay_promote.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_guarded_overlay_promote.main(
                        checkpoint_id=args.composite_guarded_checkpoint_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_guarded_overlay_promote: executed")

        if args.mode == "ml-composite-guarded-overlay-shadow-queue":
            import ml_composite_guarded_overlay_shadow_queue

            print("[main] Running stage: ml_composite_guarded_overlay_shadow_queue (ml_composite_guarded_overlay_shadow_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_guarded_overlay_shadow_queue.main(
                        checkpoint_id=args.composite_guarded_checkpoint_id,
                        priority=args.composite_shadow_priority,
                        release_scope=args.composite_shadow_release_scope,
                        limit=args.composite_shadow_limit,
                        skip_reviewed=args.composite_shadow_skip_reviewed,
                        plan_only=args.composite_shadow_plan_only,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- ml_composite_guarded_overlay_shadow_queue: executed, plan_only={args.composite_shadow_plan_only}"
            )

        if args.mode == "ml-composite-guarded-overlay-shadow-decisions":
            import ml_composite_guarded_overlay_shadow_decisions

            print("[main] Running stage: ml_composite_guarded_overlay_shadow_decisions (ml_composite_guarded_overlay_shadow_decisions.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_composite_guarded_overlay_shadow_decisions.main(
                        queue_run_id=args.composite_shadow_queue_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_composite_guarded_overlay_shadow_decisions: executed")

        if args.mode == "auto-confirmation-text-shadow-policy":
            import auto_confirmation_reopen_text_shadow_policy

            print("[main] Running stage: auto_confirmation_reopen_text_shadow_policy (auto_confirmation_reopen_text_shadow_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_shadow_policy.main(
                        diagnostic_run_id=args.text_diagnostic_run_id,
                        policy_key=args.text_shadow_policy,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_shadow_policy: executed")

        if args.mode == "auto-confirmation-text-policy-checkpoint":
            import auto_confirmation_reopen_text_policy_checkpoint

            print("[main] Running stage: auto_confirmation_reopen_text_policy_checkpoint (auto_confirmation_reopen_text_policy_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_policy_checkpoint.main(
                        shadow_policy_run_id=args.text_shadow_policy_run_id,
                        policy_key=args.text_checkpoint_policy,
                        min_ready_required=args.text_checkpoint_min_ready,
                        max_blocked_allowed=args.text_checkpoint_max_blocked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_policy_checkpoint: executed")

        if args.mode == "auto-confirmation-text-lifecycle-policy":
            import auto_confirmation_reopen_text_lifecycle_policy

            print("[main] Running stage: auto_confirmation_reopen_text_lifecycle_policy (auto_confirmation_reopen_text_lifecycle_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_lifecycle_policy.main(
                        checkpoint_run_id=args.text_checkpoint_run_id,
                        policy_status=args.text_lifecycle_status,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_lifecycle_policy: executed")

        if args.mode == "auto-confirmation-text-boundary-policy":
            import auto_confirmation_reopen_text_boundary_policy

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_policy (auto_confirmation_reopen_text_boundary_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_policy.main(
                        policy_key=args.text_boundary_policy,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                f"- auto_confirmation_reopen_text_boundary_policy: executed, policy={args.text_boundary_policy}"
            )

        if args.mode == "auto-confirmation-text-boundary-repair-queue":
            import auto_confirmation_reopen_text_boundary_repair_queue

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_repair_queue (auto_confirmation_reopen_text_boundary_repair_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_repair_queue.main(
                        boundary_policy_run_id=args.text_boundary_policy_run_id,
                        scope=args.text_boundary_repair_scope,
                        limit=args.text_boundary_repair_limit,
                        include_existing=args.text_boundary_repair_include_existing,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(
                (
                    "- auto_confirmation_reopen_text_boundary_repair_queue: executed, "
                    f"scope={args.text_boundary_repair_scope}"
                )
            )

        if args.mode == "auto-confirmation-text-boundary-repair-shadow-policy":
            import auto_confirmation_reopen_text_boundary_repair_shadow_policy

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_repair_shadow_policy (auto_confirmation_reopen_text_boundary_repair_shadow_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_repair_shadow_policy.main(
                        repair_queue_run_id=args.text_boundary_repair_queue_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_repair_shadow_policy: executed")

        if args.mode == "auto-confirmation-text-boundary-repair-checkpoint":
            import auto_confirmation_reopen_text_boundary_repair_checkpoint

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_repair_checkpoint (auto_confirmation_reopen_text_boundary_repair_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_repair_checkpoint.main(
                        repair_shadow_run_id=args.text_boundary_repair_shadow_run_id,
                        min_ready_required=args.text_boundary_repair_min_ready,
                        max_blocked_allowed=args.text_boundary_repair_max_blocked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_repair_checkpoint: executed")

        if args.mode == "auto-confirmation-text-boundary-repair-lifecycle-policy":
            import auto_confirmation_reopen_text_boundary_repair_lifecycle_policy

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_repair_lifecycle_policy (auto_confirmation_reopen_text_boundary_repair_lifecycle_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_repair_lifecycle_policy.main(
                        checkpoint_run_id=args.text_boundary_repair_checkpoint_run_id,
                        policy_status="shadow",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_repair_lifecycle_policy: executed")

        if args.mode == "auto-confirmation-text-boundary-repair-production-audit":
            import auto_confirmation_reopen_text_boundary_repair_production_audit

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_repair_production_audit (auto_confirmation_reopen_text_boundary_repair_production_audit.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_repair_production_audit.main(
                        lifecycle_run_id=args.text_boundary_repair_lifecycle_run_id,
                        state_run_id=args.text_boundary_repair_state_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_repair_production_audit: executed")

        if args.mode == "auto-confirmation-text-boundary-token-policy-bridge":
            import auto_confirmation_reopen_text_boundary_token_policy_bridge

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_token_policy_bridge (auto_confirmation_reopen_text_boundary_token_policy_bridge.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_token_policy_bridge.main(
                        repair_queue_run_id=args.text_boundary_repair_queue_run_id,
                        limit=args.text_boundary_token_bridge_limit,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_token_policy_bridge: executed")

        if args.mode == "auto-confirmation-text-boundary-token-subpolicy-shadow":
            import auto_confirmation_reopen_text_boundary_token_subpolicy_shadow

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_token_subpolicy_shadow (auto_confirmation_reopen_text_boundary_token_subpolicy_shadow.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_token_subpolicy_shadow.main(
                        bridge_run_id=args.text_boundary_token_bridge_run_id,
                        subpolicy_name=args.text_boundary_token_subpolicy,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_token_subpolicy_shadow: executed")

        if args.mode == "auto-confirmation-text-boundary-token-subpolicy-checkpoint":
            import auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint (auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint.main(
                        subpolicy_shadow_run_id=args.text_boundary_token_subpolicy_run_id,
                        subpolicy_name=args.text_boundary_token_subpolicy,
                        min_ready_required=args.text_boundary_token_subpolicy_min_ready,
                        max_blocked_allowed=args.text_boundary_token_subpolicy_max_blocked,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint: executed")

        if args.mode == "auto-confirmation-text-boundary-token-subpolicy-lifecycle-policy":
            import auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy (auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy.main(
                        checkpoint_run_ids=auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy.parse_ids(
                            args.text_boundary_token_subpolicy_checkpoint_run_ids
                        ),
                        policy_status="shadow",
                        min_checkpoints=args.text_boundary_token_subpolicy_lifecycle_min_checkpoints,
                        expected_total=args.text_boundary_token_subpolicy_lifecycle_expected_total,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy: executed")

        if args.mode == "auto-confirmation-text-boundary-token-subpolicy-production-audit":
            import auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit

            print("[main] Running stage: auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit (auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit.main(
                        lifecycle_run_id=args.text_boundary_token_subpolicy_lifecycle_run_id,
                        state_run_id=args.text_boundary_token_subpolicy_state_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit: executed")

        if args.mode == "controlled-token-subpolicy-apply":
            import controlled_token_subpolicy_apply

            print("[main] Running stage: controlled_token_subpolicy_apply (controlled_token_subpolicy_apply.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    controlled_token_subpolicy_apply.main(
                        audit_run_id=args.controlled_token_subpolicy_audit_run_id,
                        apply=args.auto_apply,
                        reaudit=args.controlled_token_subpolicy_reaudit,
                        create_backup=not args.apply_no_backup,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- controlled_token_subpolicy_apply: executed")

        if args.mode == "same-token-boundary-repair-apply":
            import same_token_boundary_repair_apply

            print("[main] Running stage: same_token_boundary_repair_apply (same_token_boundary_repair_apply.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    same_token_boundary_repair_apply.main(
                        audit_run_id=args.same_token_boundary_repair_audit_run_id,
                        apply=args.auto_apply,
                        reaudit=args.same_token_boundary_repair_reaudit,
                        create_backup=not args.apply_no_backup,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- same_token_boundary_repair_apply: executed")

        if args.mode == "select-cstring-governed-bridge-apply":
            import select_cstring_governed_bridge_apply

            print("[main] Running stage: select_cstring_governed_bridge_apply (select_cstring_governed_bridge_apply.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    select_cstring_governed_bridge_apply.main(
                        proposal_run_id=args.select_cstring_bridge_proposal_run_id,
                        apply=args.auto_apply,
                        reaudit=args.select_cstring_bridge_reaudit,
                        create_backup=not args.apply_no_backup,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- select_cstring_governed_bridge_apply: executed")

        if args.mode == "auto-confirmation-weak-boundary-queue":
            import auto_confirmation_reopen_weak_boundary_queue

            print("[main] Running stage: auto_confirmation_reopen_weak_boundary_queue (auto_confirmation_reopen_weak_boundary_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    auto_confirmation_reopen_weak_boundary_queue.main(
                        boundary=args.weak_boundary,
                        limit=args.weak_boundary_limit if args.weak_boundary_limit > 0 else None,
                        skip_existing=not args.weak_boundary_include_existing,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- auto_confirmation_reopen_weak_boundary_queue: executed, boundary={args.weak_boundary}")

        if args.mode == "segment-state":
            import segment_state_snapshot

            print("[main] Running stage: segment_state_snapshot (segment_state_snapshot.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_state_snapshot.main(
                        limit=args.auto_limit,
                        generate_report=args.segment_state_report,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_state: executed")

        if args.mode == "segment-apply":
            import apply_segment_state_updates

            print("[main] Running stage: segment_apply (apply_segment_state_updates.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    apply_segment_state_updates.main(
                        state_run_id=None,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        segment_ids_csv=args.segment_ids,
                        review_states_csv=args.segment_review_states,
                        include_auto_confirmed=args.segment_include_auto_confirmed,
                        include_intentional_blank=args.segment_include_intentional_blank,
                        allow_locked_token_override=args.segment_allow_locked_token_override,
                        require_token_policy_decision=args.segment_require_token_policy_decision,
                        allow_token_policy_decision=args.segment_allow_token_policy_decision,
                        token_policy_run_id=args.token_policy_run_id,
                        apply=args.auto_apply,
                        create_backup=not args.apply_no_backup,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- segment_apply: executed, apply={args.auto_apply}")

            if args.auto_apply:
                index_current, changes = source_index_is_current(settings)
                if index_current:
                    print("[main] Post-segment-apply index is current; skipping refresh")
                    report_lines.append("- post-segment-apply refresh: skipped, file hashes unchanged")
                else:
                    print(f"[main] Post-segment-apply refresh detected {len(changes)} changed file(s)")
                    report_lines.append(f"- post-segment-apply refresh: detected {len(changes)} changed file(s)")
                    report_lines.extend(f"  - {change}" for change in changes[:20])
                    for stage in ["index", "inline", "analyze", "memory", "suggest", "evaluate"]:
                        run_stage_with_log(stage, log_lines)
                        report_lines.append(f"- post-segment-apply {stage}: executed")

        if args.mode == "segment-token-queue":
            import segment_token_mismatch_queue

            print("[main] Running stage: segment_token_queue (segment_token_mismatch_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_mismatch_queue.main(
                        state_run_id=None,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        review_states_csv=args.segment_review_states,
                        include_auto_confirmed=args.segment_include_auto_confirmed,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_queue: executed")

        if args.mode == "segment-token-policy":
            import segment_token_policy

            print("[main] Running stage: segment_token_policy (segment_token_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_policy.main(
                        state_run_id=None,
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        review_states_csv=args.segment_review_states,
                        include_auto_confirmed=args.segment_include_auto_confirmed,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_policy: executed")

        if args.mode == "segment-token-policy-queue":
            import segment_token_policy_review_queue

            print("[main] Running stage: segment_token_policy_queue (segment_token_policy_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_policy_review_queue.main(
                        policy_run_id=args.token_policy_run_id,
                        per_bucket=args.token_policy_per_bucket,
                        limit=args.auto_limit,
                        buckets_csv=args.token_policy_buckets,
                        risks_csv=args.token_policy_risks,
                        path_like=args.auto_path_like,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        skip_apply_approved=args.token_policy_skip_apply_approved,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_policy_queue: executed")

        if args.mode == "segment-token-tutorial-concept-policy":
            import segment_token_tutorial_concept_policy

            print("[main] Running stage: segment_token_tutorial_concept_policy (segment_token_tutorial_concept_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_tutorial_concept_policy.main(
                        policy_run_id=args.token_policy_run_id,
                        tutorial_only=False,
                        limit=args.auto_limit,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_tutorial_concept_policy: executed")

        if args.mode == "segment-token-tutorial-concept-promotion":
            import segment_token_tutorial_concept_promotion

            print("[main] Running stage: segment_token_tutorial_concept_promotion (segment_token_tutorial_concept_promotion.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_tutorial_concept_promotion.main(
                        policy_run_id=args.token_policy_run_id,
                        min_evidence=args.composite_subpolicy_min_positive,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_tutorial_concept_promotion: executed")

        if args.mode == "segment-token-tutorial-concept-candidate-policy":
            import segment_token_tutorial_concept_candidate_policy

            print("[main] Running stage: segment_token_tutorial_concept_candidate_policy (segment_token_tutorial_concept_candidate_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_tutorial_concept_candidate_policy.main(
                        policy_run_id=args.token_policy_run_id,
                        min_evidence=args.composite_subpolicy_min_positive,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_tutorial_concept_candidate_policy: executed")

        if args.mode == "segment-token-gender-subpolicy":
            import segment_token_gender_subpolicy

            print("[main] Running stage: segment_token_gender_subpolicy (segment_token_gender_subpolicy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_subpolicy.main(
                        policy_run_id=args.token_policy_run_id,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        skip_apply_approved=args.token_policy_skip_apply_approved,
                        undecided_only=args.token_policy_undecided_only,
                        limit=args.token_overlay_limit,
                        statuses_value=args.token_subpolicy_statuses,
                        subtypes_value=args.token_gender_subtypes,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_subpolicy: executed")

        if args.mode == "segment-token-gender-split-subpolicy":
            import segment_token_gender_split_subpolicy

            print("[main] Running stage: segment_token_gender_split_subpolicy (segment_token_gender_split_subpolicy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_subpolicy.main(
                        policy_run_id=args.token_policy_run_id,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        skip_apply_approved=args.token_policy_skip_apply_approved,
                        undecided_only=args.token_policy_undecided_only,
                        limit=args.token_overlay_limit,
                        split_agents_value=args.token_split_agents,
                        split_maturity_value=args.token_split_maturity,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_subpolicy: executed")

        if args.mode == "segment-token-gender-split-evidence-queue":
            import segment_token_gender_split_evidence_queue

            print("[main] Running stage: segment_token_gender_split_evidence_queue (segment_token_gender_split_evidence_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_evidence_queue.main(
                        policy_run_id=args.token_policy_run_id,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        skip_apply_approved=args.token_policy_skip_apply_approved,
                        undecided_only=args.token_policy_undecided_only,
                        limit=args.token_overlay_limit,
                        split_agents_value=args.token_split_agents,
                        split_maturity_value=args.token_split_maturity,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_evidence_queue: executed")

        if args.mode == "segment-token-gender-simple-evidence-queue":
            import segment_token_gender_simple_evidence_queue

            print("[main] Running stage: segment_token_gender_simple_evidence_queue (segment_token_gender_simple_evidence_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_simple_evidence_queue.main(
                        policy_run_id=args.token_policy_run_id,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        skip_apply_approved=args.token_policy_skip_apply_approved,
                        undecided_only=args.token_policy_undecided_only,
                        statuses_value=args.token_subpolicy_statuses,
                        simple_agents_value=args.token_split_agents,
                        maturity_value=args.token_split_maturity,
                        limit=args.token_overlay_limit,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_simple_evidence_queue: executed")

        if args.mode == "segment-token-gender-split-evidence-ingest":
            import segment_token_gender_split_evidence_ingest

            if not args.token_split_evidence_decisions:
                raise RuntimeError("--token-split-evidence-decisions is required for segment-token-gender-split-evidence-ingest.")
            if not args.token_split_evidence_source_queue:
                raise RuntimeError("--token-split-evidence-source-queue is required for segment-token-gender-split-evidence-ingest.")
            print("[main] Running stage: segment_token_gender_split_evidence_ingest (segment_token_gender_split_evidence_ingest.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_evidence_ingest.main(
                        decisions_path=args.token_split_evidence_decisions,
                        source_queue_path=args.token_split_evidence_source_queue,
                        policy_run_id=args.token_policy_run_id,
                        reviewer=args.token_policy_reviewer or "codex_learning_front",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_evidence_ingest: executed")

        if args.mode == "segment-token-gender-split-promotion-audit":
            import segment_token_gender_split_promotion_audit

            print("[main] Running stage: segment_token_gender_split_promotion_audit (segment_token_gender_split_promotion_audit.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_promotion_audit.main(
                        min_positive=args.token_split_min_positive,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_promotion_audit: executed")

        if args.mode == "segment-token-gender-split-guarded-policy":
            import segment_token_gender_split_guarded_policy

            print("[main] Running stage: segment_token_gender_split_guarded_policy (segment_token_gender_split_guarded_policy.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_guarded_policy.main(
                        policy_run_id=args.token_policy_run_id,
                        audit_run_id=args.token_split_audit_run_id,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        skip_apply_approved=args.token_policy_skip_apply_approved,
                        agents_value=args.token_split_agents,
                        limit=args.token_overlay_limit,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_guarded_policy: executed")

        if args.mode == "segment-token-gender-split-coordinator-dry-run":
            import segment_token_gender_split_coordinator_dry_run

            print("[main] Running stage: segment_token_gender_split_coordinator_dry_run (segment_token_gender_split_coordinator_dry_run.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_coordinator_dry_run.main(
                        guarded_run_id=args.token_split_guarded_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_coordinator_dry_run: executed")

        if args.mode == "segment-token-gender-split-bridge-dry-run":
            import segment_token_gender_split_bridge_dry_run

            print("[main] Running stage: segment_token_gender_split_bridge_dry_run (segment_token_gender_split_bridge_dry_run.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_gender_split_bridge_dry_run.main(
                        guarded_run_id=args.token_split_guarded_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_gender_split_bridge_dry_run: executed")

        if args.mode == "segment-token-policy-overlay":
            import segment_token_policy_overlay

            print("[main] Running stage: segment_token_policy_overlay (segment_token_policy_overlay.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_policy_overlay.main(
                        policy_run_id=args.token_policy_run_id,
                        min_evidence=args.token_policy_per_bucket,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_policy_overlay: executed")

        if args.mode == "segment-token-overlay-queue":
            import segment_token_overlay_review_queue

            print("[main] Running stage: segment_token_overlay_review_queue (segment_token_overlay_review_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_overlay_review_queue.main(
                        overlay_run_id=args.token_overlay_run_id,
                        critical_only=not args.token_overlay_all,
                        use_active_gate=args.use_active_composite_gate,
                        route=args.token_overlay_route,
                        risk=args.token_overlay_risk,
                        limit=args.token_overlay_limit,
                        skip_reviewed=args.token_overlay_skip_reviewed,
                        skip_queued=args.token_overlay_skip_queued,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_overlay_review_queue: executed")

        if args.mode == "segment-token-overlay-text-decisions":
            import segment_token_overlay_text_fix_decisions

            print("[main] Running stage: segment_token_overlay_text_fix_decisions (segment_token_overlay_text_fix_decisions.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_overlay_text_fix_decisions.main(
                        overlay_run_id=args.token_overlay_run_id,
                        use_active_gate=args.use_active_composite_gate,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_overlay_text_fix_decisions: executed")

        if args.mode == "segment-token-overlay-structural-decisions":
            import segment_token_overlay_structural_fix_decisions

            print("[main] Running stage: segment_token_overlay_structural_fix_decisions (segment_token_overlay_structural_fix_decisions.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_overlay_structural_fix_decisions.main(
                        overlay_run_id=args.token_overlay_run_id,
                        use_active_gate=args.use_active_composite_gate,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_overlay_structural_fix_decisions: executed")

        if args.mode == "segment-token-policy-decisions":
            import segment_token_policy_decisions

            if not args.token_policy_decisions:
                raise RuntimeError("--token-policy-decisions is required for segment-token-policy-decisions.")
            print("[main] Running stage: segment_token_policy_decisions (segment_token_policy_decisions.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_policy_decisions.main(
                        decisions_path=args.token_policy_decisions,
                        policy_run_id=args.token_policy_run_id,
                        source_report=args.token_policy_source_report,
                        reviewer=args.token_policy_reviewer,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- segment_token_policy_decisions: executed")

        if args.mode == "segment-token-policy-decision-rebase":
            import segment_token_policy_decision_rebase

            print("[main] Running stage: segment_token_policy_decision_rebase (segment_token_policy_decision_rebase.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_policy_decision_rebase.main(
                        state_run_id=None,
                        policy_run_id=args.token_policy_run_id,
                        apply=args.auto_apply,
                        reviewer=args.token_policy_reviewer or "codex_token_policy_rebase",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- segment_token_policy_decision_rebase: executed, apply={args.auto_apply}")

        if args.mode == "segment-token-composite-bridge":
            import segment_token_composite_decision_bridge

            print("[main] Running stage: segment_token_composite_decision_bridge (segment_token_composite_decision_bridge.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_composite_decision_bridge.main(
                        state_run_id=None,
                        policy_run_id=args.token_policy_run_id,
                        audit_run_id=args.composite_promotion_audit_run_id,
                        statuses_value=args.composite_guarded_overlay_statuses,
                        buckets_value=args.token_policy_buckets,
                        pending_apply_only=args.token_policy_pending_apply_only,
                        apply=args.auto_apply,
                        reviewer=args.token_policy_reviewer or "codex_composite_bridge",
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append(f"- segment_token_composite_decision_bridge: executed, apply={args.auto_apply}")

        if args.mode == "segment-token-confirmation-fixes":
            import segment_token_policy_confirmation_fixes

            print("[main] Running stage: segment_token_confirmation_fixes (segment_token_policy_confirmation_fixes.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    segment_token_policy_confirmation_fixes.main(
                        policy_run_id=args.token_policy_run_id,
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
            report_lines.append(f"- segment_token_confirmation_fixes: executed, apply={args.auto_apply}")

        if args.mode == "ml-score":
            import ml_score_segments

            print("[main] Running stage: ml_score (ml_score_segments.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_score_segments.main(
                        limit=args.auto_limit,
                        path_like=args.auto_path_like,
                        safe_threshold=args.auto_min_score,
                        include_locked=args.ml_include_locked,
                        batch_size=args.ml_score_batch_size,
                        model_run_id=args.ml_model_run_id,
                        candidate_text_source=args.ml_candidate_text_source,
                        resume_run_id=args.ml_resume_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_score: executed")

        if args.mode == "ml-score-audit":
            import ml_score_audit

            print("[main] Running stage: ml_score_audit (ml_score_audit.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_score_audit.main(sample_limit=args.auto_limit or 25)
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_score_audit: executed")

        if args.mode == "ml-score-regression-queue":
            import ml_score_regression_queue

            print("[main] Running stage: ml_score_regression_queue (ml_score_regression_queue.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    ml_score_regression_queue.main(
                        active_score_run_id=args.ml_active_score_run_id,
                        candidate_score_run_id=args.ml_candidate_score_run_id,
                        limit=args.auto_limit or 120,
                        per_path_limit=args.ml_per_path_limit,
                        batch_size=args.triage_batch_size or 20,
                        clean_only=True,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- ml_score_regression_queue: executed")

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

        if args.mode == "mojibake-strict":
            import strict_mojibake_confirmation_cleanup

            print("[main] Running stage: mojibake_strict (strict_mojibake_confirmation_cleanup.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    strict_mojibake_confirmation_cleanup.main(
                        policy_run_id=args.token_policy_run_id,
                        buckets_csv=args.token_policy_buckets,
                        path_like=args.auto_path_like,
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
            report_lines.append(f"- mojibake_strict: executed, apply={args.auto_apply}")

        if args.mode == "mojibake-curated-decisions":
            import curated_mojibake_policy_decisions

            print("[main] Running stage: mojibake_curated_decisions (curated_mojibake_policy_decisions.py)")
            buffer = io.StringIO()
            tee_stdout = Tee(sys.stdout, buffer)
            tee_stderr = Tee(sys.stderr, buffer)
            try:
                with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
                    curated_mojibake_policy_decisions.main(
                        policy_run_id=args.token_policy_run_id,
                    )
            except Exception:
                traceback.print_exc(file=buffer)
                output = buffer.getvalue()
                log_lines.extend(output.splitlines())
                raise
            output = buffer.getvalue()
            log_lines.extend(output.splitlines())
            report_lines.append("- mojibake_curated_decisions: executed")

        if args.mode == "gender-token-audit":
            run_stage_with_log("gender_token_audit", log_lines)
            report_lines.append("- gender_token_audit: executed")

        if args.mode == "micro-review-queue":
            run_stage_with_log("micro_review_queue", log_lines)
            report_lines.append("- micro_review_queue: executed")

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
