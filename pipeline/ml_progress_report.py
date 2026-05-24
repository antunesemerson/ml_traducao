from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_progress_report_v2"


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def percent_value(value: float | None) -> str:
    if value is None:
        return "0.00%"
    return f"{float(value):.2%}"


def scalar(conn, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def latest_row(conn, table: str, where: str = "1=1") -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE {where}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def latest_full_score_for_model(conn, model_run_id: int | None) -> dict[str, Any] | None:
    if model_run_id is None:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE model_run_id = ?
          AND path_filter IS NULL
          AND limit_count IS NULL
          AND finished_at IS NOT NULL
          AND scored_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (model_run_id,),
    ).fetchone()
    return dict(row) if row else None


def count_rows(conn, query: str, params: tuple = ()) -> list:
    return conn.execute(query, params).fetchall()


def format_count_rows(rows, key_name: str = "key") -> list[str]:
    if not rows:
        return ["- none: 0"]
    return [f"- {row[key_name] or 'unknown'}: {row['total']}" for row in rows]


def latest_report(settings: dict, pattern: str) -> Path | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(reports_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
    return reports[-1] if reports else None


def latest_holdout_report_for_scope(conn, settings: dict, source_scope: str) -> Path | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(reports_dir.glob("*_ml_holdout_eval.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in reports:
        parsed = parse_holdout_report(path)
        dataset_run_id = parsed.get("dataset_run_id")
        if not dataset_run_id:
            continue
        row = conn.execute(
            "SELECT source_scope FROM ml_dataset_runs WHERE id = ?",
            (int(dataset_run_id),),
        ).fetchone()
        if row and row["source_scope"] == source_scope:
            return path
    return None


def parse_holdout_report(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    fields = {
        "path": str(path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
    }
    patterns = {
        "dataset_run_id": r"Dataset run id: (\d+)",
        "holdout_examples": r"- Holdout examples: (\d+)",
        "accuracy": r"- Accuracy: ([0-9.]+)",
        "macro_f1": r"- Macro F1: ([0-9.]+)",
        "predicted_safe": r"- Predicted safe: (\d+)",
        "false_safe": r"- False safe: (\d+)",
        "false_safe_rate": r"- False safe rate among predicted safe: ([0-9.]+%)",
        "safe_precision": r"- Safe precision: ([0-9.]+%)",
        "safe_recall": r"- Safe recall: ([0-9.]+%)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            fields[key] = match.group(1)
    return fields


def parse_progress_summary(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    summary: dict[str, str] = {
        "path": str(path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("- "):
            continue
        if ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        summary[key.strip()] = value.strip()
        if key.strip() == "Observacoes para a frente principal":
            break
    return summary


def model_lines(model: dict[str, Any] | None, prefix: str) -> list[str]:
    if not model:
        return [f"- {prefix}: none"]
    return [
        f"- {prefix} run id: {model['id']}",
        f"- Version: {model['model_version']}",
        f"- Dataset run id: {model['dataset_run_id']}",
        f"- Accuracy: {float(model['accuracy'] or 0):.4f}",
        f"- Macro F1: {float(model['macro_f1'] or 0):.4f}",
        f"- False safe: {int(model['false_safe_count'] or 0)}",
        f"- False safe rate: {percent_value(model['false_safe_rate'])}",
        f"- Safe precision: {percent_value(model['safe_precision'])}",
        f"- Safe recall: {percent_value(model['safe_recall'])}",
    ]


def latest_model_for_kind(conn, model_kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind = ?
          AND model_path IS NOT NULL
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (model_kind,),
    ).fetchone()
    return dict(row) if row else None


def latest_specialist_models(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind LIKE 'specialist_%'
          AND model_path IS NOT NULL
          AND finished_at IS NOT NULL
        ORDER BY model_kind, finished_at DESC, id DESC
        """
    ).fetchall()
    latest_by_kind: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        model_kind = row["model_kind"]
        if model_kind in seen:
            continue
        seen.add(model_kind)
        latest_by_kind.append(dict(row))
    latest_by_kind.sort(key=lambda model: model["model_kind"])
    return latest_by_kind


def latest_full_score_for_kind(conn, model_kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT r.*
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE m.model_kind = ?
          AND r.path_filter IS NULL
          AND r.limit_count IS NULL
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT 1
        """,
        (model_kind,),
    ).fetchone()
    return dict(row) if row else None


def latest_score_for_model(conn, model_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE model_run_id = ?
          AND finished_at IS NOT NULL
          AND scored_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (model_run_id,),
    ).fetchone()
    return dict(row) if row else None


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_progress_report] Starting ML progress report")
    print(f"[ml_progress_report] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)

        active_segments = scalar(conn, "SELECT COUNT(*) FROM source_segments WHERE is_active = 1")
        confirmations = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT sc.segment_id)
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            """,
        )
        human_reviewed = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
            """,
        )
        holdout_reviewed = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
              AND queue_source = 'ml_holdout_review_queue'
            """,
        )
        unsynced_learning = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
              AND learned_at IS NULL
            """,
        )
        unsynced_confirmations = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
              AND confirmation_synced_at IS NULL
            """,
        )
        learning_labels = count_rows(
            conn,
            """
            SELECT human_label AS key, COUNT(*) AS total
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
            GROUP BY human_label
            ORDER BY total DESC, key
            """,
        )
        learning_sources = count_rows(
            conn,
            """
            SELECT queue_source AS key, COUNT(*) AS total
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
            GROUP BY queue_source
            ORDER BY total DESC, key
            """,
        )
        latest_dataset = latest_row(
            conn,
            "ml_dataset_runs",
            "total_count > 0 AND source_scope = 'feedback+local_learning+locked_human'",
        )
        latest_model = latest_model_for_kind(conn, "risk_action_classifier")
        specialist_models = latest_specialist_models(conn)
        active_model_row = conn.execute(
            """
            SELECT m.*
            FROM ml_model_registry r
            JOIN ml_model_runs m ON m.id = r.active_model_run_id
            WHERE r.model_kind = 'risk_action_classifier'
            """
        ).fetchone()
        active_model = dict(active_model_row) if active_model_row else None
        latest_score = latest_full_score_for_kind(conn, "risk_action_classifier")
        specialist_scores = [
            (model, latest_score_for_model(conn, int(model["id"])))
            for model in specialist_models
        ]
        active_score = latest_full_score_for_model(conn, int(active_model["id"])) if active_model else None
        latest_policy = latest_row(conn, "ml_policy_runs", "scored_count > 0")
        latest_promotion = latest_row(conn, "ml_model_promotions")
        dataset_labels = []
        if latest_dataset:
            dataset_labels = count_rows(
                conn,
                """
                SELECT label AS key, COUNT(*) AS total
                FROM ml_training_examples
                WHERE run_id = ?
                GROUP BY label
                ORDER BY total DESC, key
                """,
                (latest_dataset["id"],),
            )
        holdout_report = latest_holdout_report_for_scope(
            conn,
            settings,
            "feedback+local_learning+locked_human",
        )
    holdout = parse_holdout_report(holdout_report)
    progress = parse_progress_summary(db.project_path(settings["reports_dir"]) / "parallel_review_progress.md")
    latest_holdout_queue = latest_report(settings, "*_ml_holdout_review_queue.csv")

    elapsed = datetime.now() - started_at
    lines = [
        "ML progress report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Corpus:",
        f"- Active segments: {active_segments}",
        f"- Confirmed segments: {confirmations} ({percent(confirmations, active_segments)})",
        f"- Pending/unconfirmed segments: {max(active_segments - confirmations, 0)}",
        "",
        "Human learning:",
        f"- Reviewed local candidates: {human_reviewed}",
        f"- Holdout false-safe reviews: {holdout_reviewed}",
        f"- Reviewed rows not yet learned: {unsynced_learning}",
        f"- Reviewed rows not yet confirmation-synced: {unsynced_confirmations}",
        "",
        "Human labels:",
        *format_count_rows(learning_labels),
        "",
        "Human queue sources:",
        *format_count_rows(learning_sources),
        "",
        "Parallel review progress:",
        f"- Progress file: {progress.get('path', 'missing')}",
        f"- Ciclos revisados: {progress.get('Ciclos revisados', 'unknown')}",
        f"- Segmentos revisados: {progress.get('Segmentos revisados', 'unknown')}",
        f"- Positivos: {progress.get('Positivos', 'unknown')}",
        f"- Negativos: {progress.get('Negativos', 'unknown')}",
        f"- Correcoes/editados: {progress.get('Correcoes/editados', 'unknown')}",
        f"- Ultima fila holdout: {progress.get('Ultima fila holdout usada', 'unknown')}",
        "",
        "Dataset:",
    ]
    if latest_dataset:
        lines.extend(
            [
                f"- Latest dataset run id: {latest_dataset['id']}",
                f"- Total examples: {latest_dataset['total_count']}",
                f"- Positive: {latest_dataset['positive_count']}",
                f"- Negative: {latest_dataset['negative_count']}",
                f"- Strong positive: {latest_dataset['strong_positive_count']}",
                f"- Strong negative: {latest_dataset['strong_negative_count']}",
                f"- Finished at: {latest_dataset['finished_at'] or 'unknown'}",
                "",
                "Dataset labels:",
                *format_count_rows(dataset_labels),
            ]
        )
    else:
        lines.append("- Latest dataset: none")
    lines.extend(
        [
            "",
            "Latest model:",
            *model_lines(latest_model, "Latest model"),
            "",
            "Active model:",
            *model_lines(active_model, "Active model"),
            "",
            "Specialist models:",
        ]
    )
    if specialist_models:
        for model in specialist_models:
            lines.extend(model_lines(model, model["model_kind"]))
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Latest score run:",
        ]
    )
    if latest_score:
        scored = int(latest_score["scored_count"] or 0)
        lines.extend(
            [
                f"- Score run id: {latest_score['id']}",
                f"- Model: {latest_score['model_version']}",
                f"- Scored: {scored}",
                f"- Final auto safe: {latest_score['final_auto_safe_count']} ({percent(int(latest_score['final_auto_safe_count'] or 0), scored)})",
                f"- Needs human: {latest_score['needs_human_count']} ({percent(int(latest_score['needs_human_count'] or 0), scored)})",
                f"- Needs autofix: {latest_score['needs_autofix_count']} ({percent(int(latest_score['needs_autofix_count'] or 0), scored)})",
                f"- Blocked structure: {latest_score['blocked_structure_count']} ({percent(int(latest_score['blocked_structure_count'] or 0), scored)})",
                f"- Deterministic blocks: {latest_score['deterministic_block_count']}",
            ]
        )
    else:
        lines.append("- Latest score: none")
    lines.extend(
        [
            "",
            "Active model operational score:",
        ]
    )
    if active_score:
        active_scored = int(active_score["scored_count"] or 0)
        latest_is_active = latest_score is not None and int(latest_score["id"]) == int(active_score["id"])
        lines.extend(
            [
                f"- Score run id: {active_score['id']}",
                f"- Model: {active_score['model_version']}",
                f"- Scored: {active_scored}",
                f"- Final auto safe: {active_score['final_auto_safe_count']} ({percent(int(active_score['final_auto_safe_count'] or 0), active_scored)})",
                f"- Needs human: {active_score['needs_human_count']} ({percent(int(active_score['needs_human_count'] or 0), active_scored)})",
                f"- Needs autofix: {active_score['needs_autofix_count']} ({percent(int(active_score['needs_autofix_count'] or 0), active_scored)})",
                f"- Blocked structure: {active_score['blocked_structure_count']} ({percent(int(active_score['blocked_structure_count'] or 0), active_scored)})",
                f"- Is latest score run: {latest_is_active}",
            ]
        )
    else:
        lines.append("- Active model score: none")
    lines.extend(
        [
            "",
            "Latest specialist scores:",
        ]
    )
    if specialist_scores:
        for model, score in specialist_scores:
            if not score:
                lines.append(f"- {model['model_kind']}: no score yet")
                continue
            specialist_scored = int(score["scored_count"] or 0)
            lines.extend(
                [
                    f"- {model['model_kind']} score run id: {score['id']}",
                    f"  Model: {score['model_version']}",
                    f"  Path filter: {score['path_filter'] or 'none'}",
                    f"  Scored: {specialist_scored}",
                    f"  Final auto safe: {score['final_auto_safe_count']} ({percent(int(score['final_auto_safe_count'] or 0), specialist_scored)})",
                    f"  Needs human: {score['needs_human_count']} ({percent(int(score['needs_human_count'] or 0), specialist_scored)})",
                    f"  Deterministic blocks: {score['deterministic_block_count']}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Latest policy run:",
        ]
    )
    if latest_policy:
        policy_scored = int(latest_policy["scored_count"] or 0)
        active_safe = int(latest_policy["active_auto_safe_count"] or 0)
        policy_safe = int(latest_policy["policy_auto_safe_count"] or 0)
        lines.extend(
            [
                f"- Policy run id: {latest_policy['id']}",
                f"- Rule version: {latest_policy['rule_version']}",
                f"- Score run id: {latest_policy['score_run_id']}",
                f"- Model: {latest_policy['model_version']}",
                f"- Scored: {policy_scored}",
                f"- Active auto safe: {active_safe} ({percent(active_safe, policy_scored)})",
                f"- Policy auto safe: {policy_safe} ({percent(policy_safe, policy_scored)})",
                f"- New safe: {latest_policy['new_safe_count']} ({percent(int(latest_policy['new_safe_count'] or 0), policy_scored)})",
                f"- Active safe below stricter policy: {latest_policy['demoted_safe_count']}",
                f"- Protect active safe: {bool(latest_policy['protect_active_safe'])}",
            ]
        )
    else:
        lines.append("- Latest policy: none")
    lines.extend(
        [
            "",
            "Latest holdout:",
            f"- Report: {holdout.get('path', 'missing')}",
            f"- Dataset run id: {holdout.get('dataset_run_id', 'unknown')}",
            f"- Holdout examples: {holdout.get('holdout_examples', 'unknown')}",
            f"- Accuracy: {holdout.get('accuracy', 'unknown')}",
            f"- Macro F1: {holdout.get('macro_f1', 'unknown')}",
            f"- Predicted safe: {holdout.get('predicted_safe', 'unknown')}",
            f"- False safe: {holdout.get('false_safe', 'unknown')}",
            f"- False safe rate: {holdout.get('false_safe_rate', 'unknown')}",
            f"- Safe precision: {holdout.get('safe_precision', 'unknown')}",
            f"- Safe recall: {holdout.get('safe_recall', 'unknown')}",
            f"- Latest holdout queue: {str(latest_holdout_queue.relative_to(db.PROJECT_ROOT)).replace(chr(92), '/') if latest_holdout_queue else 'missing'}",
            "",
            "Latest promotion:",
        ]
    )
    if latest_promotion:
        lines.extend(
            [
                f"- Promotion id: {latest_promotion['id']}",
                f"- Candidate model run id: {latest_promotion['candidate_model_run_id']}",
                f"- Previous model run id: {latest_promotion['previous_model_run_id'] or 'none'}",
                f"- Decision: {latest_promotion['decision']}",
                f"- Policy: {latest_promotion['policy_version']}",
                f"- Reason: {latest_promotion['reason'] or 'none'}",
            ]
        )
    else:
        lines.append("- Latest promotion: none")
    lines.extend(
        [
            "",
            "Next action:",
            "- If reviewed rows not yet learned is greater than zero, run learn-feedback before rebuilding the ML dataset.",
            "- After the second chat finishes a holdout batch, rerun ml-dataset, ml-train-risk, ml-holdout-eval and this progress report.",
            "- Watch false safe count/rate first; coverage can improve later.",
        ]
    )
    report_path = db.write_report(settings, "ml_progress_report", lines)
    print(f"[ml_progress_report] Reviewed local candidates: {human_reviewed}")
    print(f"[ml_progress_report] Holdout reviews: {holdout_reviewed}")
    print(f"[ml_progress_report] Reviewed rows not yet learned: {unsynced_learning}")
    print(f"[ml_progress_report] Latest holdout false safe: {holdout.get('false_safe', 'unknown')}")
    print(f"[ml_progress_report] Report: {report_path}")
    print("[ml_progress_report] Done")


if __name__ == "__main__":
    main()
