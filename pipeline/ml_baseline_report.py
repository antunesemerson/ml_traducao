from __future__ import annotations

from datetime import datetime

import db


RULE_VERSION = "ml_baseline_report_v1"


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def scalar(conn, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def count_rows(conn, query: str, params: tuple = ()) -> list:
    return conn.execute(query, params).fetchall()


def format_count_rows(rows, key_name: str = "key") -> list[str]:
    if not rows:
        return ["- none: 0"]
    return [f"- {row[key_name] or 'unknown'}: {row['total']}" for row in rows]


def format_precision_rows(rows, key_name: str = "key") -> list[str]:
    if not rows:
        return ["- none: 0"]
    lines: list[str] = []
    for row in rows:
        total = int(row["total"] or 0)
        useful = int(row["useful"] or 0)
        rejected = int(row["rejected"] or 0)
        edited = int(row["edited"] or 0)
        lines.append(
            f"- {row[key_name] or 'unknown'}: useful={useful}/{total} "
            f"({percent(useful, total)}), edited={edited}, rejected={rejected}"
        )
    return lines


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_baseline_report] Starting ML baseline report")
    print(f"[ml_baseline_report] Rule version: {RULE_VERSION}")
    print(f"[ml_baseline_report] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)

        active_segments = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM source_segments
            WHERE is_active = 1
            """,
        )
        output_segments = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM source_segments s
            JOIN output_segments o ON o.segment_id = s.id
            WHERE s.is_active = 1
              AND o.portuguese_text IS NOT NULL
              AND o.portuguese_text <> ''
            """,
        )
        english_segments = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM source_segments
            WHERE is_active = 1
              AND has_english = 1
            """,
        )
        old_segments = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM source_segments
            WHERE is_active = 1
              AND has_old = 1
            """,
        )
        protected_token_segments = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT pt.segment_id)
            FROM protected_tokens pt
            JOIN source_segments s ON s.id = pt.segment_id
            WHERE s.is_active = 1
            """,
        )
        protected_token_total = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM protected_tokens pt
            JOIN source_segments s ON s.id = pt.segment_id
            WHERE s.is_active = 1
            """,
        )

        confirmations_total = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            """,
        )
        human_locked = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
              AND sc.confirmation_level IN ('human_confirmed', 'human')
              AND sc.locked = 1
            """,
        )
        human_confirmed = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
              AND sc.confirmation_level IN ('human_confirmed', 'human')
            """,
        )
        auto_high_confidence = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
              AND sc.confirmation_level = 'auto_confirmed'
              AND coalesce(sc.confidence_score, 0) >= 0.95
            """,
        )
        auto_confirmed = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
              AND sc.confirmation_level = 'auto_confirmed'
            """,
        )
        pending_confirmation = active_segments - confirmations_total

        confirmation_rows = count_rows(
            conn,
            """
            SELECT
                sc.confirmation_level || ' locked=' || sc.locked AS key,
                COUNT(*) AS total
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            GROUP BY sc.confirmation_level, sc.locked
            ORDER BY total DESC, key
            """,
        )
        confirmation_source_rows = count_rows(
            conn,
            """
            SELECT
                sc.confirmation_source || ' / ' || coalesce(sc.confirmation_label, 'none') AS key,
                COUNT(*) AS total
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            GROUP BY sc.confirmation_source, sc.confirmation_label
            ORDER BY total DESC, key
            LIMIT 20
            """,
        )

        reviewed_feedback = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM suggestion_feedback
            WHERE decision IN ('accepted', 'edited', 'accepted_old', 'rejected')
            """,
        )
        useful_feedback = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM suggestion_feedback
            WHERE decision IN ('accepted', 'edited', 'accepted_old')
            """,
        )
        edited_feedback = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM suggestion_feedback
            WHERE decision = 'edited'
            """,
        )
        rejected_feedback = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM suggestion_feedback
            WHERE decision = 'rejected'
            """,
        )
        feedback_segments = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT segment_id)
            FROM suggestion_feedback
            WHERE decision IN ('accepted', 'edited', 'accepted_old', 'rejected')
            """,
        )
        feedback_rows = count_rows(
            conn,
            """
            SELECT decision AS key, COUNT(*) AS total
            FROM suggestion_feedback
            GROUP BY decision
            ORDER BY total DESC, key
            """,
        )
        feedback_origin_precision = count_rows(
            conn,
            """
            SELECT
                coalesce(ts.origin, 'unknown') AS key,
                COUNT(*) AS total,
                SUM(CASE WHEN f.decision IN ('accepted', 'edited', 'accepted_old') THEN 1 ELSE 0 END) AS useful,
                SUM(CASE WHEN f.decision = 'edited' THEN 1 ELSE 0 END) AS edited,
                SUM(CASE WHEN f.decision = 'rejected' THEN 1 ELSE 0 END) AS rejected
            FROM suggestion_feedback f
            LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
            WHERE f.decision IN ('accepted', 'edited', 'accepted_old', 'rejected')
            GROUP BY coalesce(ts.origin, 'unknown')
            HAVING COUNT(*) >= 10
            ORDER BY useful * 1.0 / COUNT(*) DESC, total DESC
            LIMIT 20
            """,
        )

        local_learning_label_rows = count_rows(
            conn,
            """
            SELECT human_label AS key, COUNT(*) AS total
            FROM local_learning_candidates
            GROUP BY human_label
            ORDER BY total DESC, key
            """,
        )
        local_learning_positive = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE human_label IN (
                'positive',
                'near_positive',
                'partial',
                'auto_confirmed',
                'correct',
                'minor_fix'
            )
            """,
        )
        local_learning_negative = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE human_label IN (
                'negative',
                'harmful',
                'rejected',
                'residual_spanish',
                'structure_error',
                'semantic_error',
                'major_fix'
            )
            """,
        )

        memory_total = scalar(conn, "SELECT COUNT(*) FROM translation_memory")
        human_memory = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM translation_memory
            WHERE origin LIKE 'human_feedback_%'
            """,
        )
        trusted_memory = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM translation_memory
            WHERE origin LIKE 'trusted_%'
            """,
        )
        memory_origin_rows = count_rows(
            conn,
            """
            SELECT origin AS key, COUNT(*) AS total
            FROM translation_memory
            GROUP BY origin
            ORDER BY total DESC, key
            LIMIT 25
            """,
        )
        memory_confidence_rows = count_rows(
            conn,
            """
            SELECT
                CASE
                    WHEN confidence_score IS NULL THEN 'unknown'
                    WHEN confidence_score >= 0.99 THEN '0.99+'
                    WHEN confidence_score >= 0.95 THEN '0.95-0.989'
                    WHEN confidence_score >= 0.85 THEN '0.85-0.949'
                    ELSE '<0.85'
                END AS key,
                COUNT(*) AS total
            FROM translation_memory
            GROUP BY key
            ORDER BY total DESC, key
            """,
        )

        suggestion_status_rows = count_rows(
            conn,
            """
            SELECT status AS key, COUNT(*) AS total
            FROM translation_suggestions
            GROUP BY status
            ORDER BY total DESC, key
            """,
        )
        suggestion_token_rows = count_rows(
            conn,
            """
            SELECT token_status AS key, COUNT(*) AS total
            FROM translation_suggestions
            GROUP BY token_status
            ORDER BY total DESC, key
            """,
        )

        latest_learned_run = conn.execute(
            """
            SELECT *
            FROM learned_validation_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        learned_action_rows = []
        learned_risk_rows = []
        if latest_learned_run:
            learned_action_rows = count_rows(
                conn,
                """
                SELECT action AS key, COUNT(*) AS total
                FROM learned_validation_items
                WHERE run_id = ?
                GROUP BY action
                ORDER BY total DESC, key
                """,
                (latest_learned_run["id"],),
            )
            learned_risk_rows = count_rows(
                conn,
                """
                SELECT risk_class AS key, COUNT(*) AS total
                FROM learned_validation_items
                WHERE run_id = ?
                GROUP BY risk_class
                ORDER BY total DESC, key
                """,
                (latest_learned_run["id"],),
            )

        offline_status_rows = count_rows(
            conn,
            """
            SELECT status AS key, COUNT(*) AS total
            FROM offline_proposals
            GROUP BY status
            ORDER BY total DESC, key
            """,
        )
        offline_token_rows = count_rows(
            conn,
            """
            SELECT token_status AS key, COUNT(*) AS total
            FROM offline_proposals
            GROUP BY token_status
            ORDER BY total DESC, key
            """,
        )
        offline_clean_auto_ready = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM offline_proposals
            WHERE status = 'auto_ready'
              AND token_status = 'ok'
              AND high_issue_count = 0
            """,
        )

        package_rows = count_rows(
            conn,
            """
            WITH package_totals AS (
                SELECT
                    s.relative_path,
                    COUNT(*) AS total_segments,
                    SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments,
                    SUM(CASE WHEN sc.confirmation_level = 'human_confirmed' AND sc.locked = 1 THEN 1 ELSE 0 END) AS human_locked_segments
                FROM source_segments s
                LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
                WHERE s.is_active = 1
                GROUP BY s.relative_path
            )
            SELECT
                CASE
                    WHEN confirmed_segments = total_segments THEN 'resolved'
                    WHEN confirmed_segments = 0 THEN 'unconfirmed'
                    ELSE 'partial'
                END AS key,
                COUNT(*) AS total
            FROM package_totals
            GROUP BY key
            ORDER BY total DESC, key
            """,
        )
        package_pending_rows = count_rows(
            conn,
            """
            WITH package_totals AS (
                SELECT
                    s.relative_path,
                    COUNT(*) AS total_segments,
                    SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments
                FROM source_segments s
                LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
                WHERE s.is_active = 1
                GROUP BY s.relative_path
            )
            SELECT
                relative_path AS key,
                total_segments - confirmed_segments AS total
            FROM package_totals
            WHERE confirmed_segments < total_segments
            ORDER BY total DESC, relative_path
            LIMIT 15
            """,
        )

        positive_seed_examples = useful_feedback + local_learning_positive + human_locked
        negative_seed_examples = rejected_feedback + local_learning_negative
        strong_positive_seed_examples = edited_feedback + human_locked
        reviewed_seed_examples = positive_seed_examples + negative_seed_examples

        latest_dataset_run = conn.execute(
            """
            SELECT *
            FROM ml_dataset_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        dataset_source_rows = []
        dataset_action_rows = []
        if latest_dataset_run:
            dataset_source_rows = count_rows(
                conn,
                """
                SELECT evidence_source AS key, COUNT(*) AS total
                FROM ml_training_examples
                WHERE run_id = ?
                GROUP BY evidence_source
                ORDER BY total DESC, key
                """,
                (latest_dataset_run["id"],),
            )
            dataset_action_rows = count_rows(
                conn,
                """
                SELECT action_label AS key, COUNT(*) AS total
                FROM ml_training_examples
                WHERE run_id = ?
                GROUP BY action_label
                ORDER BY total DESC, key
                """,
                (latest_dataset_run["id"],),
            )

        latest_model_run = conn.execute(
            """
            SELECT *
            FROM ml_model_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        active_model = conn.execute(
            """
            SELECT
                registry.*,
                runs.false_safe_count,
                runs.false_safe_rate,
                runs.safe_precision,
                runs.safe_recall,
                runs.macro_f1,
                runs.model_path
            FROM ml_model_registry registry
            JOIN ml_model_runs runs ON runs.id = registry.active_model_run_id
            WHERE registry.model_kind = 'risk_action_classifier'
            LIMIT 1
            """
        ).fetchone()

        latest_score_run = conn.execute(
            """
            SELECT *
            FROM ml_score_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    elapsed = datetime.now() - started_at
    trust_tier_lines = [
        f"- tier 5 human locked: {human_locked}",
        f"- tier 4 human confirmed total: {human_confirmed}",
        f"- tier 3 auto confirmed score 0.95+: {auto_high_confidence}",
        f"- tier 2 auto confirmed total: {auto_confirmed}",
        f"- tier 0 pending confirmation: {pending_confirmation}",
    ]

    latest_run_lines = ["- none: 0"]
    if latest_learned_run:
        latest_run_lines = [
            f"- run id: {latest_learned_run['id']}",
            f"- model version: {latest_learned_run['model_version'] or 'unknown'}",
            f"- active segments: {latest_learned_run['active_segments']}",
            f"- pending segments: {latest_learned_run['pending_segments']}",
            f"- auto safe: {latest_learned_run['auto_safe_count']}",
            f"- needs human: {latest_learned_run['needs_human_count']}",
            f"- blocked structure: {latest_learned_run['blocked_structure_count']}",
        ]

    latest_dataset_lines = ["- none: 0"]
    if latest_dataset_run:
        latest_dataset_lines = [
            f"- run id: {latest_dataset_run['id']}",
            f"- dataset version: {latest_dataset_run['dataset_version']}",
            f"- total examples: {latest_dataset_run['total_count']}",
            f"- positive examples: {latest_dataset_run['positive_count']}",
            f"- negative examples: {latest_dataset_run['negative_count']}",
            f"- strong positive examples: {latest_dataset_run['strong_positive_count']}",
            f"- strong negative examples: {latest_dataset_run['strong_negative_count']}",
        ]

    latest_model_lines = ["- none: 0"]
    if latest_model_run:
        latest_model_lines = [
            f"- run id: {latest_model_run['id']}",
            f"- model version: {latest_model_run['model_version']}",
            f"- model kind: {latest_model_run['model_kind']}",
            f"- dataset run id: {latest_model_run['dataset_run_id']}",
            f"- training examples: {latest_model_run['training_examples']}",
            f"- test examples: {latest_model_run['test_examples']}",
            f"- safe threshold: {latest_model_run['safe_threshold']:.2f}",
            f"- accuracy: {latest_model_run['accuracy']:.4f}",
            f"- macro f1: {latest_model_run['macro_f1']:.4f}",
            f"- predicted safe: {latest_model_run['predicted_safe_count']}",
            f"- false safe: {latest_model_run['false_safe_count']}",
            f"- false safe rate: {latest_model_run['false_safe_rate']:.4f}",
            f"- safe precision: {latest_model_run['safe_precision']:.4f}",
            f"- safe recall: {latest_model_run['safe_recall']:.4f}",
            f"- model path: {latest_model_run['model_path']}",
        ]

    active_model_lines = ["- none: 0"]
    if active_model:
        active_model_lines = [
            f"- model run id: {active_model['active_model_run_id']}",
            f"- model version: {active_model['active_model_version']}",
            f"- policy version: {active_model['policy_version']}",
            f"- promoted at: {active_model['promoted_at']}",
            f"- false safe: {active_model['false_safe_count']}",
            f"- false safe rate: {active_model['false_safe_rate']:.4f}",
            f"- safe precision: {active_model['safe_precision']:.4f}",
            f"- safe recall: {active_model['safe_recall']:.4f}",
            f"- macro f1: {active_model['macro_f1']:.4f}",
            f"- model path: {active_model['model_path']}",
        ]

    latest_score_lines = ["- none: 0"]
    if latest_score_run:
        latest_score_lines = [
            f"- run id: {latest_score_run['id']}",
            f"- model run id: {latest_score_run['model_run_id']}",
            f"- model version: {latest_score_run['model_version']}",
            f"- scored segments: {latest_score_run['scored_count']}",
            f"- model auto safe: {latest_score_run['model_auto_safe_count']}",
            f"- final auto safe: {latest_score_run['final_auto_safe_count']}",
            f"- needs human: {latest_score_run['needs_human_count']}",
            f"- needs autofix: {latest_score_run['needs_autofix_count']}",
            f"- blocked structure: {latest_score_run['blocked_structure_count']}",
            f"- deterministic blocks: {latest_score_run['deterministic_block_count']}",
        ]

    report_lines = [
        "ML baseline report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Purpose:",
        "- Establish the statistical baseline before training local ML models.",
        "- Treat deterministic validators and human confirmations as safety rails.",
        "- Estimate supervised positive/negative signal without changing output files.",
        "",
        "Corpus coverage:",
        f"- Active source segments: {active_segments}",
        f"- Output segments with PT-BR text: {output_segments} ({percent(output_segments, active_segments)})",
        f"- Segments with English reference: {english_segments} ({percent(english_segments, active_segments)})",
        f"- Segments with old PT-BR base: {old_segments} ({percent(old_segments, active_segments)})",
        f"- Segments with protected tokens: {protected_token_segments} ({percent(protected_token_segments, active_segments)})",
        f"- Protected tokens indexed: {protected_token_total}",
        "",
        "Inferred trust tiers:",
        *trust_tier_lines,
        "",
        "Confirmations by level:",
        *format_count_rows(confirmation_rows),
        "",
        "Top confirmation sources:",
        *format_count_rows(confirmation_source_rows),
        "",
        "Feedback supervision:",
        f"- Reviewed feedback rows: {reviewed_feedback}",
        f"- Reviewed feedback segments: {feedback_segments} ({percent(feedback_segments, active_segments)} of active corpus)",
        f"- Useful feedback: {useful_feedback}/{reviewed_feedback} ({percent(useful_feedback, reviewed_feedback)})",
        f"- Edited feedback: {edited_feedback}/{useful_feedback} useful ({percent(edited_feedback, useful_feedback)})",
        f"- Rejected feedback: {rejected_feedback}/{reviewed_feedback} ({percent(rejected_feedback, reviewed_feedback)})",
        "",
        "Feedback decision counts:",
        *format_count_rows(feedback_rows),
        "",
        "Useful precision by suggestion origin:",
        *format_precision_rows(feedback_origin_precision),
        "",
        "Local learning labels:",
        *format_count_rows(local_learning_label_rows),
        "",
        "Translation memory:",
        f"- Total memory pairs: {memory_total}",
        f"- Trusted memory pairs: {trusted_memory}",
        f"- Human feedback memory pairs: {human_memory}",
        "",
        "Memory confidence buckets:",
        *format_count_rows(memory_confidence_rows),
        "",
        "Top memory origins:",
        *format_count_rows(memory_origin_rows),
        "",
        "Current suggestion status:",
        *format_count_rows(suggestion_status_rows),
        "",
        "Current suggestion token status:",
        *format_count_rows(suggestion_token_rows),
        "",
        "Latest learned validation run:",
        *latest_run_lines,
        "",
        "Latest learned actions:",
        *format_count_rows(learned_action_rows),
        "",
        "Latest learned risk classes:",
        *format_count_rows(learned_risk_rows),
        "",
        "Latest ML dataset:",
        *latest_dataset_lines,
        "",
        "Latest ML dataset by source:",
        *format_count_rows(dataset_source_rows),
        "",
        "Latest ML dataset by action:",
        *format_count_rows(dataset_action_rows),
        "",
        "Latest ML model:",
        *latest_model_lines,
        "",
        "Active ML model:",
        *active_model_lines,
        "",
        "Latest ML score run:",
        *latest_score_lines,
        "",
        "Offline proposals:",
        f"- Clean auto-ready proposals: {offline_clean_auto_ready}",
        "",
        "Offline proposal status:",
        *format_count_rows(offline_status_rows),
        "",
        "Offline proposal token status:",
        *format_count_rows(offline_token_rows),
        "",
        "Package confirmation status:",
        *format_count_rows(package_rows),
        "",
        "Largest pending packages:",
        *format_count_rows(package_pending_rows),
        "",
        "Supervised seed estimate:",
        f"- Positive seed examples: {positive_seed_examples}",
        f"- Strong positive examples: {strong_positive_seed_examples}",
        f"- Negative seed examples: {negative_seed_examples}",
        f"- Reviewed seed examples: {reviewed_seed_examples}",
        f"- Positive/negative balance: {positive_seed_examples}/{negative_seed_examples}",
        "",
        "Recommended next step:",
        "- Build a curated ml_training_examples dataset from reviewed feedback, local learning labels, and locked confirmations.",
        "- Keep the first model limited to risk/action classification, not free-form translation generation.",
    ]

    report_path = db.write_report(settings, "ml_baseline_report", report_lines)
    print(f"[ml_baseline_report] Active segments: {active_segments}")
    print(
        "[ml_baseline_report] Confirmed segments: "
        f"{confirmations_total}/{active_segments} ({percent(confirmations_total, active_segments)})"
    )
    print(
        "[ml_baseline_report] Reviewed feedback: "
        f"{reviewed_feedback} rows across {feedback_segments} segments"
    )
    print(
        "[ml_baseline_report] Seed examples: "
        f"positive={positive_seed_examples}, negative={negative_seed_examples}"
    )
    print(f"[ml_baseline_report] Translation memory pairs: {memory_total}")
    print(f"[ml_baseline_report] Report: {report_path}")
    print("[ml_baseline_report] Done")


if __name__ == "__main__":
    main()
