from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

import db
import local_quality_validator
import ml_score_segments
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "quality_missing_space_after_token_shadow_v1"
ISSUE_CODE = "missing_space_after_token"
BOUNDARY_RE = re.compile(
    r"(?P<token>\[[^\]\r\n]+\]|\$[A-Za-z0-9_]+\$|#!)"
    r"(?P<next>[A-Za-z\u00c0-\u00ff])"
)
ES_CUSTOM_RE = re.compile(r"Custom\(\s*['\"]ES_", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def preview(value: Any, limit: int = 360) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def issue_codes(value: Any) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        str(item.get("code") or item.get("issue_code"))
        for item in parsed
        if isinstance(item, dict) and (item.get("code") or item.get("issue_code"))
    }


def latest_full_output_score_run(
    conn: sqlite3.Connection,
    requested_id: int | None,
) -> dict[str, Any]:
    if requested_id is not None:
        row = conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (requested_id,)).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM ml_score_runs
            WHERE candidate_text_source = 'output'
              AND finished_at IS NOT NULL
              AND limit_count IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("No completed full output score run was found.")
    result = dict(row)
    if str(result.get("candidate_text_source") or "") != "output":
        raise RuntimeError("Selected score run does not measure output text.")
    return result


def boundary_kind(token: str, next_char: str) -> str:
    if ES_CUSTOM_RE.search(token):
        return "gender_custom_ambiguous"
    if token == "#!":
        return "safe_style_close"
    if token.startswith("$") and next_char.isupper():
        # CK3's own English and Spanish localization place display text
        # immediately after layout macros such as $EFFECT_LIST_BULLET$.
        return "source_convention_no_space"
    if token.startswith("[") and next_char.isupper():
        # Scripted loc functions may already emit their own separator (for
        # example ConcatIfNeitherEmpty(..., ' ')). Static insertion is unsafe.
        return "dynamic_spacing_ambiguous"
    if token.startswith("[") and next_char.casefold() == "s":
        return "plural_suffix_ambiguous"
    return "lowercase_or_other_ambiguous"


def inspect_boundaries(text: str) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for match in BOUNDARY_RE.finditer(text):
        token = match.group("token")
        next_char = match.group("next")
        boundaries.append(
            {
                "start": match.start(),
                "end": match.end(),
                "token": token,
                "next_char": next_char,
                "kind": boundary_kind(token, next_char),
                "context": preview(text[max(0, match.start() - 60) : match.end() + 60], 140),
            }
        )
    return boundaries


def insert_safe_spaces(text: str, boundaries: list[dict[str, Any]]) -> str:
    if not boundaries or not all(str(item["kind"]).startswith("safe_") for item in boundaries):
        return text
    return BOUNDARY_RE.sub(lambda match: f"{match.group('token')} {match.group('next')}", text)


def load_context_rows(
    conn: sqlite3.Connection,
    segment_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
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
          source.has_english,
          source.has_old,
          output.portuguese_text AS output_text,
          confirmation.confirmation_level,
          COALESCE(confirmation.locked, 0) AS locked,
          confirmation.confidence_score AS confirmation_confidence,
          COALESCE(tokens.token_count, 0) AS token_count
        FROM source_segments source
        LEFT JOIN output_segments output ON output.segment_id = source.id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = source.id
        LEFT JOIN (
          SELECT segment_id, COUNT(*) AS token_count
          FROM protected_tokens
          GROUP BY segment_id
        ) tokens ON tokens.segment_id = source.id
        WHERE source.id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def load_score_rows(
    conn: sqlite3.Connection,
    score_run_id: int,
    threshold: float,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM ml_score_items score
            WHERE score.run_id = ?
              AND score.model_safe_probability < ?
              AND EXISTS (
                SELECT 1
                FROM json_each(score.issues_json) issue
                WHERE json_extract(issue.value, '$.code') = ?
              )
            ORDER BY score.model_safe_probability ASC, score.segment_id ASC
            """,
            (score_run_id, threshold, ISSUE_CODE),
        ).fetchall()
    ]


def build_records(
    conn: sqlite3.Connection,
    score_run: dict[str, Any],
    threshold: float,
) -> list[dict[str, Any]]:
    score_rows = load_score_rows(conn, int(score_run["id"]), threshold)
    context_rows = load_context_rows(conn, [int(row["segment_id"]) for row in score_rows])

    model_run_id = int(score_run.get("model_run_id") or 0)
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = bundle.get("metadata", {}).get("feature_set") or ml_score_segments.DEFAULT_FEATURE_SET
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)

    eligible: list[tuple[dict[str, Any], dict[str, Any], str, str, list[dict[str, Any]]]] = []
    records: list[dict[str, Any]] = []
    for score_row in score_rows:
        segment_id = int(score_row["segment_id"])
        context = context_rows.get(segment_id) or {}
        original = str(score_row.get("candidate_text") or context.get("output_text") or "")
        boundaries = inspect_boundaries(original)
        codes = issue_codes(score_row.get("issues_json"))
        blockers: list[str] = []
        if not boundaries:
            blockers.append("no_detected_boundary")
        ambiguous = sorted(
            {str(item["kind"]) for item in boundaries if not str(item["kind"]).startswith("safe_")}
        )
        blockers.extend(ambiguous)
        if codes != {ISSUE_CODE}:
            blockers.append("other_issue_codes")
        if int(context.get("locked") or 0):
            blockers.append("human_locked_confirmation")

        candidate = insert_safe_spaces(original, boundaries)
        if candidate == original:
            blockers.append("no_change")
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        if not token_ok:
            blockers.append("token_signature_changed")
        post_validation = local_quality_validator.validate_text(candidate)
        post_codes = [str(item.get("code")) for item in post_validation.get("issues") or []]
        if post_codes:
            blockers.append("post_validation_issue")

        base_record = {
            "source": RULE_VERSION,
            "score_run_id": int(score_run["id"]),
            "model_run_id": model_run_id,
            "segment_id": segment_id,
            "relative_path": score_row.get("relative_path"),
            "source_key": score_row.get("source_key"),
            "raw_current_score": round(float(score_row.get("model_safe_probability") or 0.0), 6),
            "original_preview": preview(original),
            "candidate_preview": preview(candidate),
            "boundary_count": len(boundaries),
            "boundary_kinds": dict(Counter(str(item["kind"]) for item in boundaries)),
            "boundary_samples": boundaries[:8],
            "pre_issue_codes": sorted(codes),
            "post_issue_codes": post_codes,
            "token_integrity_ok": token_ok,
            "human_locked": bool(int(context.get("locked") or 0)),
            "blockers": sorted(set(blockers)),
            "candidate_generation_only": True,
            "ready_for_apply": False,
            "output_changed": False,
        }
        if blockers:
            base_record.update(
                {
                    "lane": "blocked_or_context",
                    "raw_candidate_score": None,
                    "raw_score_delta": None,
                    "calibrated_candidate_score": None,
                    "calibrated_score_delta": None,
                }
            )
            records.append(base_record)
            continue

        scoring_row = dict(context)
        scoring_row.update(
            {
                "candidate_text": candidate,
                "candidate_text_source": RULE_VERSION,
                "text_length": len(candidate),
            }
        )
        eligible.append((score_row, scoring_row, original, candidate, boundaries))
        records.append(base_record)

    predictions = ml_score_segments.model_predictions(
        model,
        [item[1] for item in eligible],
        safe_threshold,
        feature_set,
    )
    eligible_by_segment: dict[int, dict[str, Any]] = {}
    for (score_row, scoring_row, _original, _candidate, _boundaries), prediction in zip(
        eligible, predictions
    ):
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
        pairwise_score = min(1.0, max(float(raw_candidate_score), current_score + 0.02))
        eligible_by_segment[int(score_row["segment_id"])] = {
            "lane": "pairwise_evidence_eligible",
            "raw_candidate_score": round(float(raw_candidate_score), 6),
            "raw_score_delta": round(float(raw_candidate_score) - current_score, 6),
            "calibrated_candidate_score": round(pairwise_score, 6),
            "calibrated_score_delta": round(pairwise_score - current_score, 6),
            "calibration": "deterministic_missing_space_after_token_pairwise_v1",
            "model_action_after": str(model_action),
            "final_action_after": str(decision["final_action"]),
            "model_confidence_after": round(float(model_confidence), 6),
        }

    for record in records:
        enrichment = eligible_by_segment.get(int(record["segment_id"]))
        if enrichment:
            record.update(enrichment)
    return records


def write_reports(
    score_run: dict[str, Any],
    threshold: float,
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Path]]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{stamp()}_quality_missing_space_after_token_shadow"
    paths = {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lanes = Counter(str(record["lane"]) for record in records)
    blockers = Counter(reason for record in records for reason in record["blockers"])
    boundary_kinds: Counter[str] = Counter()
    for record in records:
        boundary_kinds.update(record["boundary_kinds"])
    eligible = [record for record in records if record["lane"] == "pairwise_evidence_eligible"]
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "model_run_id": int(score_run.get("model_run_id") or 0),
        "threshold": threshold,
        "record_count": len(records),
        "pairwise_evidence_eligible_count": len(eligible),
        "blocked_count": len(records) - len(eligible),
        "lane_counts": dict(lanes),
        "blocker_counts": dict(blockers),
        "boundary_kind_counts": dict(boundary_kinds),
        "token_integrity_ok_count": sum(bool(record["token_integrity_ok"]) for record in records),
        "post_validation_clean_count": sum(not record["post_issue_codes"] for record in records),
        "raw_model_regression_count": sum(
            record.get("raw_score_delta") is not None and float(record["raw_score_delta"]) < 0
            for record in eligible
        ),
        "candidate_generation_count": len(eligible),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Retain only style-close boundaries as pairwise preference evidence. "
            "Preserve CK3 macro adjacency and keep scripted, lowercase, plural and gender-token "
            "boundaries blocked."
        ),
        "artifacts": {name: str(path) for name, path in paths.items()},
    }
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Missing space after token shadow (read-only)",
        "",
        f"- Score run: `{summary['score_run_id']}`",
        f"- Low-score threshold: `< {threshold:.2f}`",
        f"- Records inspected: `{summary['record_count']}`",
        f"- Pairwise evidence eligible: `{summary['pairwise_evidence_eligible_count']}`",
        f"- Blocked/context: `{summary['blocked_count']}`",
        f"- Token integrity ok: `{summary['token_integrity_ok_count']}`",
        f"- Post-validation clean: `{summary['post_validation_clean_count']}`",
        "- Candidate generation only; output/apply writes: `0`",
        "",
        "## Lanes",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in lanes.most_common())
    lines.extend(["", "## Blockers", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in blockers.most_common())
    lines.extend(["", "## Boundary kinds", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in boundary_kinds.most_common())
    lines.extend(["", "## Eligible samples", ""])
    for record in eligible[:30]:
        lines.append(
            f"- `{record['segment_id']}` `{record['relative_path']}`: "
            f"`{record['original_preview']}` -> `{record['candidate_preview']}`"
        )
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary, paths


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Audit deterministic spaces missing after CK3 tokens without changing state."
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.50)
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        raise ValueError("threshold must be greater than zero and at most one")

    settings = db.load_settings()
    database_path = db.project_path(settings["database_path"])
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        records = build_records(conn, score_run, args.threshold)
    summary, paths = write_reports(score_run, args.threshold, records)
    print("[quality-token-spacing] Read-only shadow completed")
    print(f"[quality-token-spacing] Score run: {summary['score_run_id']}")
    print(f"[quality-token-spacing] Records: {summary['record_count']}")
    print(
        "[quality-token-spacing] Pairwise eligible: "
        f"{summary['pairwise_evidence_eligible_count']}"
    )
    print(f"[quality-token-spacing] Markdown: {paths['markdown']}")
    print(f"[quality-token-spacing] JSONL: {paths['jsonl']}")
    print(f"[quality-token-spacing] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
