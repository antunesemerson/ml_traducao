from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_specialist_auditor_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> str:
    if total <= 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_score_run(conn, model_kind: str | None = None) -> int:
    params: list[Any] = []
    where = "WHERE r.finished_at IS NOT NULL"
    if model_kind:
        where += " AND m.model_kind = ?"
        params.append(model_kind)
    row = conn.execute(
        f"""
        SELECT r.id
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        {where}
        ORDER BY r.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No finished score run found for model_kind={model_kind or 'any'}.")
    return int(row["id"])


def score_run_info(conn, score_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            r.*,
            m.model_kind
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE r.id = ?
          AND r.finished_at IS NOT NULL
        """,
        (score_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Score run {score_run_id} is missing or unfinished.")
    return dict(row)


def auditor_action(general_action: str, specialist_action: str) -> tuple[str, bool]:
    if general_action == "auto_safe" and specialist_action == "auto_safe":
        return "auto_safe_agree", False
    if general_action != "auto_safe" and specialist_action == "auto_safe":
        return "specialist_new_safe_review", True
    if general_action == "auto_safe" and specialist_action != "auto_safe":
        return "specialist_demoted_review", True
    return "needs_human_agree", False


def compare_rows(conn, general_score_run_id: int, specialist_score_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.candidate_text,
            g.final_action AS general_action,
            s.final_action AS specialist_action,
            g.model_safe_probability AS general_safe_probability,
            s.model_safe_probability AS specialist_safe_probability,
            g.token_status AS general_token_status,
            s.token_status AS specialist_token_status,
            g.issue_count AS general_issue_count,
            s.issue_count AS specialist_issue_count,
            g.reasons_json AS general_reasons_json,
            s.reasons_json AS specialist_reasons_json,
            COALESCE(r.reviewed_count, 0) AS reviewed_count,
            r.reviewed_labels,
            r.latest_reviewed_at
        FROM ml_score_items s
        JOIN ml_score_items g
          ON g.segment_id = s.segment_id
         AND g.run_id = ?
        LEFT JOIN (
            SELECT
                segment_id,
                COUNT(*) AS reviewed_count,
                GROUP_CONCAT(DISTINCT human_label) AS reviewed_labels,
                MAX(reviewed_at) AS latest_reviewed_at
            FROM local_learning_candidates
            WHERE queue_source = 'ml_specialist_auditor'
              AND local_status = 'reviewed_human'
            GROUP BY segment_id
        ) r ON r.segment_id = s.segment_id
        WHERE s.run_id = ?
        ORDER BY
            CASE
                WHEN s.final_action = 'auto_safe' AND g.final_action <> 'auto_safe' THEN 0
                WHEN s.final_action <> 'auto_safe' AND g.final_action = 'auto_safe' THEN 1
                WHEN s.final_action = 'auto_safe' AND g.final_action = 'auto_safe' THEN 2
                ELSE 3
            END,
            s.model_safe_probability DESC,
            s.segment_id
        """,
        (general_score_run_id, specialist_score_run_id),
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        action, requires_review = auditor_action(item["general_action"], item["specialist_action"])
        already_reviewed = int(item.get("reviewed_count") or 0) > 0
        reasons = [
            f"general_action:{item['general_action']}",
            f"specialist_action:{item['specialist_action']}",
            f"general_safe_probability:{float(item['general_safe_probability'] or 0):.4f}",
            f"specialist_safe_probability:{float(item['specialist_safe_probability'] or 0):.4f}",
        ]
        if requires_review:
            reasons.append("auditor:divergence_requires_human_review")
        if already_reviewed:
            reasons.append("auditor:already_reviewed_human")
        item["auditor_action"] = action
        item["divergence_requires_review"] = int(requires_review)
        item["already_reviewed"] = int(already_reviewed)
        item["requires_human_review"] = int(requires_review and not already_reviewed)
        item["auditor_reasons_json"] = json.dumps(reasons, ensure_ascii=False)
        results.append(item)
    return results


def write_csv(
    settings: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    general_score_run_id: int,
    specialist_score_run_id: int,
) -> Path:
    reports_dir = db.project_path(settings.get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = (
        reports_dir
        / f"{started_at.strftime('%Y%m%d_%H%M%S')}_ml_specialist_auditor_g{general_score_run_id}_s{specialist_score_run_id}.csv"
    )
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "candidate_text",
        "general_action",
        "specialist_action",
        "auditor_action",
        "divergence_requires_review",
        "already_reviewed",
        "requires_human_review",
        "reviewed_count",
        "reviewed_labels",
        "latest_reviewed_at",
        "general_safe_probability",
        "specialist_safe_probability",
        "general_token_status",
        "specialist_token_status",
        "general_issue_count",
        "specialist_issue_count",
        "auditor_reasons_json",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def sample_lines(
    rows: list[dict[str, Any]],
    action: str,
    limit: int = 12,
    pending_only: bool = True,
) -> list[str]:
    selected = [row for row in rows if row["auditor_action"] == action]
    if pending_only:
        selected = [row for row in selected if int(row.get("requires_human_review") or 0)]
    if not selected:
        return ["- none"]
    lines = []
    for row in selected[:limit]:
        lines.append(
            "- segment {segment_id} | {relative_path}::{source_key} | "
            "general={general_action}/{general_safe_probability:.4f} | "
            "specialist={specialist_action}/{specialist_safe_probability:.4f} | "
            "{candidate_text}".format(
                **{
                    **row,
                    "general_safe_probability": float(row["general_safe_probability"] or 0),
                    "specialist_safe_probability": float(row["specialist_safe_probability"] or 0),
                }
            )
        )
    return lines


def main(
    general_score_run_id: int | None = None,
    specialist_score_run_id: int | None = None,
    specialist_model_kind: str = "specialist_religion",
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_specialist_auditor] Starting specialist auditor dry-run")
    print(f"[ml_specialist_auditor] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        general_score_run_id = general_score_run_id or latest_score_run(conn, "risk_action_classifier")
        specialist_score_run_id = specialist_score_run_id or latest_score_run(conn, specialist_model_kind)
        general_info = score_run_info(conn, general_score_run_id)
        specialist_info = score_run_info(conn, specialist_score_run_id)
        rows = compare_rows(conn, general_score_run_id, specialist_score_run_id)

    csv_path = write_csv(settings, rows, started_at, general_score_run_id, specialist_score_run_id)
    counts = Counter(row["auditor_action"] for row in rows)
    pair_counts = Counter((row["general_action"], row["specialist_action"]) for row in rows)
    review_count = sum(int(row["requires_human_review"]) for row in rows)
    divergent_count = sum(int(row["divergence_requires_review"]) for row in rows)
    already_reviewed_count = sum(
        1
        for row in rows
        if int(row["divergence_requires_review"]) and int(row["already_reviewed"])
    )
    elapsed = datetime.now() - started_at
    total = len(rows)
    report_lines = [
        "ML specialist auditor dry-run report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Runs:",
        f"- General score run id: {general_score_run_id} ({general_info['model_version']})",
        f"- Specialist score run id: {specialist_score_run_id} ({specialist_info['model_version']})",
        f"- Specialist model kind: {specialist_info['model_kind']}",
        "",
        "Summary:",
        f"- Compared segments: {total}",
        f"- Divergent rows: {divergent_count} ({percent(divergent_count, total)})",
        f"- Already reviewed divergences: {already_reviewed_count} ({percent(already_reviewed_count, total)})",
        f"- Requires human review: {review_count} ({percent(review_count, total)})",
        f"- CSV: {csv_path}",
        "",
        "Auditor actions:",
        *[f"- {key}: {counts[key]}" for key in sorted(counts)],
        "",
        "General vs specialist action pairs:",
        *[f"- {general} / {specialist}: {count}" for (general, specialist), count in pair_counts.most_common()],
        "",
        "Pending specialist new-safe review samples:",
        *sample_lines(rows, "specialist_new_safe_review", pending_only=True),
        "",
        "Pending specialist demoted review samples:",
        *sample_lines(rows, "specialist_demoted_review", pending_only=True),
        "",
        "Interpretation:",
        "- This is a dry-run auditor; it does not change output or confirmations.",
        "- New-safe and demoted-safe disagreements should become human review queues before any promotion.",
        "- Already reviewed divergences are counted separately so old general-score disagreements do not stay pending forever.",
        "- Deterministic validators still dominate over model votes.",
    ]
    report_path = db.write_report(
        settings,
        f"ml_specialist_auditor_g{general_score_run_id}_s{specialist_score_run_id}",
        report_lines,
    )
    print(f"[ml_specialist_auditor] Compared segments: {total}")
    print(f"[ml_specialist_auditor] Divergent rows: {divergent_count}")
    print(f"[ml_specialist_auditor] Already reviewed divergences: {already_reviewed_count}")
    print(f"[ml_specialist_auditor] Requires human review: {review_count}")
    print(f"[ml_specialist_auditor] CSV: {csv_path}")
    print(f"[ml_specialist_auditor] Report: {report_path}")
    print("[ml_specialist_auditor] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run auditor for general and specialist ML scores.")
    parser.add_argument("--general-score-run-id", type=int, default=None)
    parser.add_argument("--specialist-score-run-id", type=int, default=None)
    parser.add_argument("--specialist-model-kind", default="specialist_religion")
    args = parser.parse_args()
    main(
        general_score_run_id=args.general_score_run_id,
        specialist_score_run_id=args.specialist_score_run_id,
        specialist_model_kind=args.specialist_model_kind,
    )
