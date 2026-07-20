from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from offline_residual_proposals import (
    token_status,
    translatable_literal_risk_terms,
    visible_spanish_risk_terms,
)


RULE_VERSION = "quality_token_punctuation_boundary_shadow_v1"
ISSUE_CODE = "space_before_punctuation"
TOKEN_PUNCTUATION_BOUNDARY_RE = re.compile(
    r"(?P<token>\$[^$\s]+\$|\[[^\]\r\n]+\]|#!|@[A-Za-z0-9_]+!)"
    # Question marks are deliberately excluded: in this package many of them
    # are replacement characters for accented letters (for example "é"), not
    # punctuation. They belong to the mojibake lane.
    r"(?P<space>[ \t]+)(?P<punctuation>[,.;:!])"
)


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


def repair_token_punctuation_boundaries(text: str) -> tuple[str, list[dict[str, Any]]]:
    repairs: list[dict[str, Any]] = []

    def replace(match: re.Match[str]) -> str:
        repairs.append(
            {
                "token": match.group("token"),
                "punctuation": match.group("punctuation"),
                "removed_space_count": len(match.group("space")),
                "context": preview(text[max(0, match.start() - 60) : match.end() + 60], 150),
            }
        )
        return f"{match.group('token')}{match.group('punctuation')}"

    return TOKEN_PUNCTUATION_BOUNDARY_RE.sub(replace, text), repairs


def latest_output_score_run(conn: sqlite3.Connection, requested_id: int | None) -> dict[str, Any]:
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


def load_rows(
    conn: sqlite3.Connection,
    score_run_id: int,
    threshold: float,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT score.*, COALESCE(confirmation.locked, 0) AS human_locked,
                   source.spanish_text AS source_spanish_text,
                   output.portuguese_text AS current_output_text
            FROM ml_score_items AS score
            JOIN source_segments AS source ON source.id = score.segment_id
            LEFT JOIN segment_confirmations AS confirmation
              ON confirmation.segment_id = score.segment_id
            LEFT JOIN output_segments AS output
              ON output.segment_id = score.segment_id
            WHERE score.run_id = ?
              AND score.model_safe_probability < ?
              AND EXISTS (
                SELECT 1 FROM json_each(score.issues_json) AS issue
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
    records: list[dict[str, Any]] = []
    for row in load_rows(conn, int(score_run["id"]), threshold):
        original = str(row.get("candidate_text") or "")
        candidate, repairs = repair_token_punctuation_boundaries(original)
        pre_codes = issue_codes(row.get("issues_json"))
        post_validation = local_quality_validator.validate_text(candidate)
        post_codes = {
            str(issue.get("code"))
            for issue in post_validation.get("issues") or []
            if issue.get("code")
        }
        token_ok = protected_tokens(original) == protected_tokens(candidate)
        blockers: list[str] = []
        if not repairs:
            blockers.append("not_token_punctuation_boundary")
        if candidate == original:
            blockers.append("no_change")
        if str(row.get("current_output_text") or "") != original:
            blockers.append("stale_output_text")
        if not token_ok:
            blockers.append("token_signature_changed")
        if pre_codes - {ISSUE_CODE}:
            blockers.append("other_preexisting_issues")
        if ISSUE_CODE in post_codes:
            blockers.append("space_before_punctuation_remains")
        if post_codes - {ISSUE_CODE}:
            blockers.append("other_post_validation_issues")
        source_token_status = token_status(str(row.get("source_spanish_text") or ""), candidate)
        if source_token_status != "ok":
            blockers.append(f"source_token_status_{source_token_status}")
        visible_risk_terms = visible_spanish_risk_terms(candidate)
        literal_risk_terms = translatable_literal_risk_terms(candidate)
        if visible_risk_terms:
            blockers.append("visible_spanish_risk_terms")
        if literal_risk_terms:
            blockers.append("translatable_literal_risk_terms")
        locked = bool(int(row.get("human_locked") or 0))
        if not repairs:
            lane = "not_token_boundary"
        elif not token_ok or candidate == original or "stale_output_text" in blockers:
            lane = "blocked_integrity"
        elif locked:
            lane = "review_locked"
        elif blockers:
            lane = "review_with_other_issues"
        else:
            lane = "ready_for_review"
        records.append(
            {
                "source": RULE_VERSION,
                "score_run_id": int(score_run["id"]),
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "score": round(float(row.get("model_safe_probability") or 0.0), 6),
                "original_preview": preview(original),
                "candidate_preview": preview(candidate),
                "repair_count": len(repairs),
                "repair_samples": repairs[:8],
                "pre_issue_codes": sorted(pre_codes),
                "post_issue_codes": sorted(post_codes),
                "token_integrity_ok": token_ok,
                "source_token_status": source_token_status,
                "visible_spanish_risk_terms": visible_risk_terms,
                "translatable_literal_risk_terms": literal_risk_terms,
                "human_locked": locked,
                "blockers": sorted(set(blockers)),
                "lane": lane,
                "candidate_generation_only": True,
                "ready_for_apply": False,
                "source_changed": False,
                "output_changed": False,
            }
        )
    return records


def materialize_ready_proposals(
    conn: sqlite3.Connection,
    score_run: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    ready = [record for record in records if record["lane"] == "ready_for_review"]
    prepared: list[tuple[dict[str, Any], str, str]] = []
    skipped_existing = 0
    for record in ready:
        current = conn.execute(
            """
            SELECT output.portuguese_text, score.candidate_text
            FROM output_segments AS output
            JOIN ml_score_items AS score ON score.segment_id = output.segment_id
            WHERE output.segment_id = ? AND score.run_id = ?
            """,
            (int(record["segment_id"]), int(score_run["id"])),
        ).fetchone()
        if not current or str(current["portuguese_text"] or "") != str(current["candidate_text"] or ""):
            raise RuntimeError(f"Output changed after score run for segment {record['segment_id']}.")
        original = str(current["portuguese_text"] or "")
        proposed, repairs = repair_token_punctuation_boundaries(original)
        if not repairs or proposed == original:
            raise RuntimeError(f"Candidate is no longer repairable for segment {record['segment_id']}.")
        existing = conn.execute(
            """
            SELECT 1 FROM offline_proposals
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
        prepared.append((record, original, proposed))

    if not prepared:
        return {
            "offline_proposal_run_id": None,
            "materialized_count": 0,
            "skipped_existing_count": skipped_existing,
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
                "token-to-punctuation boundary proposals. Queue only; evaluation is required "
                "before apply and output is not changed by this command."
            ),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    proposal_run_id = int(cursor.lastrowid)
    for record, original, proposed in prepared:
        source_row = conn.execute(
            "SELECT source_line_number FROM source_segments WHERE id = ?",
            (int(record["segment_id"]),),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO offline_proposals (
              run_id, segment_id, relative_path, source_key, source_line_number,
              candidate_bucket, proposal_source, original_text, proposed_text,
              confidence_score, status, token_status,
              issue_count, high_issue_count, medium_issue_count,
              rules_json, reasons_json, issues_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'quality_token_punctuation_boundary',
                      'remove_space_before_punctuation', ?, ?, 0.99,
                      'auto_ready', 'ok', 0, 0, 0, ?, ?, '[]', ?, ?)
            """,
            (
                proposal_run_id,
                int(record["segment_id"]),
                str(record.get("relative_path") or ""),
                str(record.get("source_key") or ""),
                source_row["source_line_number"] if source_row else None,
                original,
                proposed,
                json.dumps(["remove_space_before_punctuation"], ensure_ascii=False),
                json.dumps(
                    [
                        "quality_token_boundary_only",
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
    }


def write_reports(
    score_run: dict[str, Any],
    threshold: float,
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Path]]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{stamp()}_quality_token_punctuation_boundary_shadow"
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
    actionable = [
        record
        for record in records
        if record["lane"] in {"ready_for_review", "review_locked", "review_with_other_issues"}
    ]
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run["id"]),
        "threshold": threshold,
        "low_score_issue_count": len(records),
        "token_boundary_candidate_count": len(actionable),
        "ready_for_review_count": lanes.get("ready_for_review", 0),
        "review_locked_count": lanes.get("review_locked", 0),
        "review_with_other_issues_count": lanes.get("review_with_other_issues", 0),
        "not_token_boundary_count": lanes.get("not_token_boundary", 0),
        "lane_counts": dict(lanes),
        "blocker_counts": dict(blockers),
        "token_integrity_ok_count": sum(bool(record["token_integrity_ok"]) for record in records),
        "repair_count": sum(int(record["repair_count"]) for record in actionable),
        "candidate_generation_count": len(actionable),
        "queue_write_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Review ready_for_review first. Keep locked confirmations and rows with other issues "
            "out of apply until their lifecycle is explicitly reopened or resolved."
        ),
        "artifacts": {name: str(path) for name, path in paths.items()},
    }
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Quality token/punctuation boundary shadow",
        "",
        f"- Score run: `{summary['score_run_id']}`",
        f"- Low-score rows carrying the issue: `{summary['low_score_issue_count']}`",
        f"- Token-boundary candidates: `{summary['token_boundary_candidate_count']}`",
        f"- Ready for review: `{summary['ready_for_review_count']}`",
        f"- Locked review: `{summary['review_locked_count']}`",
        f"- Review with other issues: `{summary['review_with_other_issues_count']}`",
        f"- Outside this narrow boundary: `{summary['not_token_boundary_count']}`",
        "- Queue/apply/output writes: `0`",
        "",
        "## Lanes",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in lanes.most_common())
    lines.extend(["", "## Review samples", ""])
    for record in actionable[:50]:
        lines.append(
            f"- `{record['lane']}` · `{record['segment_id']}` · `{record['relative_path']}`: "
            f"`{record['original_preview']}` -> `{record['candidate_preview']}`"
        )
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary, paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a version-independent queue for whitespace between CK3 tokens and punctuation."
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--persist-db", action="store_true")
    parser.add_argument(
        "--materialize",
        action="store_true",
        help="Write only ready_for_review candidates to offline_proposals; never writes output.",
    )
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        raise ValueError("threshold must be greater than zero and at most one")
    settings = db.load_settings()
    database_path = db.project_path(settings["database_path"])
    materialization = None
    if args.materialize:
        with db.connect(settings) as conn:
            score_run = latest_output_score_run(conn, args.score_run_id)
            records = build_records(conn, score_run, args.threshold)
            materialization = materialize_ready_proposals(conn, score_run, records)
    else:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            score_run = latest_output_score_run(conn, args.score_run_id)
            records = build_records(conn, score_run, args.threshold)
    summary, paths = write_reports(score_run, args.threshold, records)
    if args.persist_db:
        with db.connect(settings) as write_conn:
            db.ensure_database(write_conn)
            summary.update(
                quality_shadow_store.persist_snapshot(
                    write_conn,
                    source_rule_version=RULE_VERSION,
                    score_run_id=int(score_run["id"]),
                    records=records,
                    eligible_lane="ready_for_review",
                    metadata={"threshold": args.threshold},
                )
            )
    if materialization:
        summary.update(materialization)
        summary["queue_write_count"] = int(materialization["materialized_count"])
        paths["summary"].write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(f"[quality-token-punctuation] Markdown: {paths['markdown']}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
