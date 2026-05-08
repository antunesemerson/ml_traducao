from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


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
        CREATE TABLE IF NOT EXISTS api_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER NOT NULL,
            suggestion_id INTEGER,
            segment_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            decision_suggested TEXT NOT NULL,
            corrected_text TEXT,
            confidence_score REAL NOT NULL,
            reason TEXT,
            detected_issues_json TEXT,
            token_validation_status TEXT NOT NULL,
            token_validation_details_json TEXT,
            api_response_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending_human',
            reviewed_by TEXT,
            human_decision TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(feedback_id) REFERENCES suggestion_feedback(id) ON DELETE CASCADE,
            FOREIGN KEY(suggestion_id) REFERENCES translation_suggestions(id) ON DELETE SET NULL,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(feedback_id, model, prompt_version)
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
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ],
        )
    )
    changes.extend(
        ensure_columns(
            conn,
            "api_reviews",
            [
                ("feedback_id", "INTEGER"),
                ("suggestion_id", "INTEGER"),
                ("segment_id", "INTEGER"),
                ("model", "TEXT"),
                ("prompt_version", "TEXT"),
                ("decision_suggested", "TEXT"),
                ("corrected_text", "TEXT"),
                ("confidence_score", "REAL"),
                ("reason", "TEXT"),
                ("detected_issues_json", "TEXT"),
                ("token_validation_status", "TEXT"),
                ("token_validation_details_json", "TEXT"),
                ("api_response_json", "TEXT"),
                ("status", "TEXT NOT NULL DEFAULT 'pending_human'"),
                ("reviewed_by", "TEXT"),
                ("human_decision", "TEXT"),
                ("created_at", "TEXT"),
                ("reviewed_at", "TEXT"),
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
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
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
        CREATE INDEX IF NOT EXISTS idx_api_reviews_feedback
        ON api_reviews(feedback_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_api_reviews_segment
        ON api_reviews(segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_api_reviews_status
        ON api_reviews(status)
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
