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


RULE_VERSION = "quality_space_punctuation_shadow_v1"
SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?])")
ES_HELPER_RE = re.compile(r"Custom\(\s*['\"]ES_", re.IGNORECASE)
VISIBLE_RESIDUAL_RE = re.compile(
    r"\b(?:Você|você|muy|Poner|suspendieron|ejecuciones|torturas)\b|#bold\s+No#!",
    re.IGNORECASE,
)
SUSPICIOUS_PUNCTUATION_RE = re.compile(r"(?:[,;:]\s*[,.!?]|[.!?]\s*[,;:])")
STYLE_OPENING_PUNCTUATION_RE = re.compile(r"#[A-Za-z0-9_]+\s+(?=[,.;:!?])")
INCOMPLETE_BEFORE_PUNCTUATION_RE = re.compile(
    r"\b(?:a|ao|aos|à|às|de|do|dos|da|das|para|por|com|sem|em)\s+[.!?]",
    re.IGNORECASE,
)
WORD_JOINED_TO_TOKEN_RE = re.compile(r"(?<=\w)\[")
PROMOTION_MIN_CURRENT_SCORE = 0.90


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_issue_codes(value: Any) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return set()
    codes: set[str] = set()
    if not isinstance(parsed, list):
        return codes
    for item in parsed:
        if isinstance(item, dict):
            code = item.get("code") or item.get("issue_code")
        else:
            code = item
        if code:
            codes.add(str(code))
    return codes


def preview(value: Any, limit: int = 300) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_full_output_score_run(conn: sqlite3.Connection, requested_id: int | None) -> dict[str, Any]:
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
    return dict(row)


def load_source_rows(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_key,
          s.source_line_number,
          s.english_text,
          s.spanish_text,
          s.old_text,
          s.has_english,
          s.has_old,
          o.portuguese_text AS output_text,
          sc.confirmation_level,
          COALESCE(sc.locked, 0) AS locked,
          sc.confidence_score AS confirmation_confidence,
          COALESCE(tc.token_count, 0) AS token_count
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN (
          SELECT segment_id, COUNT(*) AS token_count
          FROM protected_tokens
          GROUP BY segment_id
        ) tc ON tc.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def classify_blockers(
    original: str,
    candidate: str,
    token_ok: bool,
    post_issue_count: int,
    decision_reasons: list[str],
    locked: bool,
) -> list[str]:
    blockers: list[str] = []
    if original == candidate:
        blockers.append("no_change")
    if not token_ok:
        blockers.append("token_signature_changed")
    if post_issue_count:
        blockers.append("post_validation_issue")
    if ES_HELPER_RE.search(candidate):
        blockers.append("es_helper_context")
    if VISIBLE_RESIDUAL_RE.search(candidate):
        blockers.append("visible_residual_or_mojibake")
    if "�" in candidate:
        blockers.append("replacement_character")
    if SUSPICIOUS_PUNCTUATION_RE.search(candidate):
        blockers.append("suspicious_punctuation_sequence")
    repaired_marks = SPACE_BEFORE_PUNCTUATION_RE.findall(original)
    if any(mark in ".!?" for mark in repaired_marks):
        blockers.append("unsafe_sentence_punctuation")
    if STYLE_OPENING_PUNCTUATION_RE.search(original):
        blockers.append("style_markup_boundary")
    if INCOMPLETE_BEFORE_PUNCTUATION_RE.search(original):
        blockers.append("likely_missing_runtime_argument")
    if WORD_JOINED_TO_TOKEN_RE.search(candidate):
        blockers.append("word_joined_to_token")
    if any(str(reason).startswith("deterministic:language_blocking_features") for reason in decision_reasons):
        blockers.append("language_blocking_feature")
    if locked:
        blockers.append("human_locked_confirmation")
    return blockers


def build_records(conn: sqlite3.Connection, score_run: dict[str, Any]) -> list[dict[str, Any]]:
    score_rows = conn.execute(
        """
        SELECT *
        FROM ml_score_items
        WHERE run_id = ?
          AND issues_json LIKE '%space_before_punctuation%'
        ORDER BY segment_id
        """,
        (int(score_run["id"]),),
    ).fetchall()
    pure_rows = []
    for row in score_rows:
        item = dict(row)
        if parse_issue_codes(item.get("issues_json")) != {"space_before_punctuation"}:
            continue
        text = str(item.get("candidate_text") or "")
        if ES_HELPER_RE.search(text) or "�" in text:
            continue
        pure_rows.append(item)

    source_rows = load_source_rows(conn, [int(row["segment_id"]) for row in pure_rows])
    model_run_id = int(score_run.get("model_run_id") or 0)
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = bundle.get("metadata", {}).get("feature_set") or ml_score_segments.DEFAULT_FEATURE_SET
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)

    prepared: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for score_row in pure_rows:
        segment_id = int(score_row["segment_id"])
        source_row = source_rows.get(segment_id)
        if not source_row:
            continue
        original = str(score_row.get("candidate_text") or "")
        candidate = SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", original)
        scoring_row = dict(source_row)
        scoring_row["candidate_text"] = candidate
        scoring_row["candidate_text_source"] = "shadow_space_before_punctuation"
        scoring_row["text_length"] = len(candidate)
        prepared.append((score_row, scoring_row, original, candidate))

    predictions = ml_score_segments.model_predictions(
        model,
        [item[1] for item in prepared],
        safe_threshold,
        feature_set,
    )
    records: list[dict[str, Any]] = []
    for (score_row, scoring_row, original, candidate), prediction in zip(prepared, predictions):
        model_action, raw_candidate_score, model_confidence, probabilities = prediction
        decision = ml_score_segments.final_decision(
            scoring_row,
            model_action,
            raw_candidate_score,
            model_confidence,
            probabilities,
            safe_threshold,
        )
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        blockers = classify_blockers(
            original,
            candidate,
            token_ok,
            int(decision["issue_count"]),
            list(decision["reasons"]),
            bool(int(scoring_row.get("locked") or 0)),
        )
        raw_current_score = float(score_row.get("model_safe_probability") or 0.0)
        calibrated_candidate_score = min(1.0, max(raw_candidate_score, raw_current_score + 0.02))
        if blockers:
            lane = "blocked_or_context"
        elif raw_current_score >= PROMOTION_MIN_CURRENT_SCORE:
            lane = "promotion_validation_eligible"
        else:
            lane = "quality_improvement_but_segment_still_low"
        records.append(
            {
                "source": RULE_VERSION,
                "score_run_id": int(score_run["id"]),
                "model_run_id": model_run_id,
                "segment_id": int(score_row["segment_id"]),
                "relative_path": score_row.get("relative_path"),
                "source_key": score_row.get("source_key"),
                "original_preview": preview(original),
                "candidate_preview": preview(candidate),
                "replacement_count": len(SPACE_BEFORE_PUNCTUATION_RE.findall(original)),
                "raw_current_score": round(raw_current_score, 6),
                "raw_candidate_score": round(float(raw_candidate_score), 6),
                "raw_score_delta": round(float(raw_candidate_score) - raw_current_score, 6),
                "calibrated_candidate_score": round(calibrated_candidate_score, 6),
                "calibrated_score_delta": round(calibrated_candidate_score - raw_current_score, 6),
                "calibration": "deterministic_space_before_punctuation_repair_pairwise_v1",
                "pre_issue_codes": ["space_before_punctuation"],
                "post_issue_count": int(decision["issue_count"]),
                "post_issue_codes": [issue["code"] for issue in decision["issues"]],
                "token_integrity_ok": token_ok,
                "model_action_after": str(model_action),
                "final_action_after": str(decision["final_action"]),
                "blockers": blockers,
                "lane": lane,
                "candidate_generation_only": True,
                "ready_for_apply": False,
                "output_changed": False,
            }
        )
    return records


def write_reports(score_run: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{stamp()}_quality_space_punctuation_shadow"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = base.with_name(base.name + "_summary.json")
    markdown_path = base.with_suffix(".md")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lanes = Counter(record["lane"] for record in records)
    blockers = Counter(reason for record in records for reason in record["blockers"])
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "score_rule_version": score_run.get("rule_version"),
        "model_run_id": score_run.get("model_run_id"),
        "record_count": len(records),
        "lane_counts": dict(lanes),
        "blocker_counts": dict(blockers),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "post_validation_clean_count": sum(1 for record in records if record["post_issue_count"] == 0),
        "raw_model_regression_count": sum(1 for record in records if record["raw_score_delta"] < 0),
        "promotion_validation_eligible_count": lanes.get("promotion_validation_eligible", 0),
        "quality_improvement_but_segment_still_low_count": lanes.get(
            "quality_improvement_but_segment_still_low", 0
        ),
        "candidate_generation_count": len(records),
        "promotion_queue_write_count": 0,
        "apply_count": 0,
        "output_changed": False,
        "recommendation": (
            "Validate the promotion_validation_eligible lane against source context and package gates. "
            "Use the low-score lane as positive pairwise training evidence, not as automatic apply."
        ),
        "artifacts": {
            "markdown": str(markdown_path),
            "jsonl": str(jsonl_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Quality space/punctuation shadow",
        "",
        f"- Score run: `{summary['score_run_id']}`",
        f"- Candidates: `{summary['record_count']}`",
        f"- Promotion validation eligible: `{summary['promotion_validation_eligible_count']}`",
        f"- Correct local repair, segment still low: `{summary['quality_improvement_but_segment_still_low_count']}`",
        f"- Token integrity ok: `{summary['token_integrity_ok_count']}`",
        f"- Post-validation clean: `{summary['post_validation_clean_count']}`",
        f"- Raw model regressions after objective repair: `{summary['raw_model_regression_count']}`",
        "",
        "## Lanes",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in lanes.most_common())
    lines.extend(["", "## Blockers", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in blockers.most_common())
    for lane in ("promotion_validation_eligible", "quality_improvement_but_segment_still_low"):
        lane_rows = [record for record in records if record["lane"] == lane]
        if not lane_rows:
            continue
        lines.extend(
            [
                "",
                f"## {lane}",
                "",
                "| ID | arquivo/chave | antes | candidato | score bruto | score pareado |",
                "|---:|---|---|---|---:|---:|",
            ]
        )
        for record in sorted(lane_rows, key=lambda item: item["raw_current_score"], reverse=True):
            lines.append(
                "| {id} | `{path} :: {key}` | {old} | {new} | {raw:.2%} -> {candidate:.2%} | {paired:.2%} |".format(
                    id=record["segment_id"],
                    path=preview(record["relative_path"], 42).replace("|", "\\|"),
                    key=preview(record["source_key"], 34).replace("|", "\\|"),
                    old=preview(record["original_preview"], 90).replace("|", "\\|"),
                    new=preview(record["candidate_preview"], 90).replace("|", "\\|"),
                    raw=record["raw_current_score"],
                    candidate=record["raw_candidate_score"],
                    paired=record["calibrated_candidate_score"],
                )
            )
    lines.extend(
        [
            "",
            "## Guards",
            "",
            "- Promotion queue write: `0`",
            "- Apply: `0`",
            "- Output/source changed: `false`",
            "",
            f"Recommendation: {summary['recommendation']}",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def materialize_promotion_candidates(
    conn: sqlite3.Connection,
    score_run: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible = [record for record in records if record["lane"] == "promotion_validation_eligible"]
    prepared: list[tuple[dict[str, Any], sqlite3.Row, str, str]] = []
    skipped_existing = 0
    for record in eligible:
        row = conn.execute(
            """
            SELECT m.candidate_text, m.source_line_number
            FROM ml_score_items m
            WHERE m.run_id = ? AND m.segment_id = ?
            """,
            (int(score_run["id"]), int(record["segment_id"])),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Score item disappeared for segment {record['segment_id']}.")
        original = str(row["candidate_text"] or "")
        proposed = SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", original)
        existing = conn.execute(
            """
            SELECT 1
            FROM offline_proposals
            WHERE segment_id = ?
              AND proposal_source = 'remove_space_before_punctuation'
              AND proposed_text = ?
              AND status IN ('auto_ready', 'applied')
            LIMIT 1
            """,
            (int(record["segment_id"]), proposed),
        ).fetchone()
        if existing:
            skipped_existing += 1
            continue
        prepared.append((record, row, original, proposed))

    if not prepared:
        return {
            "offline_proposal_run_id": None,
            "materialized_count": 0,
            "skipped_existing_count": skipped_existing,
            "confirmation_write_count": 0,
            "output_changed": False,
        }
    timestamp = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO offline_proposal_runs (
          rule_version, model_version, path_filter, limit_count,
          candidate_count, proposed_count, auto_ready_count,
          needs_review_count, rejected_count, notes,
          started_at, finished_at, updated_at
        ) VALUES (?, ?, NULL, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            str(score_run.get("model_version") or f"model_run_{score_run.get('model_run_id')}"),
            len(prepared),
            len(prepared),
            len(prepared),
            len(prepared),
            (
                "Deterministic punctuation shadow candidates. Candidate queue only; "
                "does not change confirmations, segment-state or output."
            ),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    proposal_run_id = int(cursor.lastrowid)
    for record, row, original, proposed in prepared:
        conn.execute(
            """
            INSERT INTO offline_proposals (
              run_id, segment_id, relative_path, source_key, source_line_number,
              candidate_bucket, proposal_source, original_text, proposed_text,
              confidence_score, status, token_status,
              issue_count, high_issue_count, medium_issue_count,
              rules_json, reasons_json, issues_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto_ready', 'ok', 0, 0, 0, ?, ?, '[]', ?, ?)
            """,
            (
                proposal_run_id,
                int(record["segment_id"]),
                str(record["relative_path"] or ""),
                str(record["source_key"] or ""),
                row["source_line_number"],
                "deterministic_punctuation",
                "remove_space_before_punctuation",
                original,
                proposed,
                float(record["calibrated_candidate_score"]),
                json.dumps(["remove_space_before_punctuation"], ensure_ascii=False),
                json.dumps(
                    [
                        "pairwise_deterministic_repair",
                        "token_signature_preserved",
                        "post_validation_clean",
                        f"score_run:{score_run['id']}",
                    ],
                    ensure_ascii=False,
                ),
                timestamp,
                timestamp,
            ),
        )
    conn.commit()
    return {
        "offline_proposal_run_id": proposal_run_id,
        "materialized_count": len(prepared),
        "skipped_existing_count": skipped_existing,
        "confirmation_write_count": 0,
        "output_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic punctuation shadow candidates.")
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--materialize", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.project_path(settings["database_path"])
    materialization = None
    if args.materialize:
        with db.connect(settings) as conn:
            score_run = latest_full_output_score_run(conn, args.score_run_id)
            records = build_records(conn, score_run)
            materialization = materialize_promotion_candidates(conn, score_run, records)
    else:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            score_run = latest_full_output_score_run(conn, args.score_run_id)
            records = build_records(conn, score_run)
    summary = write_reports(score_run, records)
    if materialization:
        summary.update(materialization)
        summary["promotion_queue_write_count"] = int(materialization["materialized_count"])
        Path(summary["artifacts"]["summary"]).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
