from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short


RULE_VERSION = "segment_token_tutorial_concept_policy_v1"
SOURCE_BUCKET = "blocked_variable_or_icon_change"


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_runs entry found.")
    return int(row["id"])


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def is_tutorial_path(path: str | None) -> bool:
    return bool(path and path.startswith("tutorial/"))


def is_variable(token: str) -> bool:
    return token.startswith("$") and token.endswith("$")


def is_icon(token: str) -> bool:
    return token.startswith("@")


def is_game_concept_variable(token: str) -> bool:
    return token.startswith("$game_concept_") and token.endswith("$")


def is_concept_placeholder(token: str) -> bool:
    return token.startswith("[Concept(")


def is_direct_concept_link(token: str) -> bool:
    return token.startswith("[") and token.endswith("]") and "|<STYLE>" in token and not token.startswith("[Concept(")


def has_question_mojibake(value: str | None) -> bool:
    if not value:
        return False
    issues = local_quality_validator.validate_text(value)["issues"]
    return any(issue.get("code") == "replacement_question_mark_mojibake" for issue in issues)


def has_unresolved_question_marker(value: str | None) -> bool:
    if not value:
        return False
    return "?" in value


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    missing = parse_json_list(row.get("missing_tokens_json"))
    extra = parse_json_list(row.get("extra_tokens_json"))
    flags = parse_json_list(row.get("issue_flags_json"))
    all_tokens = [*missing, *extra]

    extra_variables = [token for token in extra if is_variable(token)]
    extra_game_concepts = [token for token in extra_variables if is_game_concept_variable(token)]
    extra_named_variables = [token for token in extra_variables if not is_game_concept_variable(token)]
    icons = [token for token in all_tokens if is_icon(token)]
    direct_links = [token for token in extra if is_direct_concept_link(token)]
    concept_placeholders_removed = [token for token in missing if is_concept_placeholder(token)]
    style_links_removed = [token for token in missing if is_direct_concept_link(token)]

    allowed_extra = all(
        is_game_concept_variable(token) or is_direct_concept_link(token)
        for token in extra
    )
    allowed_missing = all(
        is_concept_placeholder(token) or is_direct_concept_link(token)
        for token in missing
    )
    conceptish_change = bool(extra_game_concepts or direct_links or concept_placeholders_removed) and allowed_extra and allowed_missing
    suspicious_text = (
        "confirmed_text_suspicious_mojibake" in flags
        or has_question_mojibake(row.get("confirmed_text"))
        or has_unresolved_question_marker(row.get("confirmed_text"))
    )
    tutorial = is_tutorial_path(row.get("relative_path"))

    reasons: list[str] = []
    suggested_labels: list[str]
    if not tutorial:
        bucket = "non_tutorial_variable_or_icon_blocked"
        status = "blocked_out_of_scope"
        next_action = "keep in main token policy; this subpolicy only studies tutorial concept variables"
        suggested_labels = ["reject_policy_candidate", "manual_token_rewrite_required", "needs_subpolicy"]
        reasons.append("not_tutorial_path")
    elif icons:
        bucket = "tutorial_icon_change_blocked"
        status = "blocked_icon_change"
        next_action = "manual review; icon changes are not part of tutorial concept variable policy"
        suggested_labels = ["reject_policy_candidate", "manual_token_rewrite_required"]
        reasons.append("icon_changed")
    elif extra_named_variables:
        bucket = "tutorial_named_variable_review"
        status = "manual_review_required"
        next_action = "review named variable separately before any tutorial concept promotion"
        suggested_labels = ["reject_policy_candidate", "manual_token_rewrite_required", "needs_subpolicy"]
        reasons.append("non_game_concept_variable_added")
    elif not conceptish_change:
        bucket = "tutorial_mixed_token_review"
        status = "needs_subpolicy"
        next_action = "inspect manually; token pattern is not a clean tutorial concept rewrite"
        suggested_labels = ["reject_policy_candidate", "manual_token_rewrite_required", "needs_subpolicy"]
        reasons.append("not_clean_concept_rewrite")
    elif suspicious_text:
        bucket = "tutorial_concept_needs_text_fix"
        status = "fix_text_before_policy"
        next_action = "fix confirmed text encoding/accent issues first, then rerun token policy"
        suggested_labels = ["fix_confirmed_text", "needs_subpolicy", "reject_policy_candidate"]
        reasons.append("confirmed_text_suspicious_or_unresolved_question_marker")
    elif concept_placeholders_removed:
        bucket = "tutorial_concept_placeholder_rewrite_candidate"
        status = "subpolicy_candidate_review"
        next_action = "review as possible safe tutorial concept-link rewrite; no apply until promoted"
        suggested_labels = ["needs_subpolicy", "keep_manual_exception_only", "reject_policy_candidate"]
        reasons.append("concept_placeholder_replaced_by_direct_links")
    else:
        bucket = "tutorial_game_concept_addition_candidate"
        status = "subpolicy_candidate_review"
        next_action = "review as possible safe tutorial game_concept link addition; no apply until promoted"
        suggested_labels = ["needs_subpolicy", "keep_manual_exception_only", "reject_policy_candidate"]
        reasons.append("game_concept_variable_added")

    if extra_game_concepts:
        reasons.append("extra_game_concept_variables")
    if direct_links:
        reasons.append("extra_direct_concept_links")
    if style_links_removed:
        reasons.append("style_link_removed_or_changed")

    return {
        **row,
        "missing_tokens": missing,
        "extra_tokens": extra,
        "issue_flags": flags,
        "subpolicy_bucket": bucket,
        "subpolicy_status": status,
        "recommended_next_action": next_action,
        "suggested_review_labels": suggested_labels,
        "subpolicy_reasons": reasons,
        "allowed_for_apply": 0,
        "is_tutorial": 1 if tutorial else 0,
        "has_suspicious_text": 1 if suspicious_text else 0,
        "extra_game_concept_count": len(extra_game_concepts),
        "extra_named_variable_count": len(extra_named_variables),
        "direct_link_count": len(direct_links),
        "concept_placeholder_removed_count": len(concept_placeholders_removed),
    }


def fetch_rows(
    conn,
    *,
    policy_run_id: int,
    tutorial_only: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    params: list[Any] = [policy_run_id, SOURCE_BUCKET]
    where = ["i.run_id = ?", "i.policy_bucket = ?"]
    if tutorial_only:
        where.append("i.relative_path LIKE 'tutorial/%'")
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS policy_item_id,
            i.run_id AS policy_run_id,
            i.state_run_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.review_state,
            i.diff_kind,
            i.policy_bucket,
            i.risk_level,
            i.recommendation,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text AS output_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked
        FROM segment_token_policy_items i
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE WHEN i.relative_path LIKE 'tutorial/%' THEN 0 ELSE 1 END,
            CASE WHEN i.issue_flags_json LIKE '%confirmed_text_suspicious_mojibake%' THEN 0 ELSE 1 END,
            i.relative_path,
            i.source_line_number,
            i.segment_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def write_outputs(
    settings: dict,
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    tutorial_only: bool,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    slug = "tutorial_only" if tutorial_only else "all_blocked"
    base = reports_dir / f"{timestamp}_segment_token_tutorial_concept_policy_{slug}"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    decisions_path = base.with_name(base.name + "_decisions_template").with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "policy_run_id",
        "state_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "review_state",
        "subpolicy_bucket",
        "subpolicy_status",
        "recommended_next_action",
        "suggested_review_labels",
        "subpolicy_reasons",
        "allowed_for_apply",
        "has_suspicious_text",
        "extra_game_concept_count",
        "extra_named_variable_count",
        "direct_link_count",
        "concept_placeholder_removed_count",
        "missing_tokens",
        "extra_tokens",
        "issue_flags",
        "output_text",
        "confirmed_text",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"suggested_review_labels", "subpolicy_reasons", "missing_tokens", "extra_tokens", "issue_flags"}
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            suggested = row["suggested_review_labels"][0] if row["suggested_review_labels"] else "needs_subpolicy"
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "decision": suggested,
                        "corrected_text": "",
                        "notes": (
                            f"{RULE_VERSION}; {row['subpolicy_bucket']}; "
                            f"{row['recommended_next_action']}"
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    bucket_counts = Counter(row["subpolicy_bucket"] for row in rows)
    status_counts = Counter(row["subpolicy_status"] for row in rows)
    suspicious_count = sum(int(row["has_suspicious_text"]) for row in rows)
    tutorial_count = sum(int(row["is_tutorial"]) for row in rows)
    candidate_count = sum(1 for row in rows if row["subpolicy_status"] == "subpolicy_candidate_review")

    lines = [
        "Segment token tutorial concept subpolicy report",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Source bucket: {SOURCE_BUCKET}",
        f"Tutorial only: {tutorial_only}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Tutorial rows: {tutorial_count}",
        f"- Suspicious text rows: {suspicious_count}",
        f"- Clean subpolicy candidates: {candidate_count}",
        "- Allowed for apply: 0",
        "",
        "Subpolicy buckets:",
        *[f"- {key}: {value}" for key, value in bucket_counts.most_common()],
        "",
        "Subpolicy statuses:",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "How to use:",
        "- fix_text_before_policy: corrigir texto confirmado antes de discutir token.",
        "- subpolicy_candidate_review: candidato limpo para revisar como excecao/tutorial concept policy.",
        "- manual_review_required ou blocked_*: manter fora da promocao automatica.",
        "- O arquivo *_decisions_template.jsonl e apenas um rascunho; revise antes de ingerir.",
        "",
        "Priority sample:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['subpolicy_status']} | {row['subpolicy_bucket']}"
                ),
                f"  NEXT: {row['recommended_next_action']}",
                f"  LABELS: {', '.join(row['suggested_review_labels'])}",
                f"  REASONS: {json.dumps(row['subpolicy_reasons'], ensure_ascii=False)}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  OUTPUT: {short(row['output_text'])}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, decisions_path


def main(
    *,
    policy_run_id: int | None = None,
    tutorial_only: bool = False,
    limit: int | None = None,
) -> None:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        raw_rows = fetch_rows(
            conn,
            policy_run_id=selected_policy_run_id,
            tutorial_only=tutorial_only,
            limit=limit,
        )
    rows = [classify_row(row) for row in raw_rows]
    txt_path, csv_path, jsonl_path, decisions_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=rows,
        tutorial_only=tutorial_only,
    )

    bucket_counts = Counter(row["subpolicy_bucket"] for row in rows)
    status_counts = Counter(row["subpolicy_status"] for row in rows)
    print("[segment_token_tutorial_concept_policy] Subpolicy generated")
    print(f"[segment_token_tutorial_concept_policy] Rule version: {RULE_VERSION}")
    print(f"[segment_token_tutorial_concept_policy] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_tutorial_concept_policy] Rows inspected: {len(rows)}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_tutorial_concept_policy] status {key}: {value}")
    for key, value in bucket_counts.most_common():
        print(f"[segment_token_tutorial_concept_policy] bucket {key}: {value}")
    print("[segment_token_tutorial_concept_policy] Allowed for apply: 0")
    print(f"[segment_token_tutorial_concept_policy] Report: {txt_path}")
    print(f"[segment_token_tutorial_concept_policy] CSV: {csv_path}")
    print(f"[segment_token_tutorial_concept_policy] JSONL: {jsonl_path}")
    print(f"[segment_token_tutorial_concept_policy] Decisions template: {decisions_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify blocked tutorial concept variable token changes.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--tutorial-only", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(policy_run_id=args.policy_run_id, tutorial_only=args.tutorial_only, limit=args.limit)
