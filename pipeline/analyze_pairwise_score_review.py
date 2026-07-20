from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

import joblib

import db
import ml_score_segments
from ml_train_risk import DEFAULT_FEATURE_SET


RULE_VERSION = "pairwise_score_review_analysis_v1"
DEFAULT_EVIDENCE_TYPE = "deterministic_token_punctuation_boundary_repair"
DEFAULT_OLD_SCORE_RUN_ID = 367
DEFAULT_NEW_SCORE_RUN_ID = 368
SCORE_EPSILON = 0.0001


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback


def quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def score_outcome(delta: float) -> str:
    if delta > SCORE_EPSILON:
        return "improved"
    if delta < -SCORE_EPSILON:
        return "regressed"
    return "equal"


def regression_magnitude(delta: float) -> str:
    if delta <= -0.01:
        return "material_ge_1pp"
    if delta <= -0.002:
        return "moderate_0_2_to_1pp"
    if delta < -SCORE_EPSILON:
        return "small_lt_0_2pp"
    return "not_regressed"


def token_kind(token: str) -> str:
    if token.startswith("$"):
        return "dollar_token"
    if token.startswith("["):
        return "bracket_token"
    if token == "#!":
        return "hashbang_token"
    if token.startswith("@"):
        return "at_token"
    return "other_token"


def issue_codes(value: Any) -> list[str]:
    parsed = parse_json(value, [])
    if not isinstance(parsed, list):
        return []
    return sorted(
        {
            str(item.get("code") or item.get("issue_code"))
            for item in parsed
            if isinstance(item, dict) and (item.get("code") or item.get("issue_code"))
        }
    )


def load_score_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise RuntimeError(f"Score run {run_id} was not found.")
    return dict(row)


def load_rows(
    conn: sqlite3.Connection,
    *,
    evidence_type: str,
    old_score_run_id: int,
    new_score_run_id: int,
) -> list[dict[str, Any]]:
    result = conn.execute(
        """
        WITH latest_evidence AS (
          SELECT evidence.*
          FROM ml_pairwise_quality_evidence evidence
          WHERE evidence.evidence_type = ?
            AND evidence.id = (
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
          evidence.baseline_text,
          evidence.candidate_text,
          evidence.baseline_score_raw AS evidence_baseline_score,
          evidence.candidate_score_raw AS evidence_candidate_score,
          evidence.pairwise_score,
          evidence.pairwise_delta,
          evidence.calibration_band,
          evidence.recommended_route,
          evidence.token_integrity_ok,
          evidence.post_validation_clean,
          evidence.training_eligible,
          evidence.promotion_eligible,
          evidence.source_metadata_json,
          old_score.candidate_text AS old_score_text,
          old_score.model_safe_probability AS old_score,
          old_score.model_action AS old_model_action,
          old_score.final_action AS old_final_action,
          old_score.risk_class AS old_risk_class,
          old_score.token_status AS old_token_status,
          old_score.issue_count AS old_issue_count,
          old_score.high_issue_count AS old_high_issue_count,
          old_score.medium_issue_count AS old_medium_issue_count,
          old_score.word_count AS old_word_count,
          old_score.issues_json AS old_issues_json,
          old_score.reasons_json AS old_reasons_json,
          new_score.candidate_text AS new_score_text,
          new_score.model_safe_probability AS new_score,
          new_score.model_action AS new_model_action,
          new_score.final_action AS new_final_action,
          new_score.risk_class AS new_risk_class,
          new_score.token_status AS new_token_status,
          new_score.issue_count AS new_issue_count,
          new_score.high_issue_count AS new_high_issue_count,
          new_score.medium_issue_count AS new_medium_issue_count,
          new_score.word_count AS new_word_count,
          new_score.issues_json AS new_issues_json,
          new_score.reasons_json AS new_reasons_json,
          output.portuguese_text AS output_text,
          confirmation.confirmed_text,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked AS confirmation_locked,
          source.english_text,
          source.spanish_text,
          source.old_text AS source_old_text,
          source.has_english,
          source.has_old,
          source.source_line_number,
          COALESCE(token_count.token_count, 0) AS token_count
        FROM latest_evidence evidence
        JOIN ml_score_items old_score
          ON old_score.segment_id = evidence.segment_id
         AND old_score.run_id = ?
        JOIN ml_score_items new_score
          ON new_score.segment_id = evidence.segment_id
         AND new_score.run_id = ?
        LEFT JOIN output_segments output ON output.segment_id = evidence.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = evidence.segment_id
        JOIN source_segments source ON source.id = evidence.segment_id
        LEFT JOIN (
          SELECT token.segment_id, COUNT(*) AS token_count
          FROM protected_tokens token
          JOIN latest_evidence selected ON selected.segment_id = token.segment_id
          GROUP BY token.segment_id
        ) token_count ON token_count.segment_id = evidence.segment_id
        ORDER BY evidence.segment_id
        """,
        (evidence_type, old_score_run_id, new_score_run_id),
    ).fetchall()
    return [dict(row) for row in result]


def enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        metadata = parse_json(row.get("source_metadata_json"), {})
        repair_samples = metadata.get("repair_samples") if isinstance(metadata, dict) else []
        repair_samples = repair_samples if isinstance(repair_samples, list) else []
        tokens = [str(item.get("token") or "") for item in repair_samples if isinstance(item, dict)]
        punctuations = [
            str(item.get("punctuation") or "")
            for item in repair_samples
            if isinstance(item, dict)
        ]
        token_kinds = sorted({token_kind(token) for token in tokens if token})
        old_score = float(row.get("old_score") or 0.0)
        new_score = float(row.get("new_score") or 0.0)
        raw_delta = new_score - old_score
        old_issues = issue_codes(row.get("old_issues_json"))
        new_issues = issue_codes(row.get("new_issues_json"))
        relative_path = str(row.get("relative_path") or "")
        path_group = relative_path.split("/", 1)[0] if "/" in relative_path else "root"
        enriched.append(
            {
                **row,
                "source_metadata_json": None,
                "raw_delta": round(raw_delta, 6),
                "raw_delta_pp": round(raw_delta * 100.0, 4),
                "score_outcome": score_outcome(raw_delta),
                "regression_magnitude": regression_magnitude(raw_delta),
                "path_group": path_group,
                "repair_count": int(metadata.get("repair_count") or len(repair_samples))
                if isinstance(metadata, dict)
                else len(repair_samples),
                "tokens": tokens,
                "token_kinds": token_kinds,
                "punctuations": punctuations,
                "old_issue_codes": old_issues,
                "new_issue_codes": new_issues,
                "removed_target_issue": (
                    "space_before_punctuation" in old_issues
                    and "space_before_punctuation" not in new_issues
                ),
                "issue_count_delta": int(row.get("new_issue_count") or 0)
                - int(row.get("old_issue_count") or 0),
                "action_changed": row.get("old_final_action") != row.get("new_final_action"),
                "risk_changed": row.get("old_risk_class") != row.get("new_risk_class"),
                "baseline_matches_old_score_text": row.get("baseline_text")
                == row.get("old_score_text"),
                "candidate_matches_new_score_text": row.get("candidate_text")
                == row.get("new_score_text"),
                "candidate_matches_output": row.get("candidate_text") == row.get("output_text"),
                "candidate_matches_confirmation": row.get("candidate_text")
                == row.get("confirmed_text"),
            }
        )
    return enriched


def score_counterfactuals(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    model_run_id: int,
) -> dict[str, Any]:
    model_run_row = conn.execute(
        "SELECT * FROM ml_model_runs WHERE id = ?",
        (model_run_id,),
    ).fetchone()
    if not model_run_row:
        raise RuntimeError(f"Model run {model_run_id} was not found.")
    model_run = dict(model_run_row)
    bundle = joblib.load(db.project_path(str(model_run["model_path"])))
    model = bundle["model"]
    feature_set = str(bundle.get("feature_set") or DEFAULT_FEATURE_SET)
    threshold = float(model_run.get("safe_threshold") or 0.90)

    def context(row: dict[str, Any], candidate: str, output: str, source: str) -> dict[str, Any]:
        return {
            "segment_id": int(row["segment_id"]),
            "relative_path": str(row.get("relative_path") or ""),
            "source_key": str(row.get("source_key") or ""),
            "source_line_number": row.get("source_line_number"),
            "english_text": str(row.get("english_text") or ""),
            "spanish_text": str(row.get("spanish_text") or ""),
            "old_text": str(row.get("source_old_text") or ""),
            "has_english": int(row.get("has_english") or 0),
            "has_old": int(row.get("has_old") or 0),
            "output_text": output,
            "candidate_text": candidate,
            "candidate_text_source": source,
            "confirmation_level": row.get("confirmation_level"),
            "locked": int(row.get("confirmation_locked") or 0),
            "confirmation_confidence": None,
            "token_count": int(row.get("token_count") or 0),
            "text_length": len(candidate),
        }

    actual_old_rows = [
        context(row, str(row["baseline_text"]), str(row["candidate_text"]), "old")
        for row in rows
    ]
    actual_new_rows = [
        context(row, str(row["candidate_text"]), str(row["candidate_text"]), "output")
        for row in rows
    ]
    symmetric_old_rows = [
        context(row, str(row["baseline_text"]), str(row["baseline_text"]), "old")
        for row in rows
    ]
    symmetric_new_rows = actual_new_rows
    fixed_anchor_candidate_rows = [
        context(row, str(row["candidate_text"]), str(row["baseline_text"]), "output")
        for row in rows
    ]

    def probabilities(items: list[dict[str, Any]]) -> list[float]:
        predictions = ml_score_segments.model_predictions(model, items, threshold, feature_set)
        return [float(item[1]) for item in predictions]

    actual_old = probabilities(actual_old_rows)
    actual_new = probabilities(actual_new_rows)
    symmetric_old = probabilities(symmetric_old_rows)
    symmetric_new = probabilities(symmetric_new_rows)
    fixed_anchor_new = probabilities(fixed_anchor_candidate_rows)
    max_recompute_error = 0.0
    for index, row in enumerate(rows):
        stored_old = float(row.get("old_score") or 0.0)
        stored_new = float(row.get("new_score") or 0.0)
        max_recompute_error = max(
            max_recompute_error,
            abs(actual_old[index] - stored_old),
            abs(actual_new[index] - stored_new),
        )
        symmetric_delta = symmetric_new[index] - symmetric_old[index]
        fixed_anchor_delta = fixed_anchor_new[index] - symmetric_old[index]
        row.update(
            {
                "recomputed_old_score": round(actual_old[index], 6),
                "recomputed_new_score": round(actual_new[index], 6),
                "symmetric_old_score": round(symmetric_old[index], 6),
                "symmetric_new_score": round(symmetric_new[index], 6),
                "symmetric_delta": round(symmetric_delta, 6),
                "symmetric_outcome": score_outcome(symmetric_delta),
                "fixed_anchor_old_score": round(symmetric_old[index], 6),
                "fixed_anchor_new_score": round(fixed_anchor_new[index], 6),
                "fixed_anchor_delta": round(fixed_anchor_delta, 6),
                "fixed_anchor_outcome": score_outcome(fixed_anchor_delta),
            }
        )
    return {
        "model_run_id": model_run_id,
        "model_version": model_run.get("model_version"),
        "feature_set": feature_set,
        "safe_threshold": threshold,
        "max_stored_score_recompute_error": round(max_recompute_error, 9),
    }


def grouped_outcomes(rows: list[dict[str, Any]], field: str, limit: int = 20) -> list[dict[str, Any]]:
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        raw_value = row.get(field)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        values = values or ["unknown"]
        for value in values:
            groups[str(value or "unknown")][str(row["score_outcome"])] += 1
    result = []
    for value, counts in groups.items():
        total = sum(counts.values())
        result.append(
            {
                field: value,
                "total": total,
                "improved": counts["improved"],
                "regressed": counts["regressed"],
                "equal": counts["equal"],
                "non_improving_rate": round(
                    (counts["regressed"] + counts["equal"]) / total, 6
                ),
            }
        )
    return sorted(result, key=lambda item: (-item["total"], str(item[field])))[:limit]


def summarize(
    rows: list[dict[str, Any]],
    old_run: dict[str, Any],
    new_run: dict[str, Any],
    evidence_type: str,
) -> dict[str, Any]:
    outcomes = Counter(str(row["score_outcome"]) for row in rows)
    deltas = [float(row["raw_delta"]) for row in rows]
    regressions = [row for row in rows if row["score_outcome"] == "regressed"]
    equals = [row for row in rows if row["score_outcome"] == "equal"]
    non_improving = regressions + equals
    regression_deltas = [float(row["raw_delta"]) for row in regressions]
    magnitude_counts = Counter(str(row["regression_magnitude"]) for row in regressions)
    issue_transitions = Counter(
        f"{row['old_issue_count']}->{row['new_issue_count']}" for row in rows
    )
    action_transitions = Counter(
        f"{row['old_final_action']}->{row['new_final_action']}" for row in rows
    )
    risk_transitions = Counter(
        f"{row['old_risk_class']}->{row['new_risk_class']}" for row in rows
    )
    fixed_pairwise_delta_count = sum(
        abs(float(row.get("pairwise_delta") or 0.0) - 0.02) <= 0.000001 for row in rows
    )
    evidence_raw_equal_count = sum(
        abs(
            float(row.get("evidence_candidate_score") or 0.0)
            - float(row.get("evidence_baseline_score") or 0.0)
        )
        <= 0.000001
        for row in rows
    )
    invariants = {
        "baseline_matches_old_score_text": sum(
            bool(row["baseline_matches_old_score_text"]) for row in rows
        ),
        "candidate_matches_new_score_text": sum(
            bool(row["candidate_matches_new_score_text"]) for row in rows
        ),
        "candidate_matches_output": sum(bool(row["candidate_matches_output"]) for row in rows),
        "candidate_matches_confirmation": sum(
            bool(row["candidate_matches_confirmation"]) for row in rows
        ),
        "token_integrity_ok": sum(bool(row.get("token_integrity_ok")) for row in rows),
        "post_validation_clean": sum(bool(row.get("post_validation_clean")) for row in rows),
        "target_issue_removed": sum(bool(row["removed_target_issue"]) for row in rows),
    }
    counterfactual_available = all(row.get("symmetric_outcome") for row in rows)
    counterfactuals: dict[str, Any] = {}
    if counterfactual_available:
        stored_non_improving_ids = {
            int(row["segment_id"])
            for row in rows
            if row["score_outcome"] in {"regressed", "equal"}
        }

        def counterfactual_summary(prefix: str) -> dict[str, Any]:
            result = Counter(str(row[f"{prefix}_outcome"]) for row in rows)
            deltas = [float(row[f"{prefix}_delta"]) for row in rows]
            still_non_improving = sum(
                int(row["segment_id"]) in stored_non_improving_ids
                and row[f"{prefix}_outcome"] in {"regressed", "equal"}
                for row in rows
            )
            return {
                "improved": result["improved"],
                "regressed": result["regressed"],
                "equal": result["equal"],
                "non_improving": result["regressed"] + result["equal"],
                "stored_non_improving_still_non_improving": still_non_improving,
                "delta_min": min(deltas),
                "delta_median": median(deltas),
                "delta_mean": mean(deltas),
                "delta_max": max(deltas),
            }

        counterfactuals = {
            "symmetric_package_context": counterfactual_summary("symmetric"),
            "fixed_old_output_anchor": counterfactual_summary("fixed_anchor"),
        }
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "evidence_type": evidence_type,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cohort": {
            "evidence_count": len(rows),
            "old_score_run_id": int(old_run["id"]),
            "new_score_run_id": int(new_run["id"]),
            "same_model_run": old_run.get("model_run_id") == new_run.get("model_run_id"),
            "same_model_version": old_run.get("model_version") == new_run.get("model_version"),
            "old_candidate_text_source": old_run.get("candidate_text_source"),
            "new_candidate_text_source": new_run.get("candidate_text_source"),
            "old_tree_hash": old_run.get("candidate_tree_hash"),
            "new_tree_hash": new_run.get("candidate_tree_hash"),
        },
        "outcomes": {
            "improved": outcomes["improved"],
            "regressed": outcomes["regressed"],
            "equal": outcomes["equal"],
            "non_improving": outcomes["regressed"] + outcomes["equal"],
            "improved_rate": round(outcomes["improved"] / len(rows), 6) if rows else None,
            "non_improving_rate": round(
                (outcomes["regressed"] + outcomes["equal"]) / len(rows), 6
            )
            if rows
            else None,
        },
        "raw_delta_distribution": {
            "min": min(deltas) if deltas else None,
            "p10": quantile(deltas, 0.10),
            "p25": quantile(deltas, 0.25),
            "median": median(deltas) if deltas else None,
            "mean": mean(deltas) if deltas else None,
            "p75": quantile(deltas, 0.75),
            "p90": quantile(deltas, 0.90),
            "max": max(deltas) if deltas else None,
        },
        "regression_delta_distribution": {
            "min": min(regression_deltas) if regression_deltas else None,
            "p25": quantile(regression_deltas, 0.25),
            "median": median(regression_deltas) if regression_deltas else None,
            "mean": mean(regression_deltas) if regression_deltas else None,
            "p75": quantile(regression_deltas, 0.75),
            "max": max(regression_deltas) if regression_deltas else None,
            "magnitude_counts": dict(magnitude_counts),
        },
        "evidence_contract": {
            "fixed_plus_2pp_count": fixed_pairwise_delta_count,
            "evidence_raw_candidate_equals_baseline_count": evidence_raw_equal_count,
            "fixed_plus_2pp_rate": round(fixed_pairwise_delta_count / len(rows), 6)
            if rows
            else None,
        },
        "counterfactuals": counterfactuals,
        "invariants": invariants,
        "issue_transitions": dict(issue_transitions),
        "action_transitions": dict(action_transitions),
        "risk_transitions": dict(risk_transitions),
        "changed_action_count": sum(bool(row["action_changed"]) for row in rows),
        "changed_risk_count": sum(bool(row["risk_changed"]) for row in rows),
        "non_improving_issue_removed_count": sum(
            bool(row["removed_target_issue"]) for row in non_improving
        ),
        "breakdowns": {
            "by_punctuation": grouped_outcomes(rows, "punctuations"),
            "by_token_kind": grouped_outcomes(rows, "token_kinds"),
            "by_path_group": grouped_outcomes(rows, "path_group"),
            "by_repair_count": grouped_outcomes(rows, "repair_count"),
        },
        "worst_regressions": [
            {
                key: row.get(key)
                for key in (
                    "segment_id",
                    "relative_path",
                    "source_key",
                    "old_score",
                    "new_score",
                    "raw_delta",
                    "raw_delta_pp",
                    "repair_count",
                    "token_kinds",
                    "punctuations",
                    "old_issue_codes",
                    "new_issue_codes",
                    "old_final_action",
                    "new_final_action",
                    "old_risk_class",
                    "new_risk_class",
                    "baseline_text",
                    "candidate_text",
                )
            }
            for row in sorted(regressions, key=lambda item: float(item["raw_delta"]))[:20]
        ],
        "equal_samples": [
            {
                key: row.get(key)
                for key in (
                    "segment_id",
                    "relative_path",
                    "source_key",
                    "old_score",
                    "new_score",
                    "raw_delta",
                    "repair_count",
                    "token_kinds",
                    "punctuations",
                    "old_issue_codes",
                    "new_issue_codes",
                    "baseline_text",
                    "candidate_text",
                )
            }
            for row in equals[:20]
        ],
    }


def write_reports(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, str]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_pairwise_score_review_analysis"
    summary_path = base.with_name(base.name + "_summary.json")
    jsonl_path = base.with_suffix(".jsonl")
    markdown_path = base.with_suffix(".md")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    outcomes = summary["outcomes"]
    regression = summary["regression_delta_distribution"]
    invariants = summary["invariants"]
    lines = [
        "# Pairwise score review analysis",
        "",
        f"- Cohort: `{summary['cohort']['evidence_count']}`",
        f"- Improved/regressed/equal: `{outcomes['improved']}/{outcomes['regressed']}/{outcomes['equal']}`",
        f"- Non-improving: `{outcomes['non_improving']}` ({outcomes['non_improving_rate']:.2%})",
        f"- Median regression: `{float(regression['median'] or 0) * 100:.4f} p.p.`",
        f"- Worst regression: `{float(regression['min'] or 0) * 100:.4f} p.p.`",
        f"- Target issue removed: `{invariants['target_issue_removed']}`",
        f"- Candidate matches output/confirmation: `{invariants['candidate_matches_output']}/{invariants['candidate_matches_confirmation']}`",
        "",
        "## Evidence contract",
        "",
        (
            f"- Fixed +2 p.p. pairwise calibration: "
            f"`{summary['evidence_contract']['fixed_plus_2pp_count']}`"
        ),
        (
            "- Evidence-time raw candidate equals baseline: "
            f"`{summary['evidence_contract']['evidence_raw_candidate_equals_baseline_count']}`"
        ),
        "",
        "## Regression magnitude",
        "",
    ]
    lines.extend(
        f"- `{name}`: `{count}`"
        for name, count in regression["magnitude_counts"].items()
    )
    lines.extend(["", "## Interpretation", ""])
    lines.append(
        "This report separates the general model score from deterministic pairwise preference. "
        "It does not treat the fixed +2 p.p. calibration as an observed probability gain."
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "summary": str(summary_path),
        "jsonl": str(jsonl_path),
        "markdown": str(markdown_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze raw score regressions/equalities after an applied pairwise repair."
    )
    parser.add_argument("--evidence-type", default=DEFAULT_EVIDENCE_TYPE)
    parser.add_argument("--old-score-run-id", type=int, default=DEFAULT_OLD_SCORE_RUN_ID)
    parser.add_argument("--new-score-run-id", type=int, default=DEFAULT_NEW_SCORE_RUN_ID)
    parser.add_argument("--skip-counterfactual", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        old_run = load_score_run(conn, args.old_score_run_id)
        new_run = load_score_run(conn, args.new_score_run_id)
        rows = enrich_rows(
            load_rows(
                conn,
                evidence_type=args.evidence_type,
                old_score_run_id=args.old_score_run_id,
                new_score_run_id=args.new_score_run_id,
            )
        )
        counterfactual_model = None
        if not args.skip_counterfactual:
            if int(old_run["model_run_id"]) != int(new_run["model_run_id"]):
                raise RuntimeError("Counterfactual scoring requires the same model run.")
            counterfactual_model = score_counterfactuals(
                conn,
                rows,
                int(old_run["model_run_id"]),
            )
    if not rows:
        raise RuntimeError("No pairwise evidence matched both score runs.")
    summary = summarize(rows, old_run, new_run, args.evidence_type)
    if counterfactual_model:
        summary["counterfactual_model"] = counterfactual_model
    if not args.no_write:
        summary["artifacts"] = write_reports(summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
