from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from typing import Any

from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split

import db
from ml_holdout_eval import count_labels, select_holdout_paths
from ml_train_risk import (
    DEFAULT_FEATURE_SET,
    DEFAULT_TRAIN_STRATEGY,
    FEATURE_SETS,
    RANDOM_SEED,
    create_model,
    false_safe_stats,
    fetch_examples,
    fit_model,
    latest_dataset_run_id,
    make_text,
    percent,
    select_examples,
    threshold_predictions,
)
from ml_train_risk import TRAIN_STRATEGIES


RULE_VERSION = "ml_threshold_sweep_v2"


def parse_thresholds(value: str | None) -> list[float]:
    if not value:
        return [0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]
    thresholds = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        thresholds.append(float(item))
    if not thresholds:
        raise ValueError("No thresholds provided.")
    return thresholds


def count_by_path(examples: list[dict[str, Any]], predictions: list[str]) -> list[dict[str, Any]]:
    stats: dict[str, Counter] = {}
    for row, pred in zip(examples, predictions):
        path = row["relative_path"]
        if path not in stats:
            stats[path] = Counter()
        stats[path]["total"] += 1
        if row["risk_label"] != "auto_safe":
            stats[path]["risky"] += 1
        if pred == "auto_safe":
            stats[path]["predicted_safe"] += 1
        if row["risk_label"] != "auto_safe" and pred == "auto_safe":
            stats[path]["false_safe"] += 1
    rows = []
    for path, item in stats.items():
        predicted_safe = int(item["predicted_safe"] or 0)
        false_safe = int(item["false_safe"] or 0)
        rows.append(
            {
                "path": path,
                "total": int(item["total"] or 0),
                "risky": int(item["risky"] or 0),
                "predicted_safe": predicted_safe,
                "false_safe": false_safe,
                "false_safe_rate": false_safe / predicted_safe if predicted_safe else 0.0,
            }
        )
    rows.sort(key=lambda row: (-row["false_safe"], -row["predicted_safe"], row["path"]))
    return rows


def score_threshold(
    holdout_examples: list[dict[str, Any]],
    y_true: list[str],
    predictions: list[str],
) -> dict[str, Any]:
    stats = false_safe_stats(y_true, predictions)
    return {
        "accuracy": accuracy_score(y_true, predictions),
        "macro_f1": f1_score(y_true, predictions, average="macro", zero_division=0),
        **stats,
        "by_path": count_by_path(holdout_examples, predictions),
    }


def choose_recommendation(
    results: list[dict[str, Any]],
    max_false_safe: int,
    max_false_safe_rate: float,
    min_safe_precision: float,
) -> dict[str, Any] | None:
    eligible = [
        row
        for row in results
        if row["false_safe"] <= max_false_safe
        and row["false_safe_rate"] <= max_false_safe_rate
        and row["safe_precision"] >= min_safe_precision
        and row["predicted_safe"] > 0
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda row: (row["predicted_safe"], row["safe_recall"], -row["false_safe"]), reverse=True)
    return eligible[0]


def main(
    dataset_run_id: int | None = None,
    thresholds_value: str | None = None,
    target_ratio: float = 0.20,
    min_negative: int = 50,
    max_paths: int | None = None,
    safe_multiplier: int = 5,
    feature_set: str = DEFAULT_FEATURE_SET,
    train_strategy: str = DEFAULT_TRAIN_STRATEGY,
    max_false_safe: int = 0,
    max_false_safe_rate: float = 0.0,
    min_safe_precision: float = 1.0,
    sample_limit: int = 8,
    split_mode: str = "file",
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    thresholds = parse_thresholds(thresholds_value)
    print("[ml_threshold_sweep] Starting threshold sweep")
    print(f"[ml_threshold_sweep] Rule version: {RULE_VERSION}")
    print(f"[ml_threshold_sweep] Thresholds: {thresholds}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        dataset_run_id = dataset_run_id or latest_dataset_run_id(conn)
        examples = fetch_examples(conn, dataset_run_id)

    if len(count_labels(examples)) < 2:
        raise RuntimeError("Need at least two risk labels to evaluate thresholds.")

    holdout_paths: list[str]
    if split_mode == "file":
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
    elif split_mode == "stratified":
        selected = select_examples(
            examples,
            safe_multiplier=safe_multiplier,
            max_examples=None,
            train_strategy=train_strategy,
        )
        selected_counts = Counter(row["risk_label"] for row in selected)
        too_small = {label: count for label, count in selected_counts.items() if count < 2}
        if too_small:
            raise RuntimeError(f"Stratified split needs at least two examples per label; got {too_small}.")
        selected_train, holdout_examples = train_test_split(
            selected,
            test_size=target_ratio,
            random_state=RANDOM_SEED,
            stratify=[row["risk_label"] for row in selected],
        )
        holdout_paths = sorted({row["relative_path"] for row in holdout_examples})
    else:
        raise ValueError(f"Unknown split_mode: {split_mode}")

    model = create_model()
    fit_model(
        model,
        [make_text(row, feature_set=feature_set) for row in selected_train],
        [row["risk_label"] for row in selected_train],
        selected_train,
        train_strategy,
    )
    x_holdout = [make_text(row, feature_set=feature_set) for row in holdout_examples]
    y_true = [row["risk_label"] for row in holdout_examples]

    results = []
    for threshold in thresholds:
        predictions = threshold_predictions(model, x_holdout, threshold, holdout_examples)
        row = score_threshold(holdout_examples, y_true, predictions)
        row["threshold"] = threshold
        results.append(row)

    recommendation = choose_recommendation(
        results,
        max_false_safe=max_false_safe,
        max_false_safe_rate=max_false_safe_rate,
        min_safe_precision=min_safe_precision,
    )
    elapsed = datetime.now() - started_at

    report_lines = [
        "ML threshold sweep report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Dataset run id: {dataset_run_id}",
        "",
        "Split policy:",
        f"- Holdout unit: {'complete relative_path files' if split_mode == 'file' else 'stratified examples'}",
        f"- Split mode: {split_mode}",
        f"- Target ratio: {target_ratio:.2%}",
        f"- Minimum negative examples requested: {min_negative}",
        f"- Safe multiplier: {safe_multiplier}",
        f"- Feature set: {feature_set}",
        f"- Train strategy: {train_strategy}",
        "",
        "Holdout paths:",
        *[f"- {path}" for path in holdout_paths],
        "",
        "Safety target:",
        f"- Max false safe: {max_false_safe}",
        f"- Max false safe rate: {percent(max_false_safe_rate)}",
        f"- Min safe precision: {percent(min_safe_precision)}",
        "",
        "Threshold results:",
    ]
    for row in results:
        report_lines.append(
            f"- threshold={row['threshold']:.2f}: "
            f"predicted_safe={row['predicted_safe']}, "
            f"false_safe={row['false_safe']}, "
            f"false_safe_rate={percent(row['false_safe_rate'])}, "
            f"safe_precision={percent(row['safe_precision'])}, "
            f"safe_recall={percent(row['safe_recall'])}, "
            f"macro_f1={row['macro_f1']:.4f}"
        )
    report_lines.extend(["", "Recommendation:"])
    if recommendation:
        report_lines.extend(
            [
                f"- threshold: {recommendation['threshold']:.2f}",
                f"- predicted safe: {recommendation['predicted_safe']}",
                f"- false safe: {recommendation['false_safe']}",
                f"- false safe rate: {percent(recommendation['false_safe_rate'])}",
                f"- safe precision: {percent(recommendation['safe_precision'])}",
                f"- safe recall: {percent(recommendation['safe_recall'])}",
            ]
        )
    else:
        report_lines.append("- none: no threshold met the safety target")
    report_lines.extend(["", f"Top path risks at each threshold:"])
    for row in results:
        report_lines.append(f"- threshold={row['threshold']:.2f}:")
        for path_row in row["by_path"][:sample_limit]:
            report_lines.append(
                f"  - {path_row['path']}: predicted_safe={path_row['predicted_safe']}, "
                f"false_safe={path_row['false_safe']}, false_safe_rate={percent(path_row['false_safe_rate'])}"
            )
    report_lines.extend(
        [
            "",
            "Interpretation:",
            "- This command does not save or promote a model.",
            "- Default recommendation requires zero holdout false-safe rows.",
            "- Use it to choose a safer threshold before training/promoting.",
            "- If no threshold gives useful predicted_safe with acceptable false_safe, improve features or add rules before promotion.",
        ]
    )
    report_path = db.write_report(settings, "ml_threshold_sweep", report_lines)
    print(f"[ml_threshold_sweep] Dataset run id: {dataset_run_id}")
    if recommendation:
        print(
            "[ml_threshold_sweep] Recommendation: "
            f"threshold={recommendation['threshold']:.2f}, "
            f"predicted_safe={recommendation['predicted_safe']}, "
            f"false_safe={recommendation['false_safe']} "
            f"({percent(recommendation['false_safe_rate'])})"
        )
    else:
        print("[ml_threshold_sweep] Recommendation: none")
    print(f"[ml_threshold_sweep] Report: {report_path}")
    print("[ml_threshold_sweep] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate holdout safety across safe thresholds.")
    parser.add_argument("--dataset-run-id", type=int, default=None)
    parser.add_argument("--thresholds", default=None)
    parser.add_argument("--target-ratio", type=float, default=0.20)
    parser.add_argument("--min-negative", type=int, default=50)
    parser.add_argument("--max-paths", type=int, default=None)
    parser.add_argument("--safe-multiplier", type=int, default=5)
    parser.add_argument("--feature-set", choices=FEATURE_SETS, default=DEFAULT_FEATURE_SET)
    parser.add_argument("--train-strategy", choices=TRAIN_STRATEGIES, default=DEFAULT_TRAIN_STRATEGY)
    parser.add_argument("--max-false-safe", type=int, default=0)
    parser.add_argument("--max-false-safe-rate", type=float, default=0.0)
    parser.add_argument("--min-safe-precision", type=float, default=1.0)
    parser.add_argument("--sample-limit", type=int, default=8)
    parser.add_argument("--split-mode", choices=["file", "stratified"], default="file")
    args = parser.parse_args()
    main(
        dataset_run_id=args.dataset_run_id,
        thresholds_value=args.thresholds,
        target_ratio=args.target_ratio,
        min_negative=args.min_negative,
        max_paths=args.max_paths,
        safe_multiplier=args.safe_multiplier,
        feature_set=args.feature_set,
        train_strategy=args.train_strategy,
        max_false_safe=args.max_false_safe,
        max_false_safe_rate=args.max_false_safe_rate,
        min_safe_precision=args.min_safe_precision,
        sample_limit=args.sample_limit,
        split_mode=args.split_mode,
    )
