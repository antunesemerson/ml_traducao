from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

import db
import quality_pairwise_calibration_review_queue as review_queue


RULE_VERSION = "quality_calibration_policy_v1"
VALID_DECISIONS = {"skip", "sample", "required"}


@dataclass(frozen=True)
class CalibrationPolicyConfig:
    minimum_provider_decisions: int = 50
    minimum_provider_epochs: int = 3
    minimum_provider_accuracy: float = 0.99
    large_batch_minimum: int = 250
    large_batch_ratio: float = 0.001
    low_confidence_threshold: float = 0.5
    low_confidence_required_ratio: float = 0.10
    low_confidence_required_minimum: int = 5
    minimum_control_accuracy: float = 0.99
    periodic_sample_epochs: int = 3
    required_control_count: int = 8
    sample_control_count: int = 4
    tie_epsilon: float = review_queue.DEFAULT_TIE_EPSILON


REASON_LABELS = {
    "no_scored_epoch": "Nenhuma epoch pontuada esta disponivel.",
    "no_applied_pairwise_candidates": "Nenhum ajuste pairwise aplicado precisa de calibracao.",
    "invalid_integrity": "Existem ajustes aplicados com falha de integridade ou validacao.",
    "raw_non_improving": "Existem ajustes cujo score bruto regrediu ou permaneceu igual.",
    "score_contract_changed": "O contrato de score mudou desde a epoch comparavel anterior.",
    "large_batch": "O lote aplicado ultrapassou o limite de alto volume.",
    "immature_provider": "Um ou mais provedores ainda nao possuem historico suficiente.",
    "control_accuracy_below_threshold": "A acuracia recente dos controles caiu abaixo do limite.",
    "low_confidence_concentration": "O lote concentra muitos candidatos com score bruto baixo.",
    "low_confidence_sample": "Uma amostra e necessaria por conter candidatos de baixa confianca.",
    "control_health_not_measured": "A saude recente dos controles ainda nao foi medida.",
    "periodic_sample_due": "A amostra periodica de calibracao esta vencida.",
    "existing_pending_review": "A epoch atual ja possui uma fila de calibracao pendente.",
    "already_calibrated_current_epoch": "A calibracao da epoch atual ja foi concluida.",
    "stable_mature_small_batch": "Lote pequeno de provedores maduros, sem regressao, falha ou drift.",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
    )


def _provider_maturity(
    conn: sqlite3.Connection,
    evidence_types: list[str],
    config: CalibrationPolicyConfig,
) -> list[dict[str, Any]]:
    if not evidence_types:
        return []
    history: dict[str, dict[str, int]] = {}
    if (
        _table_exists(conn, "ml_pairwise_calibration_review_items")
        and _table_exists(conn, "ml_pairwise_calibration_review_runs")
        and _table_exists(conn, "ml_pairwise_quality_evidence")
    ):
        placeholders = ",".join("?" for _ in evidence_types)
        rows = conn.execute(
            f"""
            SELECT evidence.evidence_type,
                   COUNT(*) AS decision_count,
                   COUNT(DISTINCT runs.quality_epoch_id) AS epoch_count,
                   SUM(CASE WHEN items.reviewer_label = items.expected_preference THEN 1 ELSE 0 END) AS validated_count
            FROM ml_pairwise_calibration_review_items items
            JOIN ml_pairwise_calibration_review_runs runs
              ON runs.id = items.run_id
            JOIN ml_pairwise_quality_evidence evidence
              ON evidence.id = items.evidence_id
            WHERE evidence.evidence_type IN ({placeholders})
              AND items.review_status = 'decided'
            GROUP BY evidence.evidence_type
            """,
            tuple(evidence_types),
        ).fetchall()
        history = {
            str(row["evidence_type"]): {
                "decision_count": int(row["decision_count"] or 0),
                "epoch_count": int(row["epoch_count"] or 0),
                "validated_count": int(row["validated_count"] or 0),
            }
            for row in rows
        }
    result: list[dict[str, Any]] = []
    for evidence_type in evidence_types:
        item = history.get(
            evidence_type,
            {"decision_count": 0, "epoch_count": 0, "validated_count": 0},
        )
        validation_accuracy = (
            round(item["validated_count"] / item["decision_count"], 6)
            if item["decision_count"]
            else None
        )
        result.append(
            {
                "evidence_type": evidence_type,
                **item,
                "validation_accuracy": validation_accuracy,
                "mature": (
                    item["decision_count"] >= config.minimum_provider_decisions
                    and item["epoch_count"] >= config.minimum_provider_epochs
                    and validation_accuracy is not None
                    and validation_accuracy >= config.minimum_provider_accuracy
                ),
            }
        )
    return result


def collect_policy_metrics(
    conn: sqlite3.Connection,
    config: CalibrationPolicyConfig = CalibrationPolicyConfig(),
) -> dict[str, Any]:
    try:
        epoch = review_queue.latest_scored_epoch(conn)
    except RuntimeError:
        return {
            "quality_epoch_id": None,
            "old_score_run_id": None,
            "output_score_run_id": None,
            "candidate_count": 0,
            "population_count": 0,
            "invalid_integrity_count": 0,
            "non_improving_count": 0,
            "regressed_count": 0,
            "equal_count": 0,
            "low_confidence_count": 0,
            "active_segment_count": 0,
            "large_batch_threshold": config.large_batch_minimum,
            "batch_ratio": 0.0,
            "score_contract_changed": False,
            "provider_maturity": [],
            "immature_provider_ids": [],
            "last_control_accuracy": None,
            "scored_epochs_since_calibration": None,
            "existing_pending_count": 0,
            "current_epoch_calibrated": False,
        }

    epoch_id = int(epoch["id"])
    old_score_run_id = int(epoch["old_score_run_id"])
    output_score_run_id = int(epoch["output_score_run_id"])
    population = review_queue.load_applied_pairwise_population(
        conn,
        old_score_run_id=old_score_run_id,
        output_score_run_id=output_score_run_id,
    )
    cohort = [
        item
        for item in population
        if int(item.get("token_integrity_ok") or 0) == 1
        and int(item.get("post_validation_clean") or 0) == 1
    ]
    deltas = [
        float(item["candidate_score_raw"]) - float(item["baseline_score_raw"])
        for item in cohort
    ]
    evidence_types = sorted(
        {str(item.get("evidence_type") or "") for item in population if item.get("evidence_type")}
    )
    maturity = _provider_maturity(conn, evidence_types, config)
    active_segment_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM source_segments WHERE is_active = 1"
        ).fetchone()[0]
    )
    large_batch_threshold = max(
        config.large_batch_minimum,
        int(math.ceil(active_segment_count * config.large_batch_ratio)),
    )

    previous_epoch = conn.execute(
        """
        SELECT id, scoring_contract_hash
        FROM quality_epochs
        WHERE id < ?
          AND old_score_run_id IS NOT NULL
          AND output_score_run_id IS NOT NULL
          AND status IN ('scored', 'evaluated', 'published')
        ORDER BY id DESC
        LIMIT 1
        """,
        (epoch_id,),
    ).fetchone()
    current_contract = epoch.get("scoring_contract_hash")
    previous_contract = previous_epoch["scoring_contract_hash"] if previous_epoch else None

    previous_calibration = (
        conn.execute(
            """
            SELECT quality_epoch_id, control_accuracy
            FROM ml_pairwise_calibration_review_runs
            WHERE quality_epoch_id < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (epoch_id,),
        ).fetchone()
        if _table_exists(conn, "ml_pairwise_calibration_review_runs")
        else None
    )
    scored_epochs_since_calibration = None
    if previous_calibration:
        scored_epochs_since_calibration = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM quality_epochs
                WHERE id > ? AND id <= ?
                  AND old_score_run_id IS NOT NULL
                  AND output_score_run_id IS NOT NULL
                  AND status IN ('scored', 'evaluated', 'published')
                """,
                (int(previous_calibration["quality_epoch_id"]), epoch_id),
            ).fetchone()[0]
        )
    current_review = (
        conn.execute(
            """
            SELECT pending_count, decided_count, control_accuracy
            FROM ml_pairwise_calibration_review_runs
            WHERE quality_epoch_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (epoch_id,),
        ).fetchone()
        if _table_exists(conn, "ml_pairwise_calibration_review_runs")
        else None
    )

    candidate_count = len(cohort)
    return {
        "quality_epoch_id": epoch_id,
        "old_score_run_id": old_score_run_id,
        "output_score_run_id": output_score_run_id,
        "candidate_count": candidate_count,
        "population_count": len(population),
        "invalid_integrity_count": len(population) - candidate_count,
        "non_improving_count": sum(delta <= config.tie_epsilon for delta in deltas),
        "regressed_count": sum(delta < -config.tie_epsilon for delta in deltas),
        "equal_count": sum(abs(delta) <= config.tie_epsilon for delta in deltas),
        "low_confidence_count": sum(
            float(item["candidate_score_raw"]) < config.low_confidence_threshold
            for item in cohort
        ),
        "active_segment_count": active_segment_count,
        "large_batch_threshold": large_batch_threshold,
        "batch_ratio": round(candidate_count / active_segment_count, 9)
        if active_segment_count
        else 0.0,
        "scoring_contract_hash": current_contract,
        "previous_scoring_contract_hash": previous_contract,
        "score_contract_changed": bool(
            previous_contract and current_contract and previous_contract != current_contract
        ),
        "provider_maturity": maturity,
        "immature_provider_ids": [
            item["evidence_type"] for item in maturity if not item["mature"]
        ],
        "last_control_accuracy": (
            float(previous_calibration["control_accuracy"])
            if previous_calibration and previous_calibration["control_accuracy"] is not None
            else None
        ),
        "last_calibration_epoch_id": (
            int(previous_calibration["quality_epoch_id"])
            if previous_calibration
            else None
        ),
        "scored_epochs_since_calibration": scored_epochs_since_calibration,
        "existing_pending_count": int(current_review["pending_count"] or 0)
        if current_review
        else 0,
        "current_epoch_calibrated": bool(
            current_review
            and int(current_review["pending_count"] or 0) == 0
            and int(current_review["decided_count"] or 0) > 0
        ),
        "current_epoch_control_accuracy": (
            float(current_review["control_accuracy"])
            if current_review and current_review["control_accuracy"] is not None
            else None
        ),
    }


def decide_calibration(
    metrics: dict[str, Any],
    config: CalibrationPolicyConfig = CalibrationPolicyConfig(),
) -> dict[str, Any]:
    if metrics.get("quality_epoch_id") is None:
        reasons = ["no_scored_epoch"]
        decision = "skip"
    elif int(metrics.get("population_count") or 0) == 0:
        reasons = ["no_applied_pairwise_candidates"]
        decision = "skip"
    elif metrics.get("current_epoch_calibrated"):
        reasons = ["already_calibrated_current_epoch"]
        decision = "skip"
    else:
        required_reasons: list[str] = []
        candidate_count = int(metrics.get("candidate_count") or 0)
        if int(metrics.get("existing_pending_count") or 0) > 0:
            required_reasons.append("existing_pending_review")
        if int(metrics.get("invalid_integrity_count") or 0) > 0:
            required_reasons.append("invalid_integrity")
        if int(metrics.get("non_improving_count") or 0) > 0:
            required_reasons.append("raw_non_improving")
        if metrics.get("score_contract_changed"):
            required_reasons.append("score_contract_changed")
        if candidate_count >= int(metrics.get("large_batch_threshold") or config.large_batch_minimum):
            required_reasons.append("large_batch")
        if metrics.get("immature_provider_ids"):
            required_reasons.append("immature_provider")
        last_control_accuracy = metrics.get("last_control_accuracy")
        if (
            last_control_accuracy is not None
            and float(last_control_accuracy) < config.minimum_control_accuracy
        ):
            required_reasons.append("control_accuracy_below_threshold")
        low_confidence_count = int(metrics.get("low_confidence_count") or 0)
        low_confidence_required = max(
            config.low_confidence_required_minimum,
            int(math.ceil(candidate_count * config.low_confidence_required_ratio)),
        )
        if low_confidence_count >= low_confidence_required:
            required_reasons.append("low_confidence_concentration")

        if required_reasons:
            reasons = required_reasons
            decision = "required"
        else:
            sample_reasons: list[str] = []
            if low_confidence_count > 0:
                sample_reasons.append("low_confidence_sample")
            if last_control_accuracy is None:
                sample_reasons.append("control_health_not_measured")
            epochs_since = metrics.get("scored_epochs_since_calibration")
            if epochs_since is not None and int(epochs_since) >= config.periodic_sample_epochs:
                sample_reasons.append("periodic_sample_due")
            if sample_reasons:
                reasons = sample_reasons
                decision = "sample"
            else:
                reasons = ["stable_mature_small_batch"]
                decision = "skip"

    candidate_count = int(metrics.get("candidate_count") or 0)
    if decision == "required":
        control_count = min(
            config.required_control_count,
            max(1, int(math.ceil(candidate_count / 2))),
        ) if candidate_count else 0
    elif decision == "sample":
        control_count = min(
            config.sample_control_count,
            max(1, int(math.ceil(candidate_count / 2))),
        ) if candidate_count else 0
    else:
        control_count = 0
    return {
        "decision": decision,
        "reasons": reasons,
        "reason_labels": [REASON_LABELS[reason] for reason in reasons],
        "reason_summary": " ".join(REASON_LABELS[reason] for reason in reasons),
        "recommended_control_count": control_count,
    }


def evaluate_policy(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    config: CalibrationPolicyConfig = CalibrationPolicyConfig(),
) -> dict[str, Any]:
    metrics = collect_policy_metrics(conn, config)
    decision = decide_calibration(metrics, config)
    decision_key = hashlib.sha256(
        json.dumps(
            {
                "rule_version": RULE_VERSION,
                "metrics": metrics,
                "decision": decision,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "apply": apply,
        "decision_key": decision_key,
        **decision,
        "metrics": metrics,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "score_write_count": 0,
    }
    if not apply or metrics.get("quality_epoch_id") is None:
        return payload

    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO ml_pairwise_calibration_policy_decisions (
          decision_key, rule_version, quality_epoch_id, old_score_run_id,
          output_score_run_id, decision, recommended_control_count,
          candidate_count, non_improving_count, invalid_integrity_count,
          active_segment_count, large_batch_threshold, batch_ratio,
          reasons_json, metrics_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_key) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (
            decision_key,
            RULE_VERSION,
            metrics.get("quality_epoch_id"),
            metrics.get("old_score_run_id"),
            metrics.get("output_score_run_id"),
            decision["decision"],
            decision["recommended_control_count"],
            metrics.get("candidate_count") or 0,
            metrics.get("non_improving_count") or 0,
            metrics.get("invalid_integrity_count") or 0,
            metrics.get("active_segment_count") or 0,
            metrics.get("large_batch_threshold") or 0,
            metrics.get("batch_ratio") or 0,
            json.dumps(decision["reasons"], ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, created_at, updated_at FROM ml_pairwise_calibration_policy_decisions WHERE decision_key = ?",
        (decision_key,),
    ).fetchone()
    return {
        **payload,
        "policy_decision_id": int(row["id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decide whether pairwise calibration should be skipped, sampled or required."
    )
    parser.add_argument("--apply", action="store_true", help="Persist the auditable policy decision.")
    args = parser.parse_args()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        if args.apply:
            db.ensure_database(conn)
        payload = evaluate_policy(conn, apply=args.apply)
    print(f"[calibration_policy] Decision: {payload['decision']}")
    print(f"[calibration_policy] Controls: {payload['recommended_control_count']}")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
