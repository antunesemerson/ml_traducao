from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from typing import Any, Iterable

import db


RULE_VERSION = "quality_pairwise_calibration_consumer_v1"
DEFAULT_MINIMUM_CONTROL_ACCURACY = 0.80
CONTROL_LANES = {"positive_control", "negative_control"}
DIRECTIONAL_LABELS = {"candidate_preferred", "baseline_preferred"}
VALID_LABELS = DIRECTIONAL_LABELS | {"equivalent", "invalid_pair"}


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_pair_key(
    *,
    segment_id: int,
    evidence_type: str,
    baseline_text: str,
    candidate_text: str,
) -> str:
    """Identity for an exact directional pair, independent of epoch, run and lane."""
    return _hash_payload(
        {
            "segment_id": int(segment_id),
            "evidence_type": str(evidence_type or ""),
            "baseline_hash": sha256_text(str(baseline_text or "")),
            "candidate_hash": sha256_text(str(candidate_text or "")),
        }
    )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    )


def schema_ready(conn: sqlite3.Connection) -> bool:
    required = {
        "ml_pairwise_quality_evidence",
        "ml_pairwise_calibration_review_runs",
        "ml_pairwise_calibration_review_items",
        "ml_pairwise_calibration_labels",
    }
    if not all(_table_exists(conn, table_name) for table_name in required):
        return False
    run_columns = db.table_columns(conn, "ml_pairwise_calibration_review_runs")
    item_columns = db.table_columns(conn, "ml_pairwise_calibration_review_items")
    return {
        "consumption_status",
        "consumption_rule_version",
        "consumed_count",
        "consumed_at",
        "consumption_summary_json",
    }.issubset(run_columns) and "stable_pair_key" in item_columns


def load_labels_by_pair_keys(
    conn: sqlite3.Connection,
    pair_keys: Iterable[str],
) -> dict[str, dict[str, Any]]:
    keys = list(dict.fromkeys(str(key) for key in pair_keys if key))
    if not keys or not _table_exists(conn, "ml_pairwise_calibration_labels"):
        return {}
    labels: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(keys), 400):
        chunk = keys[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT * FROM ml_pairwise_calibration_labels WHERE pair_key IN ({placeholders})",
            chunk,
        ).fetchall()
        labels.update({str(row["pair_key"]): dict(row) for row in rows})
    return labels


def _run_row(conn: sqlite3.Connection, run_id: int | None) -> dict[str, Any] | None:
    if run_id is None:
        row = conn.execute(
            "SELECT * FROM ml_pairwise_calibration_review_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM ml_pairwise_calibration_review_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return dict(row) if row else None


def review_run_readiness(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    minimum_control_accuracy: float = DEFAULT_MINIMUM_CONTROL_ACCURACY,
) -> dict[str, Any]:
    run = _run_row(conn, run_id)
    if not run:
        return {"eligible": False, "status": "not_found", "run_id": run_id}
    counts = conn.execute(
        """
        SELECT
          COUNT(*) AS item_count,
          SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN control_blinded = 0 AND review_status = 'decided' THEN 1 ELSE 0 END) AS review_decided,
          SUM(CASE WHEN control_blinded = 1 THEN 1 ELSE 0 END) AS control_count,
          SUM(CASE WHEN control_blinded = 1 AND review_status = 'decided' THEN 1 ELSE 0 END) AS control_decided,
          SUM(CASE WHEN control_blinded = 1 AND review_status = 'decided' AND reviewer_label = expected_preference THEN 1 ELSE 0 END) AS controls_passed
        FROM ml_pairwise_calibration_review_items
        WHERE run_id = ?
        """,
        (int(run["id"]),),
    ).fetchone()
    pending = int(counts["pending_count"] or 0)
    review_decided = int(counts["review_decided"] or 0)
    control_count = int(counts["control_count"] or 0)
    control_decided = int(counts["control_decided"] or 0)
    controls_passed = int(counts["controls_passed"] or 0)
    accuracy = controls_passed / control_decided if control_decided else None
    if pending:
        status = "pending_reviews"
    elif not review_decided:
        status = "no_review_decisions"
    elif not control_count or control_decided != control_count:
        status = "controls_incomplete"
    elif accuracy is None or accuracy < minimum_control_accuracy:
        status = "blocked_control_accuracy"
    else:
        status = "ready"
    return {
        "eligible": status == "ready",
        "status": status,
        "run_id": int(run["id"]),
        "quality_epoch_id": int(run["quality_epoch_id"]),
        "review_decided_count": review_decided,
        "control_count": control_count,
        "control_decided_count": control_decided,
        "controls_passed_count": controls_passed,
        "control_accuracy": round(accuracy, 6) if accuracy is not None else None,
        "minimum_control_accuracy": minimum_control_accuracy,
        "already_consumed": str(run.get("consumption_status") or "") == "consumed",
    }


def _calibrated_evidence_values(item: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    label = str(item.get("reviewer_label") or "")
    baseline_score = float(evidence.get("baseline_score_raw") or 0.0)
    candidate_score = float(evidence.get("candidate_score_raw") or 0.0)
    current_pairwise_score = evidence.get("pairwise_score")
    if label == "candidate_preferred":
        pairwise_score = max(
            candidate_score,
            float(current_pairwise_score) if current_pairwise_score is not None else candidate_score,
        )
        training_target = "human_pairwise_preference"
        training_eligible = 1
        evidence_weight = max(float(evidence.get("evidence_weight") or 1.0), 2.0)
    elif label == "baseline_preferred":
        pairwise_score = min(candidate_score, baseline_score)
        training_target = "human_pairwise_preference"
        training_eligible = 1
        evidence_weight = max(float(evidence.get("evidence_weight") or 1.0), 2.0)
    elif label == "equivalent":
        pairwise_score = baseline_score
        training_target = "human_pairwise_equivalence"
        training_eligible = 0
        evidence_weight = max(float(evidence.get("evidence_weight") or 1.0), 1.5)
    else:
        pairwise_score = candidate_score
        training_target = "excluded_invalid_pair"
        training_eligible = 0
        evidence_weight = 0.0
    return {
        "preference_label": label,
        "training_target": training_target,
        "training_eligible": training_eligible,
        "evidence_weight": evidence_weight,
        "pairwise_score": round(pairwise_score, 9),
        "pairwise_delta": round(pairwise_score - baseline_score, 9),
    }


def consume_review_run(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    apply: bool = True,
    minimum_control_accuracy: float = DEFAULT_MINIMUM_CONTROL_ACCURACY,
    commit: bool = True,
) -> dict[str, Any]:
    if not schema_ready(conn):
        return {
            "schema_version": 1,
            "source": RULE_VERSION,
            "apply": apply,
            "status": "not_instrumented",
            "eligible": False,
            "run_id": run_id,
        }
    readiness = review_run_readiness(
        conn,
        run_id=run_id,
        minimum_control_accuracy=minimum_control_accuracy,
    )
    if readiness.get("already_consumed"):
        run = _run_row(conn, int(readiness["run_id"])) or {}
        return {
            "schema_version": 1,
            "source": RULE_VERSION,
            "apply": apply,
            **readiness,
            "status": "consumed",
            "consumed_count": int(run.get("consumed_count") or 0),
            "reused": True,
        }
    if not readiness.get("eligible"):
        if apply and readiness.get("run_id") and readiness.get("status") == "blocked_control_accuracy":
            conn.execute(
                """
                UPDATE ml_pairwise_calibration_review_runs
                SET consumption_status = ?, consumption_rule_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (readiness["status"], RULE_VERSION, db.utc_now(), int(readiness["run_id"])),
            )
            if commit:
                conn.commit()
        return {"schema_version": 1, "source": RULE_VERSION, "apply": apply, **readiness}

    selected_run_id = int(readiness["run_id"])
    rows = conn.execute(
        """
        SELECT item.*, evidence.evidence_type, evidence.baseline_hash, evidence.candidate_hash,
               evidence.source_metadata_json AS evidence_metadata_json,
               evidence.preference_label AS evidence_preference_label,
               evidence.training_target AS evidence_training_target,
               evidence.training_eligible AS evidence_training_eligible,
               evidence.evidence_weight AS evidence_weight,
               evidence.baseline_score_raw AS evidence_baseline_score_raw,
               evidence.candidate_score_raw AS evidence_candidate_score_raw,
               evidence.pairwise_score AS evidence_pairwise_score
        FROM ml_pairwise_calibration_review_items item
        JOIN ml_pairwise_quality_evidence evidence ON evidence.id = item.evidence_id
        WHERE item.run_id = ?
          AND item.control_blinded = 0
          AND item.review_status = 'decided'
        ORDER BY item.display_order, item.id
        """,
        (selected_run_id,),
    ).fetchall()
    items = [dict(row) for row in rows]
    invalid_labels = sorted(
        {str(item.get("reviewer_label") or "") for item in items} - VALID_LABELS
    )
    if invalid_labels:
        raise RuntimeError(f"Unsupported calibration labels: {invalid_labels}")
    label_counts: dict[str, int] = {}
    for item in items:
        label = str(item["reviewer_label"])
        label_counts[label] = label_counts.get(label, 0) + 1
    preview = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "apply": apply,
        **readiness,
        "status": "ready" if not apply else "consumed",
        "consumed_count": len(items),
        "training_eligible_count": sum(
            1 for item in items if str(item.get("reviewer_label")) in DIRECTIONAL_LABELS
        ),
        "label_counts": label_counts,
        "reused": False,
    }
    if not apply:
        return preview

    now = db.utc_now()
    for item in items:
        evidence_type = str(item.get("evidence_type") or "")
        baseline_text = str(item.get("baseline_text") or "")
        candidate_text = str(item.get("candidate_text") or "")
        pair_key = stable_pair_key(
            segment_id=int(item["segment_id"]),
            evidence_type=evidence_type,
            baseline_text=baseline_text,
            candidate_text=candidate_text,
        )
        label = str(item["reviewer_label"])
        training_eligible = int(label in DIRECTIONAL_LABELS)
        baseline_hash = str(item.get("baseline_hash") or sha256_text(baseline_text))
        candidate_hash = str(item.get("candidate_hash") or sha256_text(candidate_text))
        conn.execute(
            """
            INSERT INTO ml_pairwise_calibration_labels (
              pair_key, segment_id, evidence_id, evidence_type,
              baseline_hash, candidate_hash, baseline_text, candidate_text,
              review_label, review_reason, reviewer, training_eligible,
              source_run_id, source_item_id, control_accuracy, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair_key) DO UPDATE SET
              evidence_id = excluded.evidence_id,
              review_label = excluded.review_label,
              review_reason = excluded.review_reason,
              reviewer = excluded.reviewer,
              training_eligible = excluded.training_eligible,
              source_run_id = excluded.source_run_id,
              source_item_id = excluded.source_item_id,
              control_accuracy = excluded.control_accuracy,
              updated_at = excluded.updated_at
            """,
            (
                pair_key,
                int(item["segment_id"]),
                int(item["evidence_id"]),
                evidence_type,
                baseline_hash,
                candidate_hash,
                baseline_text,
                candidate_text,
                label,
                item.get("reviewer_reason"),
                str(item.get("reviewer") or "dashboard_human_review"),
                training_eligible,
                selected_run_id,
                int(item["id"]),
                readiness.get("control_accuracy"),
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE ml_pairwise_calibration_review_items SET stable_pair_key = ?, updated_at = ? WHERE id = ?",
            (pair_key, now, int(item["id"])),
        )
        evidence = {
            "baseline_score_raw": item.get("evidence_baseline_score_raw"),
            "candidate_score_raw": item.get("evidence_candidate_score_raw"),
            "pairwise_score": item.get("evidence_pairwise_score"),
            "evidence_weight": item.get("evidence_weight"),
        }
        calibrated = _calibrated_evidence_values(item, evidence)
        try:
            metadata = json.loads(str(item.get("evidence_metadata_json") or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        metadata["human_pairwise_calibration"] = {
            "rule_version": RULE_VERSION,
            "run_id": selected_run_id,
            "item_id": int(item["id"]),
            "pair_key": pair_key,
            "review_label": label,
            "control_accuracy": readiness.get("control_accuracy"),
            "consumed_at": now,
        }
        conn.execute(
            """
            UPDATE ml_pairwise_quality_evidence
            SET preference_label = ?, training_target = ?, training_eligible = ?,
                evidence_weight = ?, pairwise_score = ?, pairwise_delta = ?,
                promotion_eligible = CASE WHEN ? IN ('baseline_preferred', 'equivalent', 'invalid_pair') THEN 0 ELSE promotion_eligible END,
                auto_safe_eligible = CASE WHEN ? IN ('baseline_preferred', 'equivalent', 'invalid_pair') THEN 0 ELSE auto_safe_eligible END,
                source_metadata_json = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                calibrated["preference_label"],
                calibrated["training_target"],
                calibrated["training_eligible"],
                calibrated["evidence_weight"],
                calibrated["pairwise_score"],
                calibrated["pairwise_delta"],
                label,
                label,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                now,
                int(item["evidence_id"]),
            ),
        )

    summary_json = json.dumps(preview, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        UPDATE ml_pairwise_calibration_review_runs
        SET consumption_status = 'consumed', consumption_rule_version = ?,
            consumed_count = ?, consumed_at = ?, consumption_summary_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (RULE_VERSION, len(items), now, summary_json, now, selected_run_id),
    )
    if commit:
        conn.commit()
    return preview


def consume_ready_review_runs(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    minimum_control_accuracy: float = DEFAULT_MINIMUM_CONTROL_ACCURACY,
) -> dict[str, Any]:
    if not schema_ready(conn):
        return {
            "schema_version": 1,
            "source": RULE_VERSION,
            "apply": apply,
            "status": "not_instrumented",
            "run_count": 0,
            "runs": [],
        }
    rows = conn.execute(
        """
        SELECT id
        FROM ml_pairwise_calibration_review_runs
        WHERE pending_count = 0
          AND COALESCE(consumption_status, 'pending') <> 'consumed'
        ORDER BY id
        """
    ).fetchall()
    results = [
        consume_review_run(
            conn,
            run_id=int(row["id"]),
            apply=apply,
            minimum_control_accuracy=minimum_control_accuracy,
            commit=False,
        )
        for row in rows
    ]
    if apply:
        conn.commit()
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "apply": apply,
        "status": "completed",
        "run_count": len(results),
        "consumed_run_count": sum(result.get("status") == "consumed" for result in results),
        "consumed_item_count": sum(int(result.get("consumed_count") or 0) for result in results if result.get("status") == "consumed"),
        "runs": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Consume completed pairwise calibration reviews safely.")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--all-ready", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--minimum-control-accuracy",
        type=float,
        default=DEFAULT_MINIMUM_CONTROL_ACCURACY,
    )
    args = parser.parse_args()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        if args.apply:
            db.ensure_database(conn)
        if args.all_ready:
            payload = consume_ready_review_runs(
                conn,
                apply=args.apply,
                minimum_control_accuracy=args.minimum_control_accuracy,
            )
        else:
            payload = consume_review_run(
                conn,
                run_id=args.run_id,
                apply=args.apply,
                minimum_control_accuracy=args.minimum_control_accuracy,
            )
    print(f"[pairwise_calibration_consumer] Status: {payload.get('status')}")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
