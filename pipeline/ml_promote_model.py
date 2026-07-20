from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

import db


POLICY_VERSION = "ml_model_promotion_v6"
MODEL_KIND = "risk_action_classifier"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(value: float | None) -> str:
    if value is None:
        return "0.00%"
    return f"{value:.2%}"


def _holdout_metrics(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    return {
        "holdout_run_id": int(payload["id"]),
        "path": payload.get("report_path"),
        "dataset_run_id": int(payload["dataset_run_id"]),
        "false_safe": int(payload.get("false_safe_count") or 0),
        "predicted_safe": int(payload.get("predicted_safe_count") or 0),
        "false_safe_rate": payload.get("false_safe_rate"),
        "safe_precision": payload.get("safe_precision"),
        "safe_recall": payload.get("safe_recall"),
        "accuracy": payload.get("accuracy"),
        "macro_f1": payload.get("macro_f1"),
        "metrics_source": "ml_holdout_eval_runs",
    }


def latest_holdout_metrics(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_holdout_eval_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return _holdout_metrics(row)


def latest_holdout_metrics_for_dataset(conn, dataset_run_id: int | None) -> dict[str, Any] | None:
    if dataset_run_id is None:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM ml_holdout_eval_runs
        WHERE dataset_run_id = ?
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (int(dataset_run_id),),
    ).fetchone()
    return _holdout_metrics(row)


def fetch_model_run(conn, model_run_id: int | None) -> dict[str, Any]:
    if model_run_id is None:
        row = conn.execute(
            """
            SELECT *
            FROM ml_model_runs
            WHERE model_kind = ?
              AND model_path IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (MODEL_KIND,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM ml_model_runs
            WHERE id = ?
            """,
            (model_run_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("No candidate model run found.")
    return dict(row)


def fetch_active_model(conn) -> dict[str, Any] | None:
    registry = conn.execute(
        """
        SELECT *
        FROM ml_model_registry
        WHERE model_kind = ?
        """,
        (MODEL_KIND,),
    ).fetchone()
    if registry is None:
        return None
    model = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE id = ?
        """,
        (registry["active_model_run_id"],),
    ).fetchone()
    if model is None:
        return None
    return dict(model)


def metric(model: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = model.get(key)
    if value is None:
        return default
    return float(value)


def fetch_latest_full_score_run(conn, model_run_id: int | None) -> dict[str, Any] | None:
    if model_run_id is None:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE model_run_id = ?
          AND path_filter IS NULL
          AND limit_count IS NULL
          AND finished_at IS NOT NULL
          AND scored_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (model_run_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def score_safe_rate(score_run: dict[str, Any] | None) -> float:
    if not score_run:
        return 0.0
    scored_count = int(score_run.get("scored_count") or 0)
    if scored_count <= 0:
        return 0.0
    return int(score_run.get("final_auto_safe_count") or 0) / scored_count


def evaluate_candidate(
    candidate: dict[str, Any],
    active: dict[str, Any] | None,
    max_false_safe: int,
    max_false_safe_rate: float,
    min_safe_precision: float,
    holdout_metrics: dict[str, Any] | None,
    active_holdout_metrics: dict[str, Any] | None,
    max_holdout_false_safe: int,
    max_holdout_false_safe_rate: float,
    min_holdout_safe_precision: float,
    max_safe_recall_drop: float,
    max_holdout_predicted_safe_drop_rate: float,
    candidate_score_run: dict[str, Any] | None,
    active_score_run: dict[str, Any] | None,
    max_operational_auto_safe_drop_rate: float,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    false_safe_count = int(candidate.get("false_safe_count") or 0)
    false_safe_rate = metric(candidate, "false_safe_rate")
    safe_precision = metric(candidate, "safe_precision")

    if false_safe_count > max_false_safe:
        return "rejected", [f"false_safe_count {false_safe_count} > {max_false_safe}"]
    if false_safe_rate > max_false_safe_rate:
        return "rejected", [f"false_safe_rate {false_safe_rate:.6f} > {max_false_safe_rate:.6f}"]
    if safe_precision < min_safe_precision:
        return "rejected", [f"safe_precision {safe_precision:.6f} < {min_safe_precision:.6f}"]

    reasons.append("safety_gates:passed")

    if holdout_metrics is None:
        return "rejected", ["holdout_gate:missing_latest_holdout_eval"]
    if holdout_metrics.get("dataset_run_id") != candidate.get("dataset_run_id"):
        return "rejected", [
            "holdout_gate:dataset_mismatch "
            f"holdout={holdout_metrics.get('dataset_run_id')} candidate={candidate.get('dataset_run_id')}"
        ]
    holdout_false_safe = int(holdout_metrics.get("false_safe") or 0)
    holdout_false_safe_rate = float(holdout_metrics.get("false_safe_rate") or 0)
    holdout_safe_precision = float(holdout_metrics.get("safe_precision") or 0)
    if holdout_false_safe > max_holdout_false_safe:
        return "rejected", [f"holdout_false_safe {holdout_false_safe} > {max_holdout_false_safe}"]
    if holdout_false_safe_rate > max_holdout_false_safe_rate:
        return "rejected", [
            f"holdout_false_safe_rate {holdout_false_safe_rate:.6f} > {max_holdout_false_safe_rate:.6f}"
        ]
    if holdout_safe_precision < min_holdout_safe_precision:
        return "rejected", [
            f"holdout_safe_precision {holdout_safe_precision:.6f} < {min_holdout_safe_precision:.6f}"
        ]
    reasons.append(
        "holdout_gate:passed "
        f"false_safe={holdout_false_safe} "
        f"false_safe_rate={holdout_false_safe_rate:.6f} "
        f"report={holdout_metrics.get('path')}"
    )

    if active is None:
        reasons.append("no_active_model")
        return "promote", reasons

    if candidate_score_run is None:
        return "rejected", [f"operational_score_gate:missing_candidate_full_score model_run_id={candidate['id']}"]
    if active_score_run is None:
        return "rejected", [f"operational_score_gate:missing_active_full_score model_run_id={active['id']}"]
    candidate_score_safe_rate = score_safe_rate(candidate_score_run)
    active_score_safe_rate = score_safe_rate(active_score_run)
    if active_score_safe_rate > 0:
        operational_auto_safe_drop_rate = (
            active_score_safe_rate - candidate_score_safe_rate
        ) / active_score_safe_rate
    else:
        operational_auto_safe_drop_rate = 0.0
    if operational_auto_safe_drop_rate > max_operational_auto_safe_drop_rate:
        return "do_not_promote", [
            "operational_coverage_regression:"
            f"active_score_run={active_score_run['id']},"
            f"candidate_score_run={candidate_score_run['id']},"
            f"active_auto_safe={active_score_run['final_auto_safe_count']},"
            f"candidate_auto_safe={candidate_score_run['final_auto_safe_count']},"
            f"active_safe_rate={active_score_safe_rate:.6f},"
            f"candidate_safe_rate={candidate_score_safe_rate:.6f},"
            f"drop_rate={operational_auto_safe_drop_rate:.6f},"
            f"max={max_operational_auto_safe_drop_rate:.6f}"
        ]
    reasons.append(
        "operational_coverage_gate:passed "
        f"active_score_run={active_score_run['id']} "
        f"candidate_score_run={candidate_score_run['id']} "
        f"active_auto_safe={active_score_run['final_auto_safe_count']} "
        f"candidate_auto_safe={candidate_score_run['final_auto_safe_count']} "
        f"drop_rate={operational_auto_safe_drop_rate:.6f}"
    )

    active_holdout_false_safe: int | None = None
    active_holdout_predicted_safe: int | None = None
    candidate_holdout_predicted_safe: int | None = None
    if active_holdout_metrics is not None:
        active_holdout_false_safe = int(active_holdout_metrics.get("false_safe") or 0)
        if holdout_false_safe > active_holdout_false_safe:
            return "do_not_promote", [
                "holdout_safety_regression:"
                f"active_false_safe={active_holdout_false_safe},"
                f"candidate_false_safe={holdout_false_safe},"
                f"active_report={active_holdout_metrics.get('path')},"
                f"candidate_report={holdout_metrics.get('path')}"
            ]
        active_holdout_predicted_safe = int(active_holdout_metrics.get("predicted_safe") or 0)
        candidate_holdout_predicted_safe = int(holdout_metrics.get("predicted_safe") or 0)
        if active_holdout_predicted_safe > 0:
            holdout_predicted_safe_drop_rate = (
                active_holdout_predicted_safe - candidate_holdout_predicted_safe
            ) / active_holdout_predicted_safe
            if holdout_predicted_safe_drop_rate > max_holdout_predicted_safe_drop_rate:
                return "do_not_promote", [
                    "holdout_coverage_regression:"
                    f"active_predicted_safe={active_holdout_predicted_safe},"
                    f"candidate_predicted_safe={candidate_holdout_predicted_safe},"
                    f"drop_rate={holdout_predicted_safe_drop_rate:.6f},"
                    f"max={max_holdout_predicted_safe_drop_rate:.6f},"
                    f"active_report={active_holdout_metrics.get('path')},"
                    f"candidate_report={holdout_metrics.get('path')}"
                ]
            reasons.append(
                "holdout_coverage_gate:passed "
                f"active_predicted_safe={active_holdout_predicted_safe} "
                f"candidate_predicted_safe={candidate_holdout_predicted_safe} "
                f"drop_rate={holdout_predicted_safe_drop_rate:.6f}"
            )

    active_false_safe = int(active.get("false_safe_count") or 0)
    active_false_safe_rate = metric(active, "false_safe_rate")
    active_safe_precision = metric(active, "safe_precision")
    active_safe_recall = metric(active, "safe_recall")
    candidate_safe_recall = metric(candidate, "safe_recall")

    if candidate["id"] == active["id"]:
        return "keep_active", ["candidate_is_already_active"]

    if false_safe_count < active_false_safe:
        reasons.append(f"false_safe_count_improved:{active_false_safe}->{false_safe_count}")
        return "promote", reasons
    if false_safe_count == active_false_safe and false_safe_rate < active_false_safe_rate:
        reasons.append(f"false_safe_rate_improved:{active_false_safe_rate:.6f}->{false_safe_rate:.6f}")
        return "promote", reasons
    if (
        false_safe_count == active_false_safe
        and false_safe_rate <= active_false_safe_rate
        and safe_precision >= active_safe_precision
        and candidate_safe_recall > active_safe_recall
    ):
        reasons.append("safe_recall_improved_with_equal_safety")
        return "promote", reasons

    safe_recall_drop = active_safe_recall - candidate_safe_recall
    holdout_coverage_improved = (
        active_holdout_predicted_safe is not None
        and candidate_holdout_predicted_safe is not None
        and candidate_holdout_predicted_safe > active_holdout_predicted_safe
    )
    safety_improved = false_safe_count < active_false_safe or (
        active_holdout_false_safe is not None and holdout_false_safe < active_holdout_false_safe
    )
    if (
        false_safe_count <= active_false_safe
        and false_safe_rate <= active_false_safe_rate
        and safe_precision >= active_safe_precision
        and active_holdout_false_safe is not None
        and holdout_false_safe <= active_holdout_false_safe
        and holdout_false_safe <= 5
        and holdout_false_safe_rate <= 0.01
        and holdout_coverage_improved
        and safe_recall_drop <= max_safe_recall_drop
    ):
        reasons.append(
            "holdout_coverage_improved_with_equal_safety:"
            f"active_predicted_safe={active_holdout_predicted_safe},"
            f"candidate_predicted_safe={candidate_holdout_predicted_safe},"
            f"holdout_false_safe={holdout_false_safe},"
            f"safe_recall_drop={safe_recall_drop:.6f}"
        )
        return "promote", reasons
    if (
        false_safe_count <= active_false_safe
        and false_safe_rate <= active_false_safe_rate
        and safe_precision >= active_safe_precision
        and safety_improved
        and holdout_false_safe <= 5
        and holdout_false_safe_rate <= 0.01
        and safe_recall_drop <= max_safe_recall_drop
    ):
        reasons.append(
            "holdout_strong_safety_with_small_recall_drop:"
            f"holdout_false_safe={holdout_false_safe},"
            f"holdout_false_safe_rate={holdout_false_safe_rate:.6f},"
            f"safe_recall_drop={safe_recall_drop:.6f}"
        )
        return "promote", reasons

    reasons.append("active_model_has_equal_or_better_safety")
    return "do_not_promote", reasons


def model_metrics(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": model["id"],
        "model_version": model["model_version"],
        "dataset_run_id": model["dataset_run_id"],
        "accuracy": model["accuracy"],
        "macro_f1": model["macro_f1"],
        "false_safe_count": model["false_safe_count"],
        "false_safe_rate": model["false_safe_rate"],
        "safe_precision": model["safe_precision"],
        "safe_recall": model["safe_recall"],
        "model_path": model["model_path"],
    }


def record_decision(
    conn,
    candidate: dict[str, Any],
    active: dict[str, Any] | None,
    decision: str,
    reasons: list[str],
    apply: bool,
    created_at: str,
    candidate_score_run: dict[str, Any] | None = None,
    active_score_run: dict[str, Any] | None = None,
) -> None:
    metrics = {
        "candidate": model_metrics(candidate),
        "previous_active": model_metrics(active) if active else None,
        "reasons": reasons,
        "apply": apply,
        "candidate_score_run": candidate_score_run,
        "active_score_run": active_score_run,
    }
    conn.execute(
        """
        INSERT INTO ml_model_promotions (
            model_kind,
            candidate_model_run_id,
            previous_model_run_id,
            decision,
            policy_version,
            reason,
            metrics_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            MODEL_KIND,
            candidate["id"],
            active["id"] if active else None,
            decision,
            POLICY_VERSION,
            "; ".join(reasons),
            json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )
    if apply and decision == "promote":
        conn.execute(
            """
            INSERT INTO ml_model_registry (
                model_kind,
                active_model_run_id,
                active_model_version,
                policy_version,
                promoted_at,
                promoted_by,
                reason,
                metrics_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_kind) DO UPDATE SET
                active_model_run_id = excluded.active_model_run_id,
                active_model_version = excluded.active_model_version,
                policy_version = excluded.policy_version,
                promoted_at = excluded.promoted_at,
                promoted_by = excluded.promoted_by,
                reason = excluded.reason,
                metrics_json = excluded.metrics_json
            """,
            (
                MODEL_KIND,
                candidate["id"],
                candidate["model_version"],
                POLICY_VERSION,
                created_at,
                "codex",
                "; ".join(reasons),
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            ),
        )


def main(
    model_run_id: int | None = None,
    apply: bool = False,
    max_false_safe: int = 0,
    max_false_safe_rate: float = 0.0,
    min_safe_precision: float = 1.0,
    max_holdout_false_safe: int = 0,
    max_holdout_false_safe_rate: float = 0.0,
    min_holdout_safe_precision: float = 1.0,
    max_safe_recall_drop: float = 0.02,
    max_holdout_predicted_safe_drop_rate: float = 0.10,
    max_operational_auto_safe_drop_rate: float = 0.10,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_promote_model] Starting model promotion check")
    print(f"[ml_promote_model] Policy version: {POLICY_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        candidate = fetch_model_run(conn, model_run_id)
        active = fetch_active_model(conn)
        holdout_metrics = latest_holdout_metrics_for_dataset(
            conn,
            int(candidate["dataset_run_id"]),
        )
        active_holdout_metrics = latest_holdout_metrics_for_dataset(
            conn,
            int(active["dataset_run_id"]) if active else None,
        )
        candidate_score_run = fetch_latest_full_score_run(conn, int(candidate["id"]))
        active_score_run = fetch_latest_full_score_run(conn, int(active["id"])) if active else None
        decision, reasons = evaluate_candidate(
            candidate,
            active,
            max_false_safe=max_false_safe,
            max_false_safe_rate=max_false_safe_rate,
            min_safe_precision=min_safe_precision,
            holdout_metrics=holdout_metrics,
            active_holdout_metrics=active_holdout_metrics,
            max_holdout_false_safe=max_holdout_false_safe,
            max_holdout_false_safe_rate=max_holdout_false_safe_rate,
            min_holdout_safe_precision=min_holdout_safe_precision,
            max_safe_recall_drop=max_safe_recall_drop,
            max_holdout_predicted_safe_drop_rate=max_holdout_predicted_safe_drop_rate,
            candidate_score_run=candidate_score_run,
            active_score_run=active_score_run,
            max_operational_auto_safe_drop_rate=max_operational_auto_safe_drop_rate,
        )
        created_at = now()
        record_decision(
            conn,
            candidate,
            active,
            decision,
            reasons,
            apply,
            created_at,
            candidate_score_run=candidate_score_run,
            active_score_run=active_score_run,
        )
        conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "ML model promotion report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Policy version: {POLICY_VERSION}",
        f"Apply: {apply}",
        "",
        "Candidate:",
        f"- id: {candidate['id']}",
        f"- version: {candidate['model_version']}",
        f"- dataset run: {candidate['dataset_run_id']}",
        f"- false safe: {candidate['false_safe_count']}",
        f"- false safe rate: {percent(candidate['false_safe_rate'])}",
        f"- safe precision: {percent(candidate['safe_precision'])}",
        f"- safe recall: {percent(candidate['safe_recall'])}",
        f"- macro f1: {candidate['macro_f1']:.4f}",
    ]
    if candidate_score_run:
        report_lines.extend(
            [
                f"- latest full score run: {candidate_score_run['id']}",
                f"- scored segments: {candidate_score_run['scored_count']}",
                f"- operational auto safe: {candidate_score_run['final_auto_safe_count']} ({percent(score_safe_rate(candidate_score_run))})",
            ]
        )
    else:
        report_lines.append("- latest full score run: missing")
    report_lines.extend(
        [
            "",
            "Previous active:",
        ]
    )
    if active:
        report_lines.extend(
            [
                f"- id: {active['id']}",
                f"- version: {active['model_version']}",
                f"- false safe: {active['false_safe_count']}",
                f"- false safe rate: {percent(active['false_safe_rate'])}",
                f"- safe precision: {percent(active['safe_precision'])}",
                f"- safe recall: {percent(active['safe_recall'])}",
            ]
        )
        if active_score_run:
            report_lines.extend(
                [
                    f"- latest full score run: {active_score_run['id']}",
                    f"- scored segments: {active_score_run['scored_count']}",
                    f"- operational auto safe: {active_score_run['final_auto_safe_count']} ({percent(score_safe_rate(active_score_run))})",
                ]
            )
        else:
            report_lines.append("- latest full score run: missing")
    else:
        report_lines.append("- none")
    report_lines.extend(
        [
            "",
            "Decision:",
            f"- {decision}",
            "",
            "Reasons:",
            *[f"- {reason}" for reason in reasons],
            "",
        "Policy gates:",
        f"- max false safe: {max_false_safe}",
        f"- max false safe rate: {percent(max_false_safe_rate)}",
        f"- min safe precision: {percent(min_safe_precision)}",
        f"- max holdout false safe: {max_holdout_false_safe}",
        f"- max holdout false safe rate: {percent(max_holdout_false_safe_rate)}",
        f"- min holdout safe precision: {percent(min_holdout_safe_precision)}",
        f"- max safe recall drop for strong holdout promotion: {percent(max_safe_recall_drop)}",
        f"- max holdout predicted safe drop rate: {percent(max_holdout_predicted_safe_drop_rate)}",
        f"- max operational auto-safe drop rate: {percent(max_operational_auto_safe_drop_rate)}",
        f"- latest holdout report: {holdout_metrics.get('path') if holdout_metrics else 'missing'}",
        f"- active holdout report: {active_holdout_metrics.get('path') if active_holdout_metrics else 'missing'}",
    ]
    )
    report_path = db.write_report(settings, "ml_promote_model", report_lines)

    print(f"[ml_promote_model] Candidate: {candidate['id']} {candidate['model_version']}")
    print(f"[ml_promote_model] Previous active: {active['id'] if active else 'none'}")
    print(f"[ml_promote_model] Decision: {decision}, apply={apply}")
    print(f"[ml_promote_model] Report: {report_path}")
    print("[ml_promote_model] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote a local ML model if it passes safety gates.")
    parser.add_argument("--model-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-false-safe", type=int, default=0)
    parser.add_argument("--max-false-safe-rate", type=float, default=0.0)
    parser.add_argument("--min-safe-precision", type=float, default=1.0)
    parser.add_argument("--max-holdout-false-safe", type=int, default=0)
    parser.add_argument("--max-holdout-false-safe-rate", type=float, default=0.0)
    parser.add_argument("--min-holdout-safe-precision", type=float, default=1.0)
    parser.add_argument("--max-safe-recall-drop", type=float, default=0.02)
    parser.add_argument("--max-holdout-predicted-safe-drop-rate", type=float, default=0.10)
    parser.add_argument("--max-operational-auto-safe-drop-rate", type=float, default=0.10)
    args = parser.parse_args()
    main(
        model_run_id=args.model_run_id,
        apply=args.apply,
        max_false_safe=args.max_false_safe,
        max_false_safe_rate=args.max_false_safe_rate,
        min_safe_precision=args.min_safe_precision,
        max_holdout_false_safe=args.max_holdout_false_safe,
        max_holdout_false_safe_rate=args.max_holdout_false_safe_rate,
        min_holdout_safe_precision=args.min_holdout_safe_precision,
        max_safe_recall_drop=args.max_safe_recall_drop,
        max_holdout_predicted_safe_drop_rate=args.max_holdout_predicted_safe_drop_rate,
        max_operational_auto_safe_drop_rate=args.max_operational_auto_safe_drop_rate,
    )
