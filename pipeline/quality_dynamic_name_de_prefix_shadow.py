from __future__ import annotations

import argparse
import hashlib
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
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from quality_missing_space_after_token_shadow import load_context_rows


RULE_VERSION = "quality_dynamic_name_de_prefix_shadow_v1"
ISSUE_CODE = "unnatural_portuguese_fragment"
ELIGIBLE_LANE = "pairwise_evidence_eligible"
NAME_GETTER = r"(?:Get(?:Titled)?FirstName(?:NoTooltip)?|GetFullName(?:NoTooltip)?)"
DYNAMIC_NAME_DE_PREFIX_RE = re.compile(
    rf"(?P<prefix>\b[dD]) (?P<token>\[[^\]\r\n]*(?:{NAME_GETTER})[^\]\r\n]*\])"
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def preview(value: Any, limit: int = 360) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def issue_codes(value: Any) -> set[str]:
    if not value:
        return set()
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, list):
        return set()
    return {
        str(item.get("code") or item.get("issue_code"))
        for item in payload
        if isinstance(item, dict) and (item.get("code") or item.get("issue_code"))
    }


def repair_dynamic_name_de_prefix(text: str) -> tuple[str, list[dict[str, str]]]:
    replacements: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        token = match.group("token")
        replacement = f"{'De' if prefix == 'D' else 'de'} {token}"
        replacements.append({"original": match.group(0), "replacement": replacement, "token": token})
        return replacement

    return DYNAMIC_NAME_DE_PREFIX_RE.sub(replace, text), replacements


def latest_full_output_score_run(conn: sqlite3.Connection, requested_id: int | None) -> dict[str, Any]:
    if requested_id is not None:
        row = conn.execute("SELECT * FROM ml_score_runs WHERE id = ?", (requested_id,)).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM ml_score_runs
            WHERE candidate_text_source = 'output'
              AND finished_at IS NOT NULL
              AND limit_count IS NULL
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("No completed full output score run was found.")
    result = dict(row)
    if str(result.get("candidate_text_source") or "") != "output":
        raise RuntimeError("Selected score run does not measure output text.")
    return result


def load_score_rows(conn: sqlite3.Connection, score_run_id: int, threshold: float) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT score.*, source.relative_path, source.source_key,
                   output.portuguese_text AS current_output_text,
                   COALESCE(confirmation.locked, 0) AS human_locked
            FROM ml_score_items score
            JOIN source_segments source ON source.id = score.segment_id
            JOIN output_segments output ON output.segment_id = score.segment_id
            LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = score.segment_id
            WHERE score.run_id = ?
              AND score.model_safe_probability < ?
              AND EXISTS (
                SELECT 1 FROM json_each(score.issues_json) issue
                WHERE json_extract(issue.value, '$.code') = ?
              )
            ORDER BY score.model_safe_probability, score.segment_id
            """,
            (score_run_id, threshold, ISSUE_CODE),
        ).fetchall()
    ]


def build_records(
    conn: sqlite3.Connection,
    score_run: dict[str, Any],
    score_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    eligible_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    contexts = load_context_rows(conn, [int(row["segment_id"]) for row in score_rows])

    for row in score_rows:
        segment_id = int(row["segment_id"])
        original = str(row.get("candidate_text") or "")
        candidate, replacements = repair_dynamic_name_de_prefix(original)
        blockers: list[str] = []
        codes = issue_codes(row.get("issues_json"))
        if codes != {ISSUE_CODE}:
            blockers.append("other_issue_codes")
        if not replacements:
            blockers.append("no_dynamic_name_prefix_match")
        if candidate == original:
            blockers.append("no_change")
        if str(row.get("current_output_text") or "") != original:
            blockers.append("stale_output_text")
        if int(row.get("human_locked") or 0):
            blockers.append("human_locked_confirmation")
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        if not token_ok:
            blockers.append("token_signature_changed")
        post_codes = [
            str(item.get("code"))
            for item in local_quality_validator.validate_text(candidate).get("issues") or []
            if item.get("code")
        ]
        if post_codes:
            blockers.append("post_validation_issue")

        unique_blockers = sorted(set(blockers))
        record = {
            "source": RULE_VERSION,
            "score_run_id": int(row["run_id"]),
            "model_run_id": int(score_run.get("model_run_id") or 0),
            "segment_id": segment_id,
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "lane": ELIGIBLE_LANE if not unique_blockers else "blocked_or_context",
            "blockers": unique_blockers,
            "human_locked": bool(row.get("human_locked")),
            "original_preview": preview(original),
            "candidate_preview": preview(candidate),
            "baseline_hash": sha256_text(original),
            "candidate_hash": sha256_text(candidate),
            "replacements": replacements,
            "replacement_count": len(replacements),
            "pre_issue_codes": sorted(codes),
            "post_issue_codes": post_codes,
            "token_integrity_ok": token_ok,
            "raw_current_score": round(float(row.get("model_safe_probability") or 0.0), 6),
            "raw_candidate_score": None,
            "raw_score_delta": None,
            "calibrated_candidate_score": None,
            "calibrated_score_delta": None,
            "candidate_generation_only": True,
            "ready_for_apply": False,
            "output_changed": False,
        }
        records.append(record)
        if not unique_blockers:
            context = dict(contexts.get(segment_id) or {})
            context.update(
                {
                    "candidate_text": candidate,
                    "candidate_text_source": RULE_VERSION,
                    "text_length": len(candidate),
                }
            )
            eligible_rows.append((row, context))

    if not eligible_rows:
        return records

    model_run_id = int(score_run.get("model_run_id") or 0)
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = bundle.get("metadata", {}).get("feature_set") or ml_score_segments.DEFAULT_FEATURE_SET
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)
    predictions = ml_score_segments.model_predictions(
        model, [item[1] for item in eligible_rows], safe_threshold, feature_set
    )
    scored: dict[int, dict[str, Any]] = {}
    for (score_row, scoring_row), prediction in zip(eligible_rows, predictions):
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
        scored[int(score_row["segment_id"])] = {
            "raw_candidate_score": round(float(raw_candidate_score), 6),
            "raw_score_delta": round(float(raw_candidate_score) - current_score, 6),
            "calibrated_candidate_score": round(pairwise_score, 6),
            "calibrated_score_delta": round(pairwise_score - current_score, 6),
            "calibration": "deterministic_dynamic_name_de_prefix_pairwise_v1",
            "model_action_after": str(model_action),
            "final_action_after": str(decision["final_action"]),
            "model_confidence_after": round(float(model_confidence), 6),
        }
    for record in records:
        if int(record["segment_id"]) in scored:
            record.update(scored[int(record["segment_id"])])
    return records


def write_reports(
    settings: dict[str, Any],
    score_run: dict[str, Any],
    threshold: float,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    reports_dir = db.project_path(settings.get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{stamp()}_quality_dynamic_name_de_prefix_shadow"
    paths = {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }
    eligible = [row for row in records if row["lane"] == ELIGIBLE_LANE]
    blocker_counts = Counter(blocker for row in records for blocker in row["blockers"])
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "threshold": threshold,
        "record_count": len(records),
        "pairwise_evidence_eligible_count": len(eligible),
        "blocked_count": len(records) - len(eligible),
        "repair_occurrence_count": sum(int(row["replacement_count"]) for row in eligible),
        "blocker_counts": dict(blocker_counts),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Promote only the exact truncated 'd ' prefix before dynamic first/full-name getters. "
            "Keep titles, relations and gender helpers outside this provider."
        ),
        "artifacts": {name: str(path) for name, path in paths.items()},
    }
    paths["jsonl"].write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8"
    )
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Shadow: preposicao truncada antes de nome dinamico",
        "",
        f"- Score run: `{score_run['id']}`",
        f"- Casos inspecionados: `{len(records)}`",
        f"- Evidencias elegiveis: `{len(eligible)}`",
        f"- Ocorrencias reparadas: `{summary['repair_occurrence_count']}`",
        "- Escritas em output/confirmacoes: `0`",
        "",
        "## Bloqueios",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in blocker_counts.most_common())
    lines.extend(["", "## Amostra elegivel", ""])
    for row in eligible[:30]:
        lines.extend(
            [
                f"### Segmento {row['segment_id']}",
                f"- `{row['relative_path']}::{row['source_key']}`",
                f"- Antes: `{row['original_preview']}`",
                f"- Depois: `{row['candidate_preview']}`",
                "",
            ]
        )
    paths["markdown"].write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only shadow for truncated 'd ' before dynamic names.")
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--persist-db", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    try:
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        rows = load_score_rows(conn, int(score_run["id"]), args.threshold)
        records = build_records(conn, score_run, rows)
    finally:
        conn.close()
    shadow_snapshot = {}
    if args.persist_db:
        with db.connect(settings) as write_conn:
            db.ensure_database(write_conn)
            shadow_snapshot = quality_shadow_store.persist_snapshot(
                write_conn,
                source_rule_version=RULE_VERSION,
                score_run_id=int(score_run["id"]),
                records=records,
                eligible_lane=ELIGIBLE_LANE,
                metadata={"threshold": args.threshold},
            )
    summary = write_reports(settings, score_run, args.threshold, records)
    summary.update(shadow_snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
