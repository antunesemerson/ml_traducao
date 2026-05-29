from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short
from segment_token_mismatch_queue import fetch_candidates, latest_state_run_id, parse_review_states


RULE_VERSION = "segment_token_policy_v1"

MOJIBAKE_SEQUENCE_RE = re.compile(r"Ãƒ|Ã‚|ï¿½")
QUESTION_MARK_CONTEXT_RE = re.compile(
    r"(?<=[A-Za-zÀ-ÿ])\?+(?=[A-Za-zÀ-ÿ])|"
    r"\?(?:guas|ndia|rea|rvore|ltim[ao]|nico|ustria|spera)\b|"
    r"\b(?:NÃO|Não|não|NO|No|no)\s+\?\s+(?:até|a|o|um|uma|mais|possível)\b"
)
GENDER_CUSTOM_RE = re.compile(r"\.Custom\('ES_[A-Za-z]+'\)")
PRONOUN_RE = re.compile(r"\.(GetSheHe|GetHerHim|GetHerHis|GetWomanMan|GetLadyLord)\b")
DYNAMIC_SCOPE_RE = re.compile(
    r"(MakeScope|ScriptValue|Var\(|Scope\.|LocalPlayerString|PlayerString|Custom2|GetShortUIName|GetFirstName|GetName)"
)


@dataclass(frozen=True)
class PolicyDecision:
    policy_bucket: str
    risk_level: str
    recommendation: str
    issue_flags: list[str]
    auto_apply_allowed: bool = False
    needs_human_review: bool = True


def token_text(tokens: list[str]) -> str:
    return " ".join(tokens)


def only_gender_or_pronoun(tokens: list[str]) -> bool:
    if not tokens:
        return False
    return all(GENDER_CUSTOM_RE.search(token) or PRONOUN_RE.search(token) for token in tokens)


def has_dynamic_scope(tokens: list[str]) -> bool:
    return any(DYNAMIC_SCOPE_RE.search(token) for token in tokens)


def has_select(tokens: list[str]) -> bool:
    return any(token.startswith("[Select_CString(") for token in tokens)


def has_concept(tokens: list[str]) -> bool:
    return any(token.startswith("[Concept(") for token in tokens)


def has_variable_or_icon(tokens: list[str]) -> bool:
    return any(token.startswith("$") or token.startswith("@") for token in tokens)


def has_format_tag(tokens: list[str]) -> bool:
    return bool(tokens) and all(token.startswith("#") for token in tokens)


def suspicious_mojibake(text: str | None) -> bool:
    if not text:
        return False
    if MOJIBAKE_SEQUENCE_RE.search(text) or QUESTION_MARK_CONTEXT_RE.search(text):
        return True
    issues = local_quality_validator.validate_text(text)["issues"]
    return any(issue["code"] == "replacement_question_mark_mojibake" for issue in issues)


def classify_policy(row: dict[str, Any]) -> PolicyDecision:
    missing = list(row["missing_tokens"])
    extra = list(row["extra_tokens"])
    all_tokens = [*missing, *extra]
    flags: list[str] = []
    has_suspicious_text = suspicious_mojibake(row.get("confirmed_text"))
    if has_suspicious_text:
        flags.append("confirmed_text_suspicious_mojibake")

    if has_variable_or_icon(all_tokens):
        flags.append("variable_or_icon_changed")
        return PolicyDecision(
            policy_bucket="blocked_variable_or_icon_change",
            risk_level="critical",
            recommendation=(
                "do_not_apply; variables/icons must be preserved or manually rewritten with explicit approval"
                + ("; confirmed text also needs encoding/content review" if has_suspicious_text else "")
            ),
            issue_flags=flags,
        )

    if has_suspicious_text:
        return PolicyDecision(
            policy_bucket="blocked_suspicious_confirmed_text",
            risk_level="critical",
            recommendation="do_not_apply; review confirmed text encoding/content before any token policy",
            issue_flags=flags,
        )

    if has_select(all_tokens):
        flags.append("select_cstring_changed")
        if row["review_state"] == "human_locked":
            return PolicyDecision(
                policy_bucket="manual_exception_candidate_select_cstring",
                risk_level="high",
                recommendation="manual review; possible intentional fluency simplification but selector behavior changed",
                issue_flags=flags,
            )
        return PolicyDecision(
            policy_bucket="review_select_cstring_change",
            risk_level="high",
            recommendation="manual review; do not generalize selector removal until pattern is approved",
            issue_flags=flags,
        )

    if row["diff_kind"] == "concept_replaced_by_direct_link" or has_concept(missing):
        flags.append("concept_replaced_or_removed")
        return PolicyDecision(
            policy_bucket="review_concept_simplification",
            risk_level="medium",
            recommendation="manual review; may be acceptable if tooltip behavior remains intended",
            issue_flags=flags,
        )

    if only_gender_or_pronoun(all_tokens):
        flags.append("gender_or_pronoun_token_change")
        if row["review_state"] == "human_locked":
            return PolicyDecision(
                policy_bucket="manual_exception_candidate_gender_token",
                risk_level="medium",
                recommendation="manual review sample; likely intentional PT-BR fluency adjustment if text still resolves gender",
                issue_flags=flags,
            )
        return PolicyDecision(
            policy_bucket="review_gender_token_change",
            risk_level="medium",
            recommendation="manual review; possible future policy after enough accepted samples",
            issue_flags=flags,
        )

    if has_dynamic_scope(all_tokens):
        flags.append("dynamic_scope_or_name_token_changed")
        return PolicyDecision(
            policy_bucket="review_dynamic_scope_change",
            risk_level="high",
            recommendation="manual review; dynamic character/title/name behavior changed",
            issue_flags=flags,
        )

    if has_format_tag(all_tokens):
        flags.append("format_tag_only")
        return PolicyDecision(
            policy_bucket="policy_candidate_format_tag",
            risk_level="low",
            recommendation="review small sample; may become safe policy if visual formatting remains acceptable",
            issue_flags=flags,
        )

    if missing and not extra:
        flags.append("token_removed")
        return PolicyDecision(
            policy_bucket="review_token_removed",
            risk_level="high",
            recommendation="manual review; token removal changes runtime behavior",
            issue_flags=flags,
        )

    if extra and not missing:
        flags.append("token_added")
        return PolicyDecision(
            policy_bucket="review_token_added",
            risk_level="medium",
            recommendation="manual review; extra token may be fluency improvement but can change runtime behavior",
            issue_flags=flags,
        )

    flags.append("mixed_token_change")
    return PolicyDecision(
        policy_bucket="review_mixed_token_change",
        risk_level="high",
        recommendation="manual review; mixed token substitutions are not policy-safe yet",
        issue_flags=flags,
    )


def risk_rank(value: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(value, 9)


def insert_run(conn, state_run_id: int, started_at: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_policy_runs (
            rule_version,
            state_run_id,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (RULE_VERSION, state_run_id, started_at, started_at),
    )
    return int(cur.lastrowid)


def insert_items(
    conn,
    *,
    run_id: int,
    state_run_id: int,
    rows: list[dict[str, Any]],
    created_at: str,
) -> None:
    values = []
    for row in rows:
        decision: PolicyDecision = row["policy"]
        values.append(
            (
                run_id,
                state_run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["review_state"],
                row["diff_kind"],
                decision.policy_bucket,
                decision.risk_level,
                decision.recommendation,
                1 if decision.auto_apply_allowed else 0,
                1 if decision.needs_human_review else 0,
                json.dumps(row["missing_tokens"], ensure_ascii=False),
                json.dumps(row["extra_tokens"], ensure_ascii=False),
                json.dumps(decision.issue_flags, ensure_ascii=False),
                created_at,
            )
        )
    if not values:
        return
    conn.executemany(
        """
        INSERT INTO segment_token_policy_items (
            run_id,
            state_run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            review_state,
            diff_kind,
            policy_bucket,
            risk_level,
            recommendation,
            auto_apply_allowed,
            needs_human_review,
            missing_tokens_json,
            extra_tokens_json,
            issue_flags_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )


def write_outputs(settings: dict, rows: list[dict[str, Any]], run_id: int, state_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = db.utc_now().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    base = reports_dir / f"{timestamp}_segment_token_policy"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    rows_sorted = sorted(
        rows,
        key=lambda row: (
            risk_rank(row["policy"].risk_level),
            row["policy"].policy_bucket,
            row["relative_path"],
            row["source_line_number"],
        ),
    )
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "review_state",
        "diff_kind",
        "policy_bucket",
        "risk_level",
        "recommendation",
        "issue_flags",
        "missing_tokens",
        "extra_tokens",
        "output_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows_sorted:
            decision: PolicyDecision = row["policy"]
            writer.writerow(
                {
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "review_state": row["review_state"],
                    "diff_kind": row["diff_kind"],
                    "policy_bucket": decision.policy_bucket,
                    "risk_level": decision.risk_level,
                    "recommendation": decision.recommendation,
                    "issue_flags": json.dumps(decision.issue_flags, ensure_ascii=False),
                    "missing_tokens": json.dumps(row["missing_tokens"], ensure_ascii=False),
                    "extra_tokens": json.dumps(row["extra_tokens"], ensure_ascii=False),
                    "output_text": row["output_text"],
                    "confirmed_text": row["confirmed_text"],
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows_sorted:
            decision = row["policy"]
            payload = {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "review_state": row["review_state"],
                "diff_kind": row["diff_kind"],
                "policy_bucket": decision.policy_bucket,
                "risk_level": decision.risk_level,
                "recommendation": decision.recommendation,
                "issue_flags": decision.issue_flags,
                "missing_tokens": row["missing_tokens"],
                "extra_tokens": row["extra_tokens"],
                "spanish_text": row["spanish_text"],
                "english_text": row["english_text"],
                "old_text": row["old_text"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    risk_counts = Counter(row["policy"].risk_level for row in rows)
    bucket_counts = Counter(row["policy"].policy_bucket for row in rows)
    diff_counts = Counter(row["diff_kind"] for row in rows)
    package_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        package_counts[row["relative_path"].split("/", 1)[0]] += 1

    lines = [
        "Segment token policy report",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {run_id}",
        f"State run id: {state_run_id}",
        f"Candidates: {len(rows)}",
        "",
        "Risk:",
        *[f"- {key}: {value}" for key, value in risk_counts.most_common()],
        "",
        "Policy buckets:",
        *[f"- {key}: {value}" for key, value in bucket_counts.most_common()],
        "",
        "Diff kinds:",
        *[f"- {key}: {value}" for key, value in diff_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {key}: {value}" for key, value in sorted(package_counts.items(), key=lambda item: item[1], reverse=True)[:20]],
        "",
        "Priority review sample:",
    ]
    for row in rows_sorted[:60]:
        decision: PolicyDecision = row["policy"]
        lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']} | {decision.risk_level} | {decision.policy_bucket}",
                f"  FLAGS: {', '.join(decision.issue_flags)}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  OUTPUT: {short(row['output_text'])}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def update_run_summary(
    conn,
    *,
    run_id: int,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    finished_at: str,
) -> None:
    risk_counts = Counter(row["policy"].risk_level for row in rows)
    policy_candidate_count = sum(1 for row in rows if row["policy"].policy_bucket.startswith("policy_candidate"))
    blocked_count = sum(1 for row in rows if row["policy"].policy_bucket.startswith("blocked"))
    conn.execute(
        """
        UPDATE segment_token_policy_runs
        SET
            total_candidates = ?,
            critical_count = ?,
            high_count = ?,
            medium_count = ?,
            low_count = ?,
            manual_review_count = ?,
            policy_candidate_count = ?,
            blocked_count = ?,
            report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len(rows),
            risk_counts.get("critical", 0),
            risk_counts.get("high", 0),
            risk_counts.get("medium", 0),
            risk_counts.get("low", 0),
            sum(1 for row in rows if row["policy"].needs_human_review),
            policy_candidate_count,
            blocked_count,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(
    *,
    state_run_id: int | None = None,
    limit: int | None = None,
    path_like: str | None = None,
    review_states_csv: str | None = None,
    include_auto_confirmed: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = db.utc_now()
    review_states = parse_review_states(review_states_csv, include_auto_confirmed)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = state_run_id or latest_state_run_id(conn)
        policy_run_id = insert_run(conn, selected_run_id, started_at)
        candidates = fetch_candidates(
            conn,
            state_run_id=selected_run_id,
            review_states=review_states,
            limit=limit,
            path_like=path_like,
        )
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            enriched = dict(candidate)
            enriched["policy"] = classify_policy(enriched)
            rows.append(enriched)
        insert_items(conn, run_id=policy_run_id, state_run_id=selected_run_id, rows=rows, created_at=started_at)
        conn.commit()
        txt_path, csv_path, jsonl_path = write_outputs(settings, rows, policy_run_id, selected_run_id)
        finished_at = db.utc_now()
        update_run_summary(
            conn,
            run_id=policy_run_id,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            finished_at=finished_at,
        )
        conn.commit()

    risk_counts = Counter(row["policy"].risk_level for row in rows)
    bucket_counts = Counter(row["policy"].policy_bucket for row in rows)
    print("[segment_token_policy] Policy generated")
    print(f"[segment_token_policy] Rule version: {RULE_VERSION}")
    print(f"[segment_token_policy] Policy run id: {policy_run_id}")
    print(f"[segment_token_policy] State run id: {selected_run_id}")
    print(f"[segment_token_policy] Candidates: {len(rows)}")
    for key, value in risk_counts.most_common():
        print(f"[segment_token_policy] risk {key}: {value}")
    for key, value in bucket_counts.most_common():
        print(f"[segment_token_policy] bucket {key}: {value}")
    print(f"[segment_token_policy] Report: {txt_path}")
    print(f"[segment_token_policy] CSV: {csv_path}")
    print(f"[segment_token_policy] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify segment token mismatches into review/policy buckets.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--review-states", default=None)
    parser.add_argument("--include-auto-confirmed", action="store_true")
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        limit=args.limit,
        path_like=args.path_like,
        review_states_csv=args.review_states,
        include_auto_confirmed=args.include_auto_confirmed,
    )
