from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_settings() -> dict:
    with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
        settings = json.load(handle)
    return settings


def project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def get_database_path(settings: dict | None = None) -> Path:
    settings = settings or load_settings()
    return project_path(settings["database_path"])


def file_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect(settings: dict | None = None) -> sqlite3.Connection:
    database_path = get_database_path(settings)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 300000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Dashboard readers can briefly hold the database before WAL is active.
        # The longer busy timeout above still protects long pipeline writes.
        pass
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def ensure_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: Iterable[tuple[str, str]],
) -> list[str]:
    existing = table_columns(conn, table_name)
    added: list[str] = []
    for name, definition in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")
            added.append(f"{table_name}.{name}")
    return added


def write_report(settings: dict, script_name: str, lines: list[str]) -> Path:
    reports_dir = project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"{timestamp}_{script_name}.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def get_logs_dir(settings: dict | None = None) -> Path:
    settings = settings or load_settings()
    return project_path(settings.get("logs_dir", "logs"))


def write_log(settings: dict, script_name: str, lines: list[str]) -> Path:
    logs_dir = get_logs_dir(settings)
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"{timestamp}_{script_name}.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def ensure_database(conn: sqlite3.Connection) -> list[str]:
    changes: list[str] = []

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            migration_name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS package_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_number INTEGER NOT NULL UNIQUE,
            version_label TEXT NOT NULL,
            package_name TEXT NOT NULL,
            package_role TEXT NOT NULL,
            parent_version_id INTEGER,
            source_path TEXT NOT NULL,
            package_hash TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            segment_count INTEGER NOT NULL DEFAULT 0,
            measured_score_count INTEGER NOT NULL DEFAULT 0,
            full_average_score REAL,
            change_cohort_score REAL,
            change_cohort_delta REAL,
            changed_from_parent_count INTEGER NOT NULL DEFAULT 0,
            segment_state_run_id INTEGER,
            score_run_id INTEGER,
            score_rule_version TEXT,
            closed_count INTEGER NOT NULL DEFAULT 0,
            pending_count INTEGER NOT NULL DEFAULT 0,
            needs_output_apply_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'frozen',
            metadata_json TEXT,
            frozen_at TEXT NOT NULL,
            FOREIGN KEY(parent_version_id) REFERENCES package_versions(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS package_version_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            text_hash TEXT,
            score REAL,
            score_action TEXT,
            risk_class TEXT,
            token_status TEXT,
            final_state TEXT,
            state_group TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            needs_output_apply INTEGER NOT NULL DEFAULT 0,
            confirmation_locked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(version_id) REFERENCES package_versions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(version_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS package_version_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER NOT NULL,
            parent_version_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            previous_text TEXT,
            current_text TEXT,
            previous_text_hash TEXT,
            current_text_hash TEXT,
            previous_score REAL,
            current_score REAL,
            score_delta REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(version_id) REFERENCES package_versions(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_version_id) REFERENCES package_versions(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(version_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            absolute_path TEXT NOT NULL,
            line_count INTEGER NOT NULL DEFAULT 0,
            file_hash TEXT,
            indexed_at TEXT NOT NULL,
            UNIQUE(package_name, relative_path)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL,
            source_line_number INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            version_index TEXT,
            spanish_text TEXT,
            english_text TEXT,
            old_text TEXT,
            spanish_raw_line TEXT,
            english_raw_line TEXT,
            old_raw_line TEXT,
            spanish_hash TEXT,
            english_hash TEXT,
            old_hash TEXT,
            has_english INTEGER NOT NULL DEFAULT 0,
            has_old INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            first_indexed_at TEXT NOT NULL,
            last_indexed_at TEXT NOT NULL,
            UNIQUE(relative_path, source_line_number, source_key)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS output_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            output_line_number INTEGER,
            portuguese_text TEXT,
            output_raw_line TEXT,
            portuguese_hash TEXT,
            last_indexed_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL UNIQUE,
            confidence_score REAL,
            classification TEXT,
            reasons_json TEXT,
            rule_version TEXT,
            analyzed_at TEXT,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS protected_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL,
            package_name TEXT NOT NULL,
            token TEXT NOT NULL,
            token_type TEXT NOT NULL,
            token_order INTEGER NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER,
            issue_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_segment_id INTEGER,
            source_language TEXT NOT NULL,
            target_language TEXT NOT NULL,
            source_text TEXT NOT NULL,
            target_text TEXT NOT NULL,
            source_hash TEXT,
            target_hash TEXT,
            confidence_score REAL,
            origin TEXT NOT NULL,
            usage_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            FOREIGN KEY(source_segment_id) REFERENCES source_segments(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL,
            suggested_text TEXT NOT NULL,
            suggested_hash TEXT,
            source_language TEXT NOT NULL,
            origin TEXT NOT NULL,
            match_type TEXT NOT NULL,
            match_score REAL NOT NULL,
            token_status TEXT NOT NULL,
            status TEXT NOT NULL,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(segment_id, suggested_hash, source_language, origin, match_type)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suggestion_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id INTEGER,
            segment_id INTEGER NOT NULL,
            decision TEXT NOT NULL DEFAULT 'pending',
            suggested_text TEXT,
            corrected_text TEXT,
            reason TEXT,
            reviewer TEXT,
            reviewed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(suggestion_id) REFERENCES translation_suggestions(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inline_fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL,
            package_name TEXT NOT NULL,
            command_name TEXT,
            argument_index INTEGER NOT NULL,
            fragment_text TEXT NOT NULL,
            fragment_hash TEXT,
            fragment_role TEXT NOT NULL,
            should_translate INTEGER NOT NULL DEFAULT 0,
            suggested_text TEXT,
            confidence_score REAL,
            status TEXT NOT NULL DEFAULT 'indexed',
            reasons_json TEXT,
            indexed_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(segment_id, package_name, command_name, argument_index, fragment_hash)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS glossary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL UNIQUE,
            portuguese_term TEXT NOT NULL,
            category TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_learning_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            limit_count INTEGER NOT NULL,
            auto_confidence_threshold REAL NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            high_confidence_count INTEGER NOT NULL DEFAULT 0,
            pending_human_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            notes TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_learning_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            feedback_id INTEGER,
            suggestion_id INTEGER,
            offline_proposal_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            english_text TEXT,
            spanish_text TEXT,
            old_text TEXT,
            current_output_text TEXT,
            suggested_text TEXT NOT NULL,
            suggested_hash TEXT,
            queue_source TEXT NOT NULL DEFAULT 'pending',
            focus_group TEXT NOT NULL DEFAULT 'all',
            source_language TEXT,
            origin TEXT,
            match_type TEXT,
            match_score REAL,
            token_status TEXT,
            suggestion_status TEXT,
            local_confidence_score REAL NOT NULL,
            local_status TEXT NOT NULL DEFAULT 'pending_human',
            human_label TEXT NOT NULL DEFAULT 'pending',
            corrected_text TEXT,
            reason TEXT,
            reviewer TEXT,
            reviewed_at TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES local_learning_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(feedback_id) REFERENCES suggestion_feedback(id) ON DELETE SET NULL,
            FOREIGN KEY(suggestion_id) REFERENCES translation_suggestions(id) ON DELETE SET NULL,
            FOREIGN KEY(offline_proposal_id) REFERENCES offline_proposals(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(feedback_id, suggested_hash, run_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL UNIQUE,
            confirmation_level TEXT NOT NULL,
            confirmed_text TEXT NOT NULL,
            confirmation_source TEXT NOT NULL,
            confirmation_label TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            confidence_score REAL,
            candidate_id INTEGER,
            feedback_id INTEGER,
            reviewer TEXT,
            confirmed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            FOREIGN KEY(candidate_id) REFERENCES local_learning_candidates(id) ON DELETE SET NULL,
            FOREIGN KEY(feedback_id) REFERENCES suggestion_feedback(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_learning_pattern_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_key TEXT NOT NULL UNIQUE,
            positive_count INTEGER NOT NULL DEFAULT 0,
            near_positive_count INTEGER NOT NULL DEFAULT 0,
            partial_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            harmful_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            weight_adjustment REAL NOT NULL DEFAULT 0,
            last_label TEXT,
            last_candidate_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(last_candidate_id) REFERENCES local_learning_candidates(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS name_equivalences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            portuguese_name TEXT NOT NULL,
            name_family TEXT,
            source_kind TEXT NOT NULL DEFAULT 'character_name',
            status TEXT NOT NULL DEFAULT 'pending',
            confidence_score REAL,
            evidence_count INTEGER NOT NULL DEFAULT 1,
            first_segment_id INTEGER,
            last_segment_id INTEGER,
            reason TEXT,
            reviewer TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(first_segment_id) REFERENCES source_segments(id) ON DELETE SET NULL,
            FOREIGN KEY(last_segment_id) REFERENCES source_segments(id) ON DELETE SET NULL,
            UNIQUE(source_name, portuguese_name, source_kind)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learned_validation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            model_version TEXT,
            path_filter TEXT,
            limit_count INTEGER,
            active_segments INTEGER NOT NULL DEFAULT 0,
            pending_segments INTEGER NOT NULL DEFAULT 0,
            auto_safe_count INTEGER NOT NULL DEFAULT 0,
            auto_safe_audit_count INTEGER NOT NULL DEFAULT 0,
            needs_autofix_count INTEGER NOT NULL DEFAULT 0,
            needs_suggestion_count INTEGER NOT NULL DEFAULT 0,
            needs_human_count INTEGER NOT NULL DEFAULT 0,
            blocked_structure_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learned_validation_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            candidate_source TEXT NOT NULL,
            candidate_text TEXT,
            action TEXT NOT NULL,
            risk_class TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            medium_issue_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            token_status TEXT NOT NULL,
            reasons_json TEXT,
            issues_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES learned_validation_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS title_review_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            english_text TEXT,
            spanish_text TEXT,
            old_text TEXT,
            proposed_text TEXT,
            corrected_text TEXT,
            bucket TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            confidence_score REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            reviewer TEXT,
            reviewed_at TEXT,
            applied_at TEXT,
            apply_result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS package_focus_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            focus_group TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            total_segments INTEGER NOT NULL DEFAULT 0,
            confirmed_segments INTEGER NOT NULL DEFAULT 0,
            pending_segments INTEGER NOT NULL DEFAULT 0,
            human_confirmed_segments INTEGER NOT NULL DEFAULT 0,
            auto_confirmed_segments INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(focus_group, relative_path)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS finalization_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            closure_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            action_hint TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            package_pending INTEGER NOT NULL DEFAULT 0,
            package_total INTEGER NOT NULL DEFAULT 0,
            is_high_impact INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mojibake_context_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            fragment_summary TEXT NOT NULL,
            fragment_count INTEGER NOT NULL DEFAULT 0,
            residue_kind TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            english_text TEXT,
            spanish_text TEXT,
            old_text TEXT,
            confirmed_text TEXT NOT NULL,
            confirmation_level TEXT,
            confirmation_source TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_proposal_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            model_version TEXT NOT NULL,
            path_filter TEXT,
            limit_count INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            proposed_count INTEGER NOT NULL DEFAULT 0,
            auto_ready_count INTEGER NOT NULL DEFAULT 0,
            needs_review_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            candidate_bucket TEXT NOT NULL,
            proposal_source TEXT NOT NULL,
            original_text TEXT,
            proposed_text TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            status TEXT NOT NULL,
            token_status TEXT NOT NULL,
            issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            medium_issue_count INTEGER NOT NULL DEFAULT 0,
            rules_json TEXT,
            reasons_json TEXT,
            issues_json TEXT,
            applied_at TEXT,
            apply_result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES offline_proposal_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_dataset_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            source_scope TEXT NOT NULL,
            limit_count INTEGER,
            positive_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            neutral_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            strong_positive_count INTEGER NOT NULL DEFAULT 0,
            strong_negative_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_training_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            english_text TEXT,
            spanish_text TEXT,
            old_text TEXT,
            output_text TEXT,
            candidate_text TEXT,
            final_text TEXT,
            label TEXT NOT NULL,
            action_label TEXT NOT NULL,
            issue_label TEXT,
            trust_level INTEGER NOT NULL DEFAULT 0,
            evidence_source TEXT NOT NULL,
            evidence_id INTEGER,
            confidence_score REAL,
            locked INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            has_english INTEGER NOT NULL DEFAULT 0,
            has_old INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_dataset_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, evidence_source, evidence_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_model_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            model_version TEXT NOT NULL,
            model_kind TEXT NOT NULL,
            dataset_run_id INTEGER NOT NULL,
            model_path TEXT,
            training_examples INTEGER NOT NULL DEFAULT 0,
            test_examples INTEGER NOT NULL DEFAULT 0,
            safe_threshold REAL NOT NULL DEFAULT 0.90,
            accuracy REAL,
            macro_f1 REAL,
            predicted_safe_count INTEGER NOT NULL DEFAULT 0,
            false_safe_count INTEGER NOT NULL DEFAULT 0,
            false_safe_rate REAL,
            safe_precision REAL,
            safe_recall REAL,
            metrics_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(dataset_run_id) REFERENCES ml_dataset_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_score_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            model_run_id INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            path_filter TEXT,
            limit_count INTEGER,
            scored_count INTEGER NOT NULL DEFAULT 0,
            model_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            model_direct_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            deterministic_promoted_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            deterministic_demoted_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            final_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            needs_human_count INTEGER NOT NULL DEFAULT 0,
            needs_autofix_count INTEGER NOT NULL DEFAULT 0,
            blocked_structure_count INTEGER NOT NULL DEFAULT 0,
            deterministic_block_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(model_run_id) REFERENCES ml_model_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_holdout_eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            dataset_run_id INTEGER NOT NULL,
            safe_threshold REAL NOT NULL DEFAULT 0,
            target_ratio REAL NOT NULL DEFAULT 0,
            min_negative INTEGER NOT NULL DEFAULT 0,
            max_paths INTEGER,
            safe_multiplier INTEGER NOT NULL DEFAULT 0,
            feature_set TEXT,
            train_strategy TEXT,
            holdout_path_count INTEGER NOT NULL DEFAULT 0,
            holdout_examples INTEGER NOT NULL DEFAULT 0,
            predicted_safe_count INTEGER NOT NULL DEFAULT 0,
            false_safe_count INTEGER NOT NULL DEFAULT 0,
            false_safe_rate REAL,
            unique_predicted_safe_count INTEGER NOT NULL DEFAULT 0,
            unique_false_safe_count INTEGER NOT NULL DEFAULT 0,
            safe_precision REAL,
            safe_recall REAL,
            accuracy REAL,
            macro_f1 REAL,
            report_path TEXT,
            metrics_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(dataset_run_id) REFERENCES ml_dataset_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_score_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            candidate_text TEXT,
            model_action TEXT NOT NULL,
            final_action TEXT NOT NULL,
            risk_class TEXT NOT NULL,
            model_safe_probability REAL,
            model_confidence REAL,
            token_status TEXT NOT NULL,
            issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            medium_issue_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            deterministic_blocked INTEGER NOT NULL DEFAULT 0,
            confirmation_level TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            issues_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_score_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_model_registry (
            model_kind TEXT PRIMARY KEY,
            active_model_run_id INTEGER NOT NULL,
            active_model_version TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            promoted_at TEXT NOT NULL,
            promoted_by TEXT,
            reason TEXT,
            metrics_json TEXT,
            FOREIGN KEY(active_model_run_id) REFERENCES ml_model_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_model_promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_kind TEXT NOT NULL,
            candidate_model_run_id INTEGER NOT NULL,
            previous_model_run_id INTEGER,
            decision TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            reason TEXT,
            metrics_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(candidate_model_run_id) REFERENCES ml_model_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(previous_model_run_id) REFERENCES ml_model_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_policy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            score_run_id INTEGER NOT NULL,
            model_run_id INTEGER NOT NULL,
            model_version TEXT NOT NULL,
            scored_count INTEGER NOT NULL DEFAULT 0,
            active_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            policy_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            new_safe_count INTEGER NOT NULL DEFAULT 0,
            demoted_safe_count INTEGER NOT NULL DEFAULT 0,
            protect_active_safe INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(score_run_id) REFERENCES ml_score_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(model_run_id) REFERENCES ml_model_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_policy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            score_item_id INTEGER NOT NULL,
            score_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            policy_group TEXT NOT NULL,
            policy_threshold REAL NOT NULL,
            policy_require_learned_positive INTEGER NOT NULL DEFAULT 0,
            score_final_action TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            new_safe INTEGER NOT NULL DEFAULT 0,
            demoted_safe INTEGER NOT NULL DEFAULT 0,
            learned_positive INTEGER NOT NULL DEFAULT 0,
            learned_negative INTEGER NOT NULL DEFAULT 0,
            model_safe_probability REAL,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(score_item_id) REFERENCES ml_score_items(id) ON DELETE CASCADE,
            FOREIGN KEY(score_run_id) REFERENCES ml_score_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_specialist_policy_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            general_score_run_id INTEGER,
            specialist_key TEXT NOT NULL,
            model_kind TEXT NOT NULL,
            model_run_id INTEGER,
            model_version TEXT,
            dataset_run_id INTEGER,
            score_run_id INTEGER,
            operational_threshold REAL,
            policy_min_threshold REAL,
            threshold_below_policy INTEGER NOT NULL DEFAULT 0,
            scope_description TEXT,
            scope_sql TEXT,
            scope_active_count INTEGER NOT NULL DEFAULT 0,
            scored_count INTEGER NOT NULL DEFAULT 0,
            compared_count INTEGER NOT NULL DEFAULT 0,
            scope_delta_count INTEGER NOT NULL DEFAULT 0,
            scope_coverage_rate REAL,
            final_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            needs_human_count INTEGER NOT NULL DEFAULT 0,
            needs_autofix_count INTEGER NOT NULL DEFAULT 0,
            blocked_structure_count INTEGER NOT NULL DEFAULT 0,
            model_direct_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            deterministic_promoted_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            deterministic_demoted_auto_safe_count INTEGER NOT NULL DEFAULT 0,
            final_auto_safe_rate REAL,
            model_direct_auto_safe_rate REAL,
            specialist_new_safe_count INTEGER NOT NULL DEFAULT 0,
            specialist_demoted_count INTEGER NOT NULL DEFAULT 0,
            pending_new_safe_count INTEGER NOT NULL DEFAULT 0,
            pending_demoted_count INTEGER NOT NULL DEFAULT 0,
            reviewed_new_safe_count INTEGER NOT NULL DEFAULT 0,
            reviewed_demoted_count INTEGER NOT NULL DEFAULT 0,
            divergent_count INTEGER NOT NULL DEFAULT 0,
            reviewed_divergent_count INTEGER NOT NULL DEFAULT 0,
            pending_real_count INTEGER NOT NULL DEFAULT 0,
            missing_general_count INTEGER NOT NULL DEFAULT 0,
            divergence_rate REAL,
            pending_real_rate REAL,
            status TEXT NOT NULL,
            recommended_action TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(general_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(model_run_id) REFERENCES ml_model_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(dataset_run_id) REFERENCES ml_dataset_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_agent_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_key TEXT NOT NULL UNIQUE,
            agent_type TEXT NOT NULL,
            parent_agent_key TEXT,
            model_kind TEXT,
            status TEXT NOT NULL DEFAULT 'planned',
            operational_state TEXT NOT NULL DEFAULT 'experimental',
            decision_role TEXT NOT NULL DEFAULT 'vote',
            scope_group TEXT,
            scope_sql TEXT,
            scope_description TEXT,
            default_threshold REAL,
            priority INTEGER NOT NULL DEFAULT 100,
            dashboard_group TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notes_json TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_agent_routing_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            general_score_run_id INTEGER,
            policy_run_id INTEGER,
            coordinator_key TEXT NOT NULL,
            agents_considered_count INTEGER NOT NULL DEFAULT 0,
            segments_scanned_count INTEGER NOT NULL DEFAULT 0,
            routed_count INTEGER NOT NULL DEFAULT 0,
            active_agent_covered_count INTEGER NOT NULL DEFAULT 0,
            planned_agent_covered_count INTEGER NOT NULL DEFAULT 0,
            missing_agent_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            recommendation_count INTEGER NOT NULL DEFAULT 0,
            notes_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(general_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(policy_run_id) REFERENCES ml_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_agent_routing_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            route_agent_key TEXT,
            route_agent_type TEXT,
            route_status TEXT NOT NULL DEFAULT 'candidate',
            route_confidence REAL,
            route_reason TEXT,
            issue_family TEXT,
            general_action TEXT,
            policy_action TEXT,
            specialist_action TEXT,
            recommendation_key TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_agent_routing_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id, route_agent_key)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_agent_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            proposed_agent_key TEXT NOT NULL,
            parent_agent_key TEXT,
            recommendation_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            reason TEXT,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            positive_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            corrected_count INTEGER NOT NULL DEFAULT 0,
            sample_segments_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_agent_routing_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_ledger_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            active_score_run_id INTEGER,
            candidate_score_run_id INTEGER,
            policy_run_id INTEGER,
            source_scope TEXT NOT NULL DEFAULT 'pending_segment_state',
            pending_segments_count INTEGER NOT NULL DEFAULT 0,
            ledger_segment_count INTEGER NOT NULL DEFAULT 0,
            ledger_item_count INTEGER NOT NULL DEFAULT 0,
            actionable_item_count INTEGER NOT NULL DEFAULT 0,
            blocked_item_count INTEGER NOT NULL DEFAULT 0,
            primary_family_counts_json TEXT,
            family_counts_json TEXT,
            agent_counts_json TEXT,
            notes_json TEXT,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(segment_state_run_id) REFERENCES segment_state_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(active_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(candidate_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(policy_run_id) REFERENCES ml_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_ledger_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            final_state TEXT,
            state_group TEXT,
            active_action TEXT,
            candidate_action TEXT,
            policy_action TEXT,
            confirmation_level TEXT,
            confirmation_label TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            issue_role TEXT NOT NULL DEFAULT 'diagnostic',
            issue_severity TEXT NOT NULL DEFAULT 'medium',
            agent_key TEXT NOT NULL,
            route_status TEXT NOT NULL DEFAULT 'candidate',
            proposed_action TEXT NOT NULL,
            proposed_repair_text TEXT,
            token_impact TEXT NOT NULL DEFAULT 'unknown',
            token_status TEXT,
            confidence_score REAL,
            evidence_text TEXT,
            evidence_json TEXT,
            validation_status TEXT NOT NULL DEFAULT 'not_validated',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_ledger_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            issue_family TEXT,
            queue_strategy TEXT NOT NULL,
            limit_count INTEGER,
            per_bucket INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(ledger_run_id) REFERENCES ml_issue_ledger_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            suggested_decision TEXT NOT NULL,
            evidence_text TEXT,
            evidence_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            confirmed_text TEXT,
            reviewer_decision TEXT,
            reviewer_notes TEXT,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY(run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_run_id) REFERENCES ml_issue_ledger_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, ledger_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_decision_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            queue_run_id INTEGER,
            agent_key TEXT,
            decisions_path TEXT NOT NULL,
            source_report TEXT,
            reviewer TEXT,
            total_rows INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            invalid_count INTEGER NOT NULL DEFAULT 0,
            safe_count INTEGER NOT NULL DEFAULT 0,
            false_positive_count INTEGER NOT NULL DEFAULT 0,
            repair_count INTEGER NOT NULL DEFAULT 0,
            context_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            new_microagent_count INTEGER NOT NULL DEFAULT 0,
            evidence_counts_json TEXT,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            suggested_decision TEXT,
            reviewer_decision TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            corrected_text TEXT,
            notes TEXT,
            reviewer TEXT,
            valid INTEGER NOT NULL DEFAULT 1,
            validation_status TEXT NOT NULL DEFAULT 'accepted',
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_run_id) REFERENCES ml_issue_ledger_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(queue_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_subpolicy_shadow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER,
            queue_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            boundary_count INTEGER NOT NULL DEFAULT 0,
            context_count INTEGER NOT NULL DEFAULT 0,
            positive_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            blocker_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_subpolicy_shadow_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            block_reason TEXT,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            evidence_json TEXT,
            notes TEXT,
            current_confirmed_text_hash TEXT,
            queue_confirmed_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, decision_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_boundary_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER,
            queue_run_id INTEGER,
            min_boundary_required INTEGER NOT NULL DEFAULT 0,
            max_blocked_allowed INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            boundary_candidate_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            select_cstring_count INTEGER NOT NULL DEFAULT 0,
            extra_prefix_count INTEGER NOT NULL DEFAULT 0,
            mismatch_count INTEGER NOT NULL DEFAULT 0,
            visible_residual_count INTEGER NOT NULL DEFAULT 0,
            surface_boundary_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(shadow_run_id) REFERENCES ml_issue_gender_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_boundary_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            shadow_confirmed_text_hash TEXT,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_gender_boundary_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_run_id) REFERENCES ml_issue_gender_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_item_id) REFERENCES ml_issue_gender_subpolicy_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, shadow_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_boundary_lifecycle_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_gender_boundary_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_boundary_lifecycle_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            checkpoint_confirmed_text_hash TEXT,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_boundary_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_gender_boundary_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_item_id) REFERENCES ml_issue_gender_boundary_checkpoint_items(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_run_id) REFERENCES ml_issue_gender_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_item_id) REFERENCES ml_issue_gender_subpolicy_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, checkpoint_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_trigger_gender_role_surface_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER,
            queue_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            kinship_count INTEGER NOT NULL DEFAULT 0,
            role_article_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            blocker_counts_json TEXT,
            analytics_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_trigger_gender_role_surface_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            block_reason TEXT,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            evidence_json TEXT,
            notes TEXT,
            current_confirmed_text_hash TEXT,
            queue_confirmed_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_trigger_gender_role_surface_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, decision_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_trigger_gender_role_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER,
            queue_run_id INTEGER,
            min_ready_required INTEGER NOT NULL DEFAULT 0,
            max_blocked_allowed INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            kinship_count INTEGER NOT NULL DEFAULT 0,
            role_article_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(shadow_run_id) REFERENCES ml_issue_trigger_gender_role_surface_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_trigger_gender_role_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            shadow_confirmed_text_hash TEXT,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_trigger_gender_role_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_run_id) REFERENCES ml_issue_trigger_gender_role_surface_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_item_id) REFERENCES ml_issue_trigger_gender_role_surface_items(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, shadow_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_trigger_gender_role_lifecycle_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_count INTEGER NOT NULL DEFAULT 0,
            kinship_count INTEGER NOT NULL DEFAULT 0,
            role_article_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_trigger_gender_role_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_trigger_gender_role_lifecycle_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            checkpoint_confirmed_text_hash TEXT,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_trigger_gender_role_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_trigger_gender_role_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_item_id) REFERENCES ml_issue_trigger_gender_role_checkpoint_items(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_run_id) REFERENCES ml_issue_trigger_gender_role_surface_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_item_id) REFERENCES ml_issue_trigger_gender_role_surface_items(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, checkpoint_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_release_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER,
            queue_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_shadow_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            safe_short_label_count INTEGER NOT NULL DEFAULT 0,
            false_positive_reopen_count INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_release_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            state_run_id INTEGER,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            final_state TEXT,
            review_state TEXT,
            apply_state TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            needs_human INTEGER NOT NULL DEFAULT 0,
            locked INTEGER NOT NULL DEFAULT 0,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            queue_confirmed_text_hash TEXT,
            current_confirmed_text_hash TEXT,
            evidence_json TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_release_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_release_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            release_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER,
            queue_run_id INTEGER,
            min_released_required INTEGER NOT NULL DEFAULT 0,
            max_blocked_allowed INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            release_allowed_count INTEGER NOT NULL DEFAULT 0,
            release_blocked_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            safe_short_label_count INTEGER NOT NULL DEFAULT 0,
            false_positive_reopen_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(release_run_id) REFERENCES ml_issue_short_label_release_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_release_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            release_run_id INTEGER NOT NULL,
            release_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            state_run_id INTEGER,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            release_policy_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            final_state TEXT,
            review_state TEXT,
            apply_state TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            needs_human INTEGER NOT NULL DEFAULT 0,
            locked INTEGER NOT NULL DEFAULT 0,
            token_impact TEXT,
            token_status TEXT,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            release_confirmed_text_hash TEXT,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_release_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(release_run_id) REFERENCES ml_issue_short_label_release_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(release_item_id) REFERENCES ml_issue_short_label_release_items(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, release_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_partial_coverage_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            total_segments_with_issues INTEGER NOT NULL DEFAULT 0,
            total_issue_items INTEGER NOT NULL DEFAULT 0,
            covered_issue_items INTEGER NOT NULL DEFAULT 0,
            reviewed_issue_items INTEGER NOT NULL DEFAULT 0,
            blocked_issue_items INTEGER NOT NULL DEFAULT 0,
            open_issue_items INTEGER NOT NULL DEFAULT 0,
            fully_covered_segments INTEGER NOT NULL DEFAULT 0,
            partially_covered_segments INTEGER NOT NULL DEFAULT 0,
            uncovered_segments INTEGER NOT NULL DEFAULT 0,
            reviewed_segments INTEGER NOT NULL DEFAULT 0,
            closed_segments INTEGER NOT NULL DEFAULT 0,
            multi_issue_segments INTEGER NOT NULL DEFAULT 0,
            partially_covered_multi_issue_segments INTEGER NOT NULL DEFAULT 0,
            avg_coverage_ratio REAL NOT NULL DEFAULT 0,
            weighted_coverage_ratio REAL NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            family_counts_json TEXT,
            agent_counts_json TEXT,
            source_counts_json TEXT,
            notes_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(ledger_run_id) REFERENCES ml_issue_ledger_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_state_run_id) REFERENCES segment_state_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_partial_coverage_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            final_state TEXT,
            state_group TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            reviewed_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            coverage_ratio REAL NOT NULL DEFAULT 0,
            coverage_state TEXT NOT NULL,
            review_state TEXT NOT NULL,
            issue_families_json TEXT,
            covered_families_json TEXT,
            open_families_json TEXT,
            covered_agents_json TEXT,
            coverage_sources_json TEXT,
            reviewed_decisions_json TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_partial_coverage_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_run_id) REFERENCES ml_issue_ledger_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_state_run_id) REFERENCES segment_state_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_state_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            active_score_run_id INTEGER,
            candidate_score_run_id INTEGER,
            policy_run_id INTEGER,
            total_segments INTEGER NOT NULL DEFAULT 0,
            closed_count INTEGER NOT NULL DEFAULT 0,
            pending_count INTEGER NOT NULL DEFAULT 0,
            output_apply_pending_count INTEGER NOT NULL DEFAULT 0,
            blank_valid_count INTEGER NOT NULL DEFAULT 0,
            experimental_watch_count INTEGER NOT NULL DEFAULT 0,
            reopen_count INTEGER NOT NULL DEFAULT 0,
            notes_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(active_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(candidate_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(policy_run_id) REFERENCES ml_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_state_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            final_state TEXT NOT NULL,
            state_group TEXT NOT NULL,
            output_state TEXT NOT NULL,
            review_state TEXT NOT NULL,
            apply_state TEXT NOT NULL,
            active_action TEXT,
            candidate_action TEXT,
            policy_action TEXT,
            lifecycle_policy_action TEXT,
            lifecycle_policy_allowed INTEGER NOT NULL DEFAULT 0,
            confirmation_level TEXT,
            confirmation_label TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            has_output INTEGER NOT NULL DEFAULT 0,
            source_blank INTEGER NOT NULL DEFAULT 0,
            confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            needs_human INTEGER NOT NULL DEFAULT 0,
            needs_output_apply INTEGER NOT NULL DEFAULT 0,
            needs_reopen INTEGER NOT NULL DEFAULT 0,
            is_closed INTEGER NOT NULL DEFAULT 0,
            priority_score REAL NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_state_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_output_apply_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            state_run_id INTEGER,
            apply INTEGER NOT NULL DEFAULT 0,
            limit_count INTEGER,
            path_filter TEXT,
            review_states TEXT,
            include_auto_confirmed INTEGER NOT NULL DEFAULT 0,
            allow_locked_token_override INTEGER NOT NULL DEFAULT 0,
            require_token_policy_decision INTEGER NOT NULL DEFAULT 0,
            token_policy_run_id INTEGER,
            candidates_inspected INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            applied_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            token_mismatch_count INTEGER NOT NULL DEFAULT 0,
            files_touched_count INTEGER NOT NULL DEFAULT 0,
            backup_root TEXT,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_output_apply_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            state_run_id INTEGER,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            output_line_number INTEGER,
            final_state TEXT,
            review_state TEXT,
            result_status TEXT NOT NULL,
            applied INTEGER NOT NULL DEFAULT 0,
            token_mismatch INTEGER NOT NULL DEFAULT 0,
            previous_text_hash TEXT,
            confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_output_apply_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            state_run_id INTEGER,
            active_score_run_id INTEGER,
            candidate_score_run_id INTEGER,
            total_reopen_count INTEGER NOT NULL DEFAULT 0,
            mechanical_formatting_count INTEGER NOT NULL DEFAULT 0,
            mechanical_text_replacement_count INTEGER NOT NULL DEFAULT 0,
            weak_auto_count INTEGER NOT NULL DEFAULT 0,
            human_curated_count INTEGER NOT NULL DEFAULT 0,
            other_count INTEGER NOT NULL DEFAULT 0,
            recommended_mechanical_review_count INTEGER NOT NULL DEFAULT 0,
            exact_output_match_count INTEGER NOT NULL DEFAULT 0,
            normalized_output_match_count INTEGER NOT NULL DEFAULT 0,
            output_delta_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(active_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(candidate_score_run_id) REFERENCES ml_score_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            state_run_id INTEGER,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            confirmation_level TEXT,
            confirmation_label TEXT,
            label_family TEXT NOT NULL,
            candidate_action TEXT,
            active_action TEXT,
            policy_action TEXT,
            token_status TEXT,
            risk_class TEXT,
            word_count INTEGER NOT NULL DEFAULT 0,
            model_safe_probability REAL,
            model_confidence REAL,
            issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            recommendation TEXT NOT NULL,
            review_priority REAL NOT NULL DEFAULT 0,
            state_confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            exact_confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            normalized_confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            output_match_kind TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_audit_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_guarded_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            audit_run_id INTEGER,
            label_family TEXT NOT NULL,
            recommendation_filter TEXT,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            guarded_policy_candidate_count INTEGER NOT NULL DEFAULT 0,
            manual_boundary_count INTEGER NOT NULL DEFAULT 0,
            exact_output_match_count INTEGER NOT NULL DEFAULT 0,
            display_equivalent_count INTEGER NOT NULL DEFAULT 0,
            text_delta_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(audit_run_id) REFERENCES auto_confirmation_reopen_audit_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_guarded_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            audit_run_id INTEGER,
            audit_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            confirmation_label TEXT,
            label_family TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            suggested_decision TEXT NOT NULL,
            policy_candidate INTEGER NOT NULL DEFAULT 0,
            manual_boundary INTEGER NOT NULL DEFAULT 0,
            output_match_kind TEXT,
            token_status TEXT,
            issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            model_safe_probability REAL,
            model_confidence REAL,
            review_priority REAL NOT NULL DEFAULT 0,
            queue_rank INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_guarded_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(audit_run_id) REFERENCES auto_confirmation_reopen_audit_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_item_id) REFERENCES auto_confirmation_reopen_audit_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_lifecycle_policy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            queue_run_id INTEGER,
            audit_run_id INTEGER,
            policy_name TEXT NOT NULL,
            label_family TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            manual_boundary_count INTEGER NOT NULL DEFAULT 0,
            invalid_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES auto_confirmation_reopen_guarded_queue_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_run_id) REFERENCES auto_confirmation_reopen_audit_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_lifecycle_policy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER,
            queue_item_id INTEGER,
            audit_run_id INTEGER,
            audit_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            label_family TEXT NOT NULL,
            confirmation_label TEXT,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            output_match_kind TEXT,
            token_status TEXT,
            issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            model_safe_probability REAL,
            review_priority REAL NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_lifecycle_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_run_id) REFERENCES auto_confirmation_reopen_guarded_queue_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_item_id) REFERENCES auto_confirmation_reopen_guarded_queue_items(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_run_id) REFERENCES auto_confirmation_reopen_audit_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_item_id) REFERENCES auto_confirmation_reopen_audit_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_diagnostic_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            queue_run_id INTEGER,
            audit_run_id INTEGER,
            label_family TEXT NOT NULL DEFAULT 'mechanical_text_replacement',
            total_items INTEGER NOT NULL DEFAULT 0,
            subfamily_count INTEGER NOT NULL DEFAULT 0,
            needs_subagent_count INTEGER NOT NULL DEFAULT 0,
            candidate_short_policy_count INTEGER NOT NULL DEFAULT 0,
            manual_boundary_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES auto_confirmation_reopen_guarded_queue_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_run_id) REFERENCES auto_confirmation_reopen_audit_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_diagnostic_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER,
            queue_item_id INTEGER,
            audit_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            confirmation_label TEXT,
            text_subfamily TEXT NOT NULL,
            suggested_agent_key TEXT,
            recommendation TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            select_cstring_count INTEGER NOT NULL DEFAULT 0,
            concept_link_count INTEGER NOT NULL DEFAULT 0,
            spanish_literal_hint_count INTEGER NOT NULL DEFAULT 0,
            ui_short_text INTEGER NOT NULL DEFAULT 0,
            issue_count INTEGER NOT NULL DEFAULT 0,
            model_safe_probability REAL,
            review_priority REAL NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_run_id) REFERENCES auto_confirmation_reopen_guarded_queue_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_item_id) REFERENCES auto_confirmation_reopen_guarded_queue_items(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_item_id) REFERENCES auto_confirmation_reopen_audit_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_review_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            diagnostic_run_id INTEGER,
            agent_key TEXT NOT NULL,
            text_subfamily TEXT,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            high_risk_count INTEGER NOT NULL DEFAULT 0,
            medium_risk_count INTEGER NOT NULL DEFAULT 0,
            skipped_previously_queued_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_review_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER,
            diagnostic_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            confirmation_label TEXT,
            text_subfamily TEXT NOT NULL,
            suggested_agent_key TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            select_cstring_count INTEGER NOT NULL DEFAULT 0,
            spanish_literal_hint_count INTEGER NOT NULL DEFAULT 0,
            issue_count INTEGER NOT NULL DEFAULT 0,
            model_safe_probability REAL,
            review_priority REAL NOT NULL DEFAULT 0,
            queue_rank INTEGER NOT NULL DEFAULT 0,
            suggested_review_label TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_review_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(diagnostic_item_id) REFERENCES auto_confirmation_reopen_text_diagnostic_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_review_decision_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            queue_run_id INTEGER,
            agent_key TEXT,
            decisions_path TEXT NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            positive_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            needs_more_context_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES auto_confirmation_reopen_text_review_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER,
            queue_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER,
            diagnostic_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            text_subfamily TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            corrected_text TEXT,
            notes TEXT,
            reviewer TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_review_decision_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_run_id) REFERENCES auto_confirmation_reopen_text_review_queue_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_item_id) REFERENCES auto_confirmation_reopen_text_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(diagnostic_item_id) REFERENCES auto_confirmation_reopen_text_diagnostic_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(queue_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_specialist_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            min_reviewed INTEGER NOT NULL DEFAULT 0,
            max_negative_rate_for_release REAL NOT NULL DEFAULT 0,
            total_agents INTEGER NOT NULL DEFAULT 0,
            no_evidence_count INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            ready_for_shadow_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_specialist_audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            text_subfamily TEXT NOT NULL,
            diagnostic_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            positive_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            needs_more_context_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            high_risk_reviewed_count INTEGER NOT NULL DEFAULT 0,
            negative_rate REAL NOT NULL DEFAULT 0,
            maturity_status TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_specialist_audit_runs(id) ON DELETE CASCADE,
            UNIQUE(run_id, agent_key, text_subfamily)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_shadow_policy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            parent_agent_key TEXT,
            diagnostic_run_id INTEGER,
            specialist_audit_run_id INTEGER,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            positive_evidence_count INTEGER NOT NULL DEFAULT 0,
            negative_evidence_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(specialist_audit_run_id) REFERENCES auto_confirmation_reopen_text_specialist_audit_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_shadow_policy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER,
            diagnostic_item_id INTEGER,
            review_decision_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            text_subfamily TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            block_reason TEXT,
            pattern_match INTEGER NOT NULL DEFAULT 0,
            positive_evidence INTEGER NOT NULL DEFAULT 0,
            negative_evidence INTEGER NOT NULL DEFAULT 0,
            issue_count INTEGER NOT NULL DEFAULT 0,
            select_cstring_count INTEGER NOT NULL DEFAULT 0,
            concept_link_count INTEGER NOT NULL DEFAULT 0,
            spanish_literal_hint_count INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_shadow_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(diagnostic_item_id) REFERENCES auto_confirmation_reopen_text_diagnostic_items(id) ON DELETE SET NULL,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, diagnostic_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_policy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            total_candidates INTEGER NOT NULL DEFAULT 0,
            repair_candidate_count INTEGER NOT NULL DEFAULT 0,
            block_only_count INTEGER NOT NULL DEFAULT 0,
            same_token_count INTEGER NOT NULL DEFAULT 0,
            token_change_count INTEGER NOT NULL DEFAULT 0,
            unclassified_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_policy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER,
            diagnostic_item_id INTEGER,
            queue_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            source_agent_key TEXT NOT NULL,
            source_text_subfamily TEXT NOT NULL,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            boundary_status TEXT NOT NULL,
            boundary_action TEXT NOT NULL,
            token_status TEXT NOT NULL,
            block_reason TEXT,
            issue_count INTEGER NOT NULL DEFAULT 0,
            select_cstring_count INTEGER NOT NULL DEFAULT 0,
            concept_link_count INTEGER NOT NULL DEFAULT 0,
            spanish_literal_hint_count INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(diagnostic_item_id) REFERENCES auto_confirmation_reopen_text_diagnostic_items(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_item_id) REFERENCES auto_confirmation_reopen_text_review_queue_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, review_decision_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            boundary_policy_run_id INTEGER NOT NULL,
            queue_scope TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            same_token_count INTEGER NOT NULL DEFAULT 0,
            token_change_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(boundary_policy_run_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            boundary_policy_run_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER,
            diagnostic_item_id INTEGER,
            queue_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            boundary_status TEXT NOT NULL,
            token_status TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            queue_rank INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_run_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(diagnostic_item_id) REFERENCES auto_confirmation_reopen_text_diagnostic_items(id) ON DELETE SET NULL,
            FOREIGN KEY(queue_item_id) REFERENCES auto_confirmation_reopen_text_review_queue_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, boundary_policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_shadow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            repair_queue_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            total_candidates INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            no_text_delta_count INTEGER NOT NULL DEFAULT 0,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            same_token_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(repair_queue_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_shadow_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            repair_queue_run_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            token_status TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            block_reason TEXT,
            text_delta_kind TEXT NOT NULL,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            validation_issues_json TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, repair_queue_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            repair_shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            min_ready_required INTEGER NOT NULL DEFAULT 0,
            max_blocked_allowed INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            no_text_delta_count INTEGER NOT NULL DEFAULT 0,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(repair_shadow_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            repair_shadow_run_id INTEGER NOT NULL,
            repair_shadow_item_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_shadow_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_shadow_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, repair_shadow_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_lifecycle_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_lifecycle_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            repair_shadow_item_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_checkpoint_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_shadow_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, checkpoint_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_policy_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            repair_queue_run_id INTEGER NOT NULL,
            bridge_name TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            token_change_count INTEGER NOT NULL DEFAULT 0,
            same_token_count INTEGER NOT NULL DEFAULT 0,
            review_required_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            critical_count INTEGER NOT NULL DEFAULT 0,
            high_count INTEGER NOT NULL DEFAULT 0,
            medium_count INTEGER NOT NULL DEFAULT 0,
            low_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(repair_queue_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_policy_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            repair_queue_run_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            bridge_action TEXT NOT NULL,
            diff_kind TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            missing_tokens_json TEXT,
            extra_tokens_json TEXT,
            issue_flags_json TEXT,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, repair_queue_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_run_id INTEGER NOT NULL,
            subpolicy_name TEXT NOT NULL,
            subpolicy_status TEXT NOT NULL DEFAULT 'shadow',
            total_candidates INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(bridge_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            bridge_run_id INTEGER NOT NULL,
            bridge_item_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            subpolicy_status TEXT NOT NULL,
            subpolicy_action TEXT NOT NULL,
            block_reason TEXT,
            evidence_json TEXT,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(bridge_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(bridge_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, bridge_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            subpolicy_shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            subpolicy_status TEXT NOT NULL,
            min_ready_required INTEGER NOT NULL DEFAULT 0,
            max_blocked_allowed INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(subpolicy_shadow_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            subpolicy_shadow_run_id INTEGER NOT NULL,
            subpolicy_shadow_item_id INTEGER NOT NULL,
            bridge_run_id INTEGER NOT NULL,
            bridge_item_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            subpolicy_status TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(subpolicy_shadow_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(subpolicy_shadow_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(bridge_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(bridge_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, subpolicy_shadow_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_run_ids_json TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            subpolicy_shadow_run_id INTEGER NOT NULL,
            subpolicy_shadow_item_id INTEGER NOT NULL,
            bridge_run_id INTEGER NOT NULL,
            bridge_item_id INTEGER NOT NULL,
            repair_queue_item_id INTEGER NOT NULL,
            boundary_policy_item_id INTEGER NOT NULL,
            review_decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items(id) ON DELETE CASCADE,
            FOREIGN KEY(subpolicy_shadow_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(subpolicy_shadow_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(bridge_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(bridge_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_policy_bridge_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_queue_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(boundary_policy_item_id) REFERENCES auto_confirmation_reopen_text_boundary_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, checkpoint_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            lifecycle_run_id INTEGER NOT NULL,
            state_run_id INTEGER,
            audit_name TEXT NOT NULL,
            audit_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            policy_allowed_count INTEGER NOT NULL DEFAULT 0,
            output_delta_count INTEGER NOT NULL DEFAULT 0,
            confirmation_delta_count INTEGER NOT NULL DEFAULT 0,
            confirmation_matches_output_count INTEGER NOT NULL DEFAULT 0,
            current_pending_count INTEGER NOT NULL DEFAULT 0,
            current_closed_count INTEGER NOT NULL DEFAULT 0,
            requires_confirmation_promotion_count INTEGER NOT NULL DEFAULT 0,
            requires_output_apply_count INTEGER NOT NULL DEFAULT 0,
            requires_segment_state_lifecycle_integration_count INTEGER NOT NULL DEFAULT 0,
            eligible_controlled_production_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(lifecycle_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            lifecycle_run_id INTEGER NOT NULL,
            lifecycle_item_id INTEGER NOT NULL,
            state_run_id INTEGER,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            subpolicy_name TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            current_final_state TEXT,
            current_apply_state TEXT,
            current_state_group TEXT,
            current_review_state TEXT,
            current_lifecycle_policy_allowed INTEGER NOT NULL DEFAULT 0,
            corrected_matches_output INTEGER NOT NULL DEFAULT 0,
            corrected_matches_confirmation INTEGER NOT NULL DEFAULT 0,
            confirmation_matches_output INTEGER NOT NULL DEFAULT 0,
            requires_confirmation_promotion INTEGER NOT NULL DEFAULT 0,
            requires_output_apply INTEGER NOT NULL DEFAULT 0,
            requires_segment_state_lifecycle_integration INTEGER NOT NULL DEFAULT 0,
            eligible_controlled_production INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            output_text_hash TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(lifecycle_run_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(lifecycle_item_id) REFERENCES auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, lifecycle_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_production_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            lifecycle_run_id INTEGER NOT NULL,
            state_run_id INTEGER,
            audit_name TEXT NOT NULL,
            audit_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            policy_allowed_count INTEGER NOT NULL DEFAULT 0,
            output_delta_count INTEGER NOT NULL DEFAULT 0,
            confirmation_delta_count INTEGER NOT NULL DEFAULT 0,
            confirmation_matches_output_count INTEGER NOT NULL DEFAULT 0,
            current_pending_count INTEGER NOT NULL DEFAULT 0,
            current_closed_count INTEGER NOT NULL DEFAULT 0,
            real_repair_count INTEGER NOT NULL DEFAULT 0,
            noop_observation_count INTEGER NOT NULL DEFAULT 0,
            requires_confirmation_promotion_count INTEGER NOT NULL DEFAULT 0,
            requires_output_apply_count INTEGER NOT NULL DEFAULT 0,
            requires_segment_state_lifecycle_integration_count INTEGER NOT NULL DEFAULT 0,
            eligible_controlled_production_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(lifecycle_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_boundary_repair_production_audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            lifecycle_run_id INTEGER NOT NULL,
            lifecycle_item_id INTEGER NOT NULL,
            repair_shadow_item_id INTEGER NOT NULL,
            state_run_id INTEGER,
            state_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            boundary_agent_key TEXT NOT NULL,
            boundary_policy TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            text_delta_kind TEXT NOT NULL,
            current_final_state TEXT,
            current_apply_state TEXT,
            current_state_group TEXT,
            current_review_state TEXT,
            current_lifecycle_policy_allowed INTEGER NOT NULL DEFAULT 0,
            corrected_matches_output INTEGER NOT NULL DEFAULT 0,
            corrected_matches_confirmation INTEGER NOT NULL DEFAULT 0,
            confirmation_matches_output INTEGER NOT NULL DEFAULT 0,
            requires_confirmation_promotion INTEGER NOT NULL DEFAULT 0,
            requires_output_apply INTEGER NOT NULL DEFAULT 0,
            requires_segment_state_lifecycle_integration INTEGER NOT NULL DEFAULT 0,
            eligible_controlled_production INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            corrected_text_hash TEXT,
            output_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_production_audit_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(lifecycle_run_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_lifecycle_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(lifecycle_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_lifecycle_items(id) ON DELETE CASCADE,
            FOREIGN KEY(repair_shadow_item_id) REFERENCES auto_confirmation_reopen_text_boundary_repair_shadow_items(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(state_item_id) REFERENCES segment_state_items(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, lifecycle_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_policy_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            shadow_policy_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            parent_agent_key TEXT,
            diagnostic_run_id INTEGER,
            specialist_audit_run_id INTEGER,
            min_ready_required INTEGER NOT NULL DEFAULT 0,
            max_blocked_allowed INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            positive_evidence_count INTEGER NOT NULL DEFAULT 0,
            negative_evidence_count INTEGER NOT NULL DEFAULT 0,
            release_action TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(shadow_policy_run_id) REFERENCES auto_confirmation_reopen_text_shadow_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES auto_confirmation_reopen_text_diagnostic_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(specialist_audit_run_id) REFERENCES auto_confirmation_reopen_text_specialist_audit_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_confirmation_reopen_text_policy_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            shadow_policy_run_id INTEGER NOT NULL,
            shadow_policy_item_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER,
            review_decision_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            text_subfamily TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            pattern_match INTEGER NOT NULL DEFAULT 0,
            positive_evidence INTEGER NOT NULL DEFAULT 0,
            negative_evidence INTEGER NOT NULL DEFAULT 0,
            issue_count INTEGER NOT NULL DEFAULT 0,
            select_cstring_count INTEGER NOT NULL DEFAULT 0,
            concept_link_count INTEGER NOT NULL DEFAULT 0,
            spanish_literal_hint_count INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES auto_confirmation_reopen_text_policy_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_policy_run_id) REFERENCES auto_confirmation_reopen_text_shadow_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(shadow_policy_item_id) REFERENCES auto_confirmation_reopen_text_shadow_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_item_id) REFERENCES auto_confirmation_reopen_text_diagnostic_items(id) ON DELETE SET NULL,
            FOREIGN KEY(review_decision_id) REFERENCES auto_confirmation_reopen_text_review_decisions(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, shadow_policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_policy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            state_run_id INTEGER,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            critical_count INTEGER NOT NULL DEFAULT 0,
            high_count INTEGER NOT NULL DEFAULT 0,
            medium_count INTEGER NOT NULL DEFAULT 0,
            low_count INTEGER NOT NULL DEFAULT 0,
            manual_review_count INTEGER NOT NULL DEFAULT 0,
            policy_candidate_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_policy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            state_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            review_state TEXT,
            diff_kind TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            auto_apply_allowed INTEGER NOT NULL DEFAULT 0,
            needs_human_review INTEGER NOT NULL DEFAULT 1,
            missing_tokens_json TEXT,
            extra_tokens_json TEXT,
            issue_flags_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_policy_decision_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_run_id INTEGER,
            source_report TEXT,
            decisions_path TEXT,
            total_decisions INTEGER NOT NULL DEFAULT 0,
            approved_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            fix_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_policy_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            policy_run_id INTEGER NOT NULL,
            policy_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            policy_bucket TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            decision TEXT NOT NULL,
            approved_for_apply INTEGER NOT NULL DEFAULT 0,
            corrected_text TEXT,
            notes TEXT,
            reviewer TEXT,
            confirmed_text_hash TEXT,
            output_text_hash TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_policy_decision_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_item_id) REFERENCES segment_token_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(policy_run_id, policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_evidence_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_run_id INTEGER,
            source_queue_path TEXT,
            decisions_path TEXT,
            total_decisions INTEGER NOT NULL DEFAULT 0,
            positive_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            needs_more_context_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_evidence_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            policy_run_id INTEGER NOT NULL,
            policy_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            gender_subtype TEXT NOT NULL,
            split_agent TEXT NOT NULL,
            split_maturity TEXT NOT NULL,
            method_signature TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            corrected_text TEXT,
            notes TEXT,
            reviewer TEXT,
            hypothesis TEXT,
            missing_tokens_json TEXT,
            extra_tokens_json TEXT,
            split_reasons_json TEXT,
            confirmed_text_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_gender_split_evidence_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_item_id) REFERENCES segment_token_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(policy_run_id, policy_item_id, split_agent)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_promotion_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            min_positive INTEGER NOT NULL DEFAULT 0,
            total_agents INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            needs_more_evidence_count INTEGER NOT NULL DEFAULT 0,
            boundary_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_promotion_audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            split_agent TEXT NOT NULL,
            total_evidence INTEGER NOT NULL DEFAULT 0,
            positive_count INTEGER NOT NULL DEFAULT 0,
            negative_count INTEGER NOT NULL DEFAULT 0,
            needs_more_context_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            distinct_method_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            method_signatures_json TEXT,
            sample_policy_item_ids_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_gender_split_promotion_audit_runs(id) ON DELETE CASCADE,
            UNIQUE(run_id, split_agent)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_guarded_policy_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_run_id INTEGER,
            audit_run_id INTEGER,
            min_positive INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            guarded_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            enabled_agent_count INTEGER NOT NULL DEFAULT 0,
            apply_allowed_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(audit_run_id) REFERENCES segment_token_gender_split_promotion_audit_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_guarded_policy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            policy_run_id INTEGER NOT NULL,
            policy_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            split_agent TEXT NOT NULL,
            split_maturity TEXT NOT NULL,
            method_signature TEXT NOT NULL,
            guarded_status TEXT NOT NULL,
            guarded_action TEXT NOT NULL,
            target_policy_bucket TEXT,
            target_risk_level TEXT,
            apply_allowed INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            missing_tokens_json TEXT,
            extra_tokens_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_gender_split_guarded_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_item_id) REFERENCES segment_token_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_bridge_dry_run_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            guarded_run_id INTEGER,
            policy_run_id INTEGER,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            would_bridge_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            would_create_policy_decisions INTEGER NOT NULL DEFAULT 0,
            apply_allowed_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(guarded_run_id) REFERENCES segment_token_gender_split_guarded_policy_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_gender_split_bridge_dry_run_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            guarded_run_id INTEGER,
            policy_run_id INTEGER NOT NULL,
            policy_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            split_agent TEXT NOT NULL,
            current_policy_bucket TEXT,
            current_risk_level TEXT,
            target_policy_bucket TEXT,
            target_risk_level TEXT,
            bridge_status TEXT NOT NULL,
            bridge_action TEXT NOT NULL,
            would_create_policy_decision INTEGER NOT NULL DEFAULT 0,
            approved_for_apply INTEGER NOT NULL DEFAULT 0,
            apply_allowed INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_gender_split_bridge_dry_run_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(guarded_run_id) REFERENCES segment_token_gender_split_guarded_policy_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_item_id) REFERENCES segment_token_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_policy_overlay_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            source_policy_run_id INTEGER NOT NULL,
            source_state_run_id INTEGER,
            source_rule_version TEXT,
            overlay_name TEXT NOT NULL,
            min_evidence INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            original_critical_count INTEGER NOT NULL DEFAULT 0,
            overlay_critical_count INTEGER NOT NULL DEFAULT 0,
            released_critical_count INTEGER NOT NULL DEFAULT 0,
            remaining_blocked_count INTEGER NOT NULL DEFAULT 0,
            enabled_rule_count INTEGER NOT NULL DEFAULT 0,
            apply_allowed_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            notes_json TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segment_token_policy_overlay_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER NOT NULL,
            source_policy_item_id INTEGER NOT NULL,
            state_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            original_policy_bucket TEXT NOT NULL,
            original_risk_level TEXT NOT NULL,
            overlay_policy_bucket TEXT NOT NULL,
            overlay_risk_level TEXT NOT NULL,
            overlay_action TEXT NOT NULL,
            overlay_agent_key TEXT,
            would_release_critical INTEGER NOT NULL DEFAULT 0,
            apply_allowed INTEGER NOT NULL DEFAULT 0,
            decision TEXT,
            rule_key TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_item_id) REFERENCES segment_token_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, source_policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_scope TEXT NOT NULL,
            coordinator_key TEXT NOT NULL,
            source_policy_run_id INTEGER,
            overlay_run_id INTEGER,
            source_state_run_id INTEGER,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            base_critical_count INTEGER NOT NULL DEFAULT 0,
            overlay_critical_count INTEGER NOT NULL DEFAULT 0,
            released_critical_count INTEGER NOT NULL DEFAULT 0,
            critical_queue_count INTEGER NOT NULL DEFAULT 0,
            high_count INTEGER NOT NULL DEFAULT 0,
            medium_count INTEGER NOT NULL DEFAULT 0,
            low_count INTEGER NOT NULL DEFAULT 0,
            enabled_rule_count INTEGER NOT NULL DEFAULT 0,
            apply_allowed_count INTEGER NOT NULL DEFAULT 0,
            active_agent_count INTEGER NOT NULL DEFAULT 0,
            operational_agent_count INTEGER NOT NULL DEFAULT 0,
            experimental_agent_count INTEGER NOT NULL DEFAULT 0,
            planned_agent_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            blockers_json TEXT,
            warnings_json TEXT,
            metrics_json TEXT,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(source_state_run_id) REFERENCES segment_state_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_registry (
            gate_key TEXT PRIMARY KEY,
            coordinator_key TEXT NOT NULL,
            active_checkpoint_id INTEGER NOT NULL,
            active_guarded_checkpoint_id INTEGER,
            active_overlay_run_id INTEGER NOT NULL,
            active_policy_run_id INTEGER NOT NULL,
            operational_state TEXT NOT NULL,
            active_promotion_kind TEXT,
            auto_apply_allowed INTEGER NOT NULL DEFAULT 0,
            promoted_at TEXT NOT NULL,
            promoted_by TEXT,
            reason TEXT,
            metrics_json TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(active_checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE CASCADE,
            FOREIGN KEY(active_guarded_checkpoint_id) REFERENCES ml_composite_guarded_overlay_checkpoints(id) ON DELETE SET NULL,
            FOREIGN KEY(active_overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(active_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_key TEXT NOT NULL,
            checkpoint_id INTEGER NOT NULL,
            guarded_checkpoint_id INTEGER,
            overlay_run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER NOT NULL,
            previous_checkpoint_id INTEGER,
            previous_overlay_run_id INTEGER,
            decision TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            promotion_kind TEXT,
            auto_apply_allowed INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            blockers_json TEXT,
            warnings_json TEXT,
            metrics_json TEXT,
            promoted_by TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE CASCADE,
            FOREIGN KEY(guarded_checkpoint_id) REFERENCES ml_composite_guarded_overlay_checkpoints(id) ON DELETE SET NULL,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(previous_checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE SET NULL,
            FOREIGN KEY(previous_overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            gate_key TEXT NOT NULL,
            checkpoint_id INTEGER NOT NULL,
            overlay_run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER,
            guarded_checkpoint_id INTEGER,
            source_mode TEXT NOT NULL,
            shadow_queue_kind TEXT,
            route_filter_csv TEXT,
            risk_filter_csv TEXT,
            critical_only INTEGER NOT NULL DEFAULT 1,
            limit_count INTEGER,
            total_rows INTEGER NOT NULL DEFAULT 0,
            critical_rows INTEGER NOT NULL DEFAULT 0,
            high_rows INTEGER NOT NULL DEFAULT 0,
            medium_rows INTEGER NOT NULL DEFAULT 0,
            low_rows INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            bucket_counts_json TEXT,
            priority_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE CASCADE,
            FOREIGN KEY(guarded_checkpoint_id) REFERENCES ml_composite_guarded_overlay_checkpoints(id) ON DELETE SET NULL,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_queue_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_run_id INTEGER NOT NULL,
            suggested_route TEXT NOT NULL,
            overlay_policy_bucket TEXT NOT NULL,
            overlay_risk_level TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_composite_gate_queue_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_run_id INTEGER NOT NULL,
            gate_key TEXT NOT NULL,
            checkpoint_id INTEGER NOT NULL,
            overlay_run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER,
            guarded_checkpoint_id INTEGER,
            policy_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            suggested_route TEXT NOT NULL,
            overlay_policy_bucket TEXT NOT NULL,
            overlay_risk_level TEXT NOT NULL,
            priority_bucket TEXT,
            rule_key TEXT,
            hygiene_flags_json TEXT,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_composite_gate_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(guarded_checkpoint_id) REFERENCES ml_composite_guarded_overlay_checkpoints(id) ON DELETE SET NULL,
            FOREIGN KEY(policy_item_id) REFERENCES segment_token_policy_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(queue_run_id, policy_item_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_review_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            gate_key TEXT NOT NULL,
            checkpoint_id INTEGER NOT NULL,
            overlay_run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            queued_items INTEGER NOT NULL DEFAULT 0,
            unqueued_items INTEGER NOT NULL DEFAULT 0,
            reviewed_items INTEGER NOT NULL DEFAULT 0,
            pending_items INTEGER NOT NULL DEFAULT 0,
            approved_for_apply_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            fix_count INTEGER NOT NULL DEFAULT 0,
            needs_subpolicy_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            review_coverage_pct REAL NOT NULL DEFAULT 0,
            queue_coverage_pct REAL NOT NULL DEFAULT 0,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE CASCADE,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_gate_review_route_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            suggested_route TEXT NOT NULL,
            overlay_policy_bucket TEXT NOT NULL,
            overlay_risk_level TEXT NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            queued_items INTEGER NOT NULL DEFAULT 0,
            unqueued_items INTEGER NOT NULL DEFAULT 0,
            reviewed_items INTEGER NOT NULL DEFAULT 0,
            pending_items INTEGER NOT NULL DEFAULT 0,
            approved_for_apply_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            fix_count INTEGER NOT NULL DEFAULT 0,
            needs_subpolicy_count INTEGER NOT NULL DEFAULT 0,
            manual_exception_count INTEGER NOT NULL DEFAULT 0,
            review_coverage_pct REAL NOT NULL DEFAULT 0,
            queue_coverage_pct REAL NOT NULL DEFAULT 0,
            latest_queue_run_id INTEGER,
            latest_queue_total_rows INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES ml_composite_gate_review_snapshots(id) ON DELETE CASCADE,
            FOREIGN KEY(latest_queue_run_id) REFERENCES ml_composite_gate_queue_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_subpolicy_diagnostic_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            gate_key TEXT NOT NULL,
            checkpoint_id INTEGER NOT NULL,
            overlay_run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            reviewed_items INTEGER NOT NULL DEFAULT 0,
            pending_items INTEGER NOT NULL DEFAULT 0,
            grouped_subpolicies INTEGER NOT NULL DEFAULT 0,
            design_candidate_count INTEGER NOT NULL DEFAULT 0,
            policy_candidate_count INTEGER NOT NULL DEFAULT 0,
            needs_more_review_count INTEGER NOT NULL DEFAULT 0,
            queue_review_candidate_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE CASCADE,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_subpolicy_diagnostic_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            suggested_route TEXT NOT NULL,
            token_subtype TEXT NOT NULL,
            overlay_policy_bucket TEXT NOT NULL,
            overlay_risk_level TEXT NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            queued_items INTEGER NOT NULL DEFAULT 0,
            unqueued_items INTEGER NOT NULL DEFAULT 0,
            reviewed_items INTEGER NOT NULL DEFAULT 0,
            pending_items INTEGER NOT NULL DEFAULT 0,
            approved_for_apply_count INTEGER NOT NULL DEFAULT 0,
            accept_count INTEGER NOT NULL DEFAULT 0,
            keep_manual_exception_count INTEGER NOT NULL DEFAULT 0,
            reject_count INTEGER NOT NULL DEFAULT 0,
            needs_subpolicy_count INTEGER NOT NULL DEFAULT 0,
            fix_count INTEGER NOT NULL DEFAULT 0,
            review_coverage_pct REAL NOT NULL DEFAULT 0,
            queue_coverage_pct REAL NOT NULL DEFAULT 0,
            maturity_status TEXT NOT NULL,
            confidence_band TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            sample_policy_item_ids_json TEXT,
            sample_paths_json TEXT,
            token_families_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_composite_subpolicy_diagnostic_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_subpolicy_promotion_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            gate_key TEXT NOT NULL,
            checkpoint_id INTEGER NOT NULL,
            overlay_run_id INTEGER NOT NULL,
            source_policy_run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            candidate_group_count INTEGER NOT NULL DEFAULT 0,
            rule_family_count INTEGER NOT NULL DEFAULT 0,
            ready_rule_count INTEGER NOT NULL DEFAULT 0,
            collect_more_count INTEGER NOT NULL DEFAULT 0,
            manual_boundary_count INTEGER NOT NULL DEFAULT 0,
            split_required_count INTEGER NOT NULL DEFAULT 0,
            not_promotable_count INTEGER NOT NULL DEFAULT 0,
            pending_only_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES ml_composite_checkpoints(id) ON DELETE CASCADE,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_run_id) REFERENCES ml_composite_subpolicy_diagnostic_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_subpolicy_promotion_audit_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            suggested_route TEXT NOT NULL,
            token_subtype TEXT NOT NULL,
            rule_key TEXT NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            reviewed_items INTEGER NOT NULL DEFAULT 0,
            pending_items INTEGER NOT NULL DEFAULT 0,
            accept_count INTEGER NOT NULL DEFAULT 0,
            keep_manual_exception_count INTEGER NOT NULL DEFAULT 0,
            reject_count INTEGER NOT NULL DEFAULT 0,
            needs_subpolicy_count INTEGER NOT NULL DEFAULT 0,
            fix_count INTEGER NOT NULL DEFAULT 0,
            text_cleanup_block_count INTEGER NOT NULL DEFAULT 0,
            approved_for_apply_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            confidence_band TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            sample_policy_item_ids_json TEXT,
            sample_paths_json TEXT,
            token_signature_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_composite_subpolicy_promotion_audit_runs(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_guarded_overlay_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            gate_key TEXT NOT NULL,
            overlay_run_id INTEGER NOT NULL,
            parent_overlay_run_id INTEGER,
            source_policy_run_id INTEGER NOT NULL,
            promotion_audit_run_id INTEGER,
            ready_rule_count INTEGER NOT NULL DEFAULT 0,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            guarded_release_count INTEGER NOT NULL DEFAULT 0,
            release_rate_pct REAL NOT NULL DEFAULT 0,
            medium_to_low_count INTEGER NOT NULL DEFAULT 0,
            invalid_release_count INTEGER NOT NULL DEFAULT 0,
            apply_allowed_count INTEGER NOT NULL DEFAULT 0,
            active_gate_overlay_run_id INTEGER,
            active_gate_unchanged INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            blockers_json TEXT,
            warnings_json TEXT,
            metrics_json TEXT,
            report_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(source_policy_run_id) REFERENCES segment_token_policy_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(promotion_audit_run_id) REFERENCES ml_composite_subpolicy_promotion_audit_runs(id) ON DELETE SET NULL,
            FOREIGN KEY(active_gate_overlay_run_id) REFERENCES segment_token_policy_overlay_runs(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_composite_guarded_overlay_checkpoint_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_id INTEGER NOT NULL,
            suggested_route TEXT NOT NULL,
            token_subtype TEXT NOT NULL,
            rule_key TEXT NOT NULL,
            release_count INTEGER NOT NULL DEFAULT 0,
            reviewed_items INTEGER NOT NULL DEFAULT 0,
            pending_items INTEGER NOT NULL DEFAULT 0,
            accept_count INTEGER NOT NULL DEFAULT 0,
            keep_manual_exception_count INTEGER NOT NULL DEFAULT 0,
            reject_count INTEGER NOT NULL DEFAULT 0,
            needs_subpolicy_count INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            sample_policy_item_ids_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES ml_composite_guarded_overlay_checkpoints(id) ON DELETE CASCADE
        )
        """
    )

    changes.extend(
        ensure_columns(
            conn,
            "source_segments",
            [
                ("relative_path", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("source_key", "TEXT"),
                ("version_index", "TEXT"),
                ("spanish_text", "TEXT"),
                ("english_text", "TEXT"),
                ("old_text", "TEXT"),
                ("spanish_raw_line", "TEXT"),
                ("english_raw_line", "TEXT"),
                ("old_raw_line", "TEXT"),
                ("spanish_hash", "TEXT"),
                ("english_hash", "TEXT"),
                ("old_hash", "TEXT"),
                ("has_english", "INTEGER NOT NULL DEFAULT 0"),
                ("has_old", "INTEGER NOT NULL DEFAULT 0"),
                ("is_active", "INTEGER NOT NULL DEFAULT 1"),
                ("first_indexed_at", "TEXT"),
                ("last_indexed_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "output_segments",
            [
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("output_line_number", "INTEGER"),
                ("portuguese_text", "TEXT"),
                ("output_raw_line", "TEXT"),
                ("portuguese_hash", "TEXT"),
                ("last_indexed_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "translation_memory",
            [
                ("source_segment_id", "INTEGER"),
                ("source_language", "TEXT"),
                ("target_language", "TEXT"),
                ("source_text", "TEXT"),
                ("target_text", "TEXT"),
                ("source_hash", "TEXT"),
                ("target_hash", "TEXT"),
                ("confidence_score", "REAL"),
                ("origin", "TEXT"),
                ("usage_count", "INTEGER NOT NULL DEFAULT 1"),
                ("created_at", "TEXT"),
                ("last_seen_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "translation_suggestions",
            [
                ("segment_id", "INTEGER"),
                ("suggested_text", "TEXT"),
                ("suggested_hash", "TEXT"),
                ("source_language", "TEXT"),
                ("origin", "TEXT"),
                ("match_type", "TEXT"),
                ("match_score", "REAL"),
                ("token_status", "TEXT"),
                ("status", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "suggestion_feedback",
            [
                ("suggestion_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("decision", "TEXT NOT NULL DEFAULT 'pending'"),
                ("suggested_text", "TEXT"),
                ("corrected_text", "TEXT"),
                ("reason", "TEXT"),
                ("reviewer", "TEXT"),
                ("reviewed_at", "TEXT"),
                ("applied_at", "TEXT"),
                ("apply_result", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "inline_fragments",
            [
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("package_name", "TEXT"),
                ("command_name", "TEXT"),
                ("argument_index", "INTEGER"),
                ("fragment_text", "TEXT"),
                ("fragment_hash", "TEXT"),
                ("fragment_role", "TEXT"),
                ("should_translate", "INTEGER NOT NULL DEFAULT 0"),
                ("suggested_text", "TEXT"),
                ("confidence_score", "REAL"),
                ("status", "TEXT NOT NULL DEFAULT 'indexed'"),
                ("reasons_json", "TEXT"),
                ("indexed_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "local_learning_runs",
            [
                ("mode", "TEXT"),
                ("limit_count", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_confidence_threshold", "REAL NOT NULL DEFAULT 0.98"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_confidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_human_count", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'created'"),
                ("notes", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "local_learning_candidates",
            [
                ("run_id", "INTEGER"),
                ("feedback_id", "INTEGER"),
                ("suggestion_id", "INTEGER"),
                ("offline_proposal_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("english_text", "TEXT"),
                ("spanish_text", "TEXT"),
                ("old_text", "TEXT"),
                ("current_output_text", "TEXT"),
                ("suggested_text", "TEXT"),
                ("suggested_hash", "TEXT"),
                ("queue_source", "TEXT NOT NULL DEFAULT 'pending'"),
                ("focus_group", "TEXT NOT NULL DEFAULT 'all'"),
                ("source_language", "TEXT"),
                ("origin", "TEXT"),
                ("match_type", "TEXT"),
                ("match_score", "REAL"),
                ("token_status", "TEXT"),
                ("suggestion_status", "TEXT"),
                ("local_confidence_score", "REAL NOT NULL DEFAULT 0"),
                ("local_status", "TEXT NOT NULL DEFAULT 'pending_human'"),
                ("human_label", "TEXT NOT NULL DEFAULT 'pending'"),
                ("corrected_text", "TEXT"),
                ("reason", "TEXT"),
                ("reviewer", "TEXT"),
                ("reviewed_at", "TEXT"),
                ("learned_at", "TEXT"),
                ("confirmation_synced_at", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_confirmations",
            [
                ("segment_id", "INTEGER"),
                ("confirmation_level", "TEXT"),
                ("confirmed_text", "TEXT"),
                ("confirmation_source", "TEXT"),
                ("confirmation_label", "TEXT"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("confidence_score", "REAL"),
                ("candidate_id", "INTEGER"),
                ("feedback_id", "INTEGER"),
                ("reviewer", "TEXT"),
                ("confirmed_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "local_learning_pattern_stats",
            [
                ("pattern_key", "TEXT"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("near_positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("partial_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("harmful_count", "INTEGER NOT NULL DEFAULT 0"),
                ("total_count", "INTEGER NOT NULL DEFAULT 0"),
                ("weight_adjustment", "REAL NOT NULL DEFAULT 0"),
                ("last_label", "TEXT"),
                ("last_candidate_id", "INTEGER"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "name_equivalences",
            [
                ("source_name", "TEXT"),
                ("portuguese_name", "TEXT"),
                ("name_family", "TEXT"),
                ("source_kind", "TEXT NOT NULL DEFAULT 'character_name'"),
                ("status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("confidence_score", "REAL"),
                ("evidence_count", "INTEGER NOT NULL DEFAULT 1"),
                ("first_segment_id", "INTEGER"),
                ("last_segment_id", "INTEGER"),
                ("reason", "TEXT"),
                ("reviewer", "TEXT"),
                ("reviewed_at", "TEXT"),
                ("applied_at", "TEXT"),
                ("apply_result", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "learned_validation_runs",
            [
                ("rule_version", "TEXT"),
                ("model_version", "TEXT"),
                ("path_filter", "TEXT"),
                ("limit_count", "INTEGER"),
                ("active_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_safe_audit_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_autofix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_suggestion_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_structure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("notes", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "learned_validation_items",
            [
                ("run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("candidate_source", "TEXT"),
                ("candidate_text", "TEXT"),
                ("action", "TEXT"),
                ("risk_class", "TEXT"),
                ("confidence_score", "REAL NOT NULL DEFAULT 0"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("token_status", "TEXT"),
                ("reasons_json", "TEXT"),
                ("issues_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "title_review_queue",
            [
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("english_text", "TEXT"),
                ("spanish_text", "TEXT"),
                ("old_text", "TEXT"),
                ("proposed_text", "TEXT"),
                ("corrected_text", "TEXT"),
                ("bucket", "TEXT"),
                ("recommendation", "TEXT"),
                ("confidence_score", "REAL"),
                ("status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("reason", "TEXT"),
                ("reviewer", "TEXT"),
                ("reviewed_at", "TEXT"),
                ("applied_at", "TEXT"),
                ("apply_result", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "package_focus_queue",
            [
                ("focus_group", "TEXT"),
                ("relative_path", "TEXT"),
                ("priority_score", "REAL NOT NULL DEFAULT 0"),
                ("total_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("confirmed_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("human_confirmed_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_confirmed_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("reason", "TEXT"),
                ("first_seen_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "finalization_queue",
            [
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("closure_bucket", "TEXT"),
                ("risk_level", "TEXT"),
                ("action_hint", "TEXT"),
                ("priority_score", "REAL NOT NULL DEFAULT 0"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("package_pending", "INTEGER NOT NULL DEFAULT 0"),
                ("package_total", "INTEGER NOT NULL DEFAULT 0"),
                ("is_high_impact", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'open'"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "mojibake_context_queue",
            [
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("fragment_summary", "TEXT"),
                ("fragment_count", "INTEGER NOT NULL DEFAULT 0"),
                ("residue_kind", "TEXT"),
                ("priority_score", "REAL NOT NULL DEFAULT 0"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("english_text", "TEXT"),
                ("spanish_text", "TEXT"),
                ("old_text", "TEXT"),
                ("confirmed_text", "TEXT"),
                ("confirmation_level", "TEXT"),
                ("confirmation_source", "TEXT"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'open'"),
                ("notes", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "offline_proposal_runs",
            [
                ("rule_version", "TEXT"),
                ("model_version", "TEXT"),
                ("path_filter", "TEXT"),
                ("limit_count", "INTEGER"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("proposed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("auto_ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_review_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rejected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("notes", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "offline_proposals",
            [
                ("run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("candidate_bucket", "TEXT"),
                ("proposal_source", "TEXT"),
                ("original_text", "TEXT"),
                ("proposed_text", "TEXT"),
                ("confidence_score", "REAL NOT NULL DEFAULT 0"),
                ("status", "TEXT"),
                ("token_status", "TEXT"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rules_json", "TEXT"),
                ("reasons_json", "TEXT"),
                ("issues_json", "TEXT"),
                ("applied_at", "TEXT"),
                ("apply_result", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_dataset_runs",
            [
                ("rule_version", "TEXT"),
                ("dataset_version", "TEXT"),
                ("source_scope", "TEXT"),
                ("limit_count", "INTEGER"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("neutral_count", "INTEGER NOT NULL DEFAULT 0"),
                ("total_count", "INTEGER NOT NULL DEFAULT 0"),
                ("strong_positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("strong_negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("notes", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_training_examples",
            [
                ("run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("english_text", "TEXT"),
                ("spanish_text", "TEXT"),
                ("old_text", "TEXT"),
                ("output_text", "TEXT"),
                ("candidate_text", "TEXT"),
                ("final_text", "TEXT"),
                ("label", "TEXT"),
                ("action_label", "TEXT"),
                ("issue_label", "TEXT"),
                ("trust_level", "INTEGER NOT NULL DEFAULT 0"),
                ("evidence_source", "TEXT"),
                ("evidence_id", "INTEGER"),
                ("confidence_score", "REAL"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("token_count", "INTEGER NOT NULL DEFAULT 0"),
                ("has_english", "INTEGER NOT NULL DEFAULT 0"),
                ("has_old", "INTEGER NOT NULL DEFAULT 0"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_model_runs",
            [
                ("rule_version", "TEXT"),
                ("model_version", "TEXT"),
                ("model_kind", "TEXT"),
                ("dataset_run_id", "INTEGER"),
                ("model_path", "TEXT"),
                ("training_examples", "INTEGER NOT NULL DEFAULT 0"),
                ("test_examples", "INTEGER NOT NULL DEFAULT 0"),
                ("safe_threshold", "REAL NOT NULL DEFAULT 0.90"),
                ("accuracy", "REAL"),
                ("macro_f1", "REAL"),
                ("predicted_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_safe_rate", "REAL"),
                ("safe_precision", "REAL"),
                ("safe_recall", "REAL"),
                ("metrics_json", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_score_runs",
            [
                ("rule_version", "TEXT"),
                ("model_run_id", "INTEGER"),
                ("model_version", "TEXT"),
                ("path_filter", "TEXT"),
                ("limit_count", "INTEGER"),
                ("scored_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_direct_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deterministic_promoted_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deterministic_demoted_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("final_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_autofix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_structure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deterministic_block_count", "INTEGER NOT NULL DEFAULT 0"),
                ("notes", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_holdout_eval_runs",
            [
                ("rule_version", "TEXT"),
                ("dataset_run_id", "INTEGER"),
                ("safe_threshold", "REAL NOT NULL DEFAULT 0"),
                ("target_ratio", "REAL NOT NULL DEFAULT 0"),
                ("min_negative", "INTEGER NOT NULL DEFAULT 0"),
                ("max_paths", "INTEGER"),
                ("safe_multiplier", "INTEGER NOT NULL DEFAULT 0"),
                ("feature_set", "TEXT"),
                ("train_strategy", "TEXT"),
                ("holdout_path_count", "INTEGER NOT NULL DEFAULT 0"),
                ("holdout_examples", "INTEGER NOT NULL DEFAULT 0"),
                ("predicted_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_safe_rate", "REAL"),
                ("unique_predicted_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("unique_false_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("safe_precision", "REAL"),
                ("safe_recall", "REAL"),
                ("accuracy", "REAL"),
                ("macro_f1", "REAL"),
                ("report_path", "TEXT"),
                ("metrics_json", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_score_items",
            [
                ("run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("candidate_text", "TEXT"),
                ("model_action", "TEXT"),
                ("final_action", "TEXT"),
                ("risk_class", "TEXT"),
                ("model_safe_probability", "REAL"),
                ("model_confidence", "REAL"),
                ("token_status", "TEXT"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deterministic_blocked", "INTEGER NOT NULL DEFAULT 0"),
                ("confirmation_level", "TEXT"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("issues_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_model_registry",
            [
                ("model_kind", "TEXT"),
                ("active_model_run_id", "INTEGER"),
                ("active_model_version", "TEXT"),
                ("policy_version", "TEXT"),
                ("promoted_at", "TEXT"),
                ("promoted_by", "TEXT"),
                ("reason", "TEXT"),
                ("metrics_json", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_model_promotions",
            [
                ("model_kind", "TEXT"),
                ("candidate_model_run_id", "INTEGER"),
                ("previous_model_run_id", "INTEGER"),
                ("decision", "TEXT"),
                ("policy_version", "TEXT"),
                ("reason", "TEXT"),
                ("metrics_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_policy_runs",
            [
                ("rule_version", "TEXT"),
                ("score_run_id", "INTEGER"),
                ("model_run_id", "INTEGER"),
                ("model_version", "TEXT"),
                ("scored_count", "INTEGER NOT NULL DEFAULT 0"),
                ("active_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("policy_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("new_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("demoted_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("protect_active_safe", "INTEGER NOT NULL DEFAULT 1"),
                ("notes", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_policy_items",
            [
                ("run_id", "INTEGER"),
                ("score_item_id", "INTEGER"),
                ("score_run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("policy_group", "TEXT"),
                ("policy_threshold", "REAL"),
                ("policy_require_learned_positive", "INTEGER NOT NULL DEFAULT 0"),
                ("score_final_action", "TEXT"),
                ("policy_action", "TEXT"),
                ("new_safe", "INTEGER NOT NULL DEFAULT 0"),
                ("demoted_safe", "INTEGER NOT NULL DEFAULT 0"),
                ("learned_positive", "INTEGER NOT NULL DEFAULT 0"),
                ("learned_negative", "INTEGER NOT NULL DEFAULT 0"),
                ("model_safe_probability", "REAL"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_specialist_policy_snapshots",
            [
                ("rule_version", "TEXT"),
                ("general_score_run_id", "INTEGER"),
                ("specialist_key", "TEXT"),
                ("model_kind", "TEXT"),
                ("model_run_id", "INTEGER"),
                ("model_version", "TEXT"),
                ("dataset_run_id", "INTEGER"),
                ("score_run_id", "INTEGER"),
                ("operational_threshold", "REAL"),
                ("policy_min_threshold", "REAL"),
                ("threshold_below_policy", "INTEGER NOT NULL DEFAULT 0"),
                ("scope_description", "TEXT"),
                ("scope_sql", "TEXT"),
                ("scope_active_count", "INTEGER NOT NULL DEFAULT 0"),
                ("scored_count", "INTEGER NOT NULL DEFAULT 0"),
                ("compared_count", "INTEGER NOT NULL DEFAULT 0"),
                ("scope_delta_count", "INTEGER NOT NULL DEFAULT 0"),
                ("scope_coverage_rate", "REAL"),
                ("final_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_autofix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_structure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_direct_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deterministic_promoted_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("deterministic_demoted_auto_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("final_auto_safe_rate", "REAL"),
                ("model_direct_auto_safe_rate", "REAL"),
                ("specialist_new_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("specialist_demoted_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_new_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_demoted_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_new_safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_demoted_count", "INTEGER NOT NULL DEFAULT 0"),
                ("divergent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_divergent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_real_count", "INTEGER NOT NULL DEFAULT 0"),
                ("missing_general_count", "INTEGER NOT NULL DEFAULT 0"),
                ("divergence_rate", "REAL"),
                ("pending_real_rate", "REAL"),
                ("status", "TEXT"),
                ("recommended_action", "TEXT"),
                ("notes", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_agent_registry",
            [
                ("agent_key", "TEXT"),
                ("agent_type", "TEXT"),
                ("parent_agent_key", "TEXT"),
                ("model_kind", "TEXT"),
                ("status", "TEXT NOT NULL DEFAULT 'planned'"),
                ("operational_state", "TEXT NOT NULL DEFAULT 'experimental'"),
                ("decision_role", "TEXT NOT NULL DEFAULT 'vote'"),
                ("scope_group", "TEXT"),
                ("scope_sql", "TEXT"),
                ("scope_description", "TEXT"),
                ("default_threshold", "REAL"),
                ("priority", "INTEGER NOT NULL DEFAULT 100"),
                ("dashboard_group", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
                ("notes_json", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_agent_routing_runs",
            [
                ("rule_version", "TEXT"),
                ("general_score_run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("coordinator_key", "TEXT"),
                ("agents_considered_count", "INTEGER NOT NULL DEFAULT 0"),
                ("segments_scanned_count", "INTEGER NOT NULL DEFAULT 0"),
                ("routed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("active_agent_covered_count", "INTEGER NOT NULL DEFAULT 0"),
                ("planned_agent_covered_count", "INTEGER NOT NULL DEFAULT 0"),
                ("missing_agent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("conflict_count", "INTEGER NOT NULL DEFAULT 0"),
                ("recommendation_count", "INTEGER NOT NULL DEFAULT 0"),
                ("notes_json", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_agent_routing_items",
            [
                ("run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("route_agent_key", "TEXT"),
                ("route_agent_type", "TEXT"),
                ("route_status", "TEXT NOT NULL DEFAULT 'candidate'"),
                ("route_confidence", "REAL"),
                ("route_reason", "TEXT"),
                ("issue_family", "TEXT"),
                ("general_action", "TEXT"),
                ("policy_action", "TEXT"),
                ("specialist_action", "TEXT"),
                ("recommendation_key", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_agent_recommendations",
            [
                ("run_id", "INTEGER"),
                ("proposed_agent_key", "TEXT"),
                ("parent_agent_key", "TEXT"),
                ("recommendation_type", "TEXT"),
                ("status", "TEXT NOT NULL DEFAULT 'proposed'"),
                ("reason", "TEXT"),
                ("evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("corrected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("sample_segments_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_ledger_runs",
            [
                ("rule_version", "TEXT"),
                ("segment_state_run_id", "INTEGER"),
                ("active_score_run_id", "INTEGER"),
                ("candidate_score_run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("source_scope", "TEXT NOT NULL DEFAULT 'pending_segment_state'"),
                ("pending_segments_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ledger_segment_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ledger_item_count", "INTEGER NOT NULL DEFAULT 0"),
                ("actionable_item_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_item_count", "INTEGER NOT NULL DEFAULT 0"),
                ("primary_family_counts_json", "TEXT"),
                ("family_counts_json", "TEXT"),
                ("agent_counts_json", "TEXT"),
                ("notes_json", "TEXT"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_ledger_items",
            [
                ("run_id", "INTEGER"),
                ("state_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("final_state", "TEXT"),
                ("state_group", "TEXT"),
                ("active_action", "TEXT"),
                ("candidate_action", "TEXT"),
                ("policy_action", "TEXT"),
                ("confirmation_level", "TEXT"),
                ("confirmation_label", "TEXT"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_family", "TEXT"),
                ("issue_kind", "TEXT"),
                ("issue_role", "TEXT NOT NULL DEFAULT 'diagnostic'"),
                ("issue_severity", "TEXT NOT NULL DEFAULT 'medium'"),
                ("agent_key", "TEXT"),
                ("route_status", "TEXT NOT NULL DEFAULT 'candidate'"),
                ("proposed_action", "TEXT"),
                ("proposed_repair_text", "TEXT"),
                ("token_impact", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("token_status", "TEXT"),
                ("confidence_score", "REAL"),
                ("evidence_text", "TEXT"),
                ("evidence_json", "TEXT"),
                ("validation_status", "TEXT NOT NULL DEFAULT 'not_validated'"),
                ("status", "TEXT NOT NULL DEFAULT 'open'"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_review_queue_runs",
            [
                ("rule_version", "TEXT"),
                ("ledger_run_id", "INTEGER"),
                ("agent_key", "TEXT"),
                ("issue_family", "TEXT"),
                ("queue_strategy", "TEXT"),
                ("limit_count", "INTEGER"),
                ("per_bucket", "INTEGER NOT NULL DEFAULT 0"),
                ("selected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("open_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("bucket_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("decisions_template_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_review_queue_items",
            [
                ("run_id", "INTEGER"),
                ("ledger_run_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("issue_family", "TEXT"),
                ("issue_kind", "TEXT"),
                ("agent_key", "TEXT"),
                ("queue_bucket", "TEXT"),
                ("priority_score", "REAL NOT NULL DEFAULT 0"),
                ("review_status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("suggested_decision", "TEXT"),
                ("evidence_text", "TEXT"),
                ("evidence_json", "TEXT"),
                ("english_text", "TEXT"),
                ("spanish_text", "TEXT"),
                ("confirmed_text", "TEXT"),
                ("reviewer_decision", "TEXT"),
                ("reviewer_notes", "TEXT"),
                ("corrected_text", "TEXT"),
                ("created_at", "TEXT"),
                ("reviewed_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_review_decision_runs",
            [
                ("rule_version", "TEXT"),
                ("queue_run_id", "INTEGER"),
                ("agent_key", "TEXT"),
                ("decisions_path", "TEXT"),
                ("source_report", "TEXT"),
                ("reviewer", "TEXT"),
                ("total_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("accepted_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_count", "INTEGER NOT NULL DEFAULT 0"),
                ("invalid_count", "INTEGER NOT NULL DEFAULT 0"),
                ("safe_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("repair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("context_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("new_microagent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("evidence_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_review_decisions",
            [
                ("run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("ledger_run_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("issue_family", "TEXT"),
                ("issue_kind", "TEXT"),
                ("queue_bucket", "TEXT"),
                ("suggested_decision", "TEXT"),
                ("reviewer_decision", "TEXT"),
                ("normalized_decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("corrected_text", "TEXT"),
                ("notes", "TEXT"),
                ("reviewer", "TEXT"),
                ("valid", "INTEGER NOT NULL DEFAULT 1"),
                ("validation_status", "TEXT NOT NULL DEFAULT 'accepted'"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_short_label_release_runs",
            [
                ("rule_version", "TEXT"),
                ("policy_name", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("agent_key", "TEXT"),
                ("decision_run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("released_shadow_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_closed_gain", "INTEGER NOT NULL DEFAULT 0"),
                ("safe_short_label_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_positive_reopen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocker_counts_json", "TEXT"),
                ("bucket_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_trigger_gender_role_surface_runs",
            [
                ("rule_version", "TEXT"),
                ("policy_name", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("agent_key", "TEXT"),
                ("decision_run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("shadow_ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("kinship_count", "INTEGER NOT NULL DEFAULT 0"),
                ("role_article_count", "INTEGER NOT NULL DEFAULT 0"),
                ("subpolicy_counts_json", "TEXT"),
                ("action_counts_json", "TEXT"),
                ("blocker_counts_json", "TEXT"),
                ("analytics_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_trigger_gender_role_surface_items",
            [
                ("run_id", "INTEGER"),
                ("decision_id", "INTEGER"),
                ("decision_run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("ledger_run_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("queue_bucket", "TEXT"),
                ("issue_family", "TEXT"),
                ("issue_kind", "TEXT"),
                ("normalized_decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("subpolicy_name", "TEXT"),
                ("shadow_status", "TEXT"),
                ("shadow_action", "TEXT"),
                ("block_reason", "TEXT"),
                ("token_impact", "TEXT"),
                ("token_status", "TEXT"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("token_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_codes_json", "TEXT"),
                ("evidence_json", "TEXT"),
                ("notes", "TEXT"),
                ("current_confirmed_text_hash", "TEXT"),
                ("queue_confirmed_text_hash", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_trigger_gender_role_checkpoint_runs",
            [
                ("rule_version", "TEXT"),
                ("shadow_run_id", "INTEGER"),
                ("checkpoint_name", "TEXT"),
                ("checkpoint_status", "TEXT"),
                ("policy_name", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("agent_key", "TEXT"),
                ("decision_run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("min_ready_required", "INTEGER NOT NULL DEFAULT 0"),
                ("max_blocked_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("checkpoint_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("checkpoint_blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("kinship_count", "INTEGER NOT NULL DEFAULT 0"),
                ("role_article_count", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("production_release_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("blocker_counts_json", "TEXT"),
                ("subpolicy_counts_json", "TEXT"),
                ("action_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_trigger_gender_role_checkpoint_items",
            [
                ("checkpoint_run_id", "INTEGER"),
                ("shadow_run_id", "INTEGER"),
                ("shadow_item_id", "INTEGER"),
                ("decision_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("queue_bucket", "TEXT"),
                ("issue_family", "TEXT"),
                ("issue_kind", "TEXT"),
                ("normalized_decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("subpolicy_name", "TEXT"),
                ("shadow_status", "TEXT"),
                ("shadow_action", "TEXT"),
                ("checkpoint_action", "TEXT"),
                ("checkpoint_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("block_reason", "TEXT"),
                ("token_impact", "TEXT"),
                ("token_status", "TEXT"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("token_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_codes_json", "TEXT"),
                ("shadow_confirmed_text_hash", "TEXT"),
                ("current_confirmed_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_trigger_gender_role_lifecycle_runs",
            [
                ("rule_version", "TEXT"),
                ("checkpoint_run_id", "INTEGER"),
                ("policy_name", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("policy_action", "TEXT"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("released_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("subpolicy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("kinship_count", "INTEGER NOT NULL DEFAULT 0"),
                ("role_article_count", "INTEGER NOT NULL DEFAULT 0"),
                ("production_release_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("blocker_counts_json", "TEXT"),
                ("subpolicy_counts_json", "TEXT"),
                ("action_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_trigger_gender_role_lifecycle_items",
            [
                ("run_id", "INTEGER"),
                ("checkpoint_run_id", "INTEGER"),
                ("checkpoint_item_id", "INTEGER"),
                ("shadow_run_id", "INTEGER"),
                ("shadow_item_id", "INTEGER"),
                ("decision_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("queue_bucket", "TEXT"),
                ("issue_family", "TEXT"),
                ("issue_kind", "TEXT"),
                ("normalized_decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("subpolicy_name", "TEXT"),
                ("checkpoint_action", "TEXT"),
                ("policy_action", "TEXT"),
                ("policy_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("block_reason", "TEXT"),
                ("token_impact", "TEXT"),
                ("token_status", "TEXT"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("token_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_codes_json", "TEXT"),
                ("checkpoint_confirmed_text_hash", "TEXT"),
                ("current_confirmed_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_short_label_release_items",
            [
                ("run_id", "INTEGER"),
                ("decision_id", "INTEGER"),
                ("decision_run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("ledger_run_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("state_run_id", "INTEGER"),
                ("state_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("queue_bucket", "TEXT"),
                ("normalized_decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("policy_action", "TEXT"),
                ("policy_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("block_reason", "TEXT"),
                ("final_state", "TEXT"),
                ("review_state", "TEXT"),
                ("apply_state", "TEXT"),
                ("is_closed", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human", "INTEGER NOT NULL DEFAULT 0"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("token_impact", "TEXT"),
                ("token_status", "TEXT"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("token_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_codes_json", "TEXT"),
                ("queue_confirmed_text_hash", "TEXT"),
                ("current_confirmed_text_hash", "TEXT"),
                ("evidence_json", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_short_label_release_checkpoint_runs",
            [
                ("rule_version", "TEXT"),
                ("release_run_id", "INTEGER"),
                ("checkpoint_name", "TEXT"),
                ("checkpoint_status", "TEXT"),
                ("policy_name", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("agent_key", "TEXT"),
                ("decision_run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("min_released_required", "INTEGER NOT NULL DEFAULT 0"),
                ("max_blocked_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("release_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("release_blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("checkpoint_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("checkpoint_blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_closed_gain", "INTEGER NOT NULL DEFAULT 0"),
                ("safe_short_label_count", "INTEGER NOT NULL DEFAULT 0"),
                ("false_positive_reopen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("production_release_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("blocker_counts_json", "TEXT"),
                ("bucket_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_issue_short_label_release_checkpoint_items",
            [
                ("checkpoint_run_id", "INTEGER"),
                ("release_run_id", "INTEGER"),
                ("release_item_id", "INTEGER"),
                ("decision_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("ledger_item_id", "INTEGER"),
                ("state_run_id", "INTEGER"),
                ("state_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("queue_bucket", "TEXT"),
                ("normalized_decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("release_policy_action", "TEXT"),
                ("checkpoint_action", "TEXT"),
                ("checkpoint_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("block_reason", "TEXT"),
                ("final_state", "TEXT"),
                ("review_state", "TEXT"),
                ("apply_state", "TEXT"),
                ("is_closed", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human", "INTEGER NOT NULL DEFAULT 0"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("token_impact", "TEXT"),
                ("token_status", "TEXT"),
                ("text_length", "INTEGER NOT NULL DEFAULT 0"),
                ("token_count", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_codes_json", "TEXT"),
                ("release_confirmed_text_hash", "TEXT"),
                ("current_confirmed_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_state_runs",
            [
                ("rule_version", "TEXT"),
                ("active_score_run_id", "INTEGER"),
                ("candidate_score_run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("total_segments", "INTEGER NOT NULL DEFAULT 0"),
                ("closed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_count", "INTEGER NOT NULL DEFAULT 0"),
                ("output_apply_pending_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blank_valid_count", "INTEGER NOT NULL DEFAULT 0"),
                ("experimental_watch_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reopen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("notes_json", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_state_items",
            [
                ("run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("final_state", "TEXT"),
                ("state_group", "TEXT"),
                ("output_state", "TEXT"),
                ("review_state", "TEXT"),
                ("apply_state", "TEXT"),
                ("active_action", "TEXT"),
                ("candidate_action", "TEXT"),
                ("policy_action", "TEXT"),
                ("lifecycle_policy_action", "TEXT"),
                ("lifecycle_policy_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("confirmation_level", "TEXT"),
                ("confirmation_label", "TEXT"),
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("has_output", "INTEGER NOT NULL DEFAULT 0"),
                ("source_blank", "INTEGER NOT NULL DEFAULT 0"),
                ("confirmed_matches_output", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_output_apply", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_reopen", "INTEGER NOT NULL DEFAULT 0"),
                ("is_closed", "INTEGER NOT NULL DEFAULT 0"),
                ("priority_score", "REAL NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_output_apply_runs",
            [
                ("rule_version", "TEXT"),
                ("state_run_id", "INTEGER"),
                ("apply", "INTEGER NOT NULL DEFAULT 0"),
                ("limit_count", "INTEGER"),
                ("path_filter", "TEXT"),
                ("review_states", "TEXT"),
                ("include_auto_confirmed", "INTEGER NOT NULL DEFAULT 0"),
                ("allow_locked_token_override", "INTEGER NOT NULL DEFAULT 0"),
                ("require_token_policy_decision", "INTEGER NOT NULL DEFAULT 0"),
                ("token_policy_run_id", "INTEGER"),
                ("candidates_inspected", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("applied_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_count", "INTEGER NOT NULL DEFAULT 0"),
                ("token_mismatch_count", "INTEGER NOT NULL DEFAULT 0"),
                ("files_touched_count", "INTEGER NOT NULL DEFAULT 0"),
                ("backup_root", "TEXT"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_output_apply_items",
            [
                ("run_id", "INTEGER"),
                ("state_run_id", "INTEGER"),
                ("state_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("output_line_number", "INTEGER"),
                ("final_state", "TEXT"),
                ("review_state", "TEXT"),
                ("result_status", "TEXT"),
                ("applied", "INTEGER NOT NULL DEFAULT 0"),
                ("token_mismatch", "INTEGER NOT NULL DEFAULT 0"),
                ("previous_text_hash", "TEXT"),
                ("confirmed_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_audit_runs",
            [
                ("rule_version", "TEXT"),
                ("state_run_id", "INTEGER"),
                ("active_score_run_id", "INTEGER"),
                ("candidate_score_run_id", "INTEGER"),
                ("total_reopen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("mechanical_formatting_count", "INTEGER NOT NULL DEFAULT 0"),
                ("mechanical_text_replacement_count", "INTEGER NOT NULL DEFAULT 0"),
                ("weak_auto_count", "INTEGER NOT NULL DEFAULT 0"),
                ("human_curated_count", "INTEGER NOT NULL DEFAULT 0"),
                ("other_count", "INTEGER NOT NULL DEFAULT 0"),
                ("recommended_mechanical_review_count", "INTEGER NOT NULL DEFAULT 0"),
                ("exact_output_match_count", "INTEGER NOT NULL DEFAULT 0"),
                ("normalized_output_match_count", "INTEGER NOT NULL DEFAULT 0"),
                ("output_delta_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_audit_items",
            [
                ("run_id", "INTEGER"),
                ("state_run_id", "INTEGER"),
                ("state_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("confirmation_level", "TEXT"),
                ("confirmation_label", "TEXT"),
                ("label_family", "TEXT"),
                ("candidate_action", "TEXT"),
                ("active_action", "TEXT"),
                ("policy_action", "TEXT"),
                ("token_status", "TEXT"),
                ("risk_class", "TEXT"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_safe_probability", "REAL"),
                ("model_confidence", "REAL"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("recommendation", "TEXT"),
                ("review_priority", "REAL NOT NULL DEFAULT 0"),
                ("state_confirmed_matches_output", "INTEGER NOT NULL DEFAULT 0"),
                ("exact_confirmed_matches_output", "INTEGER NOT NULL DEFAULT 0"),
                ("normalized_confirmed_matches_output", "INTEGER NOT NULL DEFAULT 0"),
                ("output_match_kind", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_guarded_queue_runs",
            [
                ("rule_version", "TEXT"),
                ("audit_run_id", "INTEGER"),
                ("label_family", "TEXT"),
                ("recommendation_filter", "TEXT"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("selected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("guarded_policy_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_boundary_count", "INTEGER NOT NULL DEFAULT 0"),
                ("exact_output_match_count", "INTEGER NOT NULL DEFAULT 0"),
                ("display_equivalent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("text_delta_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("decisions_template_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_guarded_queue_items",
            [
                ("run_id", "INTEGER"),
                ("audit_run_id", "INTEGER"),
                ("audit_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("confirmation_label", "TEXT"),
                ("label_family", "TEXT"),
                ("recommendation", "TEXT"),
                ("suggested_decision", "TEXT"),
                ("policy_candidate", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_boundary", "INTEGER NOT NULL DEFAULT 0"),
                ("output_match_kind", "TEXT"),
                ("token_status", "TEXT"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("word_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_safe_probability", "REAL"),
                ("model_confidence", "REAL"),
                ("review_priority", "REAL NOT NULL DEFAULT 0"),
                ("queue_rank", "INTEGER NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_lifecycle_policy_runs",
            [
                ("rule_version", "TEXT"),
                ("queue_run_id", "INTEGER"),
                ("audit_run_id", "INTEGER"),
                ("policy_name", "TEXT"),
                ("label_family", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("released_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_boundary_count", "INTEGER NOT NULL DEFAULT 0"),
                ("invalid_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_lifecycle_policy_items",
            [
                ("run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("audit_run_id", "INTEGER"),
                ("audit_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("label_family", "TEXT"),
                ("confirmation_label", "TEXT"),
                ("policy_action", "TEXT"),
                ("policy_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("block_reason", "TEXT"),
                ("output_match_kind", "TEXT"),
                ("token_status", "TEXT"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_safe_probability", "REAL"),
                ("review_priority", "REAL NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_diagnostic_runs",
            [
                ("rule_version", "TEXT"),
                ("queue_run_id", "INTEGER"),
                ("audit_run_id", "INTEGER"),
                ("label_family", "TEXT NOT NULL DEFAULT 'mechanical_text_replacement'"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("subfamily_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_subagent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("candidate_short_policy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_boundary_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_diagnostic_items",
            [
                ("run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("audit_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("confirmation_label", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("suggested_agent_key", "TEXT"),
                ("recommendation", "TEXT"),
                ("risk_level", "TEXT"),
                ("select_cstring_count", "INTEGER NOT NULL DEFAULT 0"),
                ("concept_link_count", "INTEGER NOT NULL DEFAULT 0"),
                ("spanish_literal_hint_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ui_short_text", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_safe_probability", "REAL"),
                ("review_priority", "REAL NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_review_queue_runs",
            [
                ("rule_version", "TEXT"),
                ("diagnostic_run_id", "INTEGER"),
                ("agent_key", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("selected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_risk_count", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_risk_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_previously_queued_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("decisions_template_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_review_queue_items",
            [
                ("run_id", "INTEGER"),
                ("diagnostic_run_id", "INTEGER"),
                ("diagnostic_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("confirmation_label", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("suggested_agent_key", "TEXT"),
                ("risk_level", "TEXT"),
                ("select_cstring_count", "INTEGER NOT NULL DEFAULT 0"),
                ("spanish_literal_hint_count", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("model_safe_probability", "REAL"),
                ("review_priority", "REAL NOT NULL DEFAULT 0"),
                ("queue_rank", "INTEGER NOT NULL DEFAULT 0"),
                ("suggested_review_label", "TEXT NOT NULL DEFAULT 'pending'"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_review_decision_runs",
            [
                ("rule_version", "TEXT"),
                ("queue_run_id", "INTEGER"),
                ("agent_key", "TEXT"),
                ("decisions_path", "TEXT"),
                ("total_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("accepted_count", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_more_context_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_review_decisions",
            [
                ("run_id", "INTEGER"),
                ("queue_run_id", "INTEGER"),
                ("queue_item_id", "INTEGER"),
                ("diagnostic_run_id", "INTEGER"),
                ("diagnostic_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("risk_level", "TEXT"),
                ("decision", "TEXT"),
                ("evidence_label", "TEXT"),
                ("corrected_text", "TEXT"),
                ("notes", "TEXT"),
                ("reviewer", "TEXT"),
                ("current_confirmed_text_hash", "TEXT"),
                ("corrected_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_specialist_audit_runs",
            [
                ("rule_version", "TEXT"),
                ("min_reviewed", "INTEGER NOT NULL DEFAULT 0"),
                ("max_negative_rate_for_release", "REAL NOT NULL DEFAULT 0"),
                ("total_agents", "INTEGER NOT NULL DEFAULT 0"),
                ("no_evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_for_shadow_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_specialist_audit_items",
            [
                ("run_id", "INTEGER"),
                ("agent_key", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("diagnostic_count", "INTEGER NOT NULL DEFAULT 0"),
                ("queued_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_more_context_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_risk_reviewed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_rate", "REAL NOT NULL DEFAULT 0"),
                ("maturity_status", "TEXT"),
                ("recommended_action", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_shadow_policy_runs",
            [
                ("rule_version", "TEXT"),
                ("policy_name", "TEXT"),
                ("policy_status", "TEXT NOT NULL DEFAULT 'shadow'"),
                ("agent_key", "TEXT"),
                ("parent_agent_key", "TEXT"),
                ("diagnostic_run_id", "INTEGER"),
                ("specialist_audit_run_id", "INTEGER"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("shadow_ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_shadow_policy_items",
            [
                ("run_id", "INTEGER"),
                ("diagnostic_run_id", "INTEGER"),
                ("diagnostic_item_id", "INTEGER"),
                ("review_decision_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("shadow_status", "TEXT"),
                ("shadow_action", "TEXT"),
                ("block_reason", "TEXT"),
                ("pattern_match", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_evidence", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_evidence", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("select_cstring_count", "INTEGER NOT NULL DEFAULT 0"),
                ("concept_link_count", "INTEGER NOT NULL DEFAULT 0"),
                ("spanish_literal_hint_count", "INTEGER NOT NULL DEFAULT 0"),
                ("current_confirmed_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_policy_checkpoint_runs",
            [
                ("rule_version", "TEXT"),
                ("shadow_policy_run_id", "INTEGER"),
                ("policy_name", "TEXT"),
                ("checkpoint_name", "TEXT"),
                ("checkpoint_status", "TEXT"),
                ("agent_key", "TEXT"),
                ("parent_agent_key", "TEXT"),
                ("diagnostic_run_id", "INTEGER"),
                ("specialist_audit_run_id", "INTEGER"),
                ("min_ready_required", "INTEGER NOT NULL DEFAULT 0"),
                ("max_blocked_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("checkpoint_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("checkpoint_blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("release_action", "TEXT"),
                ("promotion_status", "TEXT"),
                ("production_release_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "auto_confirmation_reopen_text_policy_checkpoint_items",
            [
                ("checkpoint_run_id", "INTEGER"),
                ("shadow_policy_run_id", "INTEGER"),
                ("shadow_policy_item_id", "INTEGER"),
                ("diagnostic_item_id", "INTEGER"),
                ("review_decision_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("agent_key", "TEXT"),
                ("text_subfamily", "TEXT"),
                ("shadow_status", "TEXT"),
                ("shadow_action", "TEXT"),
                ("checkpoint_action", "TEXT"),
                ("checkpoint_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("block_reason", "TEXT"),
                ("pattern_match", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_evidence", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_evidence", "INTEGER NOT NULL DEFAULT 0"),
                ("issue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("select_cstring_count", "INTEGER NOT NULL DEFAULT 0"),
                ("concept_link_count", "INTEGER NOT NULL DEFAULT 0"),
                ("spanish_literal_hint_count", "INTEGER NOT NULL DEFAULT 0"),
                ("current_confirmed_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_policy_runs",
            [
                ("rule_version", "TEXT"),
                ("state_run_id", "INTEGER"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_count", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_count", "INTEGER NOT NULL DEFAULT 0"),
                ("low_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_review_count", "INTEGER NOT NULL DEFAULT 0"),
                ("policy_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_policy_items",
            [
                ("run_id", "INTEGER"),
                ("state_run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("review_state", "TEXT"),
                ("diff_kind", "TEXT"),
                ("policy_bucket", "TEXT"),
                ("risk_level", "TEXT"),
                ("recommendation", "TEXT"),
                ("auto_apply_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_human_review", "INTEGER NOT NULL DEFAULT 1"),
                ("missing_tokens_json", "TEXT"),
                ("extra_tokens_json", "TEXT"),
                ("issue_flags_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_policy_decision_runs",
            [
                ("rule_version", "TEXT"),
                ("policy_run_id", "INTEGER"),
                ("source_report", "TEXT"),
                ("decisions_path", "TEXT"),
                ("total_decisions", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rejected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("fix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_policy_decisions",
            [
                ("run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("policy_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("policy_bucket", "TEXT"),
                ("risk_level", "TEXT"),
                ("decision", "TEXT"),
                ("approved_for_apply", "INTEGER NOT NULL DEFAULT 0"),
                ("corrected_text", "TEXT"),
                ("notes", "TEXT"),
                ("reviewer", "TEXT"),
                ("confirmed_text_hash", "TEXT"),
                ("output_text_hash", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_evidence_runs",
            [
                ("rule_version", "TEXT"),
                ("policy_run_id", "INTEGER"),
                ("source_queue_path", "TEXT"),
                ("decisions_path", "TEXT"),
                ("total_decisions", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_more_context_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("skipped_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_evidence_items",
            [
                ("run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("policy_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("gender_subtype", "TEXT"),
                ("split_agent", "TEXT"),
                ("split_maturity", "TEXT"),
                ("method_signature", "TEXT"),
                ("evidence_label", "TEXT"),
                ("corrected_text", "TEXT"),
                ("notes", "TEXT"),
                ("reviewer", "TEXT"),
                ("hypothesis", "TEXT"),
                ("missing_tokens_json", "TEXT"),
                ("extra_tokens_json", "TEXT"),
                ("split_reasons_json", "TEXT"),
                ("confirmed_text_hash", "TEXT"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_promotion_audit_runs",
            [
                ("rule_version", "TEXT"),
                ("min_positive", "INTEGER NOT NULL DEFAULT 0"),
                ("total_agents", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_more_evidence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("boundary_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_promotion_audit_items",
            [
                ("run_id", "INTEGER"),
                ("split_agent", "TEXT"),
                ("total_evidence", "INTEGER NOT NULL DEFAULT 0"),
                ("positive_count", "INTEGER NOT NULL DEFAULT 0"),
                ("negative_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_more_context_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("distinct_method_count", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("recommended_action", "TEXT"),
                ("method_signatures_json", "TEXT"),
                ("sample_policy_item_ids_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_guarded_policy_runs",
            [
                ("rule_version", "TEXT"),
                ("policy_run_id", "INTEGER"),
                ("audit_run_id", "INTEGER"),
                ("min_positive", "INTEGER NOT NULL DEFAULT 0"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("guarded_ready_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("enabled_agent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_guarded_policy_items",
            [
                ("run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("policy_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("split_agent", "TEXT"),
                ("split_maturity", "TEXT"),
                ("method_signature", "TEXT"),
                ("guarded_status", "TEXT"),
                ("guarded_action", "TEXT"),
                ("target_policy_bucket", "TEXT"),
                ("target_risk_level", "TEXT"),
                ("apply_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("missing_tokens_json", "TEXT"),
                ("extra_tokens_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_bridge_dry_run_runs",
            [
                ("rule_version", "TEXT"),
                ("guarded_run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("would_bridge_count", "INTEGER NOT NULL DEFAULT 0"),
                ("blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("would_create_policy_decisions", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_gender_split_bridge_dry_run_items",
            [
                ("run_id", "INTEGER"),
                ("guarded_run_id", "INTEGER"),
                ("policy_run_id", "INTEGER"),
                ("policy_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("split_agent", "TEXT"),
                ("current_policy_bucket", "TEXT"),
                ("current_risk_level", "TEXT"),
                ("target_policy_bucket", "TEXT"),
                ("target_risk_level", "TEXT"),
                ("bridge_status", "TEXT"),
                ("bridge_action", "TEXT"),
                ("would_create_policy_decision", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_for_apply", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_policy_overlay_runs",
            [
                ("rule_version", "TEXT"),
                ("source_policy_run_id", "INTEGER"),
                ("source_state_run_id", "INTEGER"),
                ("source_rule_version", "TEXT"),
                ("overlay_name", "TEXT"),
                ("min_evidence", "INTEGER NOT NULL DEFAULT 0"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("original_critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("overlay_critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("released_critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("remaining_blocked_count", "INTEGER NOT NULL DEFAULT 0"),
                ("enabled_rule_count", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("notes_json", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "segment_token_policy_overlay_items",
            [
                ("run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("source_policy_item_id", "INTEGER"),
                ("state_run_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("original_policy_bucket", "TEXT"),
                ("original_risk_level", "TEXT"),
                ("overlay_policy_bucket", "TEXT"),
                ("overlay_risk_level", "TEXT"),
                ("overlay_action", "TEXT"),
                ("overlay_agent_key", "TEXT"),
                ("would_release_critical", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("decision", "TEXT"),
                ("rule_key", "TEXT"),
                ("reasons_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_checkpoints",
            [
                ("rule_version", "TEXT"),
                ("checkpoint_name", "TEXT"),
                ("checkpoint_scope", "TEXT"),
                ("coordinator_key", "TEXT"),
                ("source_policy_run_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_state_run_id", "INTEGER"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("base_critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("overlay_critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("released_critical_count", "INTEGER NOT NULL DEFAULT 0"),
                ("critical_queue_count", "INTEGER NOT NULL DEFAULT 0"),
                ("high_count", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_count", "INTEGER NOT NULL DEFAULT 0"),
                ("low_count", "INTEGER NOT NULL DEFAULT 0"),
                ("enabled_rule_count", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("active_agent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("operational_agent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("experimental_agent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("planned_agent_count", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("recommended_action", "TEXT"),
                ("blockers_json", "TEXT"),
                ("warnings_json", "TEXT"),
                ("metrics_json", "TEXT"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_registry",
            [
                ("gate_key", "TEXT"),
                ("coordinator_key", "TEXT"),
                ("active_checkpoint_id", "INTEGER"),
                ("active_guarded_checkpoint_id", "INTEGER"),
                ("active_overlay_run_id", "INTEGER"),
                ("active_policy_run_id", "INTEGER"),
                ("operational_state", "TEXT"),
                ("active_promotion_kind", "TEXT"),
                ("auto_apply_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("promoted_at", "TEXT"),
                ("promoted_by", "TEXT"),
                ("reason", "TEXT"),
                ("metrics_json", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_promotions",
            [
                ("gate_key", "TEXT"),
                ("checkpoint_id", "INTEGER"),
                ("guarded_checkpoint_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("previous_checkpoint_id", "INTEGER"),
                ("previous_overlay_run_id", "INTEGER"),
                ("decision", "TEXT"),
                ("policy_version", "TEXT"),
                ("promotion_kind", "TEXT"),
                ("auto_apply_allowed", "INTEGER NOT NULL DEFAULT 0"),
                ("reason", "TEXT"),
                ("blockers_json", "TEXT"),
                ("warnings_json", "TEXT"),
                ("metrics_json", "TEXT"),
                ("promoted_by", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_queue_runs",
            [
                ("rule_version", "TEXT"),
                ("gate_key", "TEXT"),
                ("checkpoint_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("guarded_checkpoint_id", "INTEGER"),
                ("source_mode", "TEXT"),
                ("shadow_queue_kind", "TEXT"),
                ("route_filter_csv", "TEXT"),
                ("risk_filter_csv", "TEXT"),
                ("critical_only", "INTEGER NOT NULL DEFAULT 1"),
                ("limit_count", "INTEGER"),
                ("total_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("critical_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("high_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("medium_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("low_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("route_counts_json", "TEXT"),
                ("bucket_counts_json", "TEXT"),
                ("priority_counts_json", "TEXT"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("decisions_template_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_queue_routes",
            [
                ("queue_run_id", "INTEGER"),
                ("suggested_route", "TEXT"),
                ("overlay_policy_bucket", "TEXT"),
                ("overlay_risk_level", "TEXT"),
                ("total", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_queue_items",
            [
                ("queue_run_id", "INTEGER"),
                ("gate_key", "TEXT"),
                ("checkpoint_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("guarded_checkpoint_id", "INTEGER"),
                ("policy_item_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("suggested_route", "TEXT"),
                ("overlay_policy_bucket", "TEXT"),
                ("overlay_risk_level", "TEXT"),
                ("priority_bucket", "TEXT"),
                ("rule_key", "TEXT"),
                ("hygiene_flags_json", "TEXT"),
                ("relative_path", "TEXT"),
                ("source_key", "TEXT"),
                ("source_line_number", "INTEGER"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_review_snapshots",
            [
                ("rule_version", "TEXT"),
                ("gate_key", "TEXT"),
                ("checkpoint_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("queued_items", "INTEGER NOT NULL DEFAULT 0"),
                ("unqueued_items", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_items", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_for_apply_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rejected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("fix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_subpolicy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("review_coverage_pct", "REAL NOT NULL DEFAULT 0"),
                ("queue_coverage_pct", "REAL NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_gate_review_route_status",
            [
                ("snapshot_id", "INTEGER"),
                ("suggested_route", "TEXT"),
                ("overlay_policy_bucket", "TEXT"),
                ("overlay_risk_level", "TEXT"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("queued_items", "INTEGER NOT NULL DEFAULT 0"),
                ("unqueued_items", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_items", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_for_apply_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rejected_count", "INTEGER NOT NULL DEFAULT 0"),
                ("fix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_subpolicy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("review_coverage_pct", "REAL NOT NULL DEFAULT 0"),
                ("queue_coverage_pct", "REAL NOT NULL DEFAULT 0"),
                ("latest_queue_run_id", "INTEGER"),
                ("latest_queue_total_rows", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_subpolicy_diagnostic_runs",
            [
                ("rule_version", "TEXT"),
                ("gate_key", "TEXT"),
                ("checkpoint_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_items", "INTEGER NOT NULL DEFAULT 0"),
                ("grouped_subpolicies", "INTEGER NOT NULL DEFAULT 0"),
                ("design_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("policy_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_more_review_count", "INTEGER NOT NULL DEFAULT 0"),
                ("queue_review_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("json_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_subpolicy_diagnostic_items",
            [
                ("run_id", "INTEGER"),
                ("suggested_route", "TEXT"),
                ("token_subtype", "TEXT"),
                ("overlay_policy_bucket", "TEXT"),
                ("overlay_risk_level", "TEXT"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("queued_items", "INTEGER NOT NULL DEFAULT 0"),
                ("unqueued_items", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_items", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_for_apply_count", "INTEGER NOT NULL DEFAULT 0"),
                ("accept_count", "INTEGER NOT NULL DEFAULT 0"),
                ("keep_manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reject_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_subpolicy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("fix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("review_coverage_pct", "REAL NOT NULL DEFAULT 0"),
                ("queue_coverage_pct", "REAL NOT NULL DEFAULT 0"),
                ("maturity_status", "TEXT"),
                ("confidence_band", "TEXT"),
                ("recommended_action", "TEXT"),
                ("sample_policy_item_ids_json", "TEXT"),
                ("sample_paths_json", "TEXT"),
                ("token_families_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_subpolicy_promotion_audit_runs",
            [
                ("rule_version", "TEXT"),
                ("gate_key", "TEXT"),
                ("checkpoint_id", "INTEGER"),
                ("overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("diagnostic_run_id", "INTEGER"),
                ("candidate_group_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rule_family_count", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_rule_count", "INTEGER NOT NULL DEFAULT 0"),
                ("collect_more_count", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_boundary_count", "INTEGER NOT NULL DEFAULT 0"),
                ("split_required_count", "INTEGER NOT NULL DEFAULT 0"),
                ("not_promotable_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_only_count", "INTEGER NOT NULL DEFAULT 0"),
                ("report_path", "TEXT"),
                ("csv_path", "TEXT"),
                ("jsonl_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_subpolicy_promotion_audit_items",
            [
                ("run_id", "INTEGER"),
                ("suggested_route", "TEXT"),
                ("token_subtype", "TEXT"),
                ("rule_key", "TEXT"),
                ("total_items", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_items", "INTEGER NOT NULL DEFAULT 0"),
                ("accept_count", "INTEGER NOT NULL DEFAULT 0"),
                ("keep_manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reject_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_subpolicy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("fix_count", "INTEGER NOT NULL DEFAULT 0"),
                ("text_cleanup_block_count", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_for_apply_count", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("confidence_band", "TEXT"),
                ("recommended_action", "TEXT"),
                ("sample_policy_item_ids_json", "TEXT"),
                ("sample_paths_json", "TEXT"),
                ("token_signature_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_guarded_overlay_checkpoints",
            [
                ("rule_version", "TEXT"),
                ("checkpoint_name", "TEXT"),
                ("gate_key", "TEXT"),
                ("overlay_run_id", "INTEGER"),
                ("parent_overlay_run_id", "INTEGER"),
                ("source_policy_run_id", "INTEGER"),
                ("promotion_audit_run_id", "INTEGER"),
                ("ready_rule_count", "INTEGER NOT NULL DEFAULT 0"),
                ("total_candidates", "INTEGER NOT NULL DEFAULT 0"),
                ("guarded_release_count", "INTEGER NOT NULL DEFAULT 0"),
                ("release_rate_pct", "REAL NOT NULL DEFAULT 0"),
                ("medium_to_low_count", "INTEGER NOT NULL DEFAULT 0"),
                ("invalid_release_count", "INTEGER NOT NULL DEFAULT 0"),
                ("apply_allowed_count", "INTEGER NOT NULL DEFAULT 0"),
                ("active_gate_overlay_run_id", "INTEGER"),
                ("active_gate_unchanged", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("recommended_action", "TEXT"),
                ("blockers_json", "TEXT"),
                ("warnings_json", "TEXT"),
                ("metrics_json", "TEXT"),
                ("report_path", "TEXT"),
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "ml_composite_guarded_overlay_checkpoint_rules",
            [
                ("checkpoint_id", "INTEGER"),
                ("suggested_route", "TEXT"),
                ("token_subtype", "TEXT"),
                ("rule_key", "TEXT"),
                ("release_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_items", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_items", "INTEGER NOT NULL DEFAULT 0"),
                ("accept_count", "INTEGER NOT NULL DEFAULT 0"),
                ("keep_manual_exception_count", "INTEGER NOT NULL DEFAULT 0"),
                ("reject_count", "INTEGER NOT NULL DEFAULT 0"),
                ("needs_subpolicy_count", "INTEGER NOT NULL DEFAULT 0"),
                ("promotion_status", "TEXT"),
                ("sample_policy_item_ids_json", "TEXT"),
                ("created_at", "TEXT"),
            ],
        )
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_segments_key
        ON source_segments(source_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_segments_relative_path
        ON source_segments(relative_path)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_segments_active
        ON source_segments(is_active)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_protected_tokens_segment
        ON protected_tokens(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_memory_lookup
        ON translation_memory(source_language, target_language, source_hash, target_hash, origin)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_memory_source_hash
        ON translation_memory(source_language, target_language, source_hash)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_suggestions_segment
        ON translation_suggestions(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_suggestions_status
        ON translation_suggestions(status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_segment
        ON suggestion_feedback(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_suggestion
        ON suggestion_feedback(suggestion_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_decision
        ON suggestion_feedback(decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_segment_decision
        ON suggestion_feedback(segment_id, decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_suggestion_decision
        ON suggestion_feedback(suggestion_id, decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_translation_suggestions_status_segment
        ON translation_suggestions(status, segment_id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_suggestion_feedback_pending_unique
        ON suggestion_feedback(suggestion_id)
        WHERE decision = 'pending' AND suggestion_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_inline_fragments_segment
        ON inline_fragments(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_inline_fragments_translate
        ON inline_fragments(should_translate, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_run
        ON local_learning_candidates(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_segment
        ON local_learning_candidates(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_feedback
        ON local_learning_candidates(feedback_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_suggestion
        ON local_learning_candidates(suggestion_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_offline_proposal
        ON local_learning_candidates(offline_proposal_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_label
        ON local_learning_candidates(human_label, local_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_learned
        ON local_learning_candidates(human_label, learned_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_candidates_confirmation_sync
        ON local_learning_candidates(human_label, confirmation_synced_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_confirmations_level
        ON segment_confirmations(confirmation_level, locked)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_confirmations_candidate
        ON segment_confirmations(candidate_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_confirmations_feedback
        ON segment_confirmations(feedback_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_learning_pattern_stats_weight
        ON local_learning_pattern_stats(weight_adjustment)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_name_equivalences_status
        ON name_equivalences(status, source_kind)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_name_equivalences_family
        ON name_equivalences(name_family, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_learned_validation_items_run_action
        ON learned_validation_items(run_id, action, risk_class)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_learned_validation_items_segment
        ON learned_validation_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_title_review_queue_status
        ON title_review_queue(status, bucket)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_title_review_queue_segment
        ON title_review_queue(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_package_focus_queue_group_status
        ON package_focus_queue(focus_group, status, priority_score)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_finalization_queue_bucket_status
        ON finalization_queue(status, closure_bucket, priority_score)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_finalization_queue_path
        ON finalization_queue(relative_path, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mojibake_context_queue_kind
        ON mojibake_context_queue(status, residue_kind, priority_score)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mojibake_context_queue_path
        ON mojibake_context_queue(relative_path, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_offline_proposals_run_status
        ON offline_proposals(run_id, status, candidate_bucket)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_offline_proposals_segment
        ON offline_proposals(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_training_examples_run_label
        ON ml_training_examples(run_id, label, action_label)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_training_examples_segment
        ON ml_training_examples(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_training_examples_evidence
        ON ml_training_examples(evidence_source, evidence_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_model_runs_dataset
        ON ml_model_runs(dataset_run_id, model_kind)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_score_items_run_action
        ON ml_score_items(run_id, final_action, risk_class)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_score_items_segment
        ON ml_score_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_holdout_eval_runs_dataset
        ON ml_holdout_eval_runs(dataset_run_id, finished_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_policy_runs_score
        ON ml_policy_runs(score_run_id, rule_version)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_policy_items_run_action
        ON ml_policy_items(run_id, policy_action, policy_group)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_policy_items_segment
        ON ml_policy_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_model_promotions_kind_created
        ON ml_model_promotions(model_kind, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_specialist_policy_created
        ON ml_specialist_policy_snapshots(created_at, specialist_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_specialist_policy_status
        ON ml_specialist_policy_snapshots(status, pending_real_count)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_agent_registry_status
        ON ml_agent_registry(status, operational_state, agent_type)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_agent_registry_parent
        ON ml_agent_registry(parent_agent_key, agent_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_agent_routing_runs_created
        ON ml_agent_routing_runs(started_at, coordinator_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_agent_routing_items_run_agent
        ON ml_agent_routing_items(run_id, route_agent_key, route_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_agent_routing_items_segment
        ON ml_agent_routing_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_agent_recommendations_run_status
        ON ml_agent_recommendations(run_id, status, proposed_agent_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_ledger_runs_state
        ON ml_issue_ledger_runs(segment_state_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_ledger_items_run_family
        ON ml_issue_ledger_items(run_id, issue_family, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_ledger_items_run_agent
        ON ml_issue_ledger_items(run_id, agent_key, route_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_ledger_items_segment
        ON ml_issue_ledger_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_queue_runs_agent
        ON ml_issue_review_queue_runs(agent_key, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_queue_items_run_bucket
        ON ml_issue_review_queue_items(run_id, queue_bucket, review_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_queue_items_agent_status
        ON ml_issue_review_queue_items(agent_key, review_status, issue_family)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_queue_items_segment
        ON ml_issue_review_queue_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_decision_runs_queue
        ON ml_issue_review_decision_runs(queue_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_decisions_agent
        ON ml_issue_review_decisions(agent_key, evidence_label, normalized_decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_decisions_segment
        ON ml_issue_review_decisions(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_review_decisions_ledger
        ON ml_issue_review_decisions(ledger_run_id, ledger_item_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_shadow_runs_agent
        ON ml_issue_gender_subpolicy_shadow_runs(agent_key, policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_shadow_items_run_subpolicy
        ON ml_issue_gender_subpolicy_shadow_items(run_id, subpolicy_name, shadow_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_shadow_items_segment
        ON ml_issue_gender_subpolicy_shadow_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_checkpoint_runs_status
        ON ml_issue_gender_boundary_checkpoint_runs(agent_key, checkpoint_status, promotion_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_checkpoint_runs_shadow
        ON ml_issue_gender_boundary_checkpoint_runs(shadow_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_checkpoint_items_status
        ON ml_issue_gender_boundary_checkpoint_items(checkpoint_run_id, checkpoint_allowed, block_reason)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_checkpoint_items_subpolicy
        ON ml_issue_gender_boundary_checkpoint_items(checkpoint_run_id, subpolicy_name, checkpoint_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_checkpoint_items_segment
        ON ml_issue_gender_boundary_checkpoint_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_lifecycle_runs_status
        ON ml_issue_gender_boundary_lifecycle_runs(policy_status, policy_name, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_lifecycle_runs_checkpoint
        ON ml_issue_gender_boundary_lifecycle_runs(checkpoint_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_lifecycle_items_status
        ON ml_issue_gender_boundary_lifecycle_items(run_id, policy_allowed, block_reason)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_lifecycle_items_subpolicy
        ON ml_issue_gender_boundary_lifecycle_items(run_id, subpolicy_name, policy_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_gender_boundary_lifecycle_items_segment
        ON ml_issue_gender_boundary_lifecycle_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_runs_agent
        ON ml_issue_trigger_gender_role_surface_runs(agent_key, policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_items_run_subpolicy
        ON ml_issue_trigger_gender_role_surface_items(run_id, subpolicy_name, shadow_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_items_segment
        ON ml_issue_trigger_gender_role_surface_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_items_decision
        ON ml_issue_trigger_gender_role_surface_items(decision_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_checkpoint_runs_status
        ON ml_issue_trigger_gender_role_checkpoint_runs(agent_key, checkpoint_status, promotion_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_checkpoint_runs_shadow
        ON ml_issue_trigger_gender_role_checkpoint_runs(shadow_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_checkpoint_items_status
        ON ml_issue_trigger_gender_role_checkpoint_items(checkpoint_run_id, checkpoint_allowed, block_reason)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_checkpoint_items_subpolicy
        ON ml_issue_trigger_gender_role_checkpoint_items(checkpoint_run_id, subpolicy_name, checkpoint_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_checkpoint_items_segment
        ON ml_issue_trigger_gender_role_checkpoint_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_lifecycle_runs_status
        ON ml_issue_trigger_gender_role_lifecycle_runs(policy_status, policy_name, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_lifecycle_runs_checkpoint
        ON ml_issue_trigger_gender_role_lifecycle_runs(checkpoint_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_lifecycle_items_status
        ON ml_issue_trigger_gender_role_lifecycle_items(run_id, policy_allowed, block_reason)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_lifecycle_items_subpolicy
        ON ml_issue_trigger_gender_role_lifecycle_items(run_id, subpolicy_name, policy_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_trigger_gender_role_lifecycle_items_segment
        ON ml_issue_trigger_gender_role_lifecycle_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_release_runs_status
        ON ml_issue_short_label_release_runs(agent_key, policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_release_items_run_allowed
        ON ml_issue_short_label_release_items(run_id, policy_allowed, block_reason)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_release_items_segment
        ON ml_issue_short_label_release_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_release_items_decision
        ON ml_issue_short_label_release_items(decision_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_checkpoint_runs_status
        ON ml_issue_short_label_release_checkpoint_runs(agent_key, checkpoint_status, promotion_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_checkpoint_runs_release
        ON ml_issue_short_label_release_checkpoint_runs(release_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_checkpoint_items_status
        ON ml_issue_short_label_release_checkpoint_items(checkpoint_run_id, checkpoint_allowed, block_reason)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_short_label_checkpoint_items_segment
        ON ml_issue_short_label_release_checkpoint_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_partial_coverage_runs_ledger
        ON ml_issue_partial_coverage_runs(ledger_run_id, segment_state_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_partial_coverage_items_run_state
        ON ml_issue_partial_coverage_items(run_id, coverage_state, review_state)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_partial_coverage_items_segment
        ON ml_issue_partial_coverage_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_issue_partial_coverage_items_open
        ON ml_issue_partial_coverage_items(run_id, open_issue_count, total_issue_count)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_state_runs_created
        ON segment_state_runs(started_at, rule_version)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_state_items_run_state
        ON segment_state_items(run_id, state_group, final_state)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_state_items_run_apply
        ON segment_state_items(run_id, apply_state, needs_output_apply)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_state_items_segment
        ON segment_state_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_output_apply_runs_created
        ON segment_output_apply_runs(started_at, apply)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_output_apply_items_run_status
        ON segment_output_apply_items(run_id, result_status, applied)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_output_apply_items_segment
        ON segment_output_apply_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_output_apply_items_path
        ON segment_output_apply_items(relative_path, review_state)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_audit_runs_state
        ON auto_confirmation_reopen_audit_runs(state_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_audit_items_family
        ON auto_confirmation_reopen_audit_items(run_id, label_family, recommendation)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_audit_items_segment
        ON auto_confirmation_reopen_audit_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_guarded_queue_runs_audit
        ON auto_confirmation_reopen_guarded_queue_runs(audit_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_guarded_queue_items_status
        ON auto_confirmation_reopen_guarded_queue_items(run_id, label_family, suggested_decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_guarded_queue_items_segment
        ON auto_confirmation_reopen_guarded_queue_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_lifecycle_policy_runs_status
        ON auto_confirmation_reopen_lifecycle_policy_runs(policy_status, label_family, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_lifecycle_policy_items_run_segment
        ON auto_confirmation_reopen_lifecycle_policy_items(run_id, segment_id, policy_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_diagnostic_runs_queue
        ON auto_confirmation_reopen_text_diagnostic_runs(queue_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_diagnostic_items_family
        ON auto_confirmation_reopen_text_diagnostic_items(run_id, text_subfamily, recommendation)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_diagnostic_items_segment
        ON auto_confirmation_reopen_text_diagnostic_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_review_runs_agent
        ON auto_confirmation_reopen_text_review_queue_runs(agent_key, diagnostic_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_review_items_run
        ON auto_confirmation_reopen_text_review_queue_items(run_id, risk_level, queue_rank)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_review_items_segment
        ON auto_confirmation_reopen_text_review_queue_items(segment_id, suggested_agent_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_decision_runs_queue
        ON auto_confirmation_reopen_text_review_decision_runs(queue_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_decisions_agent
        ON auto_confirmation_reopen_text_review_decisions(agent_key, text_subfamily, evidence_label)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_decisions_segment
        ON auto_confirmation_reopen_text_review_decisions(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_specialist_audit_items_status
        ON auto_confirmation_reopen_text_specialist_audit_items(run_id, maturity_status, agent_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_shadow_runs_agent
        ON auto_confirmation_reopen_text_shadow_policy_runs(agent_key, policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_shadow_items_status
        ON auto_confirmation_reopen_text_shadow_policy_items(run_id, shadow_status, agent_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_shadow_items_segment
        ON auto_confirmation_reopen_text_shadow_policy_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_runs_status
        ON auto_confirmation_reopen_text_boundary_policy_runs(policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_items_agent
        ON auto_confirmation_reopen_text_boundary_policy_items(run_id, boundary_agent_key, boundary_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_items_segment
        ON auto_confirmation_reopen_text_boundary_policy_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_runs_scope
        ON auto_confirmation_reopen_text_boundary_repair_queue_runs(queue_scope, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_items_policy
        ON auto_confirmation_reopen_text_boundary_repair_queue_items(run_id, boundary_policy, repair_route)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_items_segment
        ON auto_confirmation_reopen_text_boundary_repair_queue_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_shadow_runs_status
        ON auto_confirmation_reopen_text_boundary_repair_shadow_runs(policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_shadow_items_status
        ON auto_confirmation_reopen_text_boundary_repair_shadow_items(run_id, shadow_status, boundary_policy)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_shadow_items_segment
        ON auto_confirmation_reopen_text_boundary_repair_shadow_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_checkpoint_runs_status
        ON auto_confirmation_reopen_text_boundary_repair_checkpoint_runs(checkpoint_status, promotion_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_checkpoint_items_status
        ON auto_confirmation_reopen_text_boundary_repair_checkpoint_items(checkpoint_run_id, checkpoint_allowed, boundary_policy)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_lifecycle_runs_status
        ON auto_confirmation_reopen_text_boundary_repair_lifecycle_runs(policy_status, policy_name, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_lifecycle_items_policy
        ON auto_confirmation_reopen_text_boundary_repair_lifecycle_items(run_id, policy_allowed, boundary_policy)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_bridge_runs_status
        ON auto_confirmation_reopen_text_boundary_token_policy_bridge_runs(bridge_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_bridge_items_status
        ON auto_confirmation_reopen_text_boundary_token_policy_bridge_items(run_id, bridge_status, policy_bucket, risk_level)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_bridge_items_policy
        ON auto_confirmation_reopen_text_boundary_token_policy_bridge_items(run_id, boundary_policy, bridge_action)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_runs_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs(subpolicy_name, subpolicy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_items_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items(run_id, subpolicy_status, policy_bucket)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs(checkpoint_status, promotion_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items(checkpoint_run_id, checkpoint_allowed, policy_bucket)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs(policy_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items(run_id, policy_allowed, policy_bucket, subpolicy_name)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs(audit_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items_status
        ON auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items(run_id, eligible_controlled_production, policy_bucket, subpolicy_name)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_production_audit_runs_status
        ON auto_confirmation_reopen_text_boundary_repair_production_audit_runs(audit_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_boundary_repair_production_audit_items_status
        ON auto_confirmation_reopen_text_boundary_repair_production_audit_items(run_id, eligible_controlled_production, boundary_policy, shadow_action)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_checkpoint_runs_status
        ON auto_confirmation_reopen_text_policy_checkpoint_runs(agent_key, checkpoint_status, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_checkpoint_runs_shadow
        ON auto_confirmation_reopen_text_policy_checkpoint_runs(shadow_policy_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_checkpoint_items_allowed
        ON auto_confirmation_reopen_text_policy_checkpoint_items(checkpoint_run_id, checkpoint_allowed, agent_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_confirmation_reopen_text_checkpoint_items_segment
        ON auto_confirmation_reopen_text_policy_checkpoint_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_runs_created
        ON segment_token_policy_runs(started_at, state_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_items_run_bucket
        ON segment_token_policy_items(run_id, policy_bucket, risk_level)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_items_segment
        ON segment_token_policy_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_decision_runs_policy
        ON segment_token_policy_decision_runs(policy_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_decisions_segment
        ON segment_token_policy_decisions(segment_id, approved_for_apply)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_decisions_policy_bucket
        ON segment_token_policy_decisions(policy_run_id, policy_bucket, approved_for_apply)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_evidence_items_agent
        ON segment_token_gender_split_evidence_items(split_agent, evidence_label)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_evidence_items_policy
        ON segment_token_gender_split_evidence_items(policy_run_id, policy_item_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_promotion_items_status
        ON segment_token_gender_split_promotion_audit_items(run_id, promotion_status, split_agent)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_guarded_runs_created
        ON segment_token_gender_split_guarded_policy_runs(started_at, policy_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_guarded_items_status
        ON segment_token_gender_split_guarded_policy_items(run_id, guarded_status, split_agent)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_bridge_runs_created
        ON segment_token_gender_split_bridge_dry_run_runs(started_at, policy_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_gender_split_bridge_items_status
        ON segment_token_gender_split_bridge_dry_run_items(run_id, bridge_status, split_agent)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_overlay_runs_source
        ON segment_token_policy_overlay_runs(source_policy_run_id, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_overlay_items_run_action
        ON segment_token_policy_overlay_items(run_id, overlay_action, overlay_risk_level)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_segment_token_policy_overlay_items_segment
        ON segment_token_policy_overlay_items(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_checkpoints_created
        ON ml_composite_checkpoints(started_at, checkpoint_scope, promotion_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_promotions_created
        ON ml_composite_gate_promotions(created_at, gate_key, decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_queue_runs_created
        ON ml_composite_gate_queue_runs(started_at, gate_key, overlay_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_queue_routes_run
        ON ml_composite_gate_queue_routes(queue_run_id, suggested_route)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_queue_items_policy
        ON ml_composite_gate_queue_items(gate_key, overlay_run_id, policy_item_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_queue_items_route
        ON ml_composite_gate_queue_items(gate_key, overlay_run_id, suggested_route)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_queue_runs_guarded_checkpoint
        ON ml_composite_gate_queue_runs(guarded_checkpoint_id, shadow_queue_kind, started_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_queue_items_guarded_checkpoint
        ON ml_composite_gate_queue_items(guarded_checkpoint_id, priority_bucket, rule_key)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_review_snapshots_created
        ON ml_composite_gate_review_snapshots(started_at, gate_key, overlay_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_gate_review_route_status_snapshot
        ON ml_composite_gate_review_route_status(snapshot_id, suggested_route)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_subpolicy_diag_runs_created
        ON ml_composite_subpolicy_diagnostic_runs(started_at, gate_key, overlay_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_subpolicy_diag_items_run
        ON ml_composite_subpolicy_diagnostic_items(run_id, maturity_status, suggested_route)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_subpolicy_promotion_runs_created
        ON ml_composite_subpolicy_promotion_audit_runs(started_at, gate_key, overlay_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_subpolicy_promotion_items_run
        ON ml_composite_subpolicy_promotion_audit_items(run_id, promotion_status, suggested_route)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_package_versions_parent
        ON package_versions(parent_version_id, version_number)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_package_version_items_version_score
        ON package_version_items(version_id, score, segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_package_version_items_segment
        ON package_version_items(segment_id, version_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_package_version_changes_version_delta
        ON package_version_changes(version_id, score_delta, segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_guarded_overlay_checkpoints_created
        ON ml_composite_guarded_overlay_checkpoints(started_at, gate_key, overlay_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ml_composite_guarded_overlay_checkpoint_rules_checkpoint
        ON ml_composite_guarded_overlay_checkpoint_rules(checkpoint_id, rule_key)
        """
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (migration_name, applied_at)
        VALUES (?, ?)
        """,
        ("0001_initial_schema", utc_now()),
    )
    conn.commit()
    return changes


def main() -> None:
    settings = load_settings()
    started_at = datetime.now()
    print("[db] Starting database setup")
    print(f"[db] Settings: {SETTINGS_PATH}")
    print(f"[db] Database: {get_database_path(settings)}")

    with connect(settings) as conn:
        changes = ensure_database(conn)
        tables = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
            """
        ).fetchall()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Database setup report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Database: {get_database_path(settings)}",
        "",
        "Tables:",
        *[f"- {row['name']}" for row in tables],
        "",
        "Schema updates:",
        *(f"- Added {change}" for change in changes),
    ]
    if not changes:
        report_lines.append("- No incremental column updates needed")

    report_path = write_report(settings, "db", report_lines)
    print(f"[db] Tables ready: {len(tables)}")
    print(f"[db] Schema updates: {len(changes)}")
    print(f"[db] Report: {report_path}")
    print("[db] Done")


if __name__ == "__main__":
    main()
