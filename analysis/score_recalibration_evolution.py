"""Read-only evidence extract for the operational score recalibration review."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "memory" / "translation_engine.sqlite"
OUTPUT_PATH = ROOT / "reports" / "score_recalibration_evolution_2026-08-27.json"


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def as_json(value: object, fallback: object) -> object:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def main() -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        candidates = [dict(row) for row in con.execute(
            """
            SELECT id, rule_version, shadow_run_id, model_run_id, score_run_id,
                   status, item_count, raw_mean_probability,
                   current_mean_probability, candidate_mean_probability,
                   improved_count, degraded_count, unchanged_count,
                   band_change_count, required_review_count, gate_json,
                   summary_json, created_at, updated_at
            FROM ml_score_calibration_candidate_runs
            ORDER BY id
            """
        )]

        candidate_rows: list[dict[str, object]] = []
        reviewed_hashes_by_candidate: dict[int, set[str]] = {}
        for candidate in candidates:
            run_id = int(candidate["id"])
            items = [dict(row) for row in con.execute(
                """
                SELECT text_hash, delta, current_probability, candidate_probability,
                       review_required, complexity, family
                FROM ml_score_calibration_candidate_items
                WHERE run_id = ?
                """,
                (run_id,),
            )]
            reviews = [dict(row) for row in con.execute(
                """
                SELECT rv.text_hash, rv.decision, ci.delta, ci.complexity, ci.family
                FROM ml_score_calibration_discrepancy_reviews rv
                LEFT JOIN ml_score_calibration_candidate_items ci
                  ON ci.run_id = rv.candidate_run_id
                 AND ci.segment_id = rv.segment_id
                 AND ci.text_hash = rv.text_hash
                WHERE rv.candidate_run_id = ?
                """,
                (run_id,),
            )]
            reviewed_hashes_by_candidate[run_id] = {str(row["text_hash"]) for row in reviews}
            abs_deltas = [abs(float(row["delta"] or 0)) for row in items]
            signed_deltas = [float(row["delta"] or 0) for row in items]
            decisions = Counter(str(row["decision"]) for row in reviews)
            reviewed_deltas = [abs(float(row["delta"] or 0)) for row in reviews]
            candidate_rows.append({
                "candidate_id": run_id,
                "status": candidate["status"],
                "shadow_run_id": candidate["shadow_run_id"],
                "score_run_id": candidate["score_run_id"],
                "created_at": candidate["created_at"],
                "item_count": candidate["item_count"],
                "current_mean_probability": candidate["current_mean_probability"],
                "candidate_mean_probability": candidate["candidate_mean_probability"],
                "mean_delta": (
                    float(candidate["candidate_mean_probability"] or 0)
                    - float(candidate["current_mean_probability"] or 0)
                ),
                "improved_count": candidate["improved_count"],
                "degraded_count": candidate["degraded_count"],
                "unchanged_count": candidate["unchanged_count"],
                "band_change_count": candidate["band_change_count"],
                "required_review_count": candidate["required_review_count"],
                "reviewed_count": len(reviews),
                "decision_counts": dict(decisions),
                "coherent_count": decisions.get("coherent_change", 0),
                "actionable_feedback_count": sum(
                    decisions.get(key, 0)
                    for key in (
                        "correct_direction_excessive",
                        "correct_direction_insufficient",
                        "incorrect_direction",
                        "incorrect_change",
                    )
                ),
                "mean_absolute_delta": sum(abs_deltas) / len(abs_deltas) if abs_deltas else None,
                "p95_absolute_delta": percentile(abs_deltas, 0.95),
                "p99_absolute_delta": percentile(abs_deltas, 0.99),
                "max_absolute_delta": max(abs_deltas) if abs_deltas else None,
                "share_abs_delta_over_20pp": (
                    sum(value >= 0.20 for value in abs_deltas) / len(abs_deltas)
                    if abs_deltas else None
                ),
                "share_abs_delta_over_50pp": (
                    sum(value >= 0.50 for value in abs_deltas) / len(abs_deltas)
                    if abs_deltas else None
                ),
                "review_sample_mean_absolute_delta": (
                    sum(reviewed_deltas) / len(reviewed_deltas) if reviewed_deltas else None
                ),
                "positive_delta_share": (
                    sum(value > 0 for value in signed_deltas) / len(signed_deltas)
                    if signed_deltas else None
                ),
                "gate": as_json(candidate["gate_json"], {}),
            })

        overlaps: list[dict[str, object]] = []
        candidate_ids = sorted(reviewed_hashes_by_candidate)
        for left_index, left_id in enumerate(candidate_ids):
            for right_id in candidate_ids[left_index + 1:]:
                overlap = reviewed_hashes_by_candidate[left_id] & reviewed_hashes_by_candidate[right_id]
                overlaps.append({
                    "left_candidate_id": left_id,
                    "right_candidate_id": right_id,
                    "reviewed_text_hash_overlap": len(overlap),
                })

        shadows = []
        for row in con.execute(
            """
            SELECT id, rule_version, score_run_id, observation_count, fold_count,
                   current_ece, candidate_ece, current_brier, candidate_brier,
                   current_false_safe_count, candidate_false_safe_count,
                   gate_passed, status, review_watermark, metrics_json, model_json,
                   created_at
            FROM ml_score_calibration_shadow_runs
            ORDER BY id
            """
        ):
            item = dict(row)
            model = as_json(item.pop("model_json"), {})
            metrics = as_json(item.pop("metrics_json"), {})
            feedback = model.get("discrepancy_feedback", {}) if isinstance(model, dict) else {}
            item["feedback_parent_candidate_run_id"] = feedback.get("parent_candidate_run_id")
            item["feedback_candidate_run_ids"] = feedback.get("candidate_run_ids", [])
            item["feedback_review_count"] = feedback.get("review_count", 0)
            item["feedback_incorrect_count"] = feedback.get("incorrect_count", 0)
            item["feedback_by_direction"] = feedback.get("by_direction", {})
            if isinstance(metrics, dict):
                item["metrics_summary"] = {
                    key: metrics.get(key)
                    for key in (
                        "positive_count",
                        "negative_count",
                        "positive_rate",
                        "source_counts",
                        "probability_cap",
                        "safe_threshold",
                        "current_safe_count",
                        "candidate_safe_count",
                    )
                    if key in metrics
                }
            shadows.append(item)

        registry = [dict(row) for row in con.execute(
            "SELECT * FROM ml_score_calibration_registry ORDER BY registry_key"
        )]
        promotion_events = [dict(row) for row in con.execute(
            "SELECT * FROM ml_score_calibration_promotion_events ORDER BY id"
        )]
        evidence = {
            "as_of": "2026-08-27 America/Sao_Paulo",
            "database": "memory/translation_engine.sqlite",
            "candidate_runs": candidate_rows,
            "review_sample_overlaps": overlaps,
            "unique_reviewed_text_hashes": len(set().union(*reviewed_hashes_by_candidate.values())) if reviewed_hashes_by_candidate else 0,
            "total_review_decisions": sum(len(values) for values in reviewed_hashes_by_candidate.values()),
            "shadow_runs": shadows,
            "registry": registry,
            "promotion_events": promotion_events,
        }
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(OUTPUT_PATH),
                    "candidate_runs": len(candidate_rows),
                    "review_decisions": evidence["total_review_decisions"],
                    "unique_reviewed_text_hashes": evidence["unique_reviewed_text_hashes"],
                    "promotion_events": len(promotion_events),
                },
                ensure_ascii=False,
            )
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
