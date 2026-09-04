from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from typing import Any

import db
import local_quality_validator
import low_score_training_patterns
import quality_shadow_store
from ck3_dynamic_expression import iter_expression_spans, iter_string_literal_spans
from offline_residual_proposals import token_status
from quality_missing_space_after_token_shadow import latest_full_output_score_run


RULE_VERSION = "quality_contract_es_literal_repair_dry_run_v1"
ISSUE_CODE = "spanish_residue_in_literal"
ELIGIBLE_LANE = "proposal_ready"
BLOCKED_LANE = "blocked_or_context"

SAFE_LITERAL_TRANSLATIONS = {
    "el alborotador": "o agitador",
    "el anciano": "o ancião",
    "el heredero problemático": "o herdeiro problemático",
    "la alborotadora": "a agitadora",
    "la anciana": "a anciã",
    "la heredera problemática": "a herdeira problemática",
    "otro pupilo": "outro pupilo",
    "otra pupila": "outra pupila",
    "un auténtico": "um autêntico",
    "un extranjero": "um estrangeiro",
    "una auténtica": "uma autêntica",
    "una extranjera": "uma estrangeira",
}
SELECT_CSTRING_PATTERN = re.compile(r"\bSelect_CString\s*\(", re.IGNORECASE)


def normalized_literal(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def translated_literal(value: str) -> str | None:
    replacement = SAFE_LITERAL_TRANSLATIONS.get(normalized_literal(value))
    if replacement is None:
        return None
    if value[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def replace_allowlisted_literals(
    text: str,
) -> tuple[str, list[dict[str, Any]]]:
    replacements: list[tuple[int, int, str, dict[str, Any]]] = []
    for expression in iter_expression_spans(text):
        if not SELECT_CSTRING_PATTERN.search(expression.text):
            continue
        for literal in iter_string_literal_spans(expression.text):
            replacement = translated_literal(literal.value)
            if replacement is None or replacement == literal.value:
                continue
            start = expression.start() + literal.start_index
            end = expression.start() + literal.end_index
            quoted_replacement = f"{literal.quote}{replacement}{literal.quote}"
            replacements.append(
                (
                    start,
                    end,
                    quoted_replacement,
                    {
                        "action": "translate_exact_select_cstring_literal",
                        "original_literal": literal.value,
                        "replacement_literal": replacement,
                        "quote": literal.quote,
                        "expression": expression.text,
                    },
                )
            )
    candidate = text
    repairs: list[dict[str, Any]] = []
    for start, end, replacement, repair in reversed(replacements):
        candidate = candidate[:start] + replacement + candidate[end:]
        repairs.append(repair)
    repairs.reverse()
    return candidate, repairs


def issue_codes(value: str) -> list[str]:
    return sorted(
        {
            str(issue.get("code") or "")
            for issue in local_quality_validator.validate_text(value).get("issues")
            or []
            if issue.get("code")
        }
    )


def build_record(row: dict[str, Any]) -> dict[str, Any]:
    original = str(row.get("candidate_text") or "")
    current_output = str(row.get("current_output_text") or "")
    candidate, repairs = replace_allowlisted_literals(original)
    pre_codes = issue_codes(original)
    post_codes = issue_codes(candidate)
    integrity_status = token_status(original, candidate)
    blockers: list[str] = []
    if pre_codes != [ISSUE_CODE]:
        blockers.append("not_pure_literal_issue_scope")
    if not repairs:
        blockers.append("no_allowlisted_literal_replacement")
    if candidate == original:
        blockers.append("no_change")
    if current_output != original:
        blockers.append("stale_output_text")
    if bool(row.get("human_locked")):
        blockers.append("human_locked_confirmation")
    if not bool(row.get("is_closed")):
        blockers.append("segment_not_closed")
    if int(row.get("needs_output_apply") or 0):
        blockers.append("needs_output_apply")
    if integrity_status not in {"ok", "literal_changed"}:
        blockers.append("unexpected_token_delta")
    if post_codes:
        blockers.append("post_validation_issue")

    unique_blockers = sorted(set(blockers))
    return {
        "source": RULE_VERSION,
        "score_run_id": int(row["run_id"]),
        "segment_state_run_id": int(row.get("segment_state_run_id") or 0),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "english_text": row.get("english_text"),
        "original_text": original,
        "candidate_text": candidate,
        "original_hash": sha256_text(original),
        "candidate_hash": sha256_text(candidate),
        "lane": ELIGIBLE_LANE if not unique_blockers else BLOCKED_LANE,
        "blockers": unique_blockers,
        "repairs": repairs,
        "repair_count": len(repairs),
        "pre_issue_codes": pre_codes,
        "post_issue_codes": post_codes,
        "token_integrity_status": integrity_status,
        "token_integrity_ok": integrity_status in {"ok", "literal_changed"},
        "raw_current_score": round(
            float(row.get("model_safe_probability") or 0.0),
            6,
        ),
        "candidate_generation_only": True,
        "ready_for_apply": False,
        "output_changed": False,
        "operational_writes": False,
    }


def latest_segment_state_run(
    conn: sqlite3.Connection,
    requested_run_id: int | None,
) -> dict[str, Any]:
    if requested_run_id:
        row = conn.execute(
            """
            SELECT *
            FROM segment_state_runs
            WHERE id = ?
              AND finished_at IS NOT NULL
            LIMIT 1
            """,
            (int(requested_run_id),),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM segment_state_runs
            WHERE finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run was found.")
    return dict(row)


def load_rows(
    conn: sqlite3.Connection,
    *,
    score_run_id: int,
    segment_state_run_id: int,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              score.*,
              source.relative_path,
              source.source_key,
              source.english_text,
              output.portuguese_text AS current_output_text,
              COALESCE(confirmation.locked, 0) AS human_locked,
              state.run_id AS segment_state_run_id,
              state.is_closed,
              state.needs_output_apply
            FROM ml_score_items score
            JOIN source_segments source
              ON source.id = score.segment_id
             AND source.is_active = 1
            JOIN output_segments output
              ON output.segment_id = score.segment_id
            JOIN segment_state_items state
              ON state.segment_id = score.segment_id
             AND state.run_id = ?
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.segment_id = score.segment_id
            WHERE score.run_id = ?
              AND source.relative_path LIKE 'contracts/%'
              AND score.issue_count > 0
              AND score.candidate_text = output.portuguese_text
              AND output.portuguese_text LIKE '%Select_CString%'
            ORDER BY score.model_safe_probability, score.segment_id
            """,
            (segment_state_run_id, score_run_id),
        ).fetchall()
    ]
    return [
        row
        for row in rows
        if low_score_training_patterns.is_contract_es_article_preposition_helper(
            row
        )
        and issue_codes(str(row.get("candidate_text") or "")) == [ISSUE_CODE]
    ]


def summarize(
    *,
    score_run_id: int,
    segment_state_run_id: int,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible = [row for row in records if row["lane"] == ELIGIBLE_LANE]
    blocker_counts = Counter(
        blocker for row in records for blocker in row.get("blockers") or []
    )
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run_id),
        "segment_state_run_id": int(segment_state_run_id),
        "record_count": len(records),
        "proposal_ready_count": len(eligible),
        "blocked_count": len(records) - len(eligible),
        "repair_count": sum(int(row["repair_count"]) for row in eligible),
        "blocker_counts": dict(blocker_counts),
        "post_validation_clean_count": sum(
            not row.get("post_issue_codes") for row in eligible
        ),
        "token_integrity_ok_count": sum(
            bool(row.get("token_integrity_ok")) for row in eligible
        ),
        "candidate_generation_only": True,
        "ready_for_apply_count": 0,
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "operational_writes": False,
        "items": [
            {
                "segment_id": row["segment_id"],
                "lane": row["lane"],
                "repair_count": row["repair_count"],
                "blockers": row["blockers"],
                "raw_current_score": row["raw_current_score"],
            }
            for row in records
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic, output-safe dry-run for allowlisted Spanish literals "
            "inside contract Select_CString expressions."
        )
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--persist-db", action="store_true")
    args = parser.parse_args()

    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    conn = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
        timeout=300,
    )
    conn.row_factory = sqlite3.Row
    try:
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        state_run = latest_segment_state_run(conn, args.segment_state_run_id)
        records = [
            build_record(row)
            for row in load_rows(
                conn,
                score_run_id=int(score_run["id"]),
                segment_state_run_id=int(state_run["id"]),
            )
        ]
    finally:
        conn.close()

    summary = summarize(
        score_run_id=int(score_run["id"]),
        segment_state_run_id=int(state_run["id"]),
        records=records,
    )
    if args.persist_db:
        with db.connect(settings) as write_conn:
            db.ensure_database(write_conn)
            snapshot = quality_shadow_store.persist_snapshot(
                write_conn,
                source_rule_version=RULE_VERSION,
                score_run_id=int(score_run["id"]),
                records=records,
                eligible_lane=ELIGIBLE_LANE,
                metadata={
                    **{key: value for key, value in summary.items() if key != "items"},
                    "operational_writes": False,
                },
            )
        summary.update(snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
