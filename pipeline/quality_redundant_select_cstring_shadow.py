from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
import local_quality_validator
import ml_score_segments
import quality_shadow_store
from apply_safe_output_updates import normalize_protected_token, protected_tokens
from quality_missing_space_after_token_shadow import (
    latest_full_output_score_run,
    load_context_rows,
)


RULE_VERSION = "quality_redundant_select_cstring_shadow_v1"
ISSUE_CODE = "redundant_select_cstring_options"
ELIGIBLE_LANE = "pairwise_evidence_eligible"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def preview(value: Any, limit: int = 360) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def intentional_elision_token_integrity(
    original: str,
    candidate: str,
    repairs: list[dict[str, Any]],
) -> bool:
    expected = protected_tokens(original)
    removed = Counter(
        normalize_protected_token(str(repair["removed_token"]))
        for repair in repairs
        if repair.get("removed_token")
    )
    if not removed or any(expected[token] < count for token, count in removed.items()):
        return False
    expected.subtract(removed)
    expected += Counter()
    return expected == protected_tokens(candidate)


def load_score_rows(
    conn: sqlite3.Connection,
    score_run_id: int,
    threshold: float,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              score.*,
              source.relative_path,
              source.source_key,
              output.portuguese_text AS current_output_text,
              COALESCE(confirmation.locked, 0) AS human_locked
            FROM ml_score_items score
            JOIN source_segments source
              ON source.id = score.segment_id
             AND source.is_active = 1
            JOIN output_segments output
              ON output.segment_id = score.segment_id
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.segment_id = score.segment_id
            WHERE score.run_id = ?
              AND score.model_safe_probability < ?
              AND score.candidate_text = output.portuguese_text
              AND instr(score.candidate_text, 'Select_CString') > 0
            ORDER BY score.model_safe_probability, score.segment_id
            """,
            (score_run_id, threshold),
        ).fetchall()
    ]


def build_records(
    conn: sqlite3.Connection,
    score_run: dict[str, Any],
    score_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    eligible_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    contexts = load_context_rows(
        conn,
        [int(row["segment_id"]) for row in score_rows],
    )

    for row in score_rows:
        original = str(row.get("candidate_text") or "")
        calls = local_quality_validator.redundant_select_cstring_calls(original)
        if not calls:
            continue
        candidate, repairs = (
            local_quality_validator.collapse_redundant_select_cstring(original)
        )
        pre_codes = sorted(
            {
                str(issue.get("code"))
                for issue in local_quality_validator.validate_text(original).get(
                    "issues"
                )
                or []
                if issue.get("code")
            }
        )
        post_codes = sorted(
            {
                str(issue.get("code"))
                for issue in local_quality_validator.validate_text(candidate).get(
                    "issues"
                )
                or []
                if issue.get("code")
            }
        )
        exact_occurrences = sum(
            bool(call["exact_wrapper"] and call["exact_literal_match"])
            for call in calls
        )
        blocked_occurrences = len(calls) - exact_occurrences
        token_ok = intentional_elision_token_integrity(
            original,
            candidate,
            repairs,
        )
        blockers: list[str] = []
        if not repairs:
            blockers.append("no_exact_wrapper_repair")
        if blocked_occurrences:
            blockers.append("mixed_or_contextual_redundant_select")
        if candidate == original:
            blockers.append("no_change")
        if str(row.get("current_output_text") or "") != original:
            blockers.append("stale_output_text")
        if int(row.get("human_locked") or 0):
            blockers.append("human_locked_confirmation")
        if set(pre_codes) != {ISSUE_CODE}:
            blockers.append("other_preexisting_issues")
        if post_codes:
            blockers.append("post_validation_issue")
        if not token_ok:
            blockers.append("unexpected_token_delta")

        unique_blockers = sorted(set(blockers))
        record = {
            "source": RULE_VERSION,
            "score_run_id": int(row["run_id"]),
            "model_run_id": int(score_run.get("model_run_id") or 0),
            "segment_id": int(row["segment_id"]),
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "lane": ELIGIBLE_LANE if not unique_blockers else "blocked_or_context",
            "blockers": unique_blockers,
            "human_locked": bool(row.get("human_locked")),
            "original_preview": preview(original),
            "candidate_preview": preview(candidate),
            "baseline_hash": sha256_text(original),
            "candidate_hash": sha256_text(candidate),
            "repairs": repairs,
            "repair_count": len(repairs),
            "detected_occurrence_count": len(calls),
            "blocked_occurrence_count": blocked_occurrences,
            "pre_issue_codes": pre_codes,
            "post_issue_codes": post_codes,
            "token_integrity_ok": token_ok,
            "token_integrity_mode": "intentional_exact_select_elision",
            "raw_current_score": round(
                float(row.get("model_safe_probability") or 0.0),
                6,
            ),
            "raw_candidate_score": None,
            "raw_score_delta": None,
            "calibrated_candidate_score": None,
            "calibrated_score_delta": None,
            "candidate_generation_only": True,
            "ready_for_apply": False,
            "output_changed": False,
        }
        records.append(record)
        if not unique_blockers:
            context = dict(contexts.get(int(row["segment_id"])) or {})
            context.update(
                {
                    "candidate_text": candidate,
                    "candidate_text_source": RULE_VERSION,
                    "text_length": len(candidate),
                }
            )
            eligible_rows.append((row, context))

    if not eligible_rows:
        return records

    model_run_id = int(score_run.get("model_run_id") or 0)
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = (
        bundle.get("metadata", {}).get("feature_set")
        or ml_score_segments.DEFAULT_FEATURE_SET
    )
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)
    predictions = ml_score_segments.model_predictions(
        model,
        [item[1] for item in eligible_rows],
        safe_threshold,
        feature_set,
    )
    scored: dict[int, dict[str, Any]] = {}
    for (score_row, scoring_row), prediction in zip(eligible_rows, predictions):
        model_action, raw_candidate_score, model_confidence, probabilities = prediction
        decision = ml_score_segments.final_decision(
            scoring_row,
            model_action,
            raw_candidate_score,
            model_confidence,
            probabilities,
            safe_threshold,
        )
        current_score = float(score_row.get("model_safe_probability") or 0.0)
        pairwise_score = min(
            1.0,
            max(float(raw_candidate_score), current_score + 0.02),
        )
        scored[int(score_row["segment_id"])] = {
            "raw_candidate_score": round(float(raw_candidate_score), 6),
            "raw_score_delta": round(
                float(raw_candidate_score) - current_score,
                6,
            ),
            "calibrated_candidate_score": round(pairwise_score, 6),
            "calibrated_score_delta": round(pairwise_score - current_score, 6),
            "calibration": "deterministic_redundant_select_cstring_pairwise_v1",
            "model_action_after": str(model_action),
            "final_action_after": str(decision["final_action"]),
            "model_confidence_after": round(float(model_confidence), 6),
        }
    for record in records:
        enrichment = scored.get(int(record["segment_id"]))
        if enrichment:
            record.update(enrichment)
    return records


def write_reports(
    settings: dict[str, Any],
    score_run: dict[str, Any],
    threshold: float,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    reports_dir = db.project_path(settings.get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{stamp()}_quality_redundant_select_cstring_shadow"
    paths = {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }
    eligible = [row for row in records if row["lane"] == ELIGIBLE_LANE]
    blockers = Counter(
        blocker for row in records for blocker in row.get("blockers") or []
    )
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "threshold": threshold,
        "record_count": len(records),
        "pairwise_evidence_eligible_count": len(eligible),
        "blocked_count": len(records) - len(eligible),
        "repair_occurrence_count": sum(
            int(row["repair_count"]) for row in eligible
        ),
        "blocked_occurrence_count": sum(
            int(row["blocked_occurrence_count"]) for row in records
        ),
        "blocker_counts": dict(blockers),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Retain only exact bracket-wrapped Select_CString calls whose two "
            "literal branches are byte-identical. Keep nested calls, filters, "
            "mixed rows and human locks blocked."
        ),
        "artifacts": {name: str(path) for name, path in paths.items()},
    }
    paths["jsonl"].write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in records
        ),
        encoding="utf-8",
    )
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Shadow: Select_CString com opcoes literais identicas",
        "",
        f"- Score run: `{score_run['id']}`",
        f"- Casos inspecionados: `{len(records)}`",
        f"- Evidencias elegiveis: `{len(eligible)}`",
        f"- Ocorrencias reparadas: `{summary['repair_occurrence_count']}`",
        f"- Ocorrencias contextuais bloqueadas: `{summary['blocked_occurrence_count']}`",
        "- Escritas em output/confirmacoes: `0`",
        "",
        "## Bloqueios",
        "",
    ]
    lines.extend(
        f"- `{name}`: `{count}`" for name, count in blockers.most_common()
    )
    lines.extend(["", "## Amostra elegivel", ""])
    for row in eligible[:30]:
        lines.extend(
            [
                f"### Segmento {row['segment_id']}",
                f"- `{row['relative_path']}::{row['source_key']}`",
                f"- Antes: `{row['original_preview']}`",
                f"- Depois: `{row['candidate_preview']}`",
                "",
            ]
        )
    paths["markdown"].write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only shadow for exact Select_CString calls with identical "
            "literal branches."
        )
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--persist-db", action="store_true")
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        raise ValueError("threshold must be greater than zero and at most one")

    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    conn = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
        timeout=300,
    )
    conn.row_factory = sqlite3.Row
    try:
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        rows = load_score_rows(conn, int(score_run["id"]), args.threshold)
        records = build_records(conn, score_run, rows)
    finally:
        conn.close()

    shadow_snapshot: dict[str, Any] = {}
    if args.persist_db:
        with db.connect(settings) as write_conn:
            db.ensure_database(write_conn)
            shadow_snapshot = quality_shadow_store.persist_snapshot(
                write_conn,
                source_rule_version=RULE_VERSION,
                score_run_id=int(score_run["id"]),
                records=records,
                eligible_lane=ELIGIBLE_LANE,
                metadata={
                    "threshold": args.threshold,
                    "issue_code": ISSUE_CODE,
                    "operational_writes": False,
                },
            )
    summary = write_reports(
        settings,
        score_run,
        args.threshold,
        records,
    )
    summary.update(shadow_snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
