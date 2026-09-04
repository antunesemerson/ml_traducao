from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from typing import Any

import db
import local_quality_validator
import low_score_training_patterns
import quality_shadow_store
from offline_residual_proposals import token_status
from quality_contract_es_literal_repair_dry_run import (
    replace_allowlisted_literals,
)
from quality_missing_space_after_token_shadow import latest_full_output_score_run


RULE_VERSION = "quality_contract_es_helper_repair_dry_run_v1"
ELIGIBLE_LANE = "proposal_ready"
BLOCKED_LANE = "blocked_or_context"

SUPPORTED_ISSUE_CODES = {
    "accent_sensitive_spanish_residue",
    "embedded_gender_token_fragment",
    "gender_token_extra_prefix",
    "gender_token_extra_suffix",
    "missing_space_after_token",
    "neutral_word_with_gender_token",
    "space_before_punctuation",
    "spanish_residue",
    "spanish_residue_in_literal",
}

# These rewrites are intentionally tied to the exact scored segment and source key.
# If either the key or an expected fragment changes, the dry-run blocks the item.
SEGMENT_REWRITES: dict[int, dict[str, Any]] = {
    11609: {
        "source_key": "laamp_base_learning_contract_events.4005.desc.success.critical",
        "replacements": [
            (
                "#EMP hacédmelo#! me avisem",
                "#EMP avisem-me#!",
                "translate_spanish_imperative",
            ),
        ],
    },
    11678: {
        "source_key": "laamp_base_learning_contract_events.4017.desc.success.standard",
        "replacements": [
            (
                "eu #EMP tengo#! tenho a razão",
                "eu #EMP tenho#! razão",
                "remove_duplicate_spanish_verb",
            ),
        ],
    },
    11758: {
        "source_key": "laamp_base_learning_contract_events.4027.desc.failure",
        "replacements": [
            ("#EMP escoria#!", "#EMP escória#!", "restore_portuguese_accent"),
            ("#EMP estudio#!", "#EMP estúdio#!", "restore_portuguese_accent"),
            ("#EMP esto#!", "#EMP isto#!", "translate_spanish_demonstrative"),
        ],
    },
    12008: {
        "source_key": "laamp_transport_ward_desc",
        "replacements": [
            (
                "corte de seu tutor",
                "corte de seu guardião",
                "replace_contextual_guardian_term",
            ),
        ],
    },
    12157: {
        "source_key": "ep3_contract_event.0006.desc_locals_temple",
        "allow_token_delta": True,
        "replacements": [
            (
                "viajante[ROOT.Char.Custom('ES_OA')]!",
                "viajante!",
                "remove_redundant_gender_token_after_neutral_word",
            ),
        ],
    },
    12230: {
        "source_key": "ep3_contract_event.0011.desc_ward_too_old",
        "replacements": [
            (
                "ingênuo[task_contract_object.Custom('ES_OA')]",
                "ingênu[task_contract_object.Custom('ES_OA')]",
                "remove_gender_token_extra_prefix",
            ),
        ],
    },
    12232: {
        "source_key": "ep3_contract_event.0011.desc_gold",
        "allow_token_delta": True,
        "replacements": [
            (
                "viajante[ROOT.Char.Custom('ES_OA')] .",
                "viajante.",
                "remove_redundant_gender_token_and_space",
            ),
        ],
    },
    12241: {
        "source_key": "ep3_contract_event.0011.desc_animal_cat_love",
        "allow_token_delta": True,
        "replacements": [
            (
                "viajante[ROOT.Char.Custom('ES_OA')],",
                "viajante,",
                "remove_redundant_gender_token_after_neutral_word",
            ),
        ],
    },
    12427: {
        "source_key": "ep3_contract_event.0060.desc",
        "replacements": [
            (
                "[task_contract_employer.Custom('KnightCulturePluralNoTooltip')|l]s",
                "[task_contract_employer.Custom('KnightCulturePluralNoTooltip')|l]",
                "remove_extra_suffix_after_plural_token",
            ),
            (
                "am[task_contract_employer.Custom('ES_OA')]a",
                "amig[task_contract_employer.Custom('ES_OA')]",
                "repair_embedded_gender_word",
            ),
        ],
    },
    12535: {
        "source_key": "ep3_contract_event.0080.desc",
        "replacements": [
            (
                "'una usurpadora'",
                "'uma usurpadora'",
                "translate_select_cstring_literal",
            ),
            (
                "'un usurpador'",
                "'um usurpador'",
                "translate_select_cstring_literal",
            ),
            (
                "#EMP encontráis#!",
                "#EMP encontrar#!",
                "translate_spanish_verb",
            ),
            ("#EMP verdad#!", "#EMP verdade#!", "translate_spanish_noun"),
        ],
    },
    12693: {
        "source_key": "laamp_rid_councillor_contract_desc",
        "replacements": [
            (
                "#EMP personaje problemático#!",
                "#EMP indivíduo problemático#!",
                "translate_spanish_noun",
            ),
        ],
    },
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_codes(value: str) -> list[str]:
    return sorted(
        {
            str(issue.get("code") or "")
            for issue in local_quality_validator.validate_text(value).get("issues")
            or []
            if issue.get("code")
        }
    )


def repair_route(codes: list[str]) -> str:
    code_set = set(codes)
    routes: set[str] = set()
    if "spanish_residue_in_literal" in code_set:
        routes.add("spanish_literal")
    if code_set & {"accent_sensitive_spanish_residue", "spanish_residue"}:
        routes.add("spanish_cleanup")
    if any(
        code.startswith("gender_")
        or code
        in {
            "embedded_gender_token_fragment",
            "missing_space_after_token",
            "neutral_word_with_gender_token",
        }
        for code in code_set
    ):
        routes.add("gender_structure")
    if "space_before_punctuation" in code_set:
        routes.add("punctuation_spacing")
    if len(routes) > 1:
        return "mixed"
    return next(iter(routes), "other")


def apply_segment_rewrites(
    row: dict[str, Any],
    text: str,
) -> tuple[str, list[dict[str, Any]], list[str], bool]:
    spec = SEGMENT_REWRITES.get(int(row.get("segment_id") or 0))
    if not spec:
        return text, [], [], False
    if str(row.get("source_key") or "") != str(spec["source_key"]):
        return text, [], ["unexpected_source_key"], False

    candidate = text
    repairs: list[dict[str, Any]] = []
    for original, replacement, action in spec.get("replacements") or []:
        occurrence_count = candidate.count(original)
        if occurrence_count != 1:
            return text, [], ["expected_fragment_mismatch"], False
        candidate = candidate.replace(original, replacement, 1)
        repairs.append(
            {
                "action": action,
                "original_fragment": original,
                "replacement_fragment": replacement,
            }
        )
    return (
        candidate,
        repairs,
        [],
        bool(spec.get("allow_token_delta")),
    )


def build_record(row: dict[str, Any]) -> dict[str, Any]:
    original = str(row.get("candidate_text") or "")
    current_output = str(row.get("current_output_text") or "")
    candidate, literal_repairs = replace_allowlisted_literals(original)
    candidate, exact_repairs, rewrite_blockers, allow_token_delta = (
        apply_segment_rewrites(row, candidate)
    )
    repairs = [*literal_repairs, *exact_repairs]
    pre_codes = issue_codes(original)
    post_codes = issue_codes(candidate)
    raw_integrity_status = token_status(original, candidate)
    token_integrity_status = raw_integrity_status
    token_integrity_ok = raw_integrity_status in {"ok", "literal_changed"}
    if raw_integrity_status == "mismatch" and allow_token_delta and exact_repairs:
        token_integrity_status = "allowlisted_token_delta"
        token_integrity_ok = True

    blockers = list(rewrite_blockers)
    if not pre_codes or not set(pre_codes).issubset(SUPPORTED_ISSUE_CODES):
        blockers.append("unsupported_issue_scope")
    if not repairs:
        blockers.append("no_allowlisted_repair")
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
    if not token_integrity_ok:
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
        "repair_route": repair_route(pre_codes),
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
        "token_integrity_status": token_integrity_status,
        "token_integrity_ok": token_integrity_ok,
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
              AND output.portuguese_text LIKE '%Custom(%ES_%'
              AND state.is_closed = 1
              AND state.needs_output_apply = 0
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
        and issue_codes(str(row.get("candidate_text") or ""))
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
    route_counts = Counter(str(row.get("repair_route") or "other") for row in records)
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run_id),
        "segment_state_run_id": int(segment_state_run_id),
        "record_count": len(records),
        "proposal_ready_count": len(eligible),
        "blocked_count": len(records) - len(eligible),
        "repair_count": sum(int(row["repair_count"]) for row in eligible),
        "route_counts": dict(route_counts),
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
                "repair_route": row["repair_route"],
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
            "Deterministic, output-safe dry-run for the reviewed contract ES "
            "helper repair batch."
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
                    **{
                        key: value
                        for key, value in summary.items()
                        if key != "items"
                    },
                    "operational_writes": False,
                },
            )
        summary.update(snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
