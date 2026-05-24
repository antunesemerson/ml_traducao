from __future__ import annotations

import argparse
from datetime import datetime

import db


RULE_VERSION = "ml_score_audit_v1"


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_score_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_score_runs
        WHERE scored_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_score_runs found. Run python pipeline/main.py ml-score first.")
    return int(row["id"])


def count_rows(conn, query: str, params: tuple = ()) -> list:
    return conn.execute(query, params).fetchall()


def format_rows(rows) -> list[str]:
    if not rows:
        return ["- none: 0"]
    return [f"- {row['key'] or 'none'}: {row['total']}" for row in rows]


def sample_line(row) -> str:
    return (
        f"- segment {row['segment_id']} | {row['final_action']} | {row['risk_class']} | "
        f"safe_prob={row['model_safe_probability']:.4f} | issues={row['issue_count']} | "
        f"{row['confirmation_source'] or 'unknown'} / {row['confirmation_label'] or 'none'} | "
        f"{row['relative_path']}::{row['source_key']}"
    )


def sample_lines(rows) -> list[str]:
    if not rows:
        return ["- none"]
    return [sample_line(row) for row in rows]


def main(score_run_id: int | None = None, sample_limit: int = 25) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_score_audit] Starting ML score audit")
    print(f"[ml_score_audit] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        score_run_id = score_run_id or latest_score_run_id(conn)
        score_run = conn.execute(
            """
            SELECT *
            FROM ml_score_runs
            WHERE id = ?
            """,
            (score_run_id,),
        ).fetchone()
        if score_run is None:
            raise RuntimeError(f"ml_score_run not found: {score_run_id}")

        final_rows = count_rows(
            conn,
            """
            SELECT final_action AS key, COUNT(*) AS total
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY final_action
            ORDER BY total DESC, key
            """,
            (score_run_id,),
        )
        source_rows = count_rows(
            conn,
            """
            SELECT
                coalesce(sc.confirmation_source, 'unknown') || ' / ' ||
                coalesce(sc.confirmation_label, 'none') AS key,
                COUNT(*) AS total
            FROM ml_score_items m
            LEFT JOIN segment_confirmations sc ON sc.segment_id = m.segment_id
            WHERE m.run_id = ?
              AND m.final_action <> 'auto_safe'
            GROUP BY sc.confirmation_source, sc.confirmation_label
            ORDER BY total DESC, key
            LIMIT 25
            """,
            (score_run_id,),
        )
        high_safe_review_rows = conn.execute(
            """
            SELECT
                m.*,
                sc.confirmation_source,
                sc.confirmation_label
            FROM ml_score_items m
            LEFT JOIN segment_confirmations sc ON sc.segment_id = m.segment_id
            WHERE m.run_id = ?
              AND m.final_action <> 'auto_safe'
              AND m.model_safe_probability >= 0.80
            ORDER BY m.model_safe_probability DESC, m.issue_count DESC, m.segment_id
            LIMIT ?
            """,
            (score_run_id, sample_limit),
        ).fetchall()
        deterministic_rows = conn.execute(
            """
            SELECT
                m.*,
                sc.confirmation_source,
                sc.confirmation_label
            FROM ml_score_items m
            LEFT JOIN segment_confirmations sc ON sc.segment_id = m.segment_id
            WHERE m.run_id = ?
              AND m.deterministic_blocked = 1
            ORDER BY m.model_safe_probability DESC, m.segment_id
            LIMIT ?
            """,
            (score_run_id, sample_limit),
        ).fetchall()
        safe_candidates = conn.execute(
            """
            SELECT
                m.*,
                sc.confirmation_source,
                sc.confirmation_label
            FROM ml_score_items m
            LEFT JOIN segment_confirmations sc ON sc.segment_id = m.segment_id
            WHERE m.run_id = ?
              AND m.final_action = 'auto_safe'
              AND m.issue_count = 0
              AND m.token_status = 'ok'
            ORDER BY m.model_safe_probability DESC, m.segment_id
            LIMIT ?
            """,
            (score_run_id, sample_limit),
        ).fetchall()
        priority_review = conn.execute(
            """
            SELECT
                m.*,
                sc.confirmation_source,
                sc.confirmation_label
            FROM ml_score_items m
            LEFT JOIN segment_confirmations sc ON sc.segment_id = m.segment_id
            WHERE m.run_id = ?
              AND m.final_action IN ('needs_human', 'needs_autofix', 'blocked_structure')
            ORDER BY
                CASE m.final_action
                    WHEN 'blocked_structure' THEN 0
                    WHEN 'needs_autofix' THEN 1
                    ELSE 2
                END,
                m.high_issue_count DESC,
                m.model_safe_probability DESC,
                m.segment_id
            LIMIT ?
            """,
            (score_run_id, sample_limit),
        ).fetchall()

    total = int(score_run["scored_count"] or 0)
    final_auto_safe = int(score_run["final_auto_safe_count"] or 0)
    needs_human = int(score_run["needs_human_count"] or 0)
    needs_autofix = int(score_run["needs_autofix_count"] or 0)
    blocked = int(score_run["blocked_structure_count"] or 0)
    deterministic = int(score_run["deterministic_block_count"] or 0)
    elapsed = datetime.now() - started_at

    report_lines = [
        "ML score audit report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Score run id: {score_run_id}",
        f"Model version: {score_run['model_version']}",
        "",
        "Summary:",
        f"- Scored segments: {total}",
        f"- Final auto safe: {final_auto_safe} ({percent(final_auto_safe, total)})",
        f"- Needs human: {needs_human} ({percent(needs_human, total)})",
        f"- Needs autofix: {needs_autofix} ({percent(needs_autofix, total)})",
        f"- Blocked structure: {blocked} ({percent(blocked, total)})",
        f"- Deterministic blocks/overrides: {deterministic}",
        "",
        "Final action counts:",
        *format_rows(final_rows),
        "",
        "Non-safe by confirmation source:",
        *format_rows(source_rows),
        "",
        "High-safe-probability review samples:",
        *sample_lines(high_safe_review_rows),
        "",
        "Deterministic override samples:",
        *sample_lines(deterministic_rows),
        "",
        "Priority review samples:",
        *sample_lines(priority_review),
        "",
        "Clean auto-safe samples:",
        *sample_lines(safe_candidates),
        "",
        "Interpretation:",
        "- Use high-safe-probability review samples to find borderline cases.",
        "- Use deterministic override samples to improve rules and negative examples.",
        "- Use clean auto-safe samples as candidates for later audited promotion, not immediate application.",
    ]

    report_path = db.write_report(settings, "ml_score_audit", report_lines)
    print(f"[ml_score_audit] Score run id: {score_run_id}")
    print(f"[ml_score_audit] Final auto safe: {final_auto_safe}/{total} ({percent(final_auto_safe, total)})")
    print(f"[ml_score_audit] Needs human: {needs_human}")
    print(f"[ml_score_audit] Needs autofix: {needs_autofix}")
    print(f"[ml_score_audit] Blocked structure: {blocked}")
    print(f"[ml_score_audit] Report: {report_path}")
    print("[ml_score_audit] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit a ML score run.")
    parser.add_argument("--score-run-id", type=int, default=None)
    parser.add_argument("--sample-limit", type=int, default=25)
    args = parser.parse_args()
    main(score_run_id=args.score_run_id, sample_limit=args.sample_limit)
