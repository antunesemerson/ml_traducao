from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from typing import Any

import db
from ml_specialist_models import SPECIALIST_GROUPS, SPECIALISTS, SpecialistConfig


RULE_VERSION = "ml_specialist_policy_v1"
REVIEW_SOURCES = ("ml_specialist_auditor", "ml_specialist_scope_review")
OPERATIONAL_SPECIALIST_KEYS = [
    "culture_title_labels",
    "religion",
    "title_adjectives",
    "title_baronies",
    "title_counties",
    "title_cultural_names",
    "title_duchies",
    "title_empires",
    "title_kingdoms",
    "title_names",
    "title_prefixes",
]


SNAPSHOT_COLUMNS = [
    "rule_version",
    "general_score_run_id",
    "specialist_key",
    "model_kind",
    "model_run_id",
    "model_version",
    "dataset_run_id",
    "score_run_id",
    "operational_threshold",
    "policy_min_threshold",
    "threshold_below_policy",
    "scope_description",
    "scope_sql",
    "scope_active_count",
    "scored_count",
    "compared_count",
    "scope_delta_count",
    "scope_coverage_rate",
    "final_auto_safe_count",
    "needs_human_count",
    "needs_autofix_count",
    "blocked_structure_count",
    "model_direct_auto_safe_count",
    "deterministic_promoted_auto_safe_count",
    "deterministic_demoted_auto_safe_count",
    "final_auto_safe_rate",
    "model_direct_auto_safe_rate",
    "specialist_new_safe_count",
    "specialist_demoted_count",
    "pending_new_safe_count",
    "pending_demoted_count",
    "reviewed_new_safe_count",
    "reviewed_demoted_count",
    "divergent_count",
    "reviewed_divergent_count",
    "pending_real_count",
    "missing_general_count",
    "divergence_rate",
    "pending_real_rate",
    "status",
    "recommended_action",
    "notes",
    "created_at",
    "updated_at",
]


def percent(part: int | float | None, total: int | float | None) -> str:
    if not total:
        return "0.00%"
    return f"{float(part or 0) / float(total):.2%}"


def rate(part: int | float | None, total: int | float | None) -> float | None:
    if not total:
        return None
    return float(part or 0) / float(total)


def compact_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def latest_general_score_run(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            r.*,
            m.model_kind
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE m.model_kind = 'risk_action_classifier'
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def score_run_info(conn, score_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            r.*,
            m.model_kind
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE r.id = ?
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        """,
        (score_run_id,),
    ).fetchone()
    return dict(row) if row else None


def latest_model_for_kind(conn, model_kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind = ?
          AND model_path IS NOT NULL
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (model_kind,),
    ).fetchone()
    return dict(row) if row else None


def latest_score_for_kind(conn, model_kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            r.*,
            m.model_kind,
            m.dataset_run_id,
            m.safe_threshold AS model_safe_threshold
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE m.model_kind = ?
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT 1
        """,
        (model_kind,),
    ).fetchone()
    return dict(row) if row else None


def score_candidates_for_kind(conn, model_kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            r.*,
            m.id AS candidate_model_id,
            m.model_kind,
            m.dataset_run_id,
            m.safe_threshold AS model_safe_threshold,
            m.false_safe_count AS model_false_safe_count,
            m.predicted_safe_count AS model_predicted_safe_count,
            m.safe_precision AS model_safe_precision
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE m.model_kind = ?
          AND m.model_path IS NOT NULL
          AND m.finished_at IS NOT NULL
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        """,
        (model_kind,),
    ).fetchall()
    return [dict(row) for row in rows]


def qualify_scope_sql(scope_sql: str) -> str:
    qualified = re.sub(r"(?<![\w.])relative_path(?!\w)", "s.relative_path", scope_sql)
    qualified = re.sub(r"(?<![\w.])source_key(?!\w)", "s.source_key", qualified)
    return qualified


def active_scope_count(conn, config: SpecialistConfig) -> int:
    qualified_scope_sql = qualify_scope_sql(config.scope_sql)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND coalesce(o.portuguese_text, sc.confirmed_text, s.old_text, s.spanish_text, '') <> ''
          AND NOT (
              sc.confirmation_level IN ('human_confirmed', 'human')
              AND sc.locked = 1
          )
          AND ({qualified_scope_sql})
        """
    ).fetchone()
    return int(row["total"] or 0) if row else 0


def nested_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        results: list[str] = []
        for item in value:
            results.extend(nested_strings(item))
        return results
    if isinstance(value, dict):
        results = []
        for item in value.values():
            results.extend(nested_strings(item))
        return results
    return []


def threshold_from_score_items(conn, score_run_id: int, fallback: float | None) -> float | None:
    rows = conn.execute(
        """
        SELECT reasons_json
        FROM ml_score_items
        WHERE run_id = ?
          AND reasons_json LIKE '%safe_threshold:%'
        LIMIT 20
        """,
        (score_run_id,),
    ).fetchall()
    for row in rows:
        raw = row["reasons_json"]
        try:
            payload = json.loads(raw or "[]")
        except json.JSONDecodeError:
            payload = raw
        for text in nested_strings(payload):
            match = re.search(r"safe_threshold:([0-9]+(?:\.[0-9]+)?)", text)
            if match:
                return float(match.group(1))
    return fallback


def compare_with_general(
    conn,
    general_score_run_id: int | None,
    specialist_score_run_id: int | None,
) -> dict[str, int]:
    if general_score_run_id is None or specialist_score_run_id is None:
        return {
            "compared_count": 0,
            "specialist_new_safe_count": 0,
            "specialist_demoted_count": 0,
            "divergent_count": 0,
            "reviewed_divergent_count": 0,
            "pending_real_count": 0,
        }

    row = conn.execute(
        """
        WITH reviewed AS (
            SELECT segment_id, COUNT(*) AS reviewed_count
            FROM local_learning_candidates
            WHERE queue_source IN (?, ?)
              AND local_status = 'reviewed_human'
            GROUP BY segment_id
        ),
        compared AS (
            SELECT
                s.segment_id,
                g.final_action AS general_action,
                s.final_action AS specialist_action,
                COALESCE(r.reviewed_count, 0) AS reviewed_count,
                CASE
                    WHEN g.final_action <> 'auto_safe' AND s.final_action = 'auto_safe' THEN 1
                    ELSE 0
                END AS specialist_new_safe,
                CASE
                    WHEN g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe' THEN 1
                    ELSE 0
                END AS specialist_demoted
            FROM ml_score_items s
            JOIN ml_score_items g
              ON g.segment_id = s.segment_id
             AND g.run_id = ?
            LEFT JOIN reviewed r ON r.segment_id = s.segment_id
            WHERE s.run_id = ?
        )
        SELECT
            COUNT(*) AS compared_count,
            SUM(specialist_new_safe) AS specialist_new_safe_count,
            SUM(specialist_demoted) AS specialist_demoted_count,
            SUM(CASE WHEN specialist_new_safe = 1 AND reviewed_count = 0 THEN 1 ELSE 0 END) AS pending_new_safe_count,
            SUM(CASE WHEN specialist_demoted = 1 AND reviewed_count = 0 THEN 1 ELSE 0 END) AS pending_demoted_count,
            SUM(CASE WHEN specialist_new_safe = 1 AND reviewed_count > 0 THEN 1 ELSE 0 END) AS reviewed_new_safe_count,
            SUM(CASE WHEN specialist_demoted = 1 AND reviewed_count > 0 THEN 1 ELSE 0 END) AS reviewed_demoted_count,
            SUM(CASE WHEN specialist_new_safe = 1 OR specialist_demoted = 1 THEN 1 ELSE 0 END) AS divergent_count,
            SUM(
                CASE
                    WHEN (specialist_new_safe = 1 OR specialist_demoted = 1)
                     AND reviewed_count > 0 THEN 1
                    ELSE 0
                END
            ) AS reviewed_divergent_count,
            SUM(
                CASE
                    WHEN (specialist_new_safe = 1 OR specialist_demoted = 1)
                     AND reviewed_count = 0 THEN 1
                    ELSE 0
                END
            ) AS pending_real_count
        FROM compared
        """,
        (*REVIEW_SOURCES, general_score_run_id, specialist_score_run_id),
    ).fetchone()
    if row is None:
        return {}
    return {key: int(row[key] or 0) for key in row.keys()}


def decide_status(
    latest_model: dict[str, Any] | None,
    latest_score: dict[str, Any] | None,
    compared: dict[str, int],
    missing_general_count: int,
    scope_delta_count: int,
    threshold_below_policy: bool,
) -> tuple[str, str]:
    if latest_model is None:
        return "NO_MODEL", "train_specialist_model"
    if int(latest_model.get("false_safe_count") or 0) > 0:
        return "MODEL_SAFETY_FAIL", "add_negative_reviews_or_raise_threshold"
    predicted_safe_count = int(latest_model.get("predicted_safe_count") or 0)
    safe_precision = latest_model.get("safe_precision")
    if predicted_safe_count > 0 and safe_precision is not None and float(safe_precision) + 0.000001 < 1.0:
        return "MODEL_SAFETY_FAIL", "add_negative_reviews_or_raise_threshold"
    if latest_score is None:
        return "NO_SCORE", "score_latest_specialist_model"
    if int(latest_score["model_run_id"]) != int(latest_model["id"]):
        return "STALE_SCORE", "score_latest_specialist_model"
    if threshold_below_policy:
        return "THRESHOLD_BELOW_POLICY", "rescore_with_policy_threshold"
    if missing_general_count > 0:
        return "NEEDS_GENERAL_RESCORE", "run_full_general_score_before_policy"
    if scope_delta_count != 0:
        return "SCOPE_DRIFT", "rescore_current_specialist_scope"
    if compared.get("pending_new_safe_count", 0) > 0:
        return "NEEDS_REVIEW_EXPANSION", "review_pending_new_safe_divergences"
    if compared.get("pending_demoted_count", 0) > 0:
        return "READY_CONSERVATIVE_AUDIT", "review_demoted_false_safe_samples"
    if compared.get("divergent_count", 0) > 0:
        return "READY_REVIEWED_DIVERGENCES", "eligible_for_dry_run_policy"
    return "READY_CLEAN", "eligible_for_dry_run_policy"


def resolve_requested_specialists(requested: str) -> list[tuple[str, SpecialistConfig]]:
    if requested == "all":
        keys = OPERATIONAL_SPECIALIST_KEYS
    else:
        keys = SPECIALIST_GROUPS.get(requested, [requested])
    return [(key, SPECIALISTS[key]) for key in keys if key in SPECIALISTS]


def build_snapshot(
    conn,
    specialist_key: str,
    config: SpecialistConfig,
    general_score_run_id: int | None,
    created_at: str,
) -> dict[str, Any]:
    raw_latest_model = latest_model_for_kind(conn, config.name)
    raw_latest_score = latest_score_for_kind(conn, config.name)
    scope_count = active_scope_count(conn, config)

    def evaluate(model: dict[str, Any] | None, score: dict[str, Any] | None, strategy: str) -> dict[str, Any]:
        compared = compare_with_general(
            conn,
            general_score_run_id=general_score_run_id,
            specialist_score_run_id=int(score["id"]) if score else None,
        )
        scored_count = int(score["scored_count"] or 0) if score else 0
        compared_count = int(compared.get("compared_count") or 0)
        missing_general_count = max(scored_count - compared_count, 0) if score else 0
        scope_delta_count = scored_count - scope_count

        fallback_threshold = None
        if score and score.get("model_safe_threshold") is not None:
            fallback_threshold = float(score["model_safe_threshold"])
        elif model and model.get("safe_threshold") is not None:
            fallback_threshold = float(model["safe_threshold"])
        threshold = (
            threshold_from_score_items(conn, int(score["id"]), fallback_threshold)
            if score
            else fallback_threshold
        )
        threshold_below_policy = bool(
            score
            and threshold is not None
            and threshold + 0.000001 < float(config.default_safe_threshold)
        )
        status, action = decide_status(
            model,
            score,
            compared,
            missing_general_count,
            scope_delta_count,
            threshold_below_policy,
        )

        final_auto_safe = int(score["final_auto_safe_count"] or 0) if score else 0
        model_direct = int(score["model_direct_auto_safe_count"] or 0) if score else 0
        divergent_count = int(compared.get("divergent_count") or 0)
        pending_real = int(compared.get("pending_real_count") or 0)
        notes = {
            "latest_model_id": raw_latest_model["id"] if raw_latest_model else None,
            "latest_score_id": raw_latest_score["id"] if raw_latest_score else None,
            "latest_score_model_id": raw_latest_score["model_run_id"] if raw_latest_score else None,
            "score_model_is_latest": (
                bool(model and score and int(score["model_run_id"]) == int(raw_latest_model["id"]))
                if raw_latest_model
                else False
            ),
            "selection_strategy": strategy,
        }
        return {
            "rule_version": RULE_VERSION,
            "general_score_run_id": general_score_run_id,
            "specialist_key": specialist_key,
            "model_kind": config.name,
            "model_run_id": int(score["model_run_id"]) if score else (model["id"] if model else None),
            "model_version": score["model_version"] if score else (model["model_version"] if model else None),
            "dataset_run_id": (
                int(score["dataset_run_id"])
                if score and score.get("dataset_run_id") is not None
                else (int(model["dataset_run_id"]) if model and model.get("dataset_run_id") else None)
            ),
            "score_run_id": int(score["id"]) if score else None,
            "operational_threshold": threshold,
            "policy_min_threshold": float(config.default_safe_threshold),
            "threshold_below_policy": int(threshold_below_policy),
            "scope_description": config.description,
            "scope_sql": config.scope_sql,
            "scope_active_count": scope_count,
            "scored_count": scored_count,
            "compared_count": compared_count,
            "scope_delta_count": scope_delta_count,
            "scope_coverage_rate": rate(scored_count, scope_count),
            "final_auto_safe_count": final_auto_safe,
            "needs_human_count": int(score["needs_human_count"] or 0) if score else 0,
            "needs_autofix_count": int(score["needs_autofix_count"] or 0) if score else 0,
            "blocked_structure_count": int(score["blocked_structure_count"] or 0) if score else 0,
            "model_direct_auto_safe_count": model_direct,
            "deterministic_promoted_auto_safe_count": (
                int(score["deterministic_promoted_auto_safe_count"] or 0) if score else 0
            ),
            "deterministic_demoted_auto_safe_count": (
                int(score["deterministic_demoted_auto_safe_count"] or 0) if score else 0
            ),
            "final_auto_safe_rate": rate(final_auto_safe, scored_count),
            "model_direct_auto_safe_rate": rate(model_direct, scored_count),
            "specialist_new_safe_count": int(compared.get("specialist_new_safe_count") or 0),
            "specialist_demoted_count": int(compared.get("specialist_demoted_count") or 0),
            "pending_new_safe_count": int(compared.get("pending_new_safe_count") or 0),
            "pending_demoted_count": int(compared.get("pending_demoted_count") or 0),
            "reviewed_new_safe_count": int(compared.get("reviewed_new_safe_count") or 0),
            "reviewed_demoted_count": int(compared.get("reviewed_demoted_count") or 0),
            "divergent_count": divergent_count,
            "reviewed_divergent_count": int(compared.get("reviewed_divergent_count") or 0),
            "pending_real_count": pending_real,
            "missing_general_count": missing_general_count,
            "divergence_rate": rate(divergent_count, compared_count),
            "pending_real_rate": rate(pending_real, compared_count),
            "status": status,
            "recommended_action": action,
            "notes": json.dumps(notes, ensure_ascii=False, sort_keys=True),
            "created_at": created_at,
            "updated_at": created_at,
        }

    candidates = []
    for score in score_candidates_for_kind(conn, config.name):
        model = {
            "id": score["candidate_model_id"],
            "model_version": score["model_version"],
            "dataset_run_id": score["dataset_run_id"],
            "safe_threshold": score["model_safe_threshold"],
            "false_safe_count": score["model_false_safe_count"],
            "predicted_safe_count": score["model_predicted_safe_count"],
            "safe_precision": score["model_safe_precision"],
        }
        candidates.append(evaluate(model, score, "best_ready_score"))

    ready_candidates = [
        row
        for row in candidates
        if str(row["status"]).startswith("READY")
        and int(row["pending_real_count"] or 0) == 0
        and int(row["threshold_below_policy"] or 0) == 0
        and int(row["scope_delta_count"] or 0) == 0
        and int(row["missing_general_count"] or 0) == 0
    ]
    if ready_candidates:
        selected = max(
            ready_candidates,
            key=lambda row: (
                int(row["final_auto_safe_count"] or 0),
                int(row["model_direct_auto_safe_count"] or 0),
                int(row["score_run_id"] or 0),
            ),
        )
        notes = json.loads(selected["notes"])
        notes["ready_candidate_count"] = len(ready_candidates)
        selected["notes"] = json.dumps(notes, ensure_ascii=False, sort_keys=True)
        return selected

    return evaluate(raw_latest_model, raw_latest_score, "latest_fallback")


def insert_snapshots(conn, snapshots: list[dict[str, Any]]) -> None:
    placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
    conn.executemany(
        f"""
        INSERT INTO ml_specialist_policy_snapshots (
            {", ".join(SNAPSHOT_COLUMNS)}
        )
        VALUES ({placeholders})
        """,
        [tuple(snapshot.get(column) for column in SNAPSHOT_COLUMNS) for snapshot in snapshots],
    )


def report_lines(
    snapshots: list[dict[str, Any]],
    started_at: datetime,
    elapsed: Any,
    general_score: dict[str, Any] | None,
    requested: str,
) -> list[str]:
    ready = [row for row in snapshots if str(row["status"]).startswith("READY_")]
    needs_review = [row for row in snapshots if row["status"] == "NEEDS_REVIEW_EXPANSION"]
    conservative_audit = [row for row in snapshots if row["status"] == "READY_CONSERVATIVE_AUDIT"]
    stale = [row for row in snapshots if row["status"] == "STALE_SCORE"]
    scope_drift = [row for row in snapshots if row["status"] == "SCOPE_DRIFT"]
    threshold_low = [row for row in snapshots if row["status"] == "THRESHOLD_BELOW_POLICY"]
    safety_fail = [row for row in snapshots if row["status"] == "MODEL_SAFETY_FAIL"]
    missing = [row for row in snapshots if row["status"] in {"NO_MODEL", "NO_SCORE", "NEEDS_GENERAL_RESCORE"}]
    total_scored = sum(int(row["scored_count"] or 0) for row in snapshots)
    total_auto = sum(int(row["final_auto_safe_count"] or 0) for row in snapshots)
    total_direct = sum(int(row["model_direct_auto_safe_count"] or 0) for row in snapshots)
    total_pending = sum(int(row["pending_real_count"] or 0) for row in snapshots)
    total_pending_new_safe = sum(int(row["pending_new_safe_count"] or 0) for row in snapshots)
    total_pending_demoted = sum(int(row["pending_demoted_count"] or 0) for row in snapshots)
    lines = [
        "ML specialist policy report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Requested specialists: {requested}",
        "",
        "General score reference:",
    ]
    if general_score:
        lines.extend(
            [
                f"- Score run id: {general_score['id']}",
                f"- Model run id: {general_score['model_run_id']}",
                f"- Model version: {general_score['model_version']}",
                f"- Scored: {general_score['scored_count']}",
                f"- Final auto_safe: {general_score['final_auto_safe_count']} ({percent(general_score['final_auto_safe_count'], general_score['scored_count'])})",
            ]
        )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Operational summary:",
            f"- Specialists inspected: {len(snapshots)}",
            f"- Ready: {len(ready)}",
            f"- Need expansion review: {len(needs_review)}",
            f"- Conservative false-safe audit: {len(conservative_audit)}",
            f"- Stale score: {len(stale)}",
            f"- Threshold below policy: {len(threshold_low)}",
            f"- Scope drift: {len(scope_drift)}",
            f"- Model safety fail: {len(safety_fail)}",
            f"- Missing/blocked: {len(missing)}",
            f"- Specialist scored rows, summed by scope: {total_scored}",
            f"- Specialist final auto_safe, summed by scope: {total_auto} ({percent(total_auto, total_scored)})",
            f"- Specialist model-direct auto_safe, summed by scope: {total_direct} ({percent(total_direct, total_scored)})",
            f"- Real pending divergences: {total_pending}",
            f"- Pending new-safe expansions: {total_pending_new_safe}",
            f"- Pending conservative demotions: {total_pending_demoted}",
            "- Scope totals use the score-eligible universe: active rows with candidate text, excluding locked human confirmations.",
            "- Scope totals can overlap when composite and sub-specialists are selected together.",
            "- Default all tracks operational leaf specialists; composite parent specialists can be requested explicitly.",
            "",
            "Specialist rows:",
        ]
    )
    for row in snapshots:
        lines.append(
            "- {specialist_key} | status={status} | model={model_run_id} | score={score_run_id} | "
            "thr={threshold}/min={policy_min_threshold:.4f} | "
            "scored={scored_count}/{scope_active_count} eligible ({coverage}, delta={scope_delta_count}) | "
            "auto={final_auto_safe_count} ({auto_rate}) | direct={model_direct_auto_safe_count} ({direct_rate}) | "
            "div={divergent_count} new={specialist_new_safe_count}/p{pending_new_safe_count} "
            "demoted={specialist_demoted_count}/p{pending_demoted_count} "
            "reviewed={reviewed_divergent_count} | "
            "action={recommended_action}".format(
                **{
                    **row,
                    "threshold": compact_float(row["operational_threshold"]),
                    "coverage": percent(row["scored_count"], row["scope_active_count"]),
                    "auto_rate": percent(row["final_auto_safe_count"], row["scored_count"]),
                    "direct_rate": percent(row["model_direct_auto_safe_count"], row["scored_count"]),
                }
            )
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- READY_* means the latest specialist score has no unreviewed divergence against the general score reference.",
            "- NEEDS_REVIEW_EXPANSION means the specialist wants to mark extra rows safe and those rows need review first.",
            "- READY_CONSERVATIVE_AUDIT means the specialist is only more cautious than the general model; review demotions as false-safe research.",
            "- THRESHOLD_BELOW_POLICY means the score used a looser threshold than this specialist policy allows.",
            "- MODEL_SAFETY_FAIL means the latest specialist model produced at least one false-safe or lost perfect safe precision in its internal evaluation.",
            "- STALE_SCORE means a newer specialist model exists and needs a fresh score run.",
            "- SCOPE_DRIFT means the eligible scope changed after the score and should be rescored.",
            "- This command does not apply output, change confirmations, or promote models.",
        ]
    )
    return lines


def main(
    general_score_run_id: int | None = None,
    specialist: str = "all",
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    created_at = started_at.isoformat(timespec="seconds")
    print("[ml_specialist_policy] Starting specialist policy snapshot")
    print(f"[ml_specialist_policy] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        general_score = score_run_info(conn, general_score_run_id) if general_score_run_id else latest_general_score_run(conn)
        if general_score is None and general_score_run_id is not None:
            raise RuntimeError(f"General score run {general_score_run_id} is missing, unfinished, or empty.")
        resolved_general_score_run_id = int(general_score["id"]) if general_score else None
        requested = resolve_requested_specialists(specialist)
        snapshots = [
            build_snapshot(conn, key, config, resolved_general_score_run_id, created_at)
            for key, config in requested
        ]
        insert_snapshots(conn, snapshots)
        conn.commit()

    elapsed = datetime.now() - started_at
    lines = report_lines(snapshots, started_at, elapsed, general_score, specialist)
    report_path = db.write_report(settings, "ml_specialist_policy", lines)
    ready = sum(1 for row in snapshots if str(row["status"]).startswith("READY_"))
    pending = sum(int(row["pending_real_count"] or 0) for row in snapshots)
    print(f"[ml_specialist_policy] Specialists inspected: {len(snapshots)}")
    print(f"[ml_specialist_policy] Ready specialists: {ready}")
    print(f"[ml_specialist_policy] Real pending divergences: {pending}")
    print(f"[ml_specialist_policy] Report: {report_path}")
    print("[ml_specialist_policy] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snapshot operational policy readiness for specialist models.")
    parser.add_argument("--general-score-run-id", type=int, default=None)
    parser.add_argument("--specialist", choices=sorted({*SPECIALIST_GROUPS, *SPECIALISTS}), default="all")
    args = parser.parse_args()
    main(
        general_score_run_id=args.general_score_run_id,
        specialist=args.specialist,
    )
