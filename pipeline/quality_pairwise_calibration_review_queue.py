from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import quality_pairwise_calibration_consumer as calibration_consumer


RULE_VERSION = "quality_pairwise_calibration_review_queue_v1"
DEFAULT_TIE_EPSILON = 0.0001
DEFAULT_PRIORITY_DELTA = -0.002
DEFAULT_CONTROL_COUNT = 8
VALID_REVIEW_LABELS = {
    "candidate_preferred",
    "baseline_preferred",
    "equivalent",
    "invalid_pair",
}
CONTROL_LANES = {"positive_control", "negative_control"}


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _score_outcome(delta: float, epsilon: float) -> str:
    if delta > epsilon:
        return "improved"
    if delta < -epsilon:
        return "regressed"
    return "equal"


def latest_scored_epoch(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM quality_epochs
        WHERE old_score_run_id IS NOT NULL
          AND output_score_run_id IS NOT NULL
          AND status IN ('scored', 'evaluated', 'published')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No scored quality epoch is available for pairwise calibration review.")
    return dict(row)


def load_applied_pairwise_population(
    conn: sqlite3.Connection,
    *,
    old_score_run_id: int,
    output_score_run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_evidence AS (
          SELECT evidence.*
          FROM ml_pairwise_quality_evidence evidence
          WHERE evidence.id = (
            SELECT MAX(latest.id)
            FROM ml_pairwise_quality_evidence latest
            WHERE latest.segment_id = evidence.segment_id
              AND latest.evidence_type = evidence.evidence_type
          )
        )
        SELECT
          evidence.id AS evidence_id,
          evidence.segment_id,
          evidence.relative_path,
          evidence.source_key,
          source.source_line_number,
          evidence.evidence_type,
          evidence.baseline_text,
          evidence.candidate_text,
          old_score.model_safe_probability AS baseline_score_raw,
          output_score.model_safe_probability AS candidate_score_raw,
          old_score.final_action AS baseline_action,
          output_score.final_action AS candidate_action,
          evidence.token_integrity_ok,
          evidence.post_validation_clean,
          evidence.source_metadata_json,
          confirmation.confirmation_source,
          confirmation.confirmation_label
        FROM latest_evidence evidence
        JOIN ml_score_items old_score
          ON old_score.run_id = ?
         AND old_score.segment_id = evidence.segment_id
         AND old_score.candidate_text = evidence.baseline_text
        JOIN ml_score_items output_score
          ON output_score.run_id = ?
         AND output_score.segment_id = evidence.segment_id
         AND output_score.candidate_text = evidence.candidate_text
        JOIN output_segments output ON output.segment_id = evidence.segment_id
        JOIN source_segments source ON source.id = evidence.segment_id AND source.is_active = 1
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = evidence.segment_id
         AND confirmation.confirmed_text = evidence.candidate_text
        WHERE evidence.baseline_text <> evidence.candidate_text
          AND output.portuguese_text = evidence.candidate_text
        ORDER BY evidence.id DESC
        """,
        (old_score_run_id, output_score_run_id),
    ).fetchall()
    # A segment may have equivalent evidence from more than one provider. The
    # newest exact pair is the canonical calibration observation.
    by_segment: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        segment_id = int(item["segment_id"])
        if segment_id in by_segment:
            continue
        if item.get("baseline_score_raw") is None or item.get("candidate_score_raw") is None:
            continue
        by_segment[segment_id] = item
    return sorted(by_segment.values(), key=lambda item: int(item["segment_id"]))


def load_applied_pairwise_cohort(
    conn: sqlite3.Connection,
    *,
    old_score_run_id: int,
    output_score_run_id: int,
) -> list[dict[str, Any]]:
    return [
        item
        for item in load_applied_pairwise_population(
            conn,
            old_score_run_id=old_score_run_id,
            output_score_run_id=output_score_run_id,
        )
        if int(item.get("token_integrity_ok") or 0) == 1
        and int(item.get("post_validation_clean") or 0) == 1
    ]


def _queue_item(
    row: dict[str, Any],
    *,
    lane: str,
    baseline_text: str,
    candidate_text: str,
    baseline_score: float,
    candidate_score: float,
    score_outcome: str,
    expected_preference: str,
    holdout_eligible: bool,
    control_blinded: bool,
) -> dict[str, Any]:
    delta = candidate_score - baseline_score
    pair_hash = _hash_payload(
        {
            "segment_id": int(row["segment_id"]),
            "evidence_id": int(row["evidence_id"]),
            "lane": lane,
            "baseline_text": baseline_text,
            "candidate_text": candidate_text,
        }
    )
    pair_key = calibration_consumer.stable_pair_key(
        segment_id=int(row["segment_id"]),
        evidence_type=str(row.get("evidence_type") or ""),
        baseline_text=baseline_text,
        candidate_text=candidate_text,
    )
    return {
        "evidence_id": int(row["evidence_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": str(row.get("relative_path") or ""),
        "source_key": str(row.get("source_key") or ""),
        "source_line_number": row.get("source_line_number"),
        "evidence_type": str(row.get("evidence_type") or ""),
        "review_lane": lane,
        "baseline_text": baseline_text,
        "candidate_text": candidate_text,
        "baseline_score_raw": round(float(baseline_score), 9),
        "candidate_score_raw": round(float(candidate_score), 9),
        "raw_delta": round(float(delta), 9),
        "score_outcome": score_outcome,
        "expected_preference": expected_preference,
        "review_status": "pending",
        "training_eligible": 0,
        "holdout_eligible": int(holdout_eligible),
        "control_blinded": int(control_blinded),
        "pair_hash": pair_hash,
        "stable_pair_key": pair_key,
        "metadata": {
            "evidence_type": str(row.get("evidence_type") or ""),
            "baseline_action": row.get("baseline_action"),
            "candidate_action": row.get("candidate_action"),
            "confirmation_source": row.get("confirmation_source"),
            "confirmation_label": row.get("confirmation_label"),
            "original_baseline_score_raw": round(float(row["baseline_score_raw"]), 9),
            "original_candidate_score_raw": round(float(row["candidate_score_raw"]), 9),
        },
    }


def build_queue_items(
    rows: list[dict[str, Any]],
    *,
    tie_epsilon: float = DEFAULT_TIE_EPSILON,
    priority_delta_threshold: float = DEFAULT_PRIORITY_DELTA,
    control_count: int = DEFAULT_CONTROL_COUNT,
) -> list[dict[str, Any]]:
    measured: list[dict[str, Any]] = []
    for row in rows:
        baseline_score = float(row["baseline_score_raw"])
        candidate_score = float(row["candidate_score_raw"])
        delta = candidate_score - baseline_score
        measured.append({**row, "raw_delta": delta, "score_outcome": _score_outcome(delta, tie_epsilon)})

    non_improving = sorted(
        (item for item in measured if item["score_outcome"] != "improved"),
        key=lambda item: (float(item["raw_delta"]), int(item["segment_id"])),
    )
    improved = sorted(
        (item for item in measured if item["score_outcome"] == "improved"),
        key=lambda item: (-float(item["raw_delta"]), int(item["segment_id"])),
    )

    selected: list[dict[str, Any]] = []
    for row in non_improving:
        lane = "priority_review" if float(row["raw_delta"]) <= priority_delta_threshold else "score_review"
        selected.append(
            _queue_item(
                row,
                lane=lane,
                baseline_text=str(row["baseline_text"]),
                candidate_text=str(row["candidate_text"]),
                baseline_score=float(row["baseline_score_raw"]),
                candidate_score=float(row["candidate_score_raw"]),
                score_outcome=str(row["score_outcome"]),
                expected_preference="candidate_preferred",
                holdout_eligible=False,
                control_blinded=False,
            )
        )

    control_count = max(0, int(control_count))
    positive_controls = improved[:control_count]
    negative_controls = improved[control_count : control_count * 2]
    for row in positive_controls:
        selected.append(
            _queue_item(
                row,
                lane="positive_control",
                baseline_text=str(row["baseline_text"]),
                candidate_text=str(row["candidate_text"]),
                baseline_score=float(row["baseline_score_raw"]),
                candidate_score=float(row["candidate_score_raw"]),
                score_outcome="control",
                expected_preference="candidate_preferred",
                holdout_eligible=True,
                control_blinded=True,
            )
        )
    for row in negative_controls:
        selected.append(
            _queue_item(
                row,
                lane="negative_control",
                baseline_text=str(row["candidate_text"]),
                candidate_text=str(row["baseline_text"]),
                baseline_score=float(row["candidate_score_raw"]),
                candidate_score=float(row["baseline_score_raw"]),
                score_outcome="control",
                expected_preference="baseline_preferred",
                holdout_eligible=True,
                control_blinded=True,
            )
        )

    lane_rank = {"priority_review": 0, "score_review": 1, "positive_control": 2, "negative_control": 3}
    selected.sort(
        key=lambda item: (
            lane_rank[item["review_lane"]],
            float(item["raw_delta"]),
            int(item["segment_id"]),
        )
    )
    for index, item in enumerate(selected, start=1):
        item["display_order"] = index
    return selected


def _run_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM ml_pairwise_calibration_review_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if not run:
        raise RuntimeError(f"Calibration review run {run_id} was not found.")
    counts = {
        row["review_lane"]: int(row["total"])
        for row in conn.execute(
            """
            SELECT review_lane, COUNT(*) AS total
            FROM ml_pairwise_calibration_review_items
            WHERE run_id = ?
            GROUP BY review_lane
            """,
            (run_id,),
        )
    }
    return {
        **dict(run),
        "lane_counts": counts,
        "pending_count": int(run["pending_count"] or 0),
        "decided_count": int(run["decided_count"] or 0),
    }


def _write_artifacts(
    settings: dict[str, Any],
    *,
    run_id: int,
    queue_key: str,
    epoch: dict[str, Any],
    items: list[dict[str, Any]],
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_path = reports_dir / f"{stamp}_pairwise_calibration_review_queue.md"
    template_path = reports_dir / f"{stamp}_pairwise_calibration_review_decisions.json"
    counts: dict[str, int] = {}
    for item in items:
        counts[item["review_lane"]] = counts.get(item["review_lane"], 0) + 1
    report_path.write_text(
        "\n".join(
            [
                "# Fila de revisão pairwise para calibração",
                "",
                f"- Run: `{run_id}`",
                f"- Quality epoch: `{epoch['id']}`",
                f"- Score old/output: `{epoch['old_score_run_id']} -> {epoch['output_score_run_id']}`",
                f"- Casos de revisão: `{counts.get('priority_review', 0) + counts.get('score_review', 0)}`",
                f"- Prioridade (delta <= {DEFAULT_PRIORITY_DELTA * 100:.2f} p.p.): `{counts.get('priority_review', 0)}`",
                f"- Controles positivos cegos: `{counts.get('positive_control', 0)}`",
                f"- Controles negativos invertidos e cegos: `{counts.get('negative_control', 0)}`",
                "",
                "A fila não altera score, confirmação ou output. Controles são holdout e nunca entram no treino.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    template = {
        "schema_version": 1,
        "queue_key": queue_key,
        "run_id": run_id,
        "valid_labels": sorted(VALID_REVIEW_LABELS),
        "items": [
            {
                "item_id": item.get("id"),
                "pair_hash": item["pair_hash"],
                "segment_id": item["segment_id"],
                "review_lane": "control" if item["control_blinded"] else item["review_lane"],
                "relative_path": item["relative_path"],
                "source_key": item["source_key"],
                "baseline_text": item["baseline_text"],
                "candidate_text": item["candidate_text"],
                "baseline_score_raw": None if item["control_blinded"] else item["baseline_score_raw"],
                "candidate_score_raw": None if item["control_blinded"] else item["candidate_score_raw"],
                "raw_delta": None if item["control_blinded"] else item["raw_delta"],
                "reviewer_label": item.get("reviewer_label"),
                "reviewer_reason": item.get("reviewer_reason"),
            }
            for item in items
        ],
    }
    template_path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path, template_path


def _sanitize_existing_control_scores(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    template_path: str | None,
) -> None:
    if not template_path:
        return
    path = Path(template_path)
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    control_ids = {
        int(row["id"])
        for row in conn.execute(
            """
            SELECT id
            FROM ml_pairwise_calibration_review_items
            WHERE run_id = ? AND control_blinded = 1
            """,
            (run_id,),
        )
    }
    changed = False
    for item in payload.get("items") or []:
        if int(item.get("item_id") or 0) not in control_ids:
            continue
        for key in ("baseline_score_raw", "candidate_score_raw", "raw_delta"):
            if item.get(key) is not None:
                item[key] = None
                changed = True
    if changed:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        conn.execute(
            "UPDATE ml_pairwise_calibration_review_runs SET updated_at = ? WHERE id = ?",
            (db.utc_now(), run_id),
        )
        conn.commit()


def create_review_queue(
    conn: sqlite3.Connection,
    settings: dict[str, Any],
    *,
    apply: bool,
    tie_epsilon: float = DEFAULT_TIE_EPSILON,
    priority_delta_threshold: float = DEFAULT_PRIORITY_DELTA,
    control_count: int = DEFAULT_CONTROL_COUNT,
) -> dict[str, Any]:
    epoch = latest_scored_epoch(conn)
    cohort = load_applied_pairwise_cohort(
        conn,
        old_score_run_id=int(epoch["old_score_run_id"]),
        output_score_run_id=int(epoch["output_score_run_id"]),
    )
    items = build_queue_items(
        cohort,
        tie_epsilon=tie_epsilon,
        priority_delta_threshold=priority_delta_threshold,
        control_count=control_count,
    )
    queue_key = _hash_payload(
        {
            "rule_version": RULE_VERSION,
            "quality_epoch_id": int(epoch["id"]),
            "old_score_run_id": int(epoch["old_score_run_id"]),
            "output_score_run_id": int(epoch["output_score_run_id"]),
            "tie_epsilon": tie_epsilon,
            "priority_delta_threshold": priority_delta_threshold,
            "control_count": control_count,
            "pairs": [item["pair_hash"] for item in items],
        }
    )
    counts = {lane: sum(1 for item in items if item["review_lane"] == lane) for lane in (
        "priority_review", "score_review", "positive_control", "negative_control"
    )}
    inherited_labels = (
        calibration_consumer.load_labels_by_pair_keys(
            conn,
            [item["stable_pair_key"] for item in items if not item["control_blinded"]],
        )
        if calibration_consumer.schema_ready(conn)
        else {}
    )
    inherited_count = 0
    for item in items:
        inherited = None if item["control_blinded"] else inherited_labels.get(item["stable_pair_key"])
        if not inherited:
            continue
        inherited_count += 1
        item["review_status"] = "decided"
        item["reviewer_label"] = inherited["review_label"]
        item["reviewer_reason"] = inherited.get("review_reason") or "Inherited exact pair decision."
        item["reviewer"] = "calibration_ledger"
        item["reviewed_at"] = inherited.get("updated_at") or db.utc_now()
        item["training_eligible"] = int(
            inherited["review_label"] in {"candidate_preferred", "baseline_preferred"}
        )
    payload = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "apply": apply,
        "queue_key": queue_key,
        "quality_epoch_id": int(epoch["id"]),
        "old_score_run_id": int(epoch["old_score_run_id"]),
        "output_score_run_id": int(epoch["output_score_run_id"]),
        "candidate_count": len(cohort),
        "review_count": counts["priority_review"] + counts["score_review"],
        "priority_count": counts["priority_review"],
        "positive_control_count": counts["positive_control"],
        "negative_control_count": counts["negative_control"],
        "pending_count": len(items) - inherited_count,
        "decided_count": inherited_count,
        "inherited_count": inherited_count,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "score_write_count": 0,
    }
    if not apply:
        return payload

    existing = conn.execute(
        "SELECT id FROM ml_pairwise_calibration_review_runs WHERE queue_key = ?",
        (queue_key,),
    ).fetchone()
    if existing:
        run_id = int(existing["id"])
        summary = _run_summary(conn, run_id)
        _sanitize_existing_control_scores(
            conn,
            run_id=run_id,
            template_path=summary.get("decisions_template_path"),
        )
        return {**payload, "run_id": run_id, "reused": True, **_run_summary(conn, run_id)}

    now = db.utc_now()
    cursor = conn.execute(
        """
        INSERT INTO ml_pairwise_calibration_review_runs (
          queue_key, rule_version, quality_epoch_id, old_score_run_id, output_score_run_id,
          technical_tie_epsilon, priority_delta_threshold, control_count,
          candidate_count, review_count, priority_count, positive_control_count,
          negative_control_count, pending_count, decided_count, inherited_count,
          started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            queue_key, RULE_VERSION, int(epoch["id"]), int(epoch["old_score_run_id"]),
            int(epoch["output_score_run_id"]), tie_epsilon, priority_delta_threshold,
            control_count, len(cohort), payload["review_count"], payload["priority_count"],
            payload["positive_control_count"], payload["negative_control_count"],
            payload["pending_count"], payload["decided_count"], inherited_count,
            now, now, now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for item in items:
        item_cursor = conn.execute(
            """
            INSERT INTO ml_pairwise_calibration_review_items (
              run_id, evidence_id, segment_id, relative_path, source_key, source_line_number,
              review_lane, display_order, baseline_text, candidate_text,
              baseline_score_raw, candidate_score_raw, raw_delta, score_outcome,
              expected_preference, review_status, reviewer_label, reviewer_reason,
              reviewer, reviewed_at, training_eligible, holdout_eligible,
              control_blinded, pair_hash, stable_pair_key, metadata_json, created_at, updated_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                run_id, item["evidence_id"], item["segment_id"], item["relative_path"],
                item["source_key"], item["source_line_number"], item["review_lane"],
                item["display_order"], item["baseline_text"], item["candidate_text"],
                item["baseline_score_raw"], item["candidate_score_raw"], item["raw_delta"],
                item["score_outcome"], item["expected_preference"], item["review_status"],
                item.get("reviewer_label"), item.get("reviewer_reason"), item.get("reviewer"),
                item.get("reviewed_at"), item["training_eligible"], item["holdout_eligible"],
                item["control_blinded"], item["pair_hash"], item["stable_pair_key"],
                json.dumps(item["metadata"], ensure_ascii=False, sort_keys=True), now, now,
            ),
        )
        item["id"] = int(item_cursor.lastrowid)
        if item["review_status"] == "decided":
            conn.execute(
                """
                INSERT INTO ml_pairwise_calibration_review_decisions (
                  run_id, item_id, review_label, review_reason, reviewer, control_passed, created_at
                ) VALUES (?, ?, ?, ?, 'calibration_ledger', NULL, ?)
                """,
                (
                    run_id,
                    item["id"],
                    item["reviewer_label"],
                    item.get("reviewer_reason"),
                    now,
                ),
            )
    report_path, template_path = _write_artifacts(
        settings, run_id=run_id, queue_key=queue_key, epoch=epoch, items=items
    )
    conn.execute(
        """
        UPDATE ml_pairwise_calibration_review_runs
        SET report_path = ?, decisions_template_path = ?, updated_at = ?
        WHERE id = ?
        """,
        (str(report_path), str(template_path), db.utc_now(), run_id),
    )
    conn.commit()
    return {
        **payload,
        "run_id": run_id,
        "reused": False,
        "report_path": str(report_path),
        "decisions_template_path": str(template_path),
    }


def record_review_decision(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    review_label: str,
    review_reason: str | None,
    reviewer: str,
) -> dict[str, Any]:
    if review_label not in VALID_REVIEW_LABELS:
        raise ValueError(f"Invalid review label: {review_label}")
    item = conn.execute(
        "SELECT * FROM ml_pairwise_calibration_review_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not item:
        raise ValueError(f"Calibration review item {item_id} was not found.")
    item = dict(item)
    is_control = item["review_lane"] in CONTROL_LANES
    control_passed = int(review_label == item["expected_preference"]) if is_control else None
    training_eligible = int(
        not is_control and review_label in {"candidate_preferred", "baseline_preferred"}
    )
    now = db.utc_now()
    conn.execute(
        """
        INSERT INTO ml_pairwise_calibration_review_decisions (
          run_id, item_id, review_label, review_reason, reviewer, control_passed, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (item["run_id"], item_id, review_label, review_reason, reviewer, control_passed, now),
    )
    conn.execute(
        """
        UPDATE ml_pairwise_calibration_review_items
        SET review_status = 'decided', reviewer_label = ?, reviewer_reason = ?, reviewer = ?,
            reviewed_at = ?, training_eligible = ?, updated_at = ?
        WHERE id = ?
        """,
        (review_label, review_reason, reviewer, now, training_eligible, now, item_id),
    )
    totals = conn.execute(
        """
        SELECT
          SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN review_status = 'decided' THEN 1 ELSE 0 END) AS decided_count,
          SUM(CASE WHEN review_lane IN ('positive_control', 'negative_control') AND review_status = 'decided' THEN 1 ELSE 0 END) AS control_decided,
          SUM(CASE WHEN review_lane IN ('positive_control', 'negative_control') AND review_status = 'decided' AND reviewer_label = expected_preference THEN 1 ELSE 0 END) AS controls_passed
        FROM ml_pairwise_calibration_review_items
        WHERE run_id = ?
        """,
        (item["run_id"],),
    ).fetchone()
    pending = int(totals["pending_count"] or 0)
    decided = int(totals["decided_count"] or 0)
    control_decided = int(totals["control_decided"] or 0)
    controls_passed = int(totals["controls_passed"] or 0)
    accuracy = round(controls_passed / control_decided, 6) if control_decided else None
    conn.execute(
        """
        UPDATE ml_pairwise_calibration_review_runs
        SET pending_count = ?, decided_count = ?, controls_passed_count = ?,
            control_accuracy = ?, updated_at = ?
        WHERE id = ?
        """,
        (pending, decided, controls_passed, accuracy, now, item["run_id"]),
    )
    consumption = None
    if pending == 0 and calibration_consumer.schema_ready(conn):
        consumption = calibration_consumer.consume_review_run(
            conn,
            run_id=int(item["run_id"]),
            apply=True,
            commit=False,
        )
    conn.commit()
    return {
        "ok": True,
        "run_id": int(item["run_id"]),
        "item_id": item_id,
        "review_label": review_label,
        "training_eligible": training_eligible,
        "holdout_eligible": int(item["holdout_eligible"] or 0),
        "control_passed": control_passed,
        "pending_count": pending,
        "decided_count": decided,
        "control_accuracy": accuracy,
        "calibration_consumption": consumption,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the generic supervised pairwise calibration review queue.")
    parser.add_argument("--apply", action="store_true", help="Persist queue metadata and review items.")
    parser.add_argument("--tie-epsilon", type=float, default=DEFAULT_TIE_EPSILON)
    parser.add_argument("--priority-delta", type=float, default=DEFAULT_PRIORITY_DELTA)
    parser.add_argument("--control-count", type=int, default=DEFAULT_CONTROL_COUNT)
    args = parser.parse_args()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        if args.apply:
            db.ensure_database(conn)
        payload = create_review_queue(
            conn,
            settings,
            apply=args.apply,
            tie_epsilon=args.tie_epsilon,
            priority_delta_threshold=args.priority_delta,
            control_count=args.control_count,
        )
    print(f"[pairwise_calibration_review] Review: {payload['review_count']}")
    print(f"[pairwise_calibration_review] Priority: {payload['priority_count']}")
    print(f"[pairwise_calibration_review] Controls: {payload['positive_control_count'] + payload['negative_control_count']}")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
