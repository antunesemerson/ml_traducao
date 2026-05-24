from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
from ml_score_segments import fetch_segments, final_decision, latest_model_run, model_prediction, percent
from ml_train_risk import DEFAULT_FEATURE_SET
from ml_train_risk import key_shape


RULE_VERSION = "ml_coverage_scan_v1"


DEFAULT_GROUPS = [
    ("all_priority_sample", None),
    ("core", "core_l_spanish.yml"),
    ("game_concepts", "%game_concepts_l_spanish.yml"),
    ("titles", "titles_l_spanish.yml"),
    ("title_cultural_names", "titles_cultural_names_l_spanish.yml"),
    ("names", "names/%"),
    ("dynasties", "dynasties/%"),
    ("mottos", "mottos_l_spanish.yml"),
    ("custom_localization", "custom_localization/%"),
    ("culture", "culture/%"),
    ("religion", "religion/%"),
    ("court_positions", "%court_positions%l_spanish.yml"),
    ("council_tasks", "council_tasks_l_spanish.yml"),
    ("important_actions", "%important_actions_l_spanish.yml"),
    ("gui", "gui/%"),
    ("debug", "debug_l_spanish.yml"),
    ("events", "%events%l_spanish.yml"),
    ("dlc", "dlc/%"),
    ("wars", "wars_l_spanish.yml"),
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_model(settings: dict, model_run_id: int | None = None) -> tuple[dict[str, Any], Any, str, Path]:
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if model_run_id is None:
            model_run = latest_model_run(conn)
        else:
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
                raise RuntimeError(f"No trained risk model found for run id {model_run_id}.")
            model_run = dict(row)
    model_path = db.project_path(model_run["model_path"])
    bundle = joblib.load(model_path)
    metadata = bundle.get("metadata", {})
    feature_set = metadata.get("feature_set") or DEFAULT_FEATURE_SET
    return model_run, bundle["model"], feature_set, model_path


def score_rows(model, rows: list[dict[str, Any]], threshold: float, feature_set: str) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        model_action, safe_probability, model_confidence, probabilities = model_prediction(
            model,
            row,
            threshold,
            feature_set,
        )
        item = final_decision(row, model_action, safe_probability, model_confidence, probabilities)
        items.append(item)
    return items


def group_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    final_counts = Counter(item["final_action"] for item in items)
    model_counts = Counter(item["model_action"] for item in items)
    key_shape_counts = Counter(key_shape(item.get("source_key")) for item in items)
    frontier_key_shape_counts = Counter(
        key_shape(item.get("source_key"))
        for item in items
        if item["final_action"] != "auto_safe"
        and item["issue_count"] == 0
        and item["token_status"] == "ok"
        and item["model_safe_probability"] >= 0.80
    )
    deterministic_blocks = sum(int(item["deterministic_blocked"]) for item in items)
    clean_frontier = [
        item
        for item in items
        if item["final_action"] != "auto_safe"
        and item["issue_count"] == 0
        and item["token_status"] == "ok"
        and item["model_safe_probability"] >= 0.80
    ]
    top_safe = sorted(items, key=lambda item: item["model_safe_probability"], reverse=True)[:8]
    top_frontier = sorted(clean_frontier, key=lambda item: item["model_safe_probability"], reverse=True)[:8]
    return {
        "total": len(items),
        "final_counts": final_counts,
        "model_counts": model_counts,
        "key_shape_counts": key_shape_counts,
        "frontier_key_shape_counts": frontier_key_shape_counts,
        "deterministic_blocks": deterministic_blocks,
        "clean_frontier_count": len(clean_frontier),
        "top_safe": top_safe,
        "top_frontier": top_frontier,
    }


def format_counts(counter: Counter) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter))


def sample_lines(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["  - none"]
    lines = []
    for item in items:
        lines.append(
            f"  - safe_prob={item['model_safe_probability']:.4f} | "
            f"model={item['model_action']} final={item['final_action']} | "
            f"issues={item['issue_count']} | {item['relative_path']}::{item['source_key']}"
        )
    return lines


def parse_groups(value: str | None) -> list[tuple[str, str | None]]:
    if not value:
        return DEFAULT_GROUPS
    groups = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, pattern = chunk.split("=", 1)
            groups.append((name.strip(), pattern.strip() or None))
        else:
            groups.append((chunk, chunk))
    return groups


def main(
    limit_per_group: int = 1000,
    threshold: float | None = None,
    include_locked: bool = False,
    groups_value: str | None = None,
    model_run_id: int | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_coverage_scan] Starting ML coverage scan")
    print(f"[ml_coverage_scan] Rule version: {RULE_VERSION}")

    model_run, model, feature_set, model_path = load_model(settings, model_run_id=model_run_id)
    threshold = threshold if threshold is not None else float(model_run["safe_threshold"] or 0.90)
    groups = parse_groups(groups_value)

    summaries = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        for group_name, path_like in groups:
            rows = fetch_segments(conn, limit=limit_per_group, path_like=path_like, include_locked=include_locked)
            items = score_rows(model, rows, threshold, feature_set)
            summaries.append((group_name, path_like, group_summary(items)))
            print(f"[ml_coverage_scan] {group_name}: {len(items)} rows")

    elapsed = datetime.now() - started_at
    report_lines = [
        "ML coverage scan report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Model run id: {model_run['id']}",
        f"Model version: {model_run['model_version']}",
        f"Model path: {model_path}",
        f"Threshold: {threshold:.2f}",
        f"Feature set: {feature_set}",
        f"Limit per group: {limit_per_group}",
        f"Include locked: {include_locked}",
        "",
        "Group summaries:",
    ]
    for group_name, path_like, summary in summaries:
        total = summary["total"]
        final_counts = summary["final_counts"]
        model_counts = summary["model_counts"]
        report_lines.extend(
            [
                f"- {group_name} ({path_like or 'no path filter'}):",
                f"  - scanned: {total}",
                f"  - final auto_safe: {final_counts['auto_safe']} ({percent(final_counts['auto_safe'], total)})",
                f"  - needs_human: {final_counts['needs_human']} ({percent(final_counts['needs_human'], total)})",
                f"  - needs_autofix: {final_counts['needs_autofix']} ({percent(final_counts['needs_autofix'], total)})",
                f"  - blocked_structure: {final_counts['blocked_structure']} ({percent(final_counts['blocked_structure'], total)})",
                f"  - deterministic blocks: {summary['deterministic_blocks']}",
                f"  - clean high-probability frontier: {summary['clean_frontier_count']}",
                f"  - model actions: {format_counts(model_counts)}",
                f"  - key shapes: {format_counts(summary['key_shape_counts'])}",
                f"  - frontier key shapes: {format_counts(summary['frontier_key_shape_counts'])}",
                "  - top safe probabilities:",
                *sample_lines(summary["top_safe"]),
                "  - top clean frontier probabilities:",
                *sample_lines(summary["top_frontier"]),
            ]
        )
    report_lines.extend(
        [
            "",
            "Interpretation:",
            "- This report does not write ml_score tables and does not apply output changes.",
            "- Groups with many high safe probabilities but no final auto_safe may need threshold or deterministic-gate review.",
            "- Groups with zero high probabilities need more positive examples or different features before automation is useful.",
        ]
    )
    report_path = db.write_report(settings, "ml_coverage_scan", report_lines)
    print(f"[ml_coverage_scan] Report: {report_path}")
    print("[ml_coverage_scan] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan real corpus coverage for active ML model.")
    parser.add_argument("--limit-per-group", type=int, default=1000)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--include-locked", action="store_true")
    parser.add_argument("--groups", default=None)
    parser.add_argument("--model-run-id", type=int, default=None)
    args = parser.parse_args()
    main(
        limit_per_group=args.limit_per_group,
        threshold=args.threshold,
        include_locked=args.include_locked,
        groups_value=args.groups,
        model_run_id=args.model_run_id,
    )
