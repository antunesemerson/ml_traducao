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
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
