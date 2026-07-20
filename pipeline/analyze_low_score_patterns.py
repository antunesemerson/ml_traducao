from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

import db


def _rows(conn, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def _connect_read_only() -> sqlite3.Connection:
    database_path = db.get_database_path().resolve()
    conn = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 300000")
    return conn


def analyze(run_id: int | None = None, threshold: float = 0.5) -> dict[str, Any]:
    conn = _connect_read_only()
    try:
        if run_id is None:
            row = conn.execute(
                "SELECT id FROM ml_score_runs WHERE candidate_text_source = 'output' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                raise RuntimeError("No output score run is available.")
            run_id = int(row["id"])
        params = (run_id, threshold)
        score_run = conn.execute(
            """
            SELECT id, rule_version, model_run_id, model_version, candidate_text_source,
                   candidate_tree_hash, scored_count, started_at, finished_at
            FROM ml_score_runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        latest_epoch = conn.execute(
            """
            SELECT id, epoch_key, status, old_score_run_id, output_score_run_id,
                   segment_state_run_id, created_at, scored_at, evaluated_at,
                   published_at, updated_at
            FROM quality_epochs
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return {
            "score_run_id": run_id,
            "threshold": threshold,
            "score_run": dict(score_run) if score_run else None,
            "latest_quality_epoch": dict(latest_epoch) if latest_epoch else None,
            "measurement": _rows(
                conn,
                """
                SELECT COUNT(*) AS measured_segments,
                       SUM(model_safe_probability < ?) AS below_threshold,
                       ROUND(100.0 * SUM(model_safe_probability < ?) / COUNT(*), 2) AS below_threshold_pct,
                       SUM(model_safe_probability IS NULL) AS unmeasured,
                       ROUND(AVG(model_safe_probability), 6) AS global_average_score
                FROM ml_score_items WHERE run_id = ?
                """,
                (threshold, threshold, run_id),
            )[0],
            "low_score_evidence_coverage": _rows(
                conn,
                """
                SELECT COUNT(*) AS low_score_segments,
                       SUM(issue_count > 0) AS with_explicit_issue,
                       SUM(issue_count = 0) AS without_explicit_issue,
                       SUM(token_status = 'mismatch') AS token_mismatch,
                       SUM(final_action = 'auto_safe') AS deterministic_auto_safe,
                       SUM(final_action IN ('needs_autofix', 'blocked_structure')) AS machine_actionable_or_blocked
                FROM ml_score_items
                WHERE run_id = ? AND model_safe_probability < ?
                """,
                params,
            )[0],
            "text_relations": _rows(
                conn,
                """
                SELECT CASE
                         WHEN item.candidate_text = source.old_text
                          AND item.candidate_text = source.spanish_text THEN 'equals_old_and_spanish'
                         WHEN item.candidate_text = source.old_text THEN 'equals_old'
                         WHEN item.candidate_text = source.spanish_text THEN 'equals_spanish'
                         WHEN item.candidate_text = source.english_text THEN 'equals_english'
                         ELSE 'distinct_candidate'
                       END AS relation,
                       item.final_action,
                       COUNT(*) AS segments,
                       SUM(item.issue_count > 0) AS with_explicit_issue,
                       ROUND(AVG(item.model_safe_probability), 4) AS avg_score
                FROM ml_score_items AS item
                JOIN source_segments AS source ON source.id = item.segment_id
                WHERE item.run_id = ? AND item.model_safe_probability < ?
                GROUP BY relation, item.final_action
                ORDER BY segments DESC
                """,
                params,
            ),
            "bands": _rows(
                conn,
                """
                SELECT CASE
                         WHEN model_safe_probability < 0.20 THEN 'critical'
                         WHEN model_safe_probability < 0.50 THEN 'low'
                         WHEN model_safe_probability < 0.75 THEN 'moderate'
                         WHEN model_safe_probability < 0.90 THEN 'good'
                         ELSE 'high'
                       END AS band,
                       COUNT(*) AS segments,
                       ROUND(AVG(model_safe_probability), 4) AS avg_score
                FROM ml_score_items WHERE run_id = ?
                GROUP BY band ORDER BY avg_score
                """,
                (run_id,),
            ),
            "action_risk": _rows(
                conn,
                """
                SELECT final_action, token_status, risk_class, COUNT(*) AS segments,
                       ROUND(AVG(model_safe_probability), 4) AS avg_score
                FROM ml_score_items
                WHERE run_id = ? AND model_safe_probability < ?
                GROUP BY final_action, token_status, risk_class
                ORDER BY segments DESC LIMIT 30
                """,
                params,
            ),
            "path_families": _rows(
                conn,
                """
                SELECT CASE WHEN instr(relative_path, '/') > 0
                         THEN substr(relative_path, 1, instr(relative_path, '/') - 1)
                         ELSE relative_path END AS family,
                       COUNT(*) AS segments, ROUND(AVG(model_safe_probability), 4) AS avg_score
                FROM ml_score_items
                WHERE run_id = ? AND model_safe_probability < ?
                GROUP BY family ORDER BY segments DESC LIMIT 30
                """,
                params,
            ),
            "word_bands": _rows(
                conn,
                """
                SELECT CASE WHEN word_count <= 3 THEN '0-3'
                         WHEN word_count <= 8 THEN '4-8'
                         WHEN word_count <= 20 THEN '9-20'
                         WHEN word_count <= 50 THEN '21-50'
                         ELSE '51+' END AS word_band,
                       COUNT(*) AS segments, ROUND(AVG(model_safe_probability), 4) AS avg_score,
                       ROUND(AVG(issue_count), 2) AS avg_issues
                FROM ml_score_items
                WHERE run_id = ? AND model_safe_probability < ?
                GROUP BY word_band ORDER BY MIN(word_count)
                """,
                params,
            ),
            "issues": _rows(
                conn,
                """
                SELECT COALESCE(json_extract(issue.value, '$.code'), json_extract(issue.value, '$.type')) AS issue_type,
                       json_extract(issue.value, '$.severity') AS severity,
                       COUNT(*) AS segments, ROUND(AVG(item.model_safe_probability), 4) AS avg_score
                FROM ml_score_items AS item, json_each(item.issues_json) AS issue
                WHERE item.run_id = ? AND item.model_safe_probability < ?
                GROUP BY issue_type, severity ORDER BY segments DESC LIMIT 40
                """,
                params,
            ),
            "issue_unique_coverage": _rows(
                conn,
                """
                WITH issue_segments AS (
                    SELECT item.segment_id,
                           COALESCE(json_extract(issue.value, '$.code'),
                                    json_extract(issue.value, '$.type')) AS issue_type
                    FROM ml_score_items AS item, json_each(item.issues_json) AS issue
                    WHERE item.run_id = ? AND item.model_safe_probability < ?
                )
                SELECT COUNT(DISTINCT segment_id) AS segments_with_any_issue,
                       COUNT(DISTINCT CASE WHEN issue_type IN (
                           'space_before_punctuation', 'missing_space_after_token',
                           'missing_space_before_token', 'spanish_punctuation',
                           'stray_leading_question_mark'
                       ) THEN segment_id END) AS punctuation_or_spacing,
                       COUNT(DISTINCT CASE WHEN issue_type LIKE 'spanish_residue%'
                           THEN segment_id END) AS spanish_residue,
                       COUNT(DISTINCT CASE WHEN issue_type LIKE '%mojibake%'
                           OR issue_type = 'utf8_mojibake_sequence'
                           THEN segment_id END) AS encoding_or_mojibake,
                       COUNT(DISTINCT CASE WHEN issue_type LIKE '%gender_token%'
                           OR issue_type = 'leading_gender_article_token'
                           OR issue_type = 'neutral_word_with_gender_token'
                           THEN segment_id END) AS gender_token,
                       COUNT(DISTINCT CASE WHEN issue_type = 'unnatural_portuguese_fragment'
                           THEN segment_id END) AS unnatural_portuguese
                FROM issue_segments
                """,
                params,
            )[0],
            "diagnostic_cohorts": _rows(
                conn,
                """
                SELECT cohort, COUNT(*) AS segments,
                       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS share_of_low_pct,
                       ROUND(AVG(model_safe_probability), 4) AS avg_score
                FROM (
                    SELECT model_safe_probability,
                           CASE
                             WHEN issue_count > 0 THEN 'explicit_text_issue'
                             WHEN token_status = 'mismatch' OR final_action = 'blocked_structure'
                               THEN 'structural_block_without_issue'
                             WHEN final_action = 'auto_safe'
                               THEN 'deterministic_safe_but_low_score'
                             WHEN candidate_text = source.old_text
                               OR candidate_text = source.spanish_text
                               OR candidate_text = source.english_text
                               THEN 'unchanged_or_preserved_text'
                             ELSE 'low_confidence_without_specific_evidence'
                           END AS cohort
                    FROM ml_score_items AS item
                    JOIN source_segments AS source ON source.id = item.segment_id
                    WHERE item.run_id = ? AND item.model_safe_probability < ?
                )
                GROUP BY cohort ORDER BY segments DESC
                """,
                params,
            ),
            "issue_samples": _rows(
                conn,
                """
                WITH exploded AS (
                    SELECT item.segment_id, item.relative_path, item.source_key,
                           item.candidate_text, item.model_safe_probability,
                           item.final_action, item.token_status,
                           COALESCE(json_extract(issue.value, '$.code'),
                                    json_extract(issue.value, '$.type')) AS issue_type,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(json_extract(issue.value, '$.code'),
                                                     json_extract(issue.value, '$.type'))
                               ORDER BY item.model_safe_probability ASC, item.segment_id
                           ) AS sample_rank
                    FROM ml_score_items AS item, json_each(item.issues_json) AS issue
                    WHERE item.run_id = ? AND item.model_safe_probability < ?
                )
                SELECT issue_type, segment_id, relative_path, source_key,
                       candidate_text, ROUND(model_safe_probability, 4) AS score,
                       final_action, token_status
                FROM exploded
                WHERE sample_rank <= 2
                ORDER BY issue_type, sample_rank
                """,
                params,
            ),
            "reasons": _rows(
                conn,
                """
                SELECT reason.value AS reason, COUNT(*) AS segments,
                       ROUND(AVG(item.model_safe_probability), 4) AS avg_score
                FROM ml_score_items AS item, json_each(item.reasons_json) AS reason
                WHERE item.run_id = ? AND item.model_safe_probability < ?
                GROUP BY reason.value ORDER BY segments DESC LIMIT 40
                """,
                params,
            ),
        }
    finally:
        conn.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Analyze recurring patterns in a low-score package cohort.")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    print(json.dumps(analyze(args.run_id, args.threshold), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
