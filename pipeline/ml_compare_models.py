from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
from ml_score_segments import fetch_segments, final_decision, model_predictions, percent
from ml_train_risk import DEFAULT_FEATURE_SET, fetch_examples, latest_dataset_run_id, make_text, path_group


RULE_VERSION = "ml_compare_models_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_model_run(conn, model_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE id = ?
          AND model_kind = 'risk_action_classifier'
          AND model_path IS NOT NULL
        """,
        (model_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No risk classifier model run found for id {model_run_id}.")
    return dict(row)


def load_bundle(model_run: dict[str, Any]) -> tuple[Any, str]:
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    return bundle["model"], bundle.get("feature_set") or DEFAULT_FEATURE_SET


def row_summary(row: dict[str, Any], left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": row["segment_id"],
        "group": path_group(row["relative_path"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "left_final_action": left["final_action"],
        "right_final_action": right["final_action"],
        "left_model_action": left["model_action"],
        "right_model_action": right["model_action"],
        "left_safe_probability": left["model_safe_probability"],
        "right_safe_probability": right["model_safe_probability"],
        "probability_delta": round(right["model_safe_probability"] - left["model_safe_probability"], 6),
        "token_status": right["token_status"],
        "issue_count": right["issue_count"],
        "right_reasons": " | ".join(right["reasons"]),
        "candidate_text": row.get("candidate_text") or "",
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "old_text": row.get("old_text") or "",
        "output_text": row.get("output_text") or "",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def top_counter_lines(counter: Counter, limit: int = 20) -> list[str]:
    if not counter:
        return ["- none"]
    return [f"- {key}: {count}" for key, count in counter.most_common(limit)]


def threshold_model_batch(
    model,
    rows: list[dict[str, Any]],
    safe_threshold: float,
    feature_set: str,
) -> list[tuple[str, float, dict[str, float]]]:
    if not rows:
        return []
    texts = [make_text(row, feature_set=feature_set) for row in rows]
    classes = list(model.named_steps["classifier"].classes_)
    probabilities_by_row = model.predict_proba(texts)
    predictions = []
    for probabilities in probabilities_by_row:
        probability_by_class = {label: float(probabilities[index]) for index, label in enumerate(classes)}
        safe_probability = probability_by_class.get("auto_safe", 0.0)
        if safe_probability >= safe_threshold:
            predictions.append(("auto_safe", safe_probability, probability_by_class))
            continue
        non_safe = [(label, probability) for label, probability in probability_by_class.items() if label != "auto_safe"]
        action, _ = max(non_safe, key=lambda item: item[1])
        predictions.append((action, safe_probability, probability_by_class))
    return predictions


def training_row_summary(
    row: dict[str, Any],
    left_pred: tuple[str, float, dict[str, float]],
    right_pred: tuple[str, float, dict[str, float]],
) -> dict[str, Any]:
    return {
        "segment_id": row["segment_id"],
        "group": path_group(row["relative_path"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "truth_label": row["risk_label"],
        "action_label": row["action_label"],
        "issue_label": row.get("issue_label") or "",
        "evidence_source": row.get("evidence_source") or "",
        "evidence_id": row.get("evidence_id") or "",
        "left_action": left_pred[0],
        "right_action": right_pred[0],
        "left_safe_probability": round(left_pred[1], 6),
        "right_safe_probability": round(right_pred[1], 6),
        "probability_delta": round(right_pred[1] - left_pred[1], 6),
        "candidate_text": row.get("candidate_text") or "",
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "old_text": row.get("old_text") or "",
        "output_text": row.get("output_text") or "",
    }


def compare_training_examples(
    settings: dict,
    left_run: dict[str, Any],
    right_run: dict[str, Any],
    left_model,
    right_model,
    left_feature_set: str,
    right_feature_set: str,
    dataset_run_id: int | None,
    left_safe_threshold: float,
    right_safe_threshold: float,
    batch_size: int,
    sample_limit: int,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        dataset_run_id = dataset_run_id or latest_dataset_run_id(conn)
        examples = fetch_examples(conn, dataset_run_id)

    total = 0
    action_pairs: Counter = Counter()
    label_pairs: dict[str, Counter] = defaultdict(Counter)
    left_safe = 0
    right_safe = 0
    left_false_safe = 0
    right_false_safe = 0
    both_safe = 0
    lost_safe = 0
    gained_safe = 0
    lost_by_group: Counter = Counter()
    gained_by_group: Counter = Counter()
    lost_by_truth: Counter = Counter()
    gained_by_truth: Counter = Counter()
    lost_samples: list[dict[str, Any]] = []
    gained_samples: list[dict[str, Any]] = []
    changed_samples: list[dict[str, Any]] = []

    for offset in range(0, len(examples), batch_size):
        rows = examples[offset : offset + batch_size]
        left_predictions = threshold_model_batch(left_model, rows, left_safe_threshold, left_feature_set)
        right_predictions = threshold_model_batch(right_model, rows, right_safe_threshold, right_feature_set)
        for row, left_pred, right_pred in zip(rows, left_predictions, right_predictions):
            total += 1
            group = path_group(row["relative_path"])
            truth = row["risk_label"]
            left_action = left_pred[0]
            right_action = right_pred[0]
            action_pairs[(left_action, right_action)] += 1
            label_pairs[truth][(left_action, right_action)] += 1
            if left_action == "auto_safe":
                left_safe += 1
                if truth != "auto_safe":
                    left_false_safe += 1
            if right_action == "auto_safe":
                right_safe += 1
                if truth != "auto_safe":
                    right_false_safe += 1
            if left_action == "auto_safe" and right_action == "auto_safe":
                both_safe += 1
            elif left_action == "auto_safe" and right_action != "auto_safe":
                lost_safe += 1
                lost_by_group[group] += 1
                lost_by_truth[truth] += 1
                sample = training_row_summary(row, left_pred, right_pred)
                if len(lost_samples) < sample_limit:
                    lost_samples.append(sample)
                if len(changed_samples) < sample_limit:
                    changed_samples.append(sample)
            elif left_action != "auto_safe" and right_action == "auto_safe":
                gained_safe += 1
                gained_by_group[group] += 1
                gained_by_truth[truth] += 1
                sample = training_row_summary(row, left_pred, right_pred)
                if len(gained_samples) < sample_limit:
                    gained_samples.append(sample)
                if len(changed_samples) < sample_limit:
                    changed_samples.append(sample)
            elif left_action != right_action and len(changed_samples) < sample_limit:
                changed_samples.append(training_row_summary(row, left_pred, right_pred))

    report_lines = [
        "Training dataset model comparison:",
        f"- Dataset run id: {dataset_run_id}",
        f"- Left safe threshold: {left_safe_threshold:.2f}",
        f"- Right safe threshold: {right_safe_threshold:.2f}",
        f"- Compared examples: {total}",
        f"- Left auto_safe: {left_safe} ({percent(left_safe, total)})",
        f"- Right auto_safe: {right_safe} ({percent(right_safe, total)})",
        f"- Both auto_safe: {both_safe}",
        f"- Lost auto_safe: {lost_safe}",
        f"- Gained auto_safe: {gained_safe}",
        f"- Net auto_safe delta: {right_safe - left_safe}",
        f"- Left false safe: {left_false_safe}",
        f"- Right false safe: {right_false_safe}",
        "",
        "Lost auto_safe by group:",
        *top_counter_lines(lost_by_group),
        "",
        "Gained auto_safe by group:",
        *top_counter_lines(gained_by_group),
        "",
        "Lost auto_safe by truth label:",
        *top_counter_lines(lost_by_truth),
        "",
        "Gained auto_safe by truth label:",
        *top_counter_lines(gained_by_truth),
        "",
        "Action transitions:",
        *[
            f"- {left} -> {right}: {count}"
            for (left, right), count in action_pairs.most_common(30)
        ],
        "",
        "Transitions by truth label:",
    ]
    for truth, counter in sorted(label_pairs.items()):
        report_lines.append(f"- {truth}:")
        report_lines.extend(f"  - {left} -> {right}: {count}" for (left, right), count in counter.most_common(10))
    return report_lines, lost_samples, gained_samples, changed_samples


def main(
    left_model_run_id: int,
    right_model_run_id: int,
    safe_threshold: float = 0.90,
    batch_size: int = 5000,
    limit: int | None = None,
    path_like: str | None = None,
    include_locked: bool = False,
    sample_limit: int = 200,
    dataset_run_id: int | None = None,
    use_model_thresholds: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    reports_dir = db.project_path(settings["reports_dir"])

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        left_run = load_model_run(conn, left_model_run_id)
        right_run = load_model_run(conn, right_model_run_id)

    left_model, left_feature_set = load_bundle(left_run)
    right_model, right_feature_set = load_bundle(right_run)
    left_safe_threshold = float(left_run["safe_threshold"] if use_model_thresholds else safe_threshold)
    right_safe_threshold = float(right_run["safe_threshold"] if use_model_thresholds else safe_threshold)

    if dataset_run_id is not None:
        comparison_lines, lost_samples, gained_samples, changed_samples = compare_training_examples(
            settings=settings,
            left_run=left_run,
            right_run=right_run,
            left_model=left_model,
            right_model=right_model,
            left_feature_set=left_feature_set,
            right_feature_set=right_feature_set,
            dataset_run_id=dataset_run_id,
            left_safe_threshold=left_safe_threshold,
            right_safe_threshold=right_safe_threshold,
            batch_size=batch_size,
            sample_limit=sample_limit,
        )
        lost_csv = reports_dir / f"{timestamp}_ml_compare_models_lost_safe.csv"
        gained_csv = reports_dir / f"{timestamp}_ml_compare_models_gained_safe.csv"
        changed_csv = reports_dir / f"{timestamp}_ml_compare_models_changed_samples.csv"
        write_csv(lost_csv, lost_samples)
        write_csv(gained_csv, gained_samples)
        write_csv(changed_csv, changed_samples)
        elapsed = datetime.now() - started_at
        report_lines = [
            "ML model comparison report",
            f"Started at: {started_at.isoformat(timespec='seconds')}",
            f"Elapsed: {elapsed}",
            f"Rule version: {RULE_VERSION}",
            "",
            "Models:",
            f"- Left model run id: {left_run['id']}",
            f"- Left version: {left_run['model_version']}",
            f"- Left dataset run id: {left_run['dataset_run_id']}",
            f"- Left feature set: {left_feature_set}",
            f"- Right model run id: {right_run['id']}",
            f"- Right version: {right_run['model_version']}",
            f"- Right dataset run id: {right_run['dataset_run_id']}",
            f"- Right feature set: {right_feature_set}",
            "",
            "Scope:",
            f"- Safe threshold mode: {'model thresholds' if use_model_thresholds else 'single threshold'}",
            f"- Left safe threshold: {left_safe_threshold:.2f}",
            f"- Right safe threshold: {right_safe_threshold:.2f}",
            "- Comparison mode: training_dataset",
            "",
            *comparison_lines,
            "",
            "CSV samples:",
            f"- Lost safe: {lost_csv.relative_to(db.PROJECT_ROOT)}",
            f"- Gained safe: {gained_csv.relative_to(db.PROJECT_ROOT)}",
            f"- Changed samples: {changed_csv.relative_to(db.PROJECT_ROOT)}",
        ]
        report_path = db.write_report(settings, "ml_compare_models", report_lines)
        print("[ml_compare_models] Starting model comparison")
        print(f"[ml_compare_models] Left model: {left_run['id']} {left_run['model_version']}")
        print(f"[ml_compare_models] Right model: {right_run['id']} {right_run['model_version']}")
        print(f"[ml_compare_models] Mode: training dataset {dataset_run_id}")
        print(f"[ml_compare_models] Report: {report_path}")
        print("[ml_compare_models] Done")
        return

    total = 0
    action_pairs: Counter = Counter()
    group_pairs: dict[str, Counter] = defaultdict(Counter)
    left_safe = 0
    right_safe = 0
    both_safe = 0
    lost_safe = 0
    gained_safe = 0
    unchanged_non_safe = 0
    lost_samples: list[dict[str, Any]] = []
    gained_samples: list[dict[str, Any]] = []
    changed_samples: list[dict[str, Any]] = []
    lost_by_group: Counter = Counter()
    gained_by_group: Counter = Counter()
    changed_by_group: Counter = Counter()

    offset = 0
    remaining = limit
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        while True:
            current_limit = batch_size if remaining is None else min(batch_size, remaining)
            if current_limit <= 0:
                break
            rows = fetch_segments(
                conn,
                limit=current_limit,
                path_like=path_like,
                include_locked=include_locked,
                offset=offset,
            )
            if not rows:
                break
            left_predictions = model_predictions(left_model, rows, left_safe_threshold, left_feature_set)
            right_predictions = model_predictions(right_model, rows, right_safe_threshold, right_feature_set)
            for row, left_pred, right_pred in zip(rows, left_predictions, right_predictions):
                left_item = final_decision(row, *left_pred)
                right_item = final_decision(row, *right_pred)
                group = path_group(row["relative_path"])
                left_action = left_item["final_action"]
                right_action = right_item["final_action"]
                action_pairs[(left_action, right_action)] += 1
                group_pairs[group][(left_action, right_action)] += 1
                total += 1
                if left_action == "auto_safe":
                    left_safe += 1
                if right_action == "auto_safe":
                    right_safe += 1
                if left_action == "auto_safe" and right_action == "auto_safe":
                    both_safe += 1
                elif left_action == "auto_safe" and right_action != "auto_safe":
                    lost_safe += 1
                    lost_by_group[group] += 1
                    sample = row_summary(row, left_item, right_item)
                    if len(lost_samples) < sample_limit:
                        lost_samples.append(sample)
                    if len(changed_samples) < sample_limit:
                        changed_samples.append(sample)
                elif left_action != "auto_safe" and right_action == "auto_safe":
                    gained_safe += 1
                    gained_by_group[group] += 1
                    sample = row_summary(row, left_item, right_item)
                    if len(gained_samples) < sample_limit:
                        gained_samples.append(sample)
                    if len(changed_samples) < sample_limit:
                        changed_samples.append(sample)
                elif left_action == right_action:
                    unchanged_non_safe += 1
                else:
                    changed_by_group[group] += 1
                    if len(changed_samples) < sample_limit:
                        changed_samples.append(row_summary(row, left_item, right_item))
            offset += len(rows)
            if remaining is not None:
                remaining -= len(rows)
            if len(rows) < current_limit:
                break

    lost_csv = reports_dir / f"{timestamp}_ml_compare_models_lost_safe.csv"
    gained_csv = reports_dir / f"{timestamp}_ml_compare_models_gained_safe.csv"
    changed_csv = reports_dir / f"{timestamp}_ml_compare_models_changed_samples.csv"
    write_csv(lost_csv, lost_samples)
    write_csv(gained_csv, gained_samples)
    write_csv(changed_csv, changed_samples)

    elapsed = datetime.now() - started_at
    report_lines = [
        "ML model comparison report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Models:",
        f"- Left model run id: {left_run['id']}",
        f"- Left version: {left_run['model_version']}",
        f"- Left dataset run id: {left_run['dataset_run_id']}",
        f"- Left feature set: {left_feature_set}",
        f"- Right model run id: {right_run['id']}",
        f"- Right version: {right_run['model_version']}",
        f"- Right dataset run id: {right_run['dataset_run_id']}",
        f"- Right feature set: {right_feature_set}",
        "",
        "Scope:",
        f"- Safe threshold mode: {'model thresholds' if use_model_thresholds else 'single threshold'}",
        f"- Left safe threshold: {left_safe_threshold:.2f}",
        f"- Right safe threshold: {right_safe_threshold:.2f}",
        f"- Path filter: {path_like or 'all'}",
        f"- Include locked: {include_locked}",
        f"- Limit: {limit if limit is not None else 'all'}",
        f"- Compared segments: {total}",
        "",
        "Summary:",
        f"- Left auto_safe: {left_safe} ({percent(left_safe, total)})",
        f"- Right auto_safe: {right_safe} ({percent(right_safe, total)})",
        f"- Both auto_safe: {both_safe}",
        f"- Lost auto_safe: {lost_safe}",
        f"- Gained auto_safe: {gained_safe}",
        f"- Net auto_safe delta: {right_safe - left_safe}",
        f"- Unchanged non-safe: {unchanged_non_safe}",
        "",
        "Lost auto_safe by group:",
        *top_counter_lines(lost_by_group),
        "",
        "Gained auto_safe by group:",
        *top_counter_lines(gained_by_group),
        "",
        "Other changed non-safe by group:",
        *top_counter_lines(changed_by_group),
        "",
        "Action transitions:",
        *[
            f"- {left} -> {right}: {count}"
            for (left, right), count in action_pairs.most_common(30)
        ],
        "",
        "CSV samples:",
        f"- Lost safe: {lost_csv.relative_to(db.PROJECT_ROOT)}",
        f"- Gained safe: {gained_csv.relative_to(db.PROJECT_ROOT)}",
        f"- Changed samples: {changed_csv.relative_to(db.PROJECT_ROOT)}",
    ]
    report_path = db.write_report(settings, "ml_compare_models", report_lines)

    print("[ml_compare_models] Starting model comparison")
    print(f"[ml_compare_models] Left model: {left_run['id']} {left_run['model_version']}")
    print(f"[ml_compare_models] Right model: {right_run['id']} {right_run['model_version']}")
    print(f"[ml_compare_models] Compared: {total}")
    print(f"[ml_compare_models] Left auto_safe: {left_safe}")
    print(f"[ml_compare_models] Right auto_safe: {right_safe}")
    print(f"[ml_compare_models] Lost/gained: {lost_safe}/{gained_safe}")
    print(f"[ml_compare_models] Report: {report_path}")
    print("[ml_compare_models] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare two saved ML risk classifiers on the same operational corpus.")
    parser.add_argument("--left-model-run-id", type=int, required=True)
    parser.add_argument("--right-model-run-id", type=int, required=True)
    parser.add_argument("--safe-threshold", type=float, default=0.90)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--include-locked", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=200)
    parser.add_argument("--dataset-run-id", type=int, default=None)
    parser.add_argument("--use-model-thresholds", action="store_true")
    args = parser.parse_args()
    main(
        left_model_run_id=args.left_model_run_id,
        right_model_run_id=args.right_model_run_id,
        safe_threshold=args.safe_threshold,
        batch_size=args.batch_size,
        limit=args.limit,
        path_like=args.path_like,
        include_locked=args.include_locked,
        sample_limit=args.sample_limit,
        dataset_run_id=args.dataset_run_id,
        use_model_thresholds=args.use_model_thresholds,
    )
