from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

import joblib
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

import db
from low_score_training_patterns import (
    CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN,
    CONTRACT_ES_HELPER_PATTERN,
    CONTRACT_ES_OA_SUFFIX_PATTERN,
    EXACT_SHARED_GLOSSARY_PATTERN,
    OUTSIDE_CUSTOM_GLOSSARY_PATTERN,
    es_helper_matches,
    has_glossary_surface,
    is_contract_es_article_preposition_helper,
    is_contract_es_helper,
    is_contract_es_oa_suffix_only,
    is_custom_exact_shared_glossary,
    is_outside_custom_exact_shared_glossary,
)
from ml_train_risk import (
    DEFAULT_FEATURE_SET,
    make_text,
)


RULE_VERSION = "manual_low_score_calibration_shadow_v1"
SEGMENTED_RULE_VERSION = "manual_low_score_segmented_calibration_shadow_v3"
ORIGIN = "manual_low_score_calibration"
SAFE_LABELS = {"correct", "contextual_exception"}
REVIEW_LABELS = SAFE_LABELS | {"semantic_error", "residual_spanish", "structure_error"}
DEFAULT_CONTROL_RATIO = 0.25
DEFAULT_SEED = "low-score-calibration-control-v1"
DEFAULT_PRIOR_SAFE = 1.0
DEFAULT_PRIOR_RISKY = 1.0
MIN_SEGMENTED_TRAINING_MATCHES = 8
MIN_EXHAUSTIVE_POPULATION_MATCHES = 3


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def split_focus_group(value: str | None) -> tuple[str, str]:
    lane, separator, group = str(value or "").partition(":")
    return lane or "unknown", group if separator and group else "unknown"


def current_score_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT coalesce(candidate_score_run_id, active_score_run_id) AS score_run_id
        FROM segment_state_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None or not row["score_run_id"]:
        raise RuntimeError("No scored segment-state snapshot is available.")
    return int(row["score_run_id"])


def fetch_reviewed_rows(conn, score_run_id: int) -> list[dict[str, Any]]:
    labels = sorted(REVIEW_LABELS)
    placeholders = ",".join("?" for _ in labels)
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT
            candidate.*,
            row_number() OVER (
              PARTITION BY candidate.segment_id, candidate.suggested_hash
              ORDER BY candidate.updated_at DESC, candidate.id DESC
            ) AS row_rank
          FROM local_learning_candidates candidate
          WHERE candidate.origin = ?
            AND candidate.human_label IN ({placeholders})
            AND candidate.local_status IN ('reviewed', 'reviewed_human')
        ),
        token_counts AS (
          SELECT segment_id, count(*) AS token_count
          FROM protected_tokens
          GROUP BY segment_id
        )
        SELECT
          ranked.id AS candidate_id,
          ranked.segment_id,
          ranked.human_label,
          ranked.reason,
          ranked.reviewer,
          ranked.reviewed_at,
          ranked.focus_group,
          ranked.suggested_hash,
          ranked.local_confidence_score AS baseline_probability,
          source.relative_path,
          source.source_key,
          source.source_line_number,
          source.english_text,
          source.spanish_text,
          source.old_text,
          source.has_english,
          source.has_old,
          output.portuguese_text AS output_text,
          score.model_safe_probability AS score_probability,
          score.final_action AS score_final_action,
          score.token_status,
          score.issue_count,
          score.high_issue_count,
          coalesce(token_counts.token_count, 0) AS token_count
        FROM ranked
        JOIN source_segments source
          ON source.id = ranked.segment_id
        JOIN output_segments output
          ON output.segment_id = ranked.segment_id
        LEFT JOIN ml_score_items score
          ON score.segment_id = ranked.segment_id
         AND score.run_id = ?
        LEFT JOIN token_counts
          ON token_counts.segment_id = ranked.segment_id
        WHERE ranked.row_rank = 1
          AND source.is_active = 1
          AND ranked.suggested_text = output.portuguese_text
        ORDER BY ranked.id
        """,
        (ORIGIN, *labels, score_run_id),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        output_hash = hashlib.sha256(str(data["output_text"] or "").encode("utf-8")).hexdigest()
        if output_hash != str(data.get("suggested_hash") or ""):
            continue
        lane, group = split_focus_group(data.get("focus_group"))
        data["calibration_lane"] = lane
        data["calibration_group"] = group
        data["truth_safe"] = data["human_label"] in SAFE_LABELS
        data["candidate_text"] = data["output_text"]
        data["text_length"] = len(str(data["output_text"] or ""))
        data["locked"] = 0
        data["evidence_source"] = "local_learning_candidate"
        result.append(data)
    return result


def _control_sort_key(row: dict[str, Any], seed: str) -> str:
    identity = f"{seed}:{row['candidate_id']}:{row['segment_id']}:{row['suggested_hash']}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def select_control_rows(
    rows: list[dict[str, Any]],
    *,
    ratio: float = DEFAULT_CONTROL_RATIO,
    seed: str = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    if not 0 < ratio < 1:
        raise ValueError("Control ratio must be between zero and one.")
    strata: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[(str(row["calibration_lane"]), bool(row["truth_safe"]))].append(row)

    selected: list[dict[str, Any]] = []
    for stratum_rows in strata.values():
        if len(stratum_rows) < 4:
            continue
        control_count = max(1, round(len(stratum_rows) * ratio))
        control_count = min(control_count, len(stratum_rows) - 1)
        selected.extend(sorted(stratum_rows, key=lambda row: _control_sort_key(row, seed))[:control_count])
    return sorted(selected, key=lambda row: int(row["candidate_id"]))


def _snapshot_key(
    rows: list[dict[str, Any]],
    score_run_id: int,
    ratio: float,
    seed: str,
) -> str:
    payload = {
        "rule_version": RULE_VERSION,
        "score_run_id": score_run_id,
        "ratio": ratio,
        "seed": seed,
        "decisions": [
            [row["candidate_id"], row["segment_id"], row["human_label"], row["suggested_hash"]]
            for row in rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"{RULE_VERSION}:{digest}"


def _counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    labels = Counter(str(row["human_label"]) for row in rows)
    lanes = Counter(str(row["calibration_lane"]) for row in rows)
    groups = Counter(str(row["calibration_group"]) for row in rows)
    truth = Counter("safe" if row["truth_safe"] else "risky" for row in rows)
    return {
        "labels": dict(sorted(labels.items())),
        "lanes": dict(sorted(lanes.items())),
        "groups": dict(sorted(groups.items())),
        "truth": dict(sorted(truth.items())),
    }


def freeze_snapshot(
    *,
    score_run_id: int | None = None,
    ratio: float = DEFAULT_CONTROL_RATIO,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    settings = db.load_settings()
    timestamp = now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        score_run_id = score_run_id or current_score_run_id(conn)
        rows = fetch_reviewed_rows(conn, score_run_id)
        if len(rows) < 20:
            raise RuntimeError("At least 20 current manual decisions are required to freeze a shadow control.")
        control_rows = select_control_rows(rows, ratio=ratio, seed=seed)
        if not control_rows:
            raise RuntimeError("The stratified split did not produce any control rows.")
        control_ids = {int(row["candidate_id"]) for row in control_rows}
        training_rows = [row for row in rows if int(row["candidate_id"]) not in control_ids]
        snapshot_key = _snapshot_key(rows, score_run_id, ratio, seed)

        existing = conn.execute(
            """
            SELECT id, metadata_json
            FROM ml_quality_shadow_runs
            WHERE snapshot_key = ?
            """,
            (snapshot_key,),
        ).fetchone()
        if existing:
            metadata = json.loads(existing["metadata_json"] or "{}")
            return {
                "snapshot_run_id": int(existing["id"]),
                "created": False,
                **metadata,
            }

        metadata = {
            "role": "manual_low_score_calibration_control",
            "origin": ORIGIN,
            "score_run_id": score_run_id,
            "control_ratio": ratio,
            "control_seed": seed,
            "record_count": len(rows),
            "control_count": len(control_rows),
            "training_count": len(training_rows),
            "control_candidate_ids": sorted(control_ids),
            "training_candidate_ids": sorted(int(row["candidate_id"]) for row in training_rows),
            "all_counts": _counts(rows),
            "control_counts": _counts(control_rows),
            "training_counts": _counts(training_rows),
            "operational_writes": False,
        }
        cursor = conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs (
              snapshot_key, source_rule_version, score_run_id,
              record_count, eligible_count, status, metadata_json,
              started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'frozen', ?, ?, ?, ?)
            """,
            (
                snapshot_key,
                RULE_VERSION,
                score_run_id,
                len(rows),
                len(rows),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        snapshot_run_id = int(cursor.lastrowid)
        payloads = []
        for row in rows:
            candidate_id = int(row["candidate_id"])
            role = "control" if candidate_id in control_ids else "training"
            payload = {
                **row,
                "role": role,
                "candidate_id": candidate_id,
                "segment_id": int(row["segment_id"]),
                "truth_safe": bool(row["truth_safe"]),
            }
            payloads.append(
                (
                    snapshot_run_id,
                    int(row["segment_id"]),
                    role,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    timestamp,
                )
            )
        conn.executemany(
            """
            INSERT INTO ml_quality_shadow_items (
              run_id, segment_id, lane, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            payloads,
        )
        conn.commit()
    return {"snapshot_run_id": snapshot_run_id, "created": True, **metadata}


def load_snapshot(conn, snapshot_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = conn.execute(
        """
        SELECT *
        FROM ml_quality_shadow_runs
        WHERE id = ?
          AND source_rule_version = ?
        """,
        (snapshot_run_id, RULE_VERSION),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"Manual calibration shadow snapshot {snapshot_run_id} was not found.")
    rows = []
    for item in conn.execute(
        """
        SELECT id, lane, payload_json
        FROM ml_quality_shadow_items
        WHERE run_id = ?
        ORDER BY id
        """,
        (snapshot_run_id,),
    ):
        payload = json.loads(item["payload_json"] or "{}")
        payload["shadow_item_id"] = int(item["id"])
        payload["role"] = str(item["lane"])
        rows.append(payload)
    return dict(run), rows


def _normalize_candidate_text(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["candidate_text"] = (
        normalized.get("candidate_text")
        if normalized.get("candidate_text") is not None
        else normalized.get("output_text")
    )
    return normalized


def matches_segmented_pattern(
    row: dict[str, Any],
    pattern: str = EXACT_SHARED_GLOSSARY_PATTERN,
) -> bool:
    normalized = _normalize_candidate_text(row)
    if pattern == EXACT_SHARED_GLOSSARY_PATTERN:
        return is_custom_exact_shared_glossary(normalized)
    if pattern == OUTSIDE_CUSTOM_GLOSSARY_PATTERN:
        return is_outside_custom_exact_shared_glossary(normalized)
    if pattern == CONTRACT_ES_HELPER_PATTERN:
        return is_contract_es_helper(normalized)
    if pattern == CONTRACT_ES_OA_SUFFIX_PATTERN:
        return is_contract_es_oa_suffix_only(normalized)
    if pattern == CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN:
        return is_contract_es_article_preposition_helper(normalized)
    raise ValueError(f"Unknown segmented calibration pattern: {pattern}")


def is_segmented_boundary_sentinel(
    row: dict[str, Any],
    pattern: str,
) -> bool:
    normalized = _normalize_candidate_text(row)
    if matches_segmented_pattern(normalized, pattern):
        return False
    if pattern == EXACT_SHARED_GLOSSARY_PATTERN:
        return (
            str(normalized.get("relative_path") or "")
            .replace("\\", "/")
            .startswith("custom_localization/")
            and has_glossary_surface(normalized)
        )
    if pattern == OUTSIDE_CUSTOM_GLOSSARY_PATTERN:
        path = str(normalized.get("relative_path") or "").replace("\\", "/")
        return (
            bool(path)
            and not path.startswith("custom_localization/")
            and has_glossary_surface(normalized)
        )
    if pattern in {
        CONTRACT_ES_HELPER_PATTERN,
        CONTRACT_ES_OA_SUFFIX_PATTERN,
        CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN,
    }:
        return (
            str(normalized.get("calibration_group") or "") == "contracts"
            or bool(es_helper_matches(normalized))
        )
    raise ValueError(f"Unknown segmented calibration pattern: {pattern}")


def is_glossary_boundary_sentinel(row: dict[str, Any]) -> bool:
    return is_segmented_boundary_sentinel(
        row,
        EXACT_SHARED_GLOSSARY_PATTERN,
    )


def segmented_probability(
    training_rows: list[dict[str, Any]],
    *,
    prior_safe: float = DEFAULT_PRIOR_SAFE,
    prior_risky: float = DEFAULT_PRIOR_RISKY,
) -> tuple[float, dict[str, Any]]:
    if prior_safe <= 0 or prior_risky <= 0:
        raise ValueError("Segmented calibration priors must be positive.")
    safe_count = sum(bool(row["truth_safe"]) for row in training_rows)
    risky_count = len(training_rows) - safe_count
    probability = (safe_count + prior_safe) / (
        safe_count + risky_count + prior_safe + prior_risky
    )
    return probability, {
        "training_count": len(training_rows),
        "training_safe_count": safe_count,
        "training_risky_count": risky_count,
        "prior_safe": prior_safe,
        "prior_risky": prior_risky,
        "posterior_safe_probability": probability,
    }


def segmented_shadow_status(*, passed: bool, limited_evidence: bool) -> str:
    if not passed:
        return "rejected"
    return "shadow_ready_limited" if limited_evidence else "shadow_ready"


def has_segmented_boundary_evidence(
    *,
    reviewed_sentinel_count: int,
    population_near_miss_count: int,
) -> bool:
    return reviewed_sentinel_count > 0 or population_near_miss_count > 0


def has_segmented_training_support(
    *,
    training_count: int,
    population_match_count: int,
    reviewed_population_match_count: int,
    reviewed_population_all_safe: bool,
) -> bool:
    if training_count >= MIN_SEGMENTED_TRAINING_MATCHES:
        return True
    return (
        MIN_EXHAUSTIVE_POPULATION_MATCHES
        <= population_match_count
        <= MIN_SEGMENTED_TRAINING_MATCHES
        and reviewed_population_match_count == population_match_count
        and reviewed_population_all_safe
        and training_count > 0
    )


def _score_threshold(conn, score_run_id: int) -> float:
    row = conn.execute(
        """
        SELECT model.safe_threshold
        FROM ml_score_runs score
        JOIN ml_model_runs model
          ON model.id = score.model_run_id
        WHERE score.id = ?
        """,
        (score_run_id,),
    ).fetchone()
    if row is None or row["safe_threshold"] is None:
        return 0.86
    return float(row["safe_threshold"])


def _fetch_segmented_population(
    conn,
    score_run_id: int,
    pattern: str,
) -> list[dict[str, Any]]:
    if pattern == EXACT_SHARED_GLOSSARY_PATTERN:
        path_clause = "source.relative_path LIKE 'custom_localization/%'"
        content_clause = """
          (
            source.english_text LIKE '%Glossary%'
            OR source.spanish_text LIKE '%Glossary%'
            OR output.portuguese_text LIKE '%Glossary%'
          )
        """
    elif pattern == OUTSIDE_CUSTOM_GLOSSARY_PATTERN:
        path_clause = "source.relative_path NOT LIKE 'custom_localization/%'"
        content_clause = """
          (
            source.english_text LIKE '%Glossary%'
            OR source.spanish_text LIKE '%Glossary%'
            OR output.portuguese_text LIKE '%Glossary%'
          )
        """
    elif pattern in {
        CONTRACT_ES_HELPER_PATTERN,
        CONTRACT_ES_OA_SUFFIX_PATTERN,
        CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN,
    }:
        path_clause = "source.relative_path LIKE 'contracts/%'"
        content_clause = "output.portuguese_text LIKE '%Custom(%ES_%'"
    else:
        raise ValueError(f"Unknown segmented calibration pattern: {pattern}")
    rows = conn.execute(
        f"""
        SELECT
          source.id AS segment_id,
          source.relative_path,
          source.source_key,
          source.source_line_number,
          source.english_text,
          source.spanish_text,
          source.old_text,
          output.portuguese_text AS output_text,
          score.model_safe_probability AS baseline_probability,
          score.final_action,
          score.token_status,
          score.issue_count,
          score.high_issue_count
        FROM source_segments source
        JOIN output_segments output
          ON output.segment_id = source.id
        JOIN ml_score_items score
          ON score.segment_id = source.id
         AND score.run_id = ?
        WHERE source.is_active = 1
          AND {path_clause}
          AND {content_clause}
        ORDER BY source.id
        """,
        (score_run_id,),
    ).fetchall()
    population = [_normalize_candidate_text(dict(row)) for row in rows]
    return population


def _segmented_snapshot_key(
    *,
    snapshot_run_id: int,
    score_run_id: int,
    pattern: str,
    training_rows: list[dict[str, Any]],
    prior_safe: float,
    prior_risky: float,
) -> str:
    payload = {
        "rule_version": SEGMENTED_RULE_VERSION,
        "snapshot_run_id": snapshot_run_id,
        "score_run_id": score_run_id,
        "pattern": pattern,
        "prior_safe": prior_safe,
        "prior_risky": prior_risky,
        "training_decisions": [
            [
                row.get("candidate_id"),
                row.get("segment_id"),
                row.get("human_label"),
                row.get("suggested_hash"),
            ]
            for row in training_rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"{SEGMENTED_RULE_VERSION}:{digest}"


def probability_metrics(rows: list[dict[str, Any]], probability_field: str) -> dict[str, Any]:
    truth = [1 if row["truth_safe"] else 0 for row in rows]
    probabilities = [
        min(max(float(row.get(probability_field) or 0.0), 1e-9), 1.0 - 1e-9)
        for row in rows
    ]
    positive = [probability for probability, label in zip(probabilities, truth) if label]
    negative = [probability for probability, label in zip(probabilities, truth) if not label]
    return {
        "count": len(rows),
        "safe_count": sum(truth),
        "risky_count": len(truth) - sum(truth),
        "positive_mean": sum(positive) / len(positive) if positive else None,
        "negative_mean": sum(negative) / len(negative) if negative else None,
        "separation": (
            (sum(positive) / len(positive)) - (sum(negative) / len(negative))
            if positive and negative
            else None
        ),
        "brier": float(brier_score_loss(truth, probabilities)),
        "log_loss": float(log_loss(truth, probabilities, labels=[0, 1])),
        "roc_auc": (
            float(roc_auc_score(truth, probabilities))
            if len(set(truth)) == 2
            else None
        ),
    }


def threshold_metrics(
    rows: list[dict[str, Any]],
    probability_field: str,
    threshold: float,
) -> dict[str, Any]:
    predicted_safe = [float(row.get(probability_field) or 0.0) >= threshold for row in rows]
    true_safe = [bool(row["truth_safe"]) for row in rows]
    predicted_safe_count = sum(predicted_safe)
    actual_safe_count = sum(true_safe)
    false_safe_count = sum(predicted and not truth for predicted, truth in zip(predicted_safe, true_safe))
    true_safe_count = sum(predicted and truth for predicted, truth in zip(predicted_safe, true_safe))
    return {
        "threshold": threshold,
        "predicted_safe": predicted_safe_count,
        "false_safe": false_safe_count,
        "safe_precision": true_safe_count / predicted_safe_count if predicted_safe_count else 1.0,
        "safe_recall": true_safe_count / actual_safe_count if actual_safe_count else 0.0,
    }


def evaluate_snapshot(snapshot_run_id: int, model_run_id: int) -> dict[str, Any]:
    settings = db.load_settings()
    timestamp = now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        run, rows = load_snapshot(conn, snapshot_run_id)
        controls = [row for row in rows if row["role"] == "control"]
        if not controls:
            raise RuntimeError("The frozen snapshot has no control rows.")
        model_run = conn.execute(
            """
            SELECT *
            FROM ml_model_runs
            WHERE id = ?
              AND model_path IS NOT NULL
              AND finished_at IS NOT NULL
            """,
            (model_run_id,),
        ).fetchone()
        if model_run is None:
            raise RuntimeError(f"Trained model run {model_run_id} was not found.")
        model_run = dict(model_run)
        bundle = joblib.load(db.project_path(model_run["model_path"]))
        model = bundle["model"]
        model_metadata = bundle.get("metadata", {})
        feature_set = str(model_metadata.get("feature_set") or DEFAULT_FEATURE_SET)
        threshold = float(model_metadata.get("safe_threshold") or model_run["safe_threshold"] or 0.90)
        classes = list(model.named_steps["classifier"].classes_)
        for row in controls:
            row.setdefault("candidate_text", row.get("output_text"))
            row.setdefault("text_length", len(str(row.get("candidate_text") or "")))
            row.setdefault("locked", 0)
            row.setdefault("evidence_source", "local_learning_candidate")
        texts = [make_text(row, feature_set=feature_set) for row in controls]
        probabilities_by_row = model.predict_proba(texts)
        auto_safe_index = classes.index("auto_safe")
        for row, probabilities in zip(controls, probabilities_by_row):
            row["candidate_probability"] = float(probabilities[auto_safe_index])

        baseline = probability_metrics(controls, "baseline_probability")
        candidate = probability_metrics(controls, "candidate_probability")
        baseline_threshold = threshold_metrics(controls, "baseline_probability", threshold)
        candidate_threshold = threshold_metrics(controls, "candidate_probability", threshold)
        by_lane: dict[str, Any] = {}
        by_group: dict[str, Any] = {}
        for dimension, target in (
            ("calibration_lane", by_lane),
            ("calibration_group", by_group),
        ):
            values = sorted({str(row[dimension]) for row in controls})
            for value in values:
                subset = [row for row in controls if str(row[dimension]) == value]
                target[value] = {
                    "baseline": probability_metrics(subset, "baseline_probability"),
                    "candidate": probability_metrics(subset, "candidate_probability"),
                }

        evaluation = {
            "model_run_id": model_run_id,
            "dataset_run_id": int(model_run["dataset_run_id"]),
            "model_version": model_run["model_version"],
            "feature_set": feature_set,
            "safe_threshold": threshold,
            "control_count": len(controls),
            "baseline": baseline,
            "candidate": candidate,
            "baseline_threshold": baseline_threshold,
            "candidate_threshold": candidate_threshold,
            "delta": {
                "positive_mean": (
                    candidate["positive_mean"] - baseline["positive_mean"]
                    if candidate["positive_mean"] is not None and baseline["positive_mean"] is not None
                    else None
                ),
                "negative_mean": (
                    candidate["negative_mean"] - baseline["negative_mean"]
                    if candidate["negative_mean"] is not None and baseline["negative_mean"] is not None
                    else None
                ),
                "separation": (
                    candidate["separation"] - baseline["separation"]
                    if candidate["separation"] is not None and baseline["separation"] is not None
                    else None
                ),
                "brier": candidate["brier"] - baseline["brier"],
                "log_loss": candidate["log_loss"] - baseline["log_loss"],
            },
            "by_lane": by_lane,
            "by_group": by_group,
            "operational_writes": False,
            "evaluated_at": timestamp,
        }

        metadata = json.loads(run["metadata_json"] or "{}")
        metadata["evaluation"] = evaluation
        conn.execute(
            """
            UPDATE ml_quality_shadow_runs
            SET status = 'evaluated',
                metadata_json = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
                snapshot_run_id,
            ),
        )
        for row in controls:
            item = conn.execute(
                """
                SELECT payload_json
                FROM ml_quality_shadow_items
                WHERE id = ?
                """,
                (row["shadow_item_id"],),
            ).fetchone()
            payload = json.loads(item["payload_json"] or "{}")
            payload["candidate_probability"] = row["candidate_probability"]
            payload["candidate_model_run_id"] = model_run_id
            conn.execute(
                """
                UPDATE ml_quality_shadow_items
                SET payload_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    row["shadow_item_id"],
                ),
            )
        conn.commit()
    return {"snapshot_run_id": snapshot_run_id, **evaluation}


def evaluate_segmented_snapshot(
    snapshot_run_id: int,
    *,
    pattern: str = EXACT_SHARED_GLOSSARY_PATTERN,
    prior_safe: float = DEFAULT_PRIOR_SAFE,
    prior_risky: float = DEFAULT_PRIOR_RISKY,
) -> dict[str, Any]:
    settings = db.load_settings()
    timestamp = now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        snapshot_run, snapshot_rows = load_snapshot(conn, snapshot_run_id)
        score_run_id = int(snapshot_run["score_run_id"])
        threshold = _score_threshold(conn, score_run_id)
        normalized_rows = [_normalize_candidate_text(row) for row in snapshot_rows]
        training_rows = [row for row in normalized_rows if row["role"] == "training"]
        control_rows = [row for row in normalized_rows if row["role"] == "control"]
        matched_training = [
            row for row in training_rows if matches_segmented_pattern(row, pattern)
        ]
        matched_controls = [
            row for row in control_rows if matches_segmented_pattern(row, pattern)
        ]
        boundary_sentinels = [
            row
            for row in normalized_rows
            if is_segmented_boundary_sentinel(row, pattern)
        ]
        if not matched_training:
            raise RuntimeError(
                "The segmented pattern has no reviewed training example "
                "in the frozen snapshot."
            )
        if not matched_controls:
            raise RuntimeError(
                "The segmented pattern has no blind control example "
                "in the frozen snapshot."
            )
        calibrated_probability, posterior = segmented_probability(
            matched_training,
            prior_safe=prior_safe,
            prior_risky=prior_risky,
        )

        for row in control_rows:
            current_probability = row.get("score_probability")
            if current_probability is None:
                current_probability = row.get("baseline_probability")
            row["current_probability"] = float(current_probability or 0.0)
            row["segmented_probability"] = (
                calibrated_probability
                if matches_segmented_pattern(row, pattern)
                else row["current_probability"]
            )
        for row in boundary_sentinels:
            current_probability = row.get("score_probability")
            if current_probability is None:
                current_probability = row.get("baseline_probability")
            row["current_probability"] = float(current_probability or 0.0)
            row["segmented_probability"] = row["current_probability"]

        baseline = probability_metrics(control_rows, "current_probability")
        candidate = probability_metrics(control_rows, "segmented_probability")
        matched_baseline = probability_metrics(matched_controls, "current_probability")
        matched_candidate = probability_metrics(
            matched_controls,
            "segmented_probability",
        )
        baseline_threshold = threshold_metrics(
            control_rows,
            "current_probability",
            threshold,
        )
        candidate_threshold = threshold_metrics(
            control_rows,
            "segmented_probability",
            threshold,
        )
        sentinel_changed_count = sum(
            abs(
                float(row["segmented_probability"])
                - float(row["current_probability"])
            )
            > 1e-12
            for row in boundary_sentinels
        )

        population = _fetch_segmented_population(conn, score_run_id, pattern)
        reviewed_by_segment = {
            int(row["segment_id"]): row
            for row in normalized_rows
            if row.get("segment_id") is not None
        }
        exact_population = [
            row for row in population if matches_segmented_pattern(row, pattern)
        ]
        near_miss_population = [
            row for row in population if not matches_segmented_pattern(row, pattern)
        ]
        for row in population:
            is_match = matches_segmented_pattern(row, pattern)
            row["pattern_match"] = is_match
            row["segmented_probability"] = (
                calibrated_probability
                if is_match
                else float(row.get("baseline_probability") or 0.0)
            )
            reviewed = reviewed_by_segment.get(int(row["segment_id"]))
            if reviewed:
                row["review_role"] = reviewed.get("role")
                row["human_label"] = reviewed.get("human_label")
                row["truth_safe"] = bool(reviewed.get("truth_safe"))
        population_sentinel_changed_count = sum(
            abs(
                float(row["segmented_probability"])
                - float(row.get("baseline_probability") or 0.0)
            )
            > 1e-12
            for row in near_miss_population
        )
        reviewed_exact_population = [
            row for row in exact_population if "truth_safe" in row
        ]
        reviewed_population_all_safe = bool(reviewed_exact_population) and all(
            bool(row["truth_safe"]) for row in reviewed_exact_population
        )

        gates = {
            "training_support": has_segmented_training_support(
                training_count=len(matched_training),
                population_match_count=len(exact_population),
                reviewed_population_match_count=len(
                    reviewed_exact_population
                ),
                reviewed_population_all_safe=reviewed_population_all_safe,
            ),
            "training_has_no_risky_label": all(
                bool(row["truth_safe"]) for row in matched_training
            ),
            "blind_control_present": bool(matched_controls),
            "blind_control_has_no_risky_label": all(
                bool(row["truth_safe"]) for row in matched_controls
            ),
            "blind_control_false_safe_zero": candidate_threshold["false_safe"] == 0,
            "boundary_sentinel_present": has_segmented_boundary_evidence(
                reviewed_sentinel_count=len(boundary_sentinels),
                population_near_miss_count=len(near_miss_population),
            ),
            "boundary_sentinel_unchanged": (
                sentinel_changed_count == 0
                and population_sentinel_changed_count == 0
            ),
            "population_has_no_integrity_issue": all(
                int(row.get("issue_count") or 0) == 0
                and int(row.get("high_issue_count") or 0) == 0
                and str(row.get("token_status") or "").lower() == "ok"
                for row in exact_population
            ),
            "population_is_deterministically_safe": all(
                str(row.get("final_action") or "").lower() == "auto_safe"
                for row in exact_population
            ),
        }
        passed = all(gates.values())
        failed_gates = [
            gate_name for gate_name, gate_passed in gates.items() if not gate_passed
        ]
        limited_evidence = (
            len(matched_controls) < 3
            or len(matched_training) < MIN_SEGMENTED_TRAINING_MATCHES
        )
        status = segmented_shadow_status(
            passed=passed,
            limited_evidence=limited_evidence,
        )
        snapshot_key = _segmented_snapshot_key(
            snapshot_run_id=snapshot_run_id,
            score_run_id=score_run_id,
            pattern=pattern,
            training_rows=matched_training,
            prior_safe=prior_safe,
            prior_risky=prior_risky,
        )
        evaluation = {
            "role": "segmented_manual_low_score_calibration",
            "base_snapshot_run_id": snapshot_run_id,
            "pattern": pattern,
            "score_run_id": score_run_id,
            "safe_threshold": threshold,
            "status": status,
            "limited_evidence": limited_evidence,
            "failed_gates": failed_gates,
            "posterior": posterior,
            "reviewed_training_match_count": len(matched_training),
            "reviewed_control_match_count": len(matched_controls),
            "reviewed_boundary_sentinel_count": len(boundary_sentinels),
            "population_count": len(population),
            "population_match_count": len(exact_population),
            "population_reviewed_match_count": len(
                reviewed_exact_population
            ),
            "population_reviewed_matches_all_safe": (
                reviewed_population_all_safe
            ),
            "population_near_miss_count": len(near_miss_population),
            "population_near_miss_changed_count": (
                population_sentinel_changed_count
            ),
            "population_match_issue_count": sum(
                int(row.get("issue_count") or 0) > 0
                or int(row.get("high_issue_count") or 0) > 0
                for row in exact_population
            ),
            "population_match_auto_safe_count": sum(
                str(row.get("final_action") or "").lower() == "auto_safe"
                for row in exact_population
            ),
            "baseline": baseline,
            "candidate": candidate,
            "matched_control_baseline": matched_baseline,
            "matched_control_candidate": matched_candidate,
            "baseline_threshold": baseline_threshold,
            "candidate_threshold": candidate_threshold,
            "delta": {
                "brier": candidate["brier"] - baseline["brier"],
                "log_loss": candidate["log_loss"] - baseline["log_loss"],
                "separation": (
                    candidate["separation"] - baseline["separation"]
                    if candidate["separation"] is not None
                    and baseline["separation"] is not None
                    else None
                ),
                "matched_control_brier": (
                    matched_candidate["brier"] - matched_baseline["brier"]
                ),
                "matched_control_log_loss": (
                    matched_candidate["log_loss"] - matched_baseline["log_loss"]
                ),
            },
            "boundary_sentinel_changed_count": sentinel_changed_count,
            "gates": gates,
            "operational_writes": False,
            "evaluated_at": timestamp,
        }

        existing = conn.execute(
            """
            SELECT id, metadata_json
            FROM ml_quality_shadow_runs
            WHERE snapshot_key = ?
            """,
            (snapshot_key,),
        ).fetchone()
        if existing:
            metadata = json.loads(existing["metadata_json"] or "{}")
            return {
                "segmented_shadow_run_id": int(existing["id"]),
                "created": False,
                **metadata,
            }

        cursor = conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs (
              snapshot_key, source_rule_version, score_run_id,
              record_count, eligible_count, status, metadata_json,
              started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_key,
                SEGMENTED_RULE_VERSION,
                score_run_id,
                len(population),
                len(exact_population),
                status,
                json.dumps(evaluation, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        segmented_shadow_run_id = int(cursor.lastrowid)
        item_payloads = []
        for row in population:
            payload = {
                **row,
                "segment_id": int(row["segment_id"]),
                "operational_write": False,
            }
            item_payloads.append(
                (
                    segmented_shadow_run_id,
                    int(row["segment_id"]),
                    "exact_match" if row["pattern_match"] else "near_miss",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    timestamp,
                )
            )
        conn.executemany(
            """
            INSERT INTO ml_quality_shadow_items (
              run_id, segment_id, lane, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            item_payloads,
        )
        conn.commit()
    return {
        "segmented_shadow_run_id": segmented_shadow_run_id,
        "created": True,
        **evaluation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze and evaluate a leakage-safe manual low-score calibration shadow."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--score-run-id", type=int, default=None)
    freeze_parser.add_argument("--control-ratio", type=float, default=DEFAULT_CONTROL_RATIO)
    freeze_parser.add_argument("--seed", default=DEFAULT_SEED)
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--snapshot-run-id", type=int, required=True)
    evaluate_parser.add_argument("--model-run-id", type=int, required=True)
    segmented_parser = subparsers.add_parser("segment-evaluate")
    segmented_parser.add_argument("--snapshot-run-id", type=int, required=True)
    segmented_parser.add_argument(
        "--pattern",
        default=EXACT_SHARED_GLOSSARY_PATTERN,
    )
    segmented_parser.add_argument(
        "--prior-safe",
        type=float,
        default=DEFAULT_PRIOR_SAFE,
    )
    segmented_parser.add_argument(
        "--prior-risky",
        type=float,
        default=DEFAULT_PRIOR_RISKY,
    )
    args = parser.parse_args()

    if args.mode == "freeze":
        result = freeze_snapshot(
            score_run_id=args.score_run_id,
            ratio=args.control_ratio,
            seed=args.seed,
        )
    elif args.mode == "evaluate":
        result = evaluate_snapshot(args.snapshot_run_id, args.model_run_id)
    else:
        result = evaluate_segmented_snapshot(
            args.snapshot_run_id,
            pattern=args.pattern,
            prior_safe=args.prior_safe,
            prior_risky=args.prior_risky,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
