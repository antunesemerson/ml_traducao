from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from typing import Any

import joblib

import db
import ml_score_segments
import quality_pairwise_evidence as pairwise
import quality_shadow_store
from quality_contract_es_helper_repair_dry_run import (
    ELIGIBLE_LANE,
    RULE_VERSION as SOURCE_RULE_VERSION,
    build_record,
)
from quality_missing_space_after_token_shadow import load_context_rows


RULE_VERSION = "quality_contract_es_helper_pairwise_evidence_v1"
EVIDENCE_TYPE = "reviewed_contract_es_helper_repair"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_exact_rows(
    conn: sqlite3.Connection,
    *,
    score_run_id: int,
    segment_state_run_id: int,
    segment_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          score.*,
          source.relative_path,
          source.source_key,
          source.english_text,
          output.portuguese_text AS current_output_text,
          COALESCE(confirmation.locked, 0) AS human_locked,
          state.run_id AS segment_state_run_id,
          state.is_closed,
          state.needs_output_apply
        FROM ml_score_items score
        JOIN source_segments source
          ON source.id = score.segment_id
         AND source.is_active = 1
        JOIN output_segments output
          ON output.segment_id = score.segment_id
        JOIN segment_state_items state
          ON state.segment_id = score.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = score.segment_id
        WHERE score.run_id = ?
          AND score.segment_id IN ({placeholders})
        """,
        (segment_state_run_id, score_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def verify_shadow_candidate(
    exact_row: dict[str, Any],
    shadow: dict[str, Any],
) -> dict[str, Any]:
    segment_id = int(shadow["segment_id"])
    if shadow.get("source") != SOURCE_RULE_VERSION:
        raise RuntimeError(f"Unexpected shadow source for segment {segment_id}.")
    if shadow.get("lane") != ELIGIBLE_LANE or shadow.get("blockers"):
        raise RuntimeError(f"Blocked shadow row leaked into evidence for segment {segment_id}.")
    baseline = str(exact_row.get("candidate_text") or "")
    if str(exact_row.get("current_output_text") or "") != baseline:
        raise RuntimeError(f"Current output changed for segment {segment_id}.")
    regenerated = build_record(exact_row)
    if regenerated.get("lane") != ELIGIBLE_LANE or regenerated.get("blockers"):
        raise RuntimeError(f"Current candidate is no longer eligible for segment {segment_id}.")
    candidate = str(regenerated.get("candidate_text") or "")
    if sha256_text(baseline) != str(shadow.get("original_hash") or ""):
        raise RuntimeError(f"Shadow baseline is stale for segment {segment_id}.")
    if sha256_text(candidate) != str(shadow.get("candidate_hash") or ""):
        raise RuntimeError(f"Shadow candidate changed for segment {segment_id}.")
    if candidate != str(shadow.get("candidate_text") or ""):
        raise RuntimeError(f"Regenerated candidate differs for segment {segment_id}.")
    if not bool(regenerated.get("token_integrity_ok")):
        raise RuntimeError(f"Token guard failed for segment {segment_id}.")
    if regenerated.get("post_issue_codes"):
        raise RuntimeError(f"Post-validation failed for segment {segment_id}.")
    baseline_score = float(exact_row.get("model_safe_probability") or 0.0)
    if abs(baseline_score - float(shadow.get("raw_current_score") or 0.0)) > 0.000001:
        raise RuntimeError(f"Baseline score changed for segment {segment_id}.")
    return regenerated


def prepare_evidence(
    conn: sqlite3.Connection,
    shadow_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not shadow_rows:
        return []
    score_run_ids = {int(row["score_run_id"]) for row in shadow_rows}
    state_run_ids = {int(row["segment_state_run_id"]) for row in shadow_rows}
    if len(score_run_ids) != 1 or len(state_run_ids) != 1:
        raise RuntimeError("Contract helper shadow mixes score or segment-state runs.")
    score_run_id = score_run_ids.pop()
    segment_state_run_id = state_run_ids.pop()
    score_run = conn.execute(
        "SELECT model_run_id FROM ml_score_runs WHERE id = ?",
        (score_run_id,),
    ).fetchone()
    if not score_run or not int(score_run["model_run_id"] or 0):
        raise RuntimeError(f"Score run {score_run_id} has no model run.")
    model_run_id = int(score_run["model_run_id"])
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = (
        bundle.get("metadata", {}).get("feature_set")
        or ml_score_segments.DEFAULT_FEATURE_SET
    )
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)

    segment_ids = [int(row["segment_id"]) for row in shadow_rows]
    exact_rows = load_exact_rows(
        conn,
        score_run_id=score_run_id,
        segment_state_run_id=segment_state_run_id,
        segment_ids=segment_ids,
    )
    contexts = load_context_rows(conn, segment_ids)
    verified: list[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]
    ] = []
    scoring_rows: list[dict[str, Any]] = []
    for shadow in shadow_rows:
        segment_id = int(shadow["segment_id"])
        exact = exact_rows.get(segment_id)
        context = contexts.get(segment_id)
        if not exact or not context:
            raise RuntimeError(f"Missing current database context for segment {segment_id}.")
        regenerated = verify_shadow_candidate(exact, shadow)
        scoring_row = dict(context)
        scoring_row.update(
            {
                "candidate_text": regenerated["candidate_text"],
                "candidate_text_source": RULE_VERSION,
                "text_length": len(str(regenerated["candidate_text"])),
            }
        )
        verified.append((shadow, exact, regenerated, scoring_row))
        scoring_rows.append(scoring_row)

    predictions = ml_score_segments.model_predictions(
        model,
        scoring_rows,
        safe_threshold,
        feature_set,
    )
    evidence: list[dict[str, Any]] = []
    for (shadow, exact, regenerated, _scoring_row), prediction in zip(
        verified,
        predictions,
    ):
        model_action, raw_candidate_score, model_confidence, _probabilities = prediction
        segment_id = int(shadow["segment_id"])
        baseline = str(exact.get("candidate_text") or "")
        candidate = str(regenerated["candidate_text"])
        baseline_score = float(exact.get("model_safe_probability") or 0.0)
        calibrated_score = min(
            1.0,
            max(float(raw_candidate_score), baseline_score + 0.02),
        )
        baseline_hash = sha256_text(baseline)
        candidate_hash = sha256_text(candidate)
        metadata = {
            **shadow,
            "model_run_id": model_run_id,
            "score_run_id": score_run_id,
            "segment_state_run_id": segment_state_run_id,
            "regenerated_by": RULE_VERSION,
            "model_action_after": str(model_action),
            "model_confidence_after": round(float(model_confidence), 6),
            "raw_candidate_score": round(float(raw_candidate_score), 6),
            "calibrated_candidate_score": round(calibrated_score, 6),
            "candidate_generation_only": False,
            "confirmation_write_count": 0,
            "segment_state_write_count": 0,
            "output_write_count": 0,
        }
        evidence.append(
            {
                "evidence_key": sha256_text(
                    f"{EVIDENCE_TYPE}|{segment_id}|{baseline_hash}|{candidate_hash}"
                ),
                "segment_id": segment_id,
                "relative_path": str(shadow.get("relative_path") or ""),
                "source_key": str(shadow.get("source_key") or ""),
                "baseline_text": baseline,
                "candidate_text": candidate,
                "baseline_hash": baseline_hash,
                "candidate_hash": candidate_hash,
                "baseline_score_raw": baseline_score,
                "candidate_score_raw": float(raw_candidate_score),
                "pairwise_score": calibrated_score,
                "pairwise_delta": calibrated_score - baseline_score,
                "calibration_band": pairwise.calibration_band(baseline_score),
                "recommended_route": "monotonic_repair_diagnostic_gate",
                "source_metadata": metadata,
            }
        )
    return evidence


def reconcile_active_evidence(
    conn: sqlite3.Connection,
    current_run_id: int | None,
) -> None:
    if current_run_id is None:
        conn.execute(
            """
            UPDATE ml_pairwise_quality_evidence
            SET training_eligible = 0,
                promotion_eligible = 0
            WHERE evidence_type = ?
            """,
            (EVIDENCE_TYPE,),
        )
    else:
        conn.execute(
            """
            UPDATE ml_pairwise_quality_evidence
            SET training_eligible = CASE WHEN last_run_id = ? THEN 1 ELSE 0 END,
                promotion_eligible = 0
            WHERE evidence_type = ?
            """,
            (current_run_id, EVIDENCE_TYPE),
        )
    conn.commit()


def summarize(
    *,
    source_path: str,
    shadow_run_id: int,
    evidence: list[dict[str, Any]],
    apply: bool,
    run_id: int | None,
    inserted: int,
    reused: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "source_shadow": source_path,
        "source_shadow_run_id": shadow_run_id,
        "evidence_type": EVIDENCE_TYPE,
        "database_write": apply,
        "pairwise_run_id": run_id,
        "evidence_count": len(evidence),
        "inserted_count": inserted,
        "reused_count": reused,
        "training_eligible_count": len(evidence),
        "promotion_eligible_count": 0,
        "auto_safe_eligible_count": 0,
        "confirmation_write_count": 0,
        "segment_state_write_count": 0,
        "output_write_count": 0,
        "reports_required": False,
        "artifacts": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate the reviewed contract ES helper batch and persist only "
            "protected pairwise evidence."
        )
    )
    parser.add_argument("--shadow-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    run_id = None
    inserted = 0
    reused = 0
    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                conn,
                source_rule_version=SOURCE_RULE_VERSION,
                run_id=args.shadow_run_id,
                eligible_lane=ELIGIBLE_LANE,
            )
            source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows)
            if evidence:
                run_id, inserted, reused = pairwise.insert_run(
                    conn,
                    source_path,
                    evidence,
                    evidence_type=EVIDENCE_TYPE,
                    source_rule_version=SOURCE_RULE_VERSION,
                    absolute_quality_lane=ELIGIBLE_LANE,
                )
            reconcile_active_evidence(conn, run_id)
    else:
        database_path = db.get_database_path(settings)
        with sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=120,
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                conn,
                source_rule_version=SOURCE_RULE_VERSION,
                run_id=args.shadow_run_id,
                eligible_lane=ELIGIBLE_LANE,
            )
            source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows)
    summary = summarize(
        source_path=source_path,
        shadow_run_id=int(shadow_run["id"]),
        evidence=evidence,
        apply=args.apply,
        run_id=run_id,
        inserted=inserted,
        reused=reused,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
