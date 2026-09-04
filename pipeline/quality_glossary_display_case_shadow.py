from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any

import db
import quality_shadow_store


RULE_VERSION = "quality_glossary_display_case_shadow_v1"
REVIEW_LANE = "review_required"
SENSITIVE_LANE = "confirmed_case_sensitive"
FLEXIBLE_LANE = "accepted_contextual_case"
CONFLICT_LANE = "policy_conflict"
MANUAL_ORIGIN = "manual_low_score_calibration"

GLOSSARY_RE = re.compile(
    r"""Glossary\(\s*
        (?P<display_quote>['"])(?P<display>.*?)(?P=display_quote)\s*,\s*
        (?P<key_quote>['"])(?P<key>.*?)(?P=key_quote)\s*
    \)""",
    re.IGNORECASE | re.VERBOSE,
)
BOLD_HEADING_RE = re.compile(r"#bold\s+(.+?)#!", re.IGNORECASE)
CASE_REASON_RE = re.compile(
    r"glossary|mai[uú]sc|capitaliza|capitaliz|case",
    re.IGNORECASE,
)
POLICY_LABELS = {
    "case_sensitive": SENSITIVE_LANE,
    "case_flexible": FLEXIBLE_LANE,
    "policy_conflict": CONFLICT_LANE,
    "unknown": REVIEW_LANE,
}
LANE_PRIORITY = {
    CONFLICT_LANE: 4,
    REVIEW_LANE: 3,
    SENSITIVE_LANE: 2,
    FLEXIBLE_LANE: 1,
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def preview(value: Any, limit: int = 420) -> str:
    text = str(value or "").replace("\r", "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def glossary_calls(text: str | None) -> list[dict[str, Any]]:
    return [
        {
            "display": match.group("display"),
            "glossary_key": match.group("key").upper(),
            "start": match.start(),
            "end": match.end(),
            "display_start": match.start("display"),
            "display_end": match.end("display"),
        }
        for match in GLOSSARY_RE.finditer(str(text or ""))
    ]


def first_cased_index(value: str) -> int | None:
    for index, character in enumerate(value):
        if character.isalpha():
            return index
    return None


def begins_upper(value: str | None) -> bool:
    index = first_cased_index(str(value or ""))
    return bool(index is not None and str(value)[index].isupper())


def begins_lower(value: str | None) -> bool:
    index = first_cased_index(str(value or ""))
    return bool(index is not None and str(value)[index].islower())


def capitalize_first_cased(value: str) -> str:
    index = first_cased_index(value)
    if index is None:
        return value
    return value[:index] + value[index].upper() + value[index + 1 :]


def has_case_loss_against(reference: str | None, candidate: str) -> bool:
    source = str(reference or "")
    if not source or source.casefold() != candidate.casefold():
        return False
    if len(source) != len(candidate):
        return begins_upper(source) and begins_lower(candidate)
    return any(
        expected.isupper() and actual.islower()
        for expected, actual in zip(source, candidate)
        if expected.isalpha() and actual.isalpha()
    )


def restore_reference_case(
    candidate: str,
    english_display: str | None,
    canonical_heading: str | None,
) -> str:
    for reference in (canonical_heading, english_display):
        source = str(reference or "")
        if (
            source
            and source.casefold() == candidate.casefold()
            and len(source) == len(candidate)
        ):
            return "".join(
                (
                    actual.upper()
                    if expected.isupper()
                    else actual.lower()
                    if expected.islower()
                    else actual
                )
                for expected, actual in zip(source, candidate)
            )
    return capitalize_first_cased(candidate)


def is_display_case_loss(
    english_display: str | None,
    output_display: str,
    canonical_heading: str | None,
) -> bool:
    english_signal = (
        has_case_loss_against(english_display, output_display)
        or begins_upper(english_display)
        and begins_lower(output_display)
    )
    canonical_signal = has_case_loss_against(canonical_heading, output_display)
    return bool(english_signal or canonical_signal)


def align_glossary_calls(
    english_text: str | None,
    output_text: str | None,
) -> list[dict[str, Any]]:
    english_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in glossary_calls(english_text):
        english_by_key[str(call["glossary_key"])].append(call)
    positions: Counter[str] = Counter()
    aligned: list[dict[str, Any]] = []
    for output_call in glossary_calls(output_text):
        key = str(output_call["glossary_key"])
        position = positions[key]
        positions[key] += 1
        english_calls = english_by_key.get(key) or []
        english_call = english_calls[position] if position < len(english_calls) else None
        aligned.append(
            {
                **output_call,
                "occurrence_index": position,
                "english_display": english_call.get("display") if english_call else None,
            }
        )
    return aligned


def policy_from_votes(sensitive_votes: int, flexible_votes: int) -> str:
    if sensitive_votes and flexible_votes:
        return "policy_conflict"
    if sensitive_votes:
        return "case_sensitive"
    if flexible_votes:
        return "case_flexible"
    return "unknown"


def manual_vote(label: str | None, reason: str | None) -> str | None:
    normalized = str(label or "").strip().casefold()
    if normalized == "structure_error" and CASE_REASON_RE.search(str(reason or "")):
        return "case_sensitive"
    if normalized in {"correct", "contextual_exception"}:
        return "case_flexible"
    return None


def canonical_headings(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        output_match = BOLD_HEADING_RE.search(str(row.get("output_text") or ""))
        english_match = BOLD_HEADING_RE.search(str(row.get("english_text") or ""))
        output_heading = output_match.group(1).strip() if output_match else ""
        english_heading = english_match.group(1).strip() if english_match else ""
        if (
            output_heading
            and english_heading
            and (
                "\ufffd" in output_heading
                or (
                    len(output_heading) == len(english_heading)
                    and any(
                        output_char == "?" and english_char != "?"
                        for output_char, english_char in zip(
                            output_heading,
                            english_heading,
                        )
                    )
                )
            )
        ):
            output_heading = english_heading
        heading = output_heading or english_heading
        if heading:
            result[str(row.get("source_key") or "").upper()] = heading
    return result


def detect_case_loss_occurrences(
    rows: list[dict[str, Any]],
    headings: dict[str, str],
) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for row in rows:
        for call in align_glossary_calls(row.get("english_text"), row.get("output_text")):
            canonical = headings.get(str(call["glossary_key"]))
            if not is_display_case_loss(
                call.get("english_display"),
                str(call["display"]),
                canonical,
            ):
                continue
            occurrences.append(
                {
                    "segment_id": int(row["segment_id"]),
                    "relative_path": str(row.get("relative_path") or ""),
                    "source_key": str(row.get("source_key") or ""),
                    "english_text": str(row.get("english_text") or ""),
                    "output_text": str(row.get("output_text") or ""),
                    "glossary_key": str(call["glossary_key"]),
                    "occurrence_index": int(call["occurrence_index"]),
                    "english_display": call.get("english_display"),
                    "output_display": str(call["display"]),
                    "canonical_heading": canonical,
                    "display_start": int(call["display_start"]),
                    "display_end": int(call["display_end"]),
                    "human_label": row.get("human_label"),
                    "review_reason": row.get("review_reason"),
                    "human_locked": bool(row.get("human_locked")),
                }
            )
    return occurrences


def key_policies(
    occurrences: list[dict[str, Any]],
    explicit_policies: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    evidence_segments: dict[str, set[str]] = defaultdict(set)
    for occurrence in occurrences:
        vote = manual_vote(
            occurrence.get("human_label"),
            occurrence.get("review_reason"),
        )
        if not vote:
            continue
        key = str(occurrence["glossary_key"])
        segment_id = int(occurrence["segment_id"])
        vote_key = f"{vote}:{segment_id}"
        if vote_key in evidence_segments[key]:
            continue
        evidence_segments[key].add(vote_key)
        votes[key][vote] += 1
    keys = {str(item["glossary_key"]) for item in occurrences}
    result = {
        key: {
            "glossary_key": key,
            "sensitive_votes": int(votes[key]["case_sensitive"]),
            "flexible_votes": int(votes[key]["case_flexible"]),
            "policy": policy_from_votes(
                int(votes[key]["case_sensitive"]),
                int(votes[key]["case_flexible"]),
            ),
        }
        for key in sorted(keys)
    }
    for key, explicit in (explicit_policies or {}).items():
        normalized_key = str(key or "").strip().upper()
        if normalized_key not in result:
            continue
        policy = str(explicit.get("policy") or "")
        if policy not in {"case_sensitive", "case_flexible"}:
            continue
        result[normalized_key].update(
            {
                "policy": policy,
                "explicit_policy": True,
                "policy_reason": explicit.get("reason"),
                "policy_reviewer": explicit.get("reviewer"),
                "policy_updated_at": explicit.get("updated_at"),
            }
        )
    return result


def apply_case_repairs(
    text: str,
    occurrences: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    candidate = text
    replacements: list[dict[str, Any]] = []
    for occurrence in sorted(
        occurrences,
        key=lambda item: int(item["display_start"]),
        reverse=True,
    ):
        start = int(occurrence["display_start"])
        end = int(occurrence["display_end"])
        original = candidate[start:end]
        replacement = restore_reference_case(
            original,
            occurrence.get("english_display"),
            occurrence.get("canonical_heading"),
        )
        candidate = candidate[:start] + replacement + candidate[end:]
        replacements.append(
            {
                "glossary_key": occurrence["glossary_key"],
                "original": original,
                "replacement": replacement,
            }
        )
    replacements.reverse()
    return candidate, replacements


def build_records(
    rows: list[dict[str, Any]],
    headings: dict[str, str],
    *,
    score_run_id: int,
    explicit_policies: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    occurrences = detect_case_loss_occurrences(rows, headings)
    policies = key_policies(occurrences, explicit_policies)
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    row_by_segment = {int(row["segment_id"]): row for row in rows}
    for occurrence in occurrences:
        by_segment[int(occurrence["segment_id"])].append(occurrence)

    records: list[dict[str, Any]] = []
    for segment_id, segment_occurrences in sorted(by_segment.items()):
        row = row_by_segment[segment_id]
        occurrence_policies = [
            policies[str(occurrence["glossary_key"])]["policy"]
            for occurrence in segment_occurrences
        ]
        lanes = [POLICY_LABELS[policy] for policy in occurrence_policies]
        lane = max(lanes, key=lambda item: LANE_PRIORITY[item])
        candidate, replacements = apply_case_repairs(
            str(row.get("output_text") or ""),
            segment_occurrences,
        )
        policy_payload = [
            policies[str(occurrence["glossary_key"])]
            for occurrence in segment_occurrences
        ]
        blockers = ["shadow_only_no_promotion"]
        if bool(row.get("human_locked")):
            blockers.append("human_locked_confirmation")
        if lane == CONFLICT_LANE:
            blockers.append("conflicting_human_policy")
        if lane == FLEXIBLE_LANE:
            blockers.append("contextual_lowercase_accepted")
        records.append(
            {
                "source": RULE_VERSION,
                "score_run_id": int(score_run_id),
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "lane": lane,
                "blockers": sorted(set(blockers)),
                "human_locked": bool(row.get("human_locked")),
                "case_loss_count": len(segment_occurrences),
                "glossary_keys": sorted(
                    {str(item["glossary_key"]) for item in segment_occurrences}
                ),
                "policies": policy_payload,
                "occurrences": [
                    {
                        key: occurrence.get(key)
                        for key in (
                            "glossary_key",
                            "occurrence_index",
                            "english_display",
                            "output_display",
                            "canonical_heading",
                            "human_label",
                            "review_reason",
                        )
                    }
                    for occurrence in segment_occurrences
                ],
                "original_preview": preview(row.get("output_text")),
                "candidate_preview": preview(candidate),
                "baseline_hash": sha256_text(str(row.get("output_text") or "")),
                "candidate_hash": sha256_text(candidate),
                "replacements": replacements,
                "glossary_key_integrity_ok": True,
                "candidate_generation_only": True,
                "ready_for_apply": False,
                "confirmation_write_count": 0,
                "output_write_count": 0,
                "output_changed": False,
            }
        )
    return records, policies


def latest_full_output_score_run(
    conn: sqlite3.Connection,
    requested_id: int | None,
) -> dict[str, Any]:
    if requested_id is not None:
        row = conn.execute(
            "SELECT * FROM ml_score_runs WHERE id = ?",
            (requested_id,),
        ).fetchone()
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
    payload = dict(row)
    if str(payload.get("candidate_text_source") or "") != "output":
        raise RuntimeError("Selected score run does not measure output text.")
    return payload


def load_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              source.id AS segment_id,
              source.relative_path,
              source.source_key,
              source.english_text,
              output.portuguese_text AS output_text,
              COALESCE(confirmation.locked, 0) AS human_locked,
              (
                SELECT candidate.human_label
                FROM local_learning_candidates candidate
                WHERE candidate.segment_id = source.id
                  AND candidate.origin = ?
                ORDER BY candidate.id DESC
                LIMIT 1
              ) AS human_label,
              (
                SELECT candidate.reason
                FROM local_learning_candidates candidate
                WHERE candidate.segment_id = source.id
                  AND candidate.origin = ?
                ORDER BY candidate.id DESC
                LIMIT 1
              ) AS review_reason
            FROM source_segments source
            JOIN output_segments output ON output.segment_id = source.id
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.segment_id = source.id
            WHERE source.is_active = 1
              AND (
                source.english_text LIKE '%Glossary(%'
                OR output.portuguese_text LIKE '%Glossary(%'
              )
            ORDER BY source.id
            """,
            (MANUAL_ORIGIN, MANUAL_ORIGIN),
        ).fetchall()
    ]


def load_definition_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT source.source_key, source.english_text,
                   output.portuguese_text AS output_text
            FROM source_segments source
            JOIN output_segments output ON output.segment_id = source.id
            WHERE source.is_active = 1
            """
        ).fetchall()
    ]


def load_explicit_policies(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    table_exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'glossary_display_case_policies'
        """
    ).fetchone()
    if not table_exists:
        return {}
    return {
        str(row["glossary_key"] or "").strip().upper(): dict(row)
        for row in conn.execute(
            """
            SELECT glossary_key, policy, reason, reviewer, updated_at
            FROM glossary_display_case_policies
            ORDER BY glossary_key
            """
        ).fetchall()
    }


def summary_payload(
    records: list[dict[str, Any]],
    policies: dict[str, dict[str, Any]],
    *,
    score_run_id: int,
) -> dict[str, Any]:
    lane_counts = Counter(str(record["lane"]) for record in records)
    policy_counts = Counter(str(item["policy"]) for item in policies.values())
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "score_run_id": int(score_run_id),
        "record_count": len(records),
        "case_loss_count": sum(int(record["case_loss_count"]) for record in records),
        "review_required_count": int(lane_counts[REVIEW_LANE]),
        "confirmed_case_sensitive_count": int(lane_counts[SENSITIVE_LANE]),
        "accepted_contextual_case_count": int(lane_counts[FLEXIBLE_LANE]),
        "policy_conflict_count": int(lane_counts[CONFLICT_LANE]),
        "lane_counts": dict(lane_counts),
        "key_policy_counts": dict(policy_counts),
        "key_policies": list(policies.values()),
        "pairwise_evidence_write_count": 0,
        "promotion_queue_write_count": 0,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Review unknown Glossary keys by group. Keep confirmed flexible keys accepted, "
            "and do not promote confirmed sensitive repairs until a dedicated monotonic gate exists."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shadow-only detector for capitalization loss in Glossary display text."
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--persist-db", action="store_true")
    args = parser.parse_args()

    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    with sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
        timeout=300,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        score_run = latest_full_output_score_run(conn, args.score_run_id)
        rows = load_rows(conn)
        headings = canonical_headings(load_definition_rows(conn))
        explicit_policies = load_explicit_policies(conn)
        records, policies = build_records(
            rows,
            headings,
            score_run_id=int(score_run["id"]),
            explicit_policies=explicit_policies,
        )

    summary = summary_payload(
        records,
        policies,
        score_run_id=int(score_run["id"]),
    )
    if args.persist_db:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            snapshot = quality_shadow_store.persist_snapshot(
                conn,
                source_rule_version=RULE_VERSION,
                score_run_id=int(score_run["id"]),
                records=records,
                eligible_lane=REVIEW_LANE,
                metadata={
                    "case_loss_count": summary["case_loss_count"],
                    "lane_counts": summary["lane_counts"],
                    "key_policy_counts": summary["key_policy_counts"],
                },
            )
        summary.update(snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
