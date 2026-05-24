from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
from ml_score_segments import fetch_segments, final_decision, latest_model_run, model_prediction
from ml_train_risk import DEFAULT_FEATURE_SET


RULE_VERSION = "ml_group_candidate_queue_v1"


DEFAULT_GROUPS = {
    "titles": "titles_l_spanish.yml",
    "title_cultural_names": "titles_cultural_names_l_spanish.yml",
    "names": "names/%",
    "dynasties": "dynasties/%",
    "mottos": "mottos_l_spanish.yml",
    "culture": "culture/%",
    "religion": "religion/%",
    "game_concepts": "%game_concepts_l_spanish.yml",
    "core": "core_l_spanish.yml",
    "custom_localization": "custom_localization/%",
    "council_tasks": "council_tasks_l_spanish.yml",
    "court_positions": "%court_positions%l_spanish.yml",
    "important_actions": "%important_actions_l_spanish.yml",
    "debug": "debug_l_spanish.yml",
    "events": "%events%l_spanish.yml",
    "dlc": "dlc/%",
    "wars": "wars_l_spanish.yml",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def parse_groups(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return list(DEFAULT_GROUPS.items())
    groups = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, pattern = chunk.split("=", 1)
            groups.append((name.strip(), pattern.strip()))
        else:
            groups.append((chunk, DEFAULT_GROUPS.get(chunk, chunk)))
    return groups


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


def score_group(
    conn,
    model,
    feature_set: str,
    group_name: str,
    path_like: str,
    proposed_threshold: float,
    active_threshold: float,
    limit: int,
    reviewed_segment_ids: set[int],
) -> tuple[list[dict[str, Any]], Counter]:
    rows = fetch_segments(conn, limit=limit, path_like=path_like, include_locked=False)
    counts: Counter = Counter(scanned=len(rows))
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if int(row["segment_id"]) in reviewed_segment_ids:
            counts["skipped_reviewed"] += 1
            continue
        model_action, safe_probability, model_confidence, probabilities = model_prediction(
            model,
            row,
            proposed_threshold,
            feature_set,
        )
        item = final_decision(row, model_action, safe_probability, model_confidence, probabilities)
        counts[f"final_{item['final_action']}"] += 1
        counts[f"model_{item['model_action']}"] += 1
        if item["final_action"] != "auto_safe":
            continue
        if item["issue_count"] != 0 or item["token_status"] != "ok":
            continue
        candidate_kind = "already_active_safe" if safe_probability >= active_threshold else "new_at_proposed_threshold"
        candidate = {
            **row,
            **item,
            "group_name": group_name,
            "path_like": path_like,
            "candidate_kind": candidate_kind,
            "proposed_threshold": proposed_threshold,
            "active_threshold": active_threshold,
        }
        candidates.append(candidate)
        counts[candidate_kind] += 1
    candidates.sort(
        key=lambda item: (
            item["candidate_kind"] != "new_at_proposed_threshold",
            -float(item["model_safe_probability"]),
            item["relative_path"],
            item["source_key"],
        )
    )
    return candidates, counts


def write_csv(settings: dict, rows: list[dict[str, Any]]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{timestamp}_ml_group_candidate_queue.csv"
    fieldnames = [
        "group_name",
        "candidate_kind",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "model_safe_probability",
        "proposed_threshold",
        "active_threshold",
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
                    "group_name": row["group_name"],
                    "candidate_kind": row["candidate_kind"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "model_safe_probability": f"{row['model_safe_probability']:.6f}",
                    "proposed_threshold": f"{row['proposed_threshold']:.2f}",
                    "active_threshold": f"{row['active_threshold']:.2f}",
                    "english_text": row.get("english_text") or "",
                    "spanish_text": row.get("spanish_text") or "",
                    "old_text": row.get("old_text") or "",
                    "output_text": row.get("output_text") or "",
                    "candidate_text": row.get("candidate_text") or "",
                    "review_decision": "",
                    "review_notes": "",
                }
            )
    return path


def sample_lines(rows: list[dict[str, Any]], sample_limit: int) -> list[str]:
    if not rows:
        return ["- none"]
    lines = []
    for row in rows[:sample_limit]:
        lines.append(
            f"- {row['group_name']} | {row['candidate_kind']} | "
            f"safe_prob={row['model_safe_probability']:.4f} | "
            f"{row['relative_path']}::{row['source_key']} | candidate=\"{row.get('candidate_text') or ''}\""
        )
    return lines


def main(
    proposed_threshold: float = 0.93,
    limit_per_group: int = 2000,
    groups_value: str | None = None,
    sample_limit: int = 40,
    model_run_id: int | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_group_candidate_queue] Starting group candidate queue")
    print(f"[ml_group_candidate_queue] Rule version: {RULE_VERSION}")

    model_run, model, feature_set, model_path = load_model(settings, model_run_id=model_run_id)
    active_threshold = float(model_run["safe_threshold"] or 0.95)
    groups = parse_groups(groups_value)

    all_candidates: list[dict[str, Any]] = []
    group_counts: list[tuple[str, str, Counter]] = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        reviewed_rows = conn.execute(
            """
            SELECT segment_id
            FROM local_learning_candidates
            WHERE queue_source = 'ml_group_candidate_queue'
              AND local_status = 'reviewed_human'
            """
        ).fetchall()
        reviewed_segment_ids = {int(row["segment_id"]) for row in reviewed_rows}
        for group_name, path_like in groups:
            candidates, counts = score_group(
                conn,
                model,
                feature_set,
                group_name,
                path_like,
                proposed_threshold,
                active_threshold,
                limit_per_group,
                reviewed_segment_ids,
            )
            all_candidates.extend(candidates)
            group_counts.append((group_name, path_like, counts))
            print(
                f"[ml_group_candidate_queue] {group_name}: "
                f"scanned={counts['scanned']} candidates={len(candidates)}"
            )

    csv_path = write_csv(settings, all_candidates)
    by_group = Counter(row["group_name"] for row in all_candidates)
    by_kind = Counter(row["candidate_kind"] for row in all_candidates)
    elapsed = datetime.now() - started_at
    report_lines = [
        "ML group candidate queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Model run id: {model_run['id']}",
        f"Model version: {model_run['model_version']}",
        f"Model path: {model_path}",
        f"Feature set: {feature_set}",
        f"Active threshold: {active_threshold:.2f}",
        f"Proposed threshold: {proposed_threshold:.2f}",
        f"Limit per group: {limit_per_group}",
        f"CSV: {csv_path}",
        "",
        "Group scan summary:",
    ]
    for group_name, path_like, counts in group_counts:
        scanned = int(counts["scanned"] or 0)
        report_lines.extend(
            [
                f"- {group_name} ({path_like}):",
                f"  - scanned: {scanned}",
                f"  - skipped already reviewed: {counts['skipped_reviewed']}",
                f"  - final auto_safe: {counts['final_auto_safe']} ({percent(counts['final_auto_safe'], scanned)})",
                f"  - new at proposed threshold: {counts['new_at_proposed_threshold']}",
                f"  - already active safe: {counts['already_active_safe']}",
                f"  - needs_human: {counts['final_needs_human']} ({percent(counts['final_needs_human'], scanned)})",
                f"  - needs_autofix: {counts['final_needs_autofix']} ({percent(counts['final_needs_autofix'], scanned)})",
                f"  - blocked_structure: {counts['final_blocked_structure']} ({percent(counts['final_blocked_structure'], scanned)})",
            ]
        )
    report_lines.extend(
        [
            "",
            "Candidate summary:",
            f"- Total candidates: {len(all_candidates)}",
            *[f"- {group}: {count}" for group, count in by_group.most_common()],
            "",
            "By candidate kind:",
            *[f"- {kind}: {count}" for kind, count in by_kind.most_common()],
            "",
            f"Top {min(sample_limit, len(all_candidates))} samples:",
            *sample_lines(all_candidates, sample_limit),
            "",
            "Review guidance:",
            "- This queue is experimental and must not be applied automatically.",
            "- Prioritize new_at_proposed_threshold rows; already_active_safe rows are useful as calibration controls.",
            "- If review precision is high, we can consider a group-specific threshold policy later.",
        ]
    )
    report_path = db.write_report(settings, "ml_group_candidate_queue", report_lines)
    print(f"[ml_group_candidate_queue] Candidates: {len(all_candidates)}")
    print(f"[ml_group_candidate_queue] CSV: {csv_path}")
    print(f"[ml_group_candidate_queue] Report: {report_path}")
    print("[ml_group_candidate_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build experimental group-specific auto-safe candidate queue.")
    parser.add_argument("--proposed-threshold", type=float, default=0.93)
    parser.add_argument("--limit-per-group", type=int, default=2000)
    parser.add_argument("--groups", default=None)
    parser.add_argument("--sample-limit", type=int, default=40)
    parser.add_argument("--model-run-id", type=int, default=None)
    args = parser.parse_args()
    main(
        proposed_threshold=args.proposed_threshold,
        limit_per_group=args.limit_per_group,
        groups_value=args.groups,
        sample_limit=args.sample_limit,
        model_run_id=args.model_run_id,
    )
