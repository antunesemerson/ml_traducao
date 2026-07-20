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
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from quality_missing_space_after_token_shadow import load_context_rows
from quality_mojibake_lexicon_shadow import issue_codes, latest_full_output_score_run, preview


RULE_VERSION = "quality_gender_token_prefix_shadow_v1"
ISSUE_CODE = "gender_token_extra_prefix"
BROKEN_PREFIX_RE = re.compile(
    r"(?P<stem>[A-Za-zÀ-ÖØ-öø-ÿ]{2,})(?P<suffix>[ao])"
    r"(?P<token>\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])",
    re.IGNORECASE,
)
TRUSTED_STEM_RE = re.compile(
    r"(?P<stem>[A-Za-zÀ-ÖØ-öø-ÿ]{2,}?)"
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\]",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def build_trusted_stems(conn: sqlite3.Connection) -> Counter[str]:
    stems: Counter[str] = Counter()
    for row in conn.execute(
        "SELECT confirmed_text FROM segment_confirmations WHERE locked = 1"
    ):
        text = str(row["confirmed_text"] or "")
        codes = {
            str(item.get("code"))
            for item in local_quality_validator.validate_text(text).get("issues") or []
        }
        if ISSUE_CODE in codes:
            continue
        document_stems = {
            match.group("stem").casefold()
            for match in TRUSTED_STEM_RE.finditer(text)
            if not match.group("stem").casefold().endswith(("a", "o"))
        }
        stems.update(document_stems)
    return stems


def load_score_rows(conn: sqlite3.Connection, score_run_id: int, threshold: float) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT score.*, source.relative_path, source.source_key,
                   COALESCE(confirmation.locked, 0) AS human_locked
            FROM ml_score_items score
            JOIN source_segments source ON source.id = score.segment_id
            LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = score.segment_id
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
    score_rows: list[dict[str, Any]],
    trusted_stems: Counter[str],
    minimum_support: int,
    minimum_stem_length: int,
) -> list[dict[str, Any]]:
    context_rows = load_context_rows(conn, [int(row["segment_id"]) for row in score_rows])
    model_run_id = int(score_run.get("model_run_id") or 0)
    model_run = ml_score_segments.model_run_by_id(conn, model_run_id)
    bundle = joblib.load(db.project_path(model_run["model_path"]))
    model = bundle["model"]
    feature_set = bundle.get("metadata", {}).get("feature_set") or ml_score_segments.DEFAULT_FEATURE_SET
    safe_threshold = float(model_run.get("safe_threshold") or 0.90)

    records: list[dict[str, Any]] = []
    eligible_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in score_rows:
        original = str(row.get("candidate_text") or "")
        matches = list(BROKEN_PREFIX_RE.finditer(original))
        repairs = [
            {
                "surface": match.group(0),
                "stem": match.group("stem"),
                "token": match.group("token"),
                "trusted_support": int(trusted_stems[match.group("stem").casefold()]),
            }
            for match in matches
        ]
        stems_supported = bool(matches) and all(
            len(match.group("stem")) >= minimum_stem_length
            and trusted_stems[match.group("stem").casefold()] >= minimum_support
            for match in matches
        )
        candidate = (
            BROKEN_PREFIX_RE.sub(lambda match: match.group("stem") + match.group("token"), original)
            if stems_supported
            else original
        )
        blockers: list[str] = []
        codes = issue_codes(row.get("issues_json"))
        if not matches:
            blockers.append("no_prefix_match")
        if not stems_supported:
            blockers.append("stem_not_trusted")
        if codes != {ISSUE_CODE}:
            blockers.append("other_issue_codes")
        if int(row.get("human_locked") or 0):
            blockers.append("human_locked_confirmation")
        if candidate == original:
            blockers.append("no_change")
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        if not token_ok:
            blockers.append("token_signature_changed")
        post_validation = local_quality_validator.validate_text(candidate)
        post_codes = [str(item.get("code")) for item in post_validation.get("issues") or []]
        if post_codes:
            blockers.append("post_validation_issue")

        unique_blockers = sorted(set(blockers))
        eligible = not unique_blockers
        record = {
                "source": RULE_VERSION,
                "score_run_id": int(row["run_id"]),
                "model_run_id": model_run_id,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "lane": "pairwise_evidence_eligible" if eligible else "blocked_or_context",
                "blockers": unique_blockers,
                "human_locked": bool(row.get("human_locked")),
                "original_preview": preview(original),
                "candidate_preview": preview(candidate),
                "repairs": repairs,
                "pre_issue_codes": sorted(codes),
                "post_issue_codes": post_codes,
                "token_integrity_ok": token_ok,
                "raw_current_score": round(float(row.get("model_safe_probability") or 0), 6),
                "raw_candidate_score": None,
                "raw_score_delta": None,
                "calibrated_candidate_score": None,
                "calibrated_score_delta": None,
                "candidate_generation_only": True,
                "ready_for_apply": False,
                "output_changed": False,
            }
        records.append(record)
        if eligible:
            context = dict(context_rows.get(int(row["segment_id"])) or {})
            context.update(
                {
                    "candidate_text": candidate,
                    "candidate_text_source": RULE_VERSION,
                    "text_length": len(candidate),
                }
            )
            eligible_rows.append((row, context))

    predictions = ml_score_segments.model_predictions(
        model,
        [item[1] for item in eligible_rows],
        safe_threshold,
        feature_set,
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
            "calibration": "deterministic_gender_token_prefix_pairwise_v1",
            "model_action_after": str(model_action),
            "final_action_after": str(decision["final_action"]),
            "model_confidence_after": round(float(model_confidence), 6),
        }
    for record in records:
        enrichment = scored.get(int(record["segment_id"]))
        if enrichment:
            record.update(enrichment)
    return records


def write_reports(
    settings: dict[str, Any],
    score_run: dict[str, Any],
    threshold: float,
    minimum_support: int,
    minimum_stem_length: int,
    trusted_stems: Counter[str],
    records: list[dict[str, Any]],
) -> dict[str, Path]:
    reports_dir = db.project_path(settings.get("reports_dir", "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{stamp()}_quality_gender_token_prefix_shadow"
    paths = {
        "markdown": reports_dir / f"{prefix}.md",
        "jsonl": reports_dir / f"{prefix}.jsonl",
        "summary": reports_dir / f"{prefix}_summary.json",
    }
    eligible = [record for record in records if record["lane"] == "pairwise_evidence_eligible"]
    blocker_counts: Counter[str] = Counter(
        blocker for record in records for blocker in record["blockers"]
    )
    eligible_stems: Counter[str] = Counter(
        str(repair["stem"]).casefold()
        for record in eligible
        for repair in record["repairs"]
    )
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "threshold": threshold,
        "minimum_trusted_support": minimum_support,
        "minimum_stem_length": minimum_stem_length,
        "trusted_stem_count": sum(count >= minimum_support for count in trusted_stems.values()),
        "record_count": len(records),
        "pairwise_evidence_eligible_count": len(eligible),
        "blocked_count": len(records) - len(eligible),
        "blocker_counts": dict(blocker_counts),
        "eligible_stem_counts": dict(eligible_stems),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Use the trusted-stem cohort as pairwise preference evidence only. "
            "Keep unsupported stems, multi-issue rows and human locks out of automatic promotion."
        ),
        "artifacts": {name: str(path) for name, path in paths.items()},
    }
    paths["jsonl"].write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    lines = [
        "# Quality Gender Token Prefix Shadow",
        "",
        f"- Rule: `{RULE_VERSION}`",
        f"- Score run: `{score_run['id']}`",
        f"- Records: `{len(records)}`",
        f"- Pairwise eligible: `{len(eligible)}`",
        f"- Trusted support: `>= {minimum_support}`",
        f"- Minimum stem length: `{minimum_stem_length}`",
        "- Writes: `0`",
        "",
        "## Blockers",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in blocker_counts.most_common())
    lines.extend(["", "## Eligible Stems", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in eligible_stems.most_common())
    lines.extend(["", "## Eligible Sample", ""])
    for record in eligible[:40]:
        lines.extend(
            [
                f"### Segment {record['segment_id']}",
                f"- `{record['relative_path']}::{record['source_key']}`",
                f"- Before: `{record['original_preview']}`",
                f"- After: `{record['candidate_preview']}`",
                f"- Repairs: `{json.dumps(record['repairs'], ensure_ascii=False)}`",
                "",
            ]
        )
    paths["markdown"].write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only trusted-stem shadow for ES_OA prefix repair.")
    parser.add_argument("--score-run-id", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--minimum-support", type=int, default=5)
    parser.add_argument("--minimum-stem-length", type=int, default=4)
    parser.add_argument("--persist-db", action="store_true")
    args = parser.parse_args()

    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    try:
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        trusted_stems = build_trusted_stems(conn)
        score_rows = load_score_rows(conn, int(score_run["id"]), args.threshold)
        records = build_records(
            conn,
            score_run,
            score_rows,
            trusted_stems,
            args.minimum_support,
            args.minimum_stem_length,
        )
    finally:
        conn.close()

    paths = write_reports(
        settings,
        score_run,
        args.threshold,
        args.minimum_support,
        args.minimum_stem_length,
        trusted_stems,
        records,
    )
    eligible_count = sum(record["lane"] == "pairwise_evidence_eligible" for record in records)
    shadow_snapshot = {}
    if args.persist_db:
        with db.connect(settings) as write_conn:
            db.ensure_database(write_conn)
            shadow_snapshot = quality_shadow_store.persist_snapshot(
                write_conn,
                source_rule_version=RULE_VERSION,
                score_run_id=int(score_run["id"]),
                records=records,
                eligible_lane="pairwise_evidence_eligible",
                metadata={
                    "threshold": args.threshold,
                    "minimum_support": args.minimum_support,
                    "minimum_stem_length": args.minimum_stem_length,
                },
            )
    print("[quality-gender-prefix] Read-only shadow completed")
    print(f"[quality-gender-prefix] Score run: {score_run['id']}")
    print(f"[quality-gender-prefix] Records: {len(records)}")
    print(f"[quality-gender-prefix] Pairwise eligible: {eligible_count}")
    for name, path in paths.items():
        print(f"[quality-gender-prefix] {name.title()}: {path}")
    print(json.dumps({
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "record_count": len(records),
        "pairwise_evidence_eligible_count": eligible_count,
        **shadow_snapshot,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
