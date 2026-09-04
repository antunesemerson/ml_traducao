from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

import db
from ml_train_risk import (
    RANDOM_SEED,
    create_model,
    deduplicate_examples,
    false_safe_stats,
    fetch_examples,
    fit_model,
    latest_dataset_run_id,
    make_text,
    percent,
    select_examples,
    threshold_predictions,
    candidate_differs_from_output,
    language_features,
    LANGUAGE_BLOCKING_FEATURES,
)
from ml_train_risk import DEFAULT_FEATURE_SET, DEFAULT_TRAIN_STRATEGY, FEATURE_SETS, TRAIN_STRATEGIES
from ml_score_segments import (
    has_blocked_spanish_geography_terms,
    has_known_unsafe_title_text,
    has_separator_whitespace_loss,
    is_exact_token_only_passthrough,
)


RULE_VERSION = "ml_holdout_eval_v7_frozen_reference_split"


def count_labels(examples: list[dict[str, Any]]) -> Counter:
    return Counter(row["risk_label"] for row in examples)


def select_holdout_paths(
    examples: list[dict[str, Any]],
    target_ratio: float,
    min_negative: int,
    max_paths: int | None,
) -> list[str]:
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in examples:
        by_path[row["relative_path"]].append(row)

    path_stats = []
    for path, rows in by_path.items():
        negatives = sum(1 for row in rows if row["risk_label"] != "auto_safe")
        path_stats.append(
            {
                "path": path,
                "total": len(rows),
                "negative": negatives,
                "safe": len(rows) - negatives,
            }
        )

    rng = random.Random(RANDOM_SEED)
    rng.shuffle(path_stats)
    path_stats.sort(key=lambda item: (item["negative"] == 0, -item["negative"], -item["total"], item["path"]))

    target_total = max(1, int(len(examples) * target_ratio))
    selected: list[str] = []
    selected_total = 0
    selected_negative = 0

    for item in path_stats:
        if max_paths is not None and len(selected) >= max_paths:
            break
        if selected and selected_total >= target_total and selected_negative >= min_negative:
            break
        selected.append(item["path"])
        selected_total += int(item["total"])
        selected_negative += int(item["negative"])

    if not selected:
        raise RuntimeError("Could not select holdout paths.")
    return selected


def load_reference_holdout_paths(conn, reference_run_id: int) -> tuple[list[str], dict[str, Any]]:
    """Reuse a completed holdout's exact file split for an apples-to-apples comparison."""
    row = conn.execute(
        """
        SELECT id, dataset_run_id, finished_at, metrics_json
        FROM ml_holdout_eval_runs
        WHERE id = ? AND finished_at IS NOT NULL
        LIMIT 1
        """,
        (reference_run_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"Holdout reference run {reference_run_id} was not found or is incomplete.")
    try:
        metrics = json.loads(row["metrics_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Holdout reference run {reference_run_id} has invalid metrics.") from exc
    paths = metrics.get("holdout_paths") or []
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path for path in paths):
        raise RuntimeError(f"Holdout reference run {reference_run_id} has no usable file split.")
    ordered_paths = sorted(set(paths))
    return ordered_paths, {
        "run_id": int(row["id"]),
        "dataset_run_id": int(row["dataset_run_id"]),
        "finished_at": row["finished_at"],
    }


def holdout_text_identity(row: dict[str, Any]) -> str:
    """Stable identity for a reviewed candidate, independent of its dataset row id."""
    value = f"{int(row['segment_id'])}\x1f{row.get('candidate_text') or ''}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_counts(counter: Counter) -> list[str]:
    if not counter:
        return ["- none: 0"]
    return [f"- {label}: {counter[label]}" for label in sorted(counter)]


def false_safe_samples(
    holdout_examples: list[dict[str, Any]],
    y_pred: list[str],
    sample_limit: int,
) -> list[str]:
    rows = []
    for row, pred in zip(holdout_examples, y_pred):
        if row["risk_label"] != "auto_safe" and pred == "auto_safe":
            rows.append(row)
    if not rows:
        return ["- none"]
    return [
        (
            f"- segment {row['segment_id']} | truth={row['risk_label']} | "
            f"action={row['action_label']} | {row['relative_path']}::{row['source_key']}"
        )
        for row in rows[:sample_limit]
    ]


def apply_holdout_safety_gates(
    examples: list[dict[str, Any]],
    y_pred: list[str],
) -> list[str]:
    gated: list[str] = []
    for row, pred in zip(examples, y_pred):
        if pred == "auto_safe" and candidate_differs_from_output(row):
            gated.append("needs_human")
            continue
        if pred == "auto_safe":
            language_blocking_features = set(language_features(row)) & LANGUAGE_BLOCKING_FEATURES
            if (
                language_blocking_features == {"RISK_STRUCTURAL_EMPTY_VISIBLE_TEXT"}
                and is_exact_token_only_passthrough(row)
            ):
                language_blocking_features = set()
            if language_blocking_features:
                gated.append("needs_human")
                continue
        if pred == "auto_safe" and has_blocked_spanish_geography_terms(row):
            gated.append("needs_human")
            continue
        if pred == "auto_safe" and has_known_unsafe_title_text(row):
            gated.append("needs_human")
            continue
        if pred == "auto_safe" and has_separator_whitespace_loss(row):
            gated.append("needs_human")
            continue
        gated.append(pred)
    return gated


def unique_false_safe_stats(
    holdout_examples: list[dict[str, Any]],
    y_pred: list[str],
) -> dict[str, int]:
    predicted_safe_segments = {
        int(row["segment_id"])
        for row, pred in zip(holdout_examples, y_pred)
        if pred == "auto_safe"
    }
    false_safe_segments = {
        int(row["segment_id"])
        for row, pred in zip(holdout_examples, y_pred)
        if row["risk_label"] != "auto_safe" and pred == "auto_safe"
    }
    return {
        "unique_predicted_safe": len(predicted_safe_segments),
        "unique_false_safe": len(false_safe_segments),
    }


def per_path_lines(
    holdout_examples: list[dict[str, Any]],
    y_pred: list[str],
    sample_limit: int,
) -> list[str]:
    stats: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    for row, pred in zip(holdout_examples, y_pred):
        path = row["relative_path"]
        stats[path]["total"] += 1
        if row["risk_label"] == "auto_safe":
            stats[path]["actual_safe"] += 1
        else:
            stats[path]["actual_risky"] += 1
        if pred == "auto_safe":
            stats[path]["predicted_safe"] += 1
        if row["risk_label"] != "auto_safe" and pred == "auto_safe":
            stats[path]["false_safe"] += 1

    ordered = sorted(stats.items(), key=lambda item: (-item[1]["false_safe"], -item[1]["actual_risky"], item[0]))
    lines = []
    for path, item in ordered[:sample_limit]:
        predicted_safe = item["predicted_safe"]
        false_safe = item["false_safe"]
        false_safe_rate = false_safe / predicted_safe if predicted_safe else 0.0
        lines.append(
            f"- {path}: total={item['total']}, risky={item['actual_risky']}, "
            f"predicted_safe={predicted_safe}, false_safe={false_safe}, "
            f"false_safe_rate={false_safe_rate:.2%}"
        )
    return lines or ["- none"]


def main(
    dataset_run_id: int | None = None,
    safe_threshold: float = 0.90,
    target_ratio: float = 0.20,
    min_negative: int = 50,
    max_paths: int | None = None,
    safe_multiplier: int = 5,
    sample_limit: int = 20,
    feature_set: str = DEFAULT_FEATURE_SET,
    train_strategy: str = DEFAULT_TRAIN_STRATEGY,
    reference_run_id: int | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_holdout_eval] Starting package/file holdout evaluation")
    print(f"[ml_holdout_eval] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        dataset_run_id = dataset_run_id or latest_dataset_run_id(conn)
        examples = fetch_examples(conn, dataset_run_id)
        reference_split = (
            load_reference_holdout_paths(conn, reference_run_id)
            if reference_run_id
            else None
        )
        reference_examples = (
            fetch_examples(conn, reference_split[1]["dataset_run_id"])
            if reference_split
            else []
        )
    if train_strategy == "dedup_weighted_v2":
        examples = deduplicate_examples(examples)
        reference_examples = deduplicate_examples(reference_examples)

    if len(count_labels(examples)) < 2:
        raise RuntimeError("Need at least two risk labels to evaluate holdout.")

    if reference_split:
        holdout_paths, reference_metadata = reference_split
        selection_mode = "frozen_reference"
    else:
        holdout_paths = select_holdout_paths(
            examples,
            target_ratio=target_ratio,
            min_negative=min_negative,
            max_paths=max_paths,
        )
        reference_metadata = None
        selection_mode = "derived"
    split_fingerprint = hashlib.sha256(
        "\n".join(sorted(holdout_paths)).encode("utf-8")
    ).hexdigest()[:16]
    holdout_path_set = set(holdout_paths)
    train_pool = [row for row in examples if row["relative_path"] not in holdout_path_set]
    holdout_examples = [row for row in examples if row["relative_path"] in holdout_path_set]
    novel_holdout_examples = 0
    if reference_split:
        reference_holdout_ids = {
            holdout_text_identity(row)
            for row in reference_examples
            if row["relative_path"] in holdout_path_set
        }
        original_holdout_count = len(holdout_examples)
        holdout_examples = [
            row for row in holdout_examples
            if holdout_text_identity(row) in reference_holdout_ids
        ]
        novel_holdout_examples = original_holdout_count - len(holdout_examples)
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

    x_train = [make_text(row, feature_set=feature_set) for row in selected_train]
    y_train = [row["risk_label"] for row in selected_train]
    x_holdout = [make_text(row, feature_set=feature_set) for row in holdout_examples]
    y_holdout = [row["risk_label"] for row in holdout_examples]

    model = create_model()
    fit_model(model, x_train, y_train, selected_train, train_strategy)

    raw_predictions = list(model.predict(x_holdout))
    thresholded_predictions = apply_holdout_safety_gates(
        holdout_examples,
        threshold_predictions(model, x_holdout, safe_threshold, holdout_examples),
    )
    train_label_counts = count_labels(selected_train)
    holdout_label_counts = count_labels(holdout_examples)
    labels_sorted = sorted(set(train_label_counts) | set(holdout_label_counts))

    accuracy = accuracy_score(y_holdout, thresholded_predictions)
    macro_f1 = f1_score(y_holdout, thresholded_predictions, average="macro", zero_division=0)
    stats = false_safe_stats(y_holdout, thresholded_predictions)
    unique_stats = unique_false_safe_stats(holdout_examples, thresholded_predictions)
    report = classification_report(
        y_holdout,
        thresholded_predictions,
        labels=labels_sorted,
        zero_division=0,
        output_dict=True,
    )
    raw_report = classification_report(
        y_holdout,
        raw_predictions,
        labels=labels_sorted,
        zero_division=0,
        output_dict=True,
    )
    matrix = confusion_matrix(y_holdout, thresholded_predictions, labels=labels_sorted).tolist()

    elapsed = datetime.now() - started_at
    report_lines = [
        "ML holdout evaluation report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Dataset run id: {dataset_run_id}",
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
        f"- Selection mode: {selection_mode}",
        f"- Split fingerprint: {split_fingerprint}",
        f"- Novel holdout texts excluded: {novel_holdout_examples}",
        (
            f"- Frozen reference: run {reference_metadata['run_id']} "
            f"(dataset {reference_metadata['dataset_run_id']})"
            if reference_metadata
            else "- Frozen reference: none"
        ),
        "",
        "Holdout paths:",
        *[f"- {path}" for path in holdout_paths],
        "",
        "Dataset size:",
        f"- Total examples: {len(examples)}",
        f"- Train pool examples before balancing: {len(train_pool)}",
        f"- Selected training examples after balancing: {len(selected_train)}",
        f"- Holdout examples: {len(holdout_examples)}",
        "",
        "Training labels:",
        *format_counts(train_label_counts),
        "",
        "Holdout labels:",
        *format_counts(holdout_label_counts),
        "",
        "Evaluation:",
        f"- Accuracy: {accuracy:.4f}",
        f"- Macro F1: {macro_f1:.4f}",
        f"- Predicted safe: {stats['predicted_safe']}",
        f"- False safe: {stats['false_safe']}",
        f"- Unique predicted-safe segments: {unique_stats['unique_predicted_safe']}",
        f"- Unique false-safe segments: {unique_stats['unique_false_safe']}",
        f"- False safe rate among predicted safe: {percent(stats['false_safe_rate'])}",
        f"- Safe precision: {percent(stats['safe_precision'])}",
        f"- Safe recall: {percent(stats['safe_recall'])}",
        "",
        "Classification report:",
    ]
    for label in labels_sorted:
        label_stats = report.get(label, {})
        raw_stats = raw_report.get(label, {})
        report_lines.append(
            f"- {label}: precision={label_stats.get('precision', 0):.4f}, "
            f"recall={label_stats.get('recall', 0):.4f}, "
            f"f1={label_stats.get('f1-score', 0):.4f}, "
            f"support={int(label_stats.get('support', 0))}, "
            f"raw_recall={raw_stats.get('recall', 0):.4f}"
        )
    report_lines.extend(
        [
            "",
            "Confusion matrix labels:",
            f"- {', '.join(labels_sorted)}",
            "Confusion matrix rows:",
            *[f"- {row}" for row in matrix],
            "",
            "False-safe samples:",
            *false_safe_samples(holdout_examples, thresholded_predictions, sample_limit),
            "",
            "Per-path holdout summary:",
            *per_path_lines(holdout_examples, thresholded_predictions, sample_limit),
            "",
            "Interpretation:",
            "- This evaluates generalization by hiding whole files from training.",
            "- It does not write or promote a model.",
            "- False safe remains the main safety metric.",
            "- Weak holdout results mean we need more reviewed negatives from similar files before trusting automation there.",
        ]
    )
    report_path = db.write_report(settings, "ml_holdout_eval", report_lines)
    finished_at = datetime.now().isoformat(timespec="seconds")
    metrics = {
        "holdout_paths": holdout_paths,
        "holdout_selection": {
            "mode": selection_mode,
            "reference_run_id": reference_metadata["run_id"] if reference_metadata else None,
            "reference_dataset_run_id": reference_metadata["dataset_run_id"] if reference_metadata else None,
            "split_fingerprint": split_fingerprint,
            "strict_text_overlap": bool(reference_metadata),
            "novel_holdout_examples_excluded": novel_holdout_examples,
        },
        "train_label_counts": dict(train_label_counts),
        "holdout_label_counts": dict(holdout_label_counts),
        "classification_report": report,
        "raw_classification_report": raw_report,
        "confusion_matrix_labels": labels_sorted,
        "confusion_matrix": matrix,
        "false_safe_samples": false_safe_samples(holdout_examples, thresholded_predictions, sample_limit),
        "per_path_summary": per_path_lines(holdout_examples, thresholded_predictions, sample_limit),
    }
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        conn.execute(
            """
            INSERT INTO ml_holdout_eval_runs (
                rule_version,
                dataset_run_id,
                safe_threshold,
                target_ratio,
                min_negative,
                max_paths,
                safe_multiplier,
                feature_set,
                train_strategy,
                holdout_path_count,
                holdout_examples,
                predicted_safe_count,
                false_safe_count,
                false_safe_rate,
                unique_predicted_safe_count,
                unique_false_safe_count,
                safe_precision,
                safe_recall,
                accuracy,
                macro_f1,
                report_path,
                metrics_json,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                dataset_run_id,
                safe_threshold,
                target_ratio,
                min_negative,
                max_paths,
                safe_multiplier,
                feature_set,
                train_strategy,
                len(holdout_paths),
                len(holdout_examples),
                int(stats["predicted_safe"]),
                int(stats["false_safe"]),
                float(stats["false_safe_rate"]),
                int(unique_stats["unique_predicted_safe"]),
                int(unique_stats["unique_false_safe"]),
                float(stats["safe_precision"]),
                float(stats["safe_recall"]),
                float(accuracy),
                float(macro_f1),
                str(report_path),
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                started_at.isoformat(timespec="seconds"),
                finished_at,
                finished_at,
            ),
        )
        conn.commit()

    print(f"[ml_holdout_eval] Dataset run id: {dataset_run_id}")
    print(f"[ml_holdout_eval] Holdout paths: {len(holdout_paths)}")
    print(f"[ml_holdout_eval] Holdout examples: {len(holdout_examples)}")
    print(f"[ml_holdout_eval] Accuracy: {accuracy:.4f}")
    print(f"[ml_holdout_eval] Macro F1: {macro_f1:.4f}")
    print(
        "[ml_holdout_eval] False safe: "
        f"{stats['false_safe']}/{stats['predicted_safe']} "
        f"({percent(stats['false_safe_rate'])})"
    )
    print(f"[ml_holdout_eval] Report: {report_path}")
    print("[ml_holdout_eval] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate ML risk classifier with file-level holdout.")
    parser.add_argument("--dataset-run-id", type=int, default=None)
    parser.add_argument("--safe-threshold", type=float, default=0.90)
    parser.add_argument("--target-ratio", type=float, default=0.20)
    parser.add_argument("--min-negative", type=int, default=50)
    parser.add_argument("--max-paths", type=int, default=None)
    parser.add_argument("--safe-multiplier", type=int, default=5)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--feature-set", choices=FEATURE_SETS, default=DEFAULT_FEATURE_SET)
    parser.add_argument("--train-strategy", choices=TRAIN_STRATEGIES, default=DEFAULT_TRAIN_STRATEGY)
    parser.add_argument(
        "--reference-run-id",
        type=int,
        default=None,
        help="Reuse the exact file holdout split from a completed evaluation run.",
    )
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
        reference_run_id=args.reference_run_id,
    )
