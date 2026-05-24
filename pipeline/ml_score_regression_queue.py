from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_score_regression_queue_v1"
QUEUE_SOURCE = "ml_group_candidate_queue"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def active_model_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT active_model_run_id
        FROM ml_model_registry
        WHERE model_kind = 'risk_action_classifier'
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No active risk_action_classifier model found in ml_model_registry.")
    return int(row["active_model_run_id"])


def latest_full_score_run(conn, model_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE model_run_id = ?
          AND path_filter IS NULL
          AND limit_count IS NULL
          AND finished_at IS NOT NULL
          AND scored_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (model_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No full score run found for model_run_id={model_run_id}.")
    return dict(row)


def latest_candidate_score_run(conn, active_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE id <> ?
          AND path_filter IS NULL
          AND limit_count IS NULL
          AND finished_at IS NOT NULL
          AND scored_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (active_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No candidate full score run found.")
    return dict(row)


def next_lote_number(conn) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review%lote%'
        """
    ).fetchall()
    highest = 0
    for row in rows:
        mode = row["mode"] or ""
        marker = "lote"
        if marker not in mode:
            continue
        suffix = mode.rsplit(marker, 1)[-1]
        digits = "".join(ch for ch in suffix if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    return highest + 1


def reviewed_segment_ids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.segment_id
        FROM local_learning_candidates c
        JOIN local_learning_runs r ON r.id = c.run_id
        WHERE r.mode LIKE 'human_review%'
        """
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def focus_group(relative_path: str) -> str:
    if relative_path == "titles_l_spanish.yml":
        return "regression_titles"
    if relative_path == "titles_cultural_names_l_spanish.yml":
        return "regression_title_cultural_names"
    if relative_path.startswith("names/"):
        return "regression_names"
    if relative_path.startswith("dynasties/"):
        return "regression_dynasties"
    if relative_path.startswith("religion/"):
        return "regression_religion"
    if relative_path.startswith("culture/"):
        return "regression_culture"
    if relative_path.startswith("dlc/"):
        return "regression_dlc"
    return "regression_general"


def fetch_regressions(
    conn,
    active_score_run_id: int,
    candidate_score_run_id: int,
    limit: int,
    per_path_limit: int,
    clean_only: bool,
) -> list[dict[str, Any]]:
    reviewed = reviewed_segment_ids(conn)
    where = [
        "a.run_id = ?",
        "c.run_id = ?",
        "a.final_action = 'auto_safe'",
        "c.final_action <> 'auto_safe'",
    ]
    if clean_only:
        where.extend(
            [
                "c.issue_count = 0",
                "c.high_issue_count = 0",
                "c.token_status = 'ok'",
            ]
        )
    rows = conn.execute(
        f"""
        SELECT
            c.segment_id,
            c.relative_path,
            c.source_key,
            c.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text,
            c.candidate_text,
            a.model_safe_probability AS active_model_safe_probability,
            c.model_safe_probability AS candidate_model_safe_probability,
            a.final_action AS active_final_action,
            c.final_action AS candidate_final_action,
            c.risk_class AS candidate_risk_class,
            c.issue_count,
            c.high_issue_count,
            c.token_status,
            c.reasons_json,
            c.issues_json
        FROM ml_score_items a
        JOIN ml_score_items c ON c.segment_id = a.segment_id
        JOIN source_segments s ON s.id = c.segment_id
        LEFT JOIN output_segments o ON o.segment_id = c.segment_id
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE
                WHEN c.relative_path = 'titles_l_spanish.yml' THEN 0
                WHEN c.relative_path = 'titles_cultural_names_l_spanish.yml' THEN 1
                WHEN c.relative_path LIKE 'religion/%' THEN 2
                WHEN c.relative_path LIKE 'culture/%' THEN 3
                WHEN c.relative_path LIKE 'dlc/%' THEN 4
                ELSE 5
            END,
            c.model_safe_probability DESC,
            c.relative_path,
            c.source_key
        """,
        (active_score_run_id, candidate_score_run_id),
    ).fetchall()

    selected: list[dict[str, Any]] = []
    per_path: Counter[str] = Counter()
    for row in rows:
        segment_id = int(row["segment_id"])
        relative_path = row["relative_path"]
        if segment_id in reviewed:
            continue
        if per_path[relative_path] >= per_path_limit:
            continue
        item = dict(row)
        item["focus_group"] = focus_group(relative_path)
        item["candidate_kind"] = "active_safe_candidate_regression"
        selected.append(item)
        per_path[relative_path] += 1
        if len(selected) >= limit:
            break
    return selected


def candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": int(row["source_line_number"]) if row.get("source_line_number") else None,
        "source_section": "ML score regression queue",
        "focus_group": row["focus_group"],
        "group_name": row["focus_group"],
        "candidate_kind": row["candidate_kind"],
        "final_action": row["candidate_final_action"],
        "risk_class": row["candidate_risk_class"],
        "model_safe_probability": float(row.get("candidate_model_safe_probability") or 0.0),
        "active_model_safe_probability": float(row.get("active_model_safe_probability") or 0.0),
        "issue_count": int(row.get("issue_count") or 0),
        "token_status": row.get("token_status") or "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": row.get("candidate_text"),
        "candidate_text": row.get("candidate_text"),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
        "regression_reason": "Active model marked this segment auto_safe, but candidate model demoted it.",
    }


def write_json(settings: dict, rows: list[dict[str, Any]], batch_size: int) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        first_lote = next_lote_number(conn)

    batches = []
    for offset in range(0, len(rows), batch_size):
        candidates = rows[offset : offset + batch_size]
        if not candidates:
            continue
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "ML score regression queue",
                "focus_group": candidates[0]["focus_group"],
                "candidates": [candidate_payload(row) for row in candidates],
            }
        )

    payload = {
        "rule_version": "parallel_review_loop_v1",
        "prepared_at": now(),
        "source_type": QUEUE_SOURCE,
        "source_subtype": RULE_VERSION,
        "batch_size": batch_size,
        "batches": batches,
        "instructions": {
            "purpose": "Review clean operational coverage regressions: active model was auto_safe, candidate model demoted.",
            "valid_labels": [
                "contextual_exception",
                "correct",
                "major_fix",
                "minor_fix",
                "rejected",
                "rejected_suggestion",
                "residual_spanish",
                "semantic_error",
                "structure_error",
                "token_mismatch",
            ],
            "recommended_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "semantic_error",
                "residual_spanish",
            ],
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    output_path = reports_dir / f"{timestamp()}_ml_score_regression_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def write_report(
    settings: dict,
    rows: list[dict[str, Any]],
    active_score_run: dict[str, Any],
    candidate_score_run: dict[str, Any],
    output_path: Path,
) -> Path:
    by_group = Counter(row["focus_group"] for row in rows)
    by_path = Counter(row["relative_path"] for row in rows)
    by_action = Counter(row["candidate_final_action"] for row in rows)
    lines = [
        "ML score regression review queue",
        f"Started at: {now()}",
        f"Rule version: {RULE_VERSION}",
        f"Active score run: {active_score_run['id']} model={active_score_run['model_run_id']}",
        f"Candidate score run: {candidate_score_run['id']} model={candidate_score_run['model_run_id']}",
        f"Decision template: {output_path}",
        "",
        "Summary:",
        f"- Candidates: {len(rows)}",
        f"- Active auto safe: {active_score_run['final_auto_safe_count']} ({percent(active_score_run['final_auto_safe_count'], active_score_run['scored_count'])})",
        f"- Candidate auto safe: {candidate_score_run['final_auto_safe_count']} ({percent(candidate_score_run['final_auto_safe_count'], candidate_score_run['scored_count'])})",
        "",
        "By candidate action:",
        *[f"- {key}: {value}" for key, value in sorted(by_action.items())],
        "",
        "By focus group:",
        *[f"- {key}: {value}" for key, value in sorted(by_group.items())],
        "",
        "Top paths:",
        *[f"- {path}: {value}" for path, value in by_path.most_common(20)],
        "",
        "Sample:",
    ]
    for row in rows[:30]:
        lines.append(
            f"- {row['focus_group']} | cand_prob={float(row.get('candidate_model_safe_probability') or 0):.4f} | "
            f"{row['relative_path']}::{row['source_key']} | {row.get('candidate_text') or ''}"
        )
    return db.write_report(settings, "ml_score_regression_queue", lines)


def main(
    active_score_run_id: int | None = None,
    candidate_score_run_id: int | None = None,
    limit: int = 120,
    per_path_limit: int = 20,
    batch_size: int = 20,
    clean_only: bool = True,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_score_regression_queue] Starting score regression queue")
    print(f"[ml_score_regression_queue] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        active_model_id = active_model_run_id(conn)
        active_score_run = (
            dict(conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (active_score_run_id,)).fetchone())
            if active_score_run_id
            else latest_full_score_run(conn, active_model_id)
        )
        candidate_score_run = (
            dict(conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (candidate_score_run_id,)).fetchone())
            if candidate_score_run_id
            else latest_candidate_score_run(conn, int(active_score_run["id"]))
        )
        rows = fetch_regressions(
            conn,
            int(active_score_run["id"]),
            int(candidate_score_run["id"]),
            limit,
            per_path_limit,
            clean_only,
        )

    output_path = write_json(settings, rows, batch_size)
    report_path = write_report(settings, rows, active_score_run, candidate_score_run, output_path)
    elapsed = datetime.now() - started_at
    print(f"[ml_score_regression_queue] Active score run: {active_score_run['id']}")
    print(f"[ml_score_regression_queue] Candidate score run: {candidate_score_run['id']}")
    print(f"[ml_score_regression_queue] Candidates: {len(rows)}")
    print(f"[ml_score_regression_queue] Decision template: {output_path}")
    print(f"[ml_score_regression_queue] Report: {report_path}")
    print(f"[ml_score_regression_queue] Elapsed: {elapsed}")
    print("[ml_score_regression_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a review queue for active-safe segments demoted by a candidate score.")
    parser.add_argument("--active-score-run-id", type=int, default=None)
    parser.add_argument("--candidate-score-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-path-limit", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--include-issues", action="store_true")
    args = parser.parse_args()
    main(
        active_score_run_id=args.active_score_run_id,
        candidate_score_run_id=args.candidate_score_run_id,
        limit=args.limit,
        per_path_limit=args.per_path_limit,
        batch_size=args.batch_size,
        clean_only=not args.include_issues,
    )
