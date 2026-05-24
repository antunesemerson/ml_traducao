from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_holdout_eval import RULE_VERSION as HOLDOUT_RULE_VERSION
from ml_holdout_eval import count_labels, select_holdout_paths
from ml_train_risk import (
    DEFAULT_FEATURE_SET,
    DEFAULT_TRAIN_STRATEGY,
    FEATURE_SETS,
    TRAIN_STRATEGIES,
    candidate_differs_from_output,
    create_model,
    deduplicate_examples,
    fetch_examples,
    fit_model,
    latest_dataset_run_id,
    make_text,
    select_examples,
)


RULE_VERSION = "ml_holdout_review_queue_v1"


def short(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def predict_false_safe_rows(
    examples: list[dict[str, Any]],
    safe_threshold: float,
    target_ratio: float,
    min_negative: int,
    max_paths: int | None,
    safe_multiplier: int,
    feature_set: str,
    train_strategy: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    holdout_paths = select_holdout_paths(
        examples,
        target_ratio=target_ratio,
        min_negative=min_negative,
        max_paths=max_paths,
    )
    holdout_path_set = set(holdout_paths)
    train_pool = [row for row in examples if row["relative_path"] not in holdout_path_set]
    holdout_examples = [row for row in examples if row["relative_path"] in holdout_path_set]
    selected_train = select_examples(
        train_pool,
        safe_multiplier=safe_multiplier,
        max_examples=None,
        train_strategy=train_strategy,
    )

    if len(count_labels(selected_train)) < 2:
        raise RuntimeError("Holdout split left fewer than two labels in training data.")
    if len(count_labels(holdout_examples)) < 2:
        raise RuntimeError("Holdout split left fewer than two labels in test data.")

    model = create_model()
    fit_model(
        model,
        [make_text(row, feature_set=feature_set) for row in selected_train],
        [row["risk_label"] for row in selected_train],
        selected_train,
        train_strategy,
    )
    classes = list(model.named_steps["classifier"].classes_)
    safe_index = classes.index("auto_safe")
    probabilities = model.predict_proba([make_text(row, feature_set=feature_set) for row in holdout_examples])

    false_safe_rows: list[dict[str, Any]] = []
    for row, probability_row in zip(holdout_examples, probabilities):
        safe_probability = float(probability_row[safe_index])
        if row["risk_label"] == "auto_safe" or safe_probability < safe_threshold or candidate_differs_from_output(row):
            continue
        item = dict(row)
        item["model_safe_probability"] = safe_probability
        false_safe_rows.append(item)

    false_safe_rows.sort(
        key=lambda row: (
            row["relative_path"],
            -float(row["model_safe_probability"]),
            str(row["source_key"] or ""),
            int(row["segment_id"] or 0),
        )
    )
    split_counts = {
        "train_pool_examples": len(train_pool),
        "selected_training_examples": len(selected_train),
        "holdout_examples": len(holdout_examples),
    }
    return false_safe_rows, holdout_paths, split_counts


def priority_bucket(row: dict[str, Any]) -> str:
    path = row["relative_path"]
    key = row["source_key"] or ""
    text = " ".join(
        str(row.get(name) or "")
        for name in ("english_text", "spanish_text", "old_text", "output_text", "candidate_text")
    ).lower()
    if "core_l_spanish.yml" in path or key.startswith("CHARACTER_"):
        return "core_context_sensitive"
    if "titles_l_spanish.yml" in path or "title" in key.lower():
        return "title_or_rank"
    if "game_concepts_l_spanish.yml" in path:
        return "concept_definition"
    if "[" in text or "$" in text or "#!" in text:
        return "token_context"
    return "semantic_or_style"


def write_csv(settings: dict, rows: list[dict[str, Any]]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{timestamp}_ml_holdout_review_queue.csv"
    fieldnames = [
        "priority_bucket",
        "segment_id",
        "relative_path",
        "source_key",
        "truth_label",
        "action_label",
        "issue_label",
        "trust_level",
        "evidence_source",
        "model_safe_probability",
        "english_text",
        "spanish_text",
        "old_text",
        "output_text",
        "candidate_text",
        "review_decision",
        "review_notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "priority_bucket": priority_bucket(row),
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "truth_label": row["risk_label"],
                    "action_label": row["action_label"],
                    "issue_label": row["issue_label"],
                    "trust_level": row["trust_level"],
                    "evidence_source": row["evidence_source"],
                    "model_safe_probability": f"{row['model_safe_probability']:.6f}",
                    "english_text": row["english_text"] or "",
                    "spanish_text": row["spanish_text"] or "",
                    "old_text": row["old_text"] or "",
                    "output_text": row["output_text"] or "",
                    "candidate_text": row["candidate_text"] or "",
                    "review_decision": "",
                    "review_notes": "",
                }
            )
    return path


def sample_lines(rows: list[dict[str, Any]], limit: int) -> list[str]:
    if not rows:
        return ["- none"]
    lines = []
    for row in rows[:limit]:
        lines.append(
            f"- segment {row['segment_id']} | {priority_bucket(row)} | "
            f"safe_prob={row['model_safe_probability']:.4f} | {row['action_label']} | "
            f"{row['relative_path']}::{row['source_key']} | candidate=\"{short(row['candidate_text'], 120)}\""
        )
    return lines


def main(
    dataset_run_id: int | None = None,
    safe_threshold: float = 0.90,
    target_ratio: float = 0.20,
    min_negative: int = 50,
    max_paths: int | None = None,
    safe_multiplier: int = 5,
    sample_limit: int = 40,
    feature_set: str = DEFAULT_FEATURE_SET,
    train_strategy: str = DEFAULT_TRAIN_STRATEGY,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_holdout_review_queue] Starting holdout false-safe review queue")
    print(f"[ml_holdout_review_queue] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        dataset_run_id = dataset_run_id or latest_dataset_run_id(conn)
        examples = fetch_examples(conn, dataset_run_id)
        if train_strategy == "dedup_weighted_v2":
            examples = deduplicate_examples(examples)
        reviewed_rows = conn.execute(
            """
            SELECT DISTINCT segment_id
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
              AND queue_source = 'ml_holdout_review_queue'
            """
        ).fetchall()
        reviewed_segments = {int(row["segment_id"]) for row in reviewed_rows}

    raw_rows, holdout_paths, split_counts = predict_false_safe_rows(
        examples,
        safe_threshold=safe_threshold,
        target_ratio=target_ratio,
        min_negative=min_negative,
        max_paths=max_paths,
        safe_multiplier=safe_multiplier,
        feature_set=feature_set,
        train_strategy=train_strategy,
    )
    rows = []
    seen_segments: set[int] = set()
    for row in raw_rows:
        segment_id = int(row["segment_id"])
        if segment_id in reviewed_segments or segment_id in seen_segments:
            continue
        rows.append(row)
        seen_segments.add(segment_id)
    csv_path = write_csv(settings, rows)
    raw_unique = len({int(row["segment_id"]) for row in raw_rows})
    by_path = Counter(row["relative_path"] for row in rows)
    by_bucket = Counter(priority_bucket(row) for row in rows)
    by_action = Counter(row["action_label"] for row in rows)
    elapsed = datetime.now() - started_at

    report_lines = [
        "ML holdout false-safe review queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Holdout rule version: {HOLDOUT_RULE_VERSION}",
        f"Dataset run id: {dataset_run_id}",
        f"CSV: {csv_path}",
        "",
        "Split policy:",
        f"- Holdout unit: complete relative_path files",
        f"- Target ratio: {target_ratio:.2%}",
        f"- Minimum negative examples requested: {min_negative}",
        f"- Max paths: {max_paths if max_paths is not None else 'none'}",
        f"- Safe multiplier: {safe_multiplier}",
        f"- Safe threshold: {safe_threshold:.2f}",
        f"- Feature set: {feature_set}",
        f"- Train strategy: {train_strategy}",
        "",
        "Split size:",
        f"- Train pool examples before balancing: {split_counts['train_pool_examples']}",
        f"- Selected training examples after balancing: {split_counts['selected_training_examples']}",
        f"- Holdout examples: {split_counts['holdout_examples']}",
        f"- Raw false-safe rows: {len(raw_rows)} ({percent(len(raw_rows), split_counts['holdout_examples'])} of holdout)",
        f"- Raw false-safe unique segments: {raw_unique}",
        f"- Already reviewed holdout segments skipped: {len(reviewed_segments)}",
        f"- New false-safe queue rows: {len(rows)}",
        "",
        "Holdout paths:",
        *[f"- {path}" for path in holdout_paths],
        "",
        "By priority bucket:",
        *[f"- {name}: {count}" for name, count in by_bucket.most_common()],
        "",
        "By action label:",
        *[f"- {name}: {count}" for name, count in by_action.most_common()],
        "",
        "By path:",
        *[f"- {name}: {count}" for name, count in by_path.most_common()],
        "",
        f"Top {min(sample_limit, len(rows))} review samples:",
        *sample_lines(rows, sample_limit),
        "",
        "Review guidance:",
        "- These rows are known risky examples that the holdout model would have considered safe.",
        "- Prioritize core_context_sensitive, title_or_rank, concept_definition, then token_context.",
        "- Record whether the candidate should be rejected, corrected, or marked as an intentional/contextual exception.",
        "- After review, the main thread should run learn-feedback, ml-dataset, ml-train-risk and ml-holdout-eval again.",
    ]
    report_path = db.write_report(settings, "ml_holdout_review_queue", report_lines)

    print(f"[ml_holdout_review_queue] Dataset run id: {dataset_run_id}")
    print(f"[ml_holdout_review_queue] False-safe queue rows: {len(rows)}")
    print(f"[ml_holdout_review_queue] CSV: {csv_path}")
    print(f"[ml_holdout_review_queue] Report: {report_path}")
    print("[ml_holdout_review_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a review queue from holdout false-safe examples.")
    parser.add_argument("--dataset-run-id", type=int, default=None)
    parser.add_argument("--safe-threshold", type=float, default=0.90)
    parser.add_argument("--target-ratio", type=float, default=0.20)
    parser.add_argument("--min-negative", type=int, default=50)
    parser.add_argument("--max-paths", type=int, default=None)
    parser.add_argument("--safe-multiplier", type=int, default=5)
    parser.add_argument("--sample-limit", type=int, default=40)
    parser.add_argument("--feature-set", choices=FEATURE_SETS, default=DEFAULT_FEATURE_SET)
    parser.add_argument("--train-strategy", choices=TRAIN_STRATEGIES, default=DEFAULT_TRAIN_STRATEGY)
    args = parser.parse_args()
    main(
        dataset_run_id=args.dataset_run_id,
        safe_threshold=args.safe_threshold,
        target_ratio=args.target_ratio,
        min_negative=args.min_negative,
        max_paths=args.max_paths,
        safe_multiplier=args.safe_multiplier,
        sample_limit=args.sample_limit,
        feature_set=args.feature_set,
        train_strategy=args.train_strategy,
    )
