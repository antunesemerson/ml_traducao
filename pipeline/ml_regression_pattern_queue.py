from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_regression_pattern_queue_v2"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def next_lote_number(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review_%_lote%'
        """
    ).fetchall()
    lote_numbers: list[int] = []
    for row in rows:
        match = re.search(r"lote(\d+)", str(row["mode"] or ""))
        if match:
            lote_numbers.append(int(match.group(1)))
    return (max(lote_numbers) + 1) if lote_numbers else 1


def key_shape(source_key: str | None) -> str:
    key = source_key or ""
    if key.endswith("_adj"):
        return "adjective"
    if key.startswith("b_"):
        return "barony"
    if key.startswith("c_"):
        return "county"
    if key.startswith("d_"):
        return "duchy"
    if key.startswith("k_"):
        return "kingdom"
    if key.startswith("e_"):
        return "empire"
    if key.isupper():
        return "uppercase"
    return "other"


def probability_bucket(value: float) -> str:
    if value >= 0.88:
        return "p088_089"
    if value >= 0.84:
        return "p084_088"
    if value >= 0.78:
        return "p078_084"
    if value >= 0.70:
        return "p070_078"
    return "p000_070"


def latest_score_run(conn: sqlite3.Connection, model_kind: str | None = None) -> int:
    params: list[Any] = []
    where = "WHERE r.finished_at IS NOT NULL AND r.scored_count > 0"
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
        raise RuntimeError("No completed score run found.")
    return int(row["id"])


def fetch_candidates(
    conn: sqlite3.Connection,
    active_score_run_id: int,
    candidate_score_run_id: int,
    include_reviewed: bool,
) -> list[dict[str, Any]]:
    reviewed_filter = ""
    if not include_reviewed:
        reviewed_filter = """
          AND NOT EXISTS (
              SELECT 1
              FROM local_learning_candidates l
              WHERE l.segment_id = s.id
                AND l.local_status = 'reviewed_human'
          )
        """
    rows = conn.execute(
        f"""
        WITH reviewed AS (
            SELECT
                segment_id,
                SUM(
                    CASE
                        WHEN local_status = 'reviewed_human'
                         AND human_label IN (
                            'major_fix',
                            'minor_fix',
                            'rejected',
                            'rejected_suggestion',
                            'residual_spanish',
                            'semantic_error',
                            'structure_error',
                            'token_mismatch'
                         )
                        THEN 1 ELSE 0
                    END
                ) AS negative_count,
                GROUP_CONCAT(DISTINCT human_label) AS labels
            FROM local_learning_candidates
            GROUP BY segment_id
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            c.candidate_text,
            c.final_action,
            c.risk_class,
            c.model_safe_probability,
            c.token_status,
            c.issue_count,
            c.high_issue_count,
            c.medium_issue_count,
            c.deterministic_blocked,
            COALESCE(r.negative_count, 0) AS negative_count,
            r.labels AS reviewed_labels
        FROM ml_score_items active
        JOIN ml_score_items c
          ON c.segment_id = active.segment_id
         AND c.run_id = ?
        JOIN source_segments s ON s.id = active.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN reviewed r ON r.segment_id = s.id
        WHERE active.run_id = ?
          AND active.final_action = 'auto_safe'
          AND c.final_action <> 'auto_safe'
          AND s.relative_path = 'titles_l_spanish.yml'
          AND TRIM(COALESCE(c.candidate_text, '')) = TRIM(COALESCE(o.portuguese_text, ''))
          AND TRIM(COALESCE(c.candidate_text, '')) = TRIM(COALESCE(s.old_text, ''))
          AND TRIM(COALESCE(c.candidate_text, '')) = TRIM(COALESCE(s.spanish_text, ''))
          AND c.token_status = 'ok'
          AND COALESCE(c.issue_count, 0) = 0
          AND COALESCE(c.high_issue_count, 0) = 0
          AND COALESCE(c.medium_issue_count, 0) = 0
          AND COALESCE(c.deterministic_blocked, 0) = 0
          AND COALESCE(r.negative_count, 0) = 0
          {reviewed_filter}
        ORDER BY c.model_safe_probability DESC, s.relative_path, s.source_key
        """,
        (candidate_score_run_id, active_score_run_id),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        probability = float(item.get("model_safe_probability") or 0.0)
        item["pattern_bucket"] = f"{key_shape(item.get('source_key'))}:{probability_bucket(probability)}"
        item["key_shape"] = key_shape(item.get("source_key"))
        result.append(item)
    return result


def select_stratified(rows: list[dict[str, Any]], limit: int, per_bucket: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row["pattern_bucket"])].append(row)

    bucket_rows_by_name: dict[str, list[dict[str, Any]]] = {}
    for bucket, bucket_values in buckets.items():
        bucket_rows_by_name[bucket] = sorted(
            bucket_values,
            key=lambda item: (-float(item.get("model_safe_probability") or 0.0), int(item["segment_id"])),
        )[:per_bucket]

    selected: list[dict[str, Any]] = []
    bucket_names = sorted(bucket_rows_by_name)
    while len(selected) < limit:
        picked_in_round = 0
        for bucket in bucket_names:
            bucket_rows = bucket_rows_by_name[bucket]
            if not bucket_rows:
                continue
            selected.append(bucket_rows.pop(0))
            picked_in_round += 1
            if len(selected) >= limit:
                break
        if picked_in_round == 0:
            break

    selected.sort(
        key=lambda item: (
            item["key_shape"],
            -float(item.get("model_safe_probability") or 0.0),
            int(item["segment_id"]),
        )
    )
    return selected[:limit]


def candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    probability = float(row.get("model_safe_probability") or 0.0)
    reasons = [
        f"rule:{RULE_VERSION}",
        "pattern:title_preserved_old_spanish_output",
        f"key_shape:{row['key_shape']}",
        f"pattern_bucket:{row['pattern_bucket']}",
        "active_model:auto_safe",
        f"candidate_action:{row.get('final_action')}",
        f"candidate_safe_probability:{probability:.6f}",
        "candidate_equals_spanish_old_output",
        "token_status:ok",
        "issue_count:0",
    ]
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "source_section": "ML regression pattern queue",
        "focus_group": "title_preserved_old_output",
        "group_name": "title_preserved_old_output",
        "candidate_kind": "active_safe_title_preserved_regression",
        "final_action": row.get("final_action"),
        "risk_class": row.get("risk_class") or "medium",
        "model_safe_probability": probability,
        "issue_count": int(row.get("issue_count") or 0),
        "token_status": row.get("token_status") or "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("current_output_text"),
        "suggested_text": row.get("candidate_text"),
        "candidate_text": row.get("candidate_text"),
        "auditor_reasons_json": json.dumps(reasons, ensure_ascii=False),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def build_batches(rows: list[dict[str, Any]], first_lote: int, batch_size: int) -> list[dict[str, Any]]:
    batches = []
    for offset in range(0, len(rows), batch_size):
        batch_rows = rows[offset : offset + batch_size]
        if not batch_rows:
            continue
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "ML regression pattern queue",
                "focus_group": "title_preserved_old_output",
                "queue_source": "ml_group_candidate_queue",
                "candidates": [candidate_payload(row) for row in batch_rows],
            }
        )
    return batches


def write_outputs(
    settings: dict[str, Any],
    rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    batches: list[dict[str, Any]],
    active_score_run_id: int,
    candidate_score_run_id: int,
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp()
    template_path = reports_dir / f"{stamp}_title_preserved_regression_review_decisions_template.json"
    report_path = reports_dir / f"{stamp}_title_preserved_regression_queue.txt"
    payload = {
        "rule_version": "parallel_review_loop_v1",
        "prepared_by": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_group_candidate_queue",
        "score_run_id": None,
        "source_report": str(report_path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "active_score_run_id": active_score_run_id,
        "candidate_score_run_id": candidate_score_run_id,
        "batch_size": len(batches[0]["candidates"]) if batches else 0,
        "batches": batches,
        "instructions": {
            "valid_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "major_fix",
                "semantic_error",
                "residual_spanish",
                "structure_error",
                "token_mismatch",
                "rejected_suggestion",
                "rejected",
            ],
            "recommended_labels": [
                "correct",
                "contextual_exception",
                "residual_spanish",
                "semantic_error",
                "minor_fix",
                "rejected_suggestion",
            ],
            "label_guidance": {
                "correct": "Use somente se preservar o texto espanhol/old/output e seguro para este titulo no jogo.",
                "contextual_exception": "Use para topônimo/adjetivo aceito como exceção especifica, sem generalizar regra ampla.",
                "residual_spanish": "Use quando o texto preservado parece espanhol residual que deveria ser PT-BR ou outra forma.",
                "semantic_error": "Use quando o texto preservado troca significado, cultura, direção ou forma do título.",
            },
            "do_not_run": ["ml-train-risk", "ml-score", "apply-output"],
        },
    }
    template_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_by_shape = Counter(row["key_shape"] for row in rows)
    selected_by_bucket = Counter(row["pattern_bucket"] for row in selected)
    lines = [
        "ML regression pattern queue",
        f"Started at: {now()}",
        f"Rule version: {RULE_VERSION}",
        f"Active score run id: {active_score_run_id}",
        f"Candidate score run id: {candidate_score_run_id}",
        "",
        "Pattern:",
        "- title_preserved_old_spanish_output",
        "- active model marked auto_safe, candidate macro demoted",
        "- candidate_text == spanish_text == old_text == output_text",
        "- token_status ok, issue_count 0, deterministic blocks 0",
        "- rows with learned negative labels are excluded",
        "",
        "Summary:",
        f"- candidates found: {len(rows)}",
        f"- selected for review: {len(selected)}",
        f"- decision template: {template_path}",
        "",
        "Candidates by key shape:",
        *[f"- {key}: {count}" for key, count in total_by_shape.most_common()],
        "",
        "Selected by bucket:",
        *[f"- {key}: {count}" for key, count in selected_by_bucket.most_common()],
        "",
        "Sample selected rows:",
    ]
    for row in selected[:30]:
        lines.append(
            "- {bucket} | prob={prob:.4f} | {path}::{key} | {text}".format(
                bucket=row["pattern_bucket"],
                prob=float(row.get("model_safe_probability") or 0.0),
                path=row["relative_path"],
                key=row["source_key"],
                text=row.get("candidate_text") or "",
            )
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This queue is for evidence gathering only; it does not update output or model scores.",
            "- A broad policy should not be promoted from this pattern until a stratified review shows low or zero residual-Spanish risk.",
            "- Known negative examples such as title residual Spanish are intentionally excluded from the queue but remain evidence against broad unguarded promotion.",
            "- Selection uses round-robin over probability/key-shape buckets to keep review evidence balanced.",
        ]
    )
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path, template_path


def main(
    active_score_run_id: int | None = None,
    candidate_score_run_id: int | None = None,
    limit: int = 120,
    per_bucket: int = 12,
    batch_size: int = 20,
    include_reviewed: bool = False,
) -> None:
    settings = db.load_settings()
    print("[ml_regression_pattern_queue] Starting regression pattern queue")
    print(f"[ml_regression_pattern_queue] Rule version: {RULE_VERSION}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        active_score_run_id = active_score_run_id or 336
        candidate_score_run_id = candidate_score_run_id or latest_score_run(conn, "risk_action_classifier")
        rows = fetch_candidates(conn, active_score_run_id, candidate_score_run_id, include_reviewed)
        selected = select_stratified(rows, limit=limit, per_bucket=per_bucket)
        first_lote = next_lote_number(conn)
    batches = build_batches(selected, first_lote=first_lote, batch_size=batch_size)
    report_path, template_path = write_outputs(
        settings,
        rows,
        selected,
        batches,
        active_score_run_id=active_score_run_id,
        candidate_score_run_id=candidate_score_run_id,
    )
    print(f"[ml_regression_pattern_queue] Active score run: {active_score_run_id}")
    print(f"[ml_regression_pattern_queue] Candidate score run: {candidate_score_run_id}")
    print(f"[ml_regression_pattern_queue] Candidates: {len(rows)}")
    print(f"[ml_regression_pattern_queue] Selected: {len(selected)}")
    print(f"[ml_regression_pattern_queue] Report: {report_path}")
    print(f"[ml_regression_pattern_queue] Decision template: {template_path}")
    print("[ml_regression_pattern_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build review queues from high-potential active/candidate score regressions.")
    parser.add_argument("--active-score-run-id", type=int, default=None)
    parser.add_argument("--candidate-score-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-bucket", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--include-reviewed", action="store_true")
    args = parser.parse_args()
    main(
        active_score_run_id=args.active_score_run_id,
        candidate_score_run_id=args.candidate_score_run_id,
        limit=args.limit,
        per_bucket=args.per_bucket,
        batch_size=args.batch_size,
        include_reviewed=args.include_reviewed,
    )
