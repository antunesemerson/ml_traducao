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
from ml_composite_subpolicy_guarded_overlay import text_hygiene_flags
from segment_token_policy_review_queue import latest_policy_run_id, parse_json_list, split_csv


RULE_VERSION = "segment_token_gender_subpolicy_v1"
SOURCE_BUCKET = "review_gender_token_change"

PRONOUN_MARKERS = (
    "GetSheHe",
    "GetHerHim",
    "GetHerHis",
    "GetHerselfHimself",
    "GetWomanMan",
)

GENDER_MARKERS = (
    "ES_OA",
    "ES_XA",
    "ES_EA",
    "ES_ElLa",
    "ES_DelDela",
    "ES_AlAla",
    "ES_LoLa",
)

NEUTRAL_HINTS = (
    "crianca",
    "criança",
    "pessoa",
    "alguem",
    "alguém",
    "personagem",
    "alvo",
)

RELATIONSHIP_HINTS = (
    "meu/minha",
    "seu/sua",
    "dele/dela",
    "o/a ",
    "um/uma",
)


def slugify(value: str) -> str:
    safe = []
    for char in value.lower():
        if char.isalnum() or char in {"_", "-"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "filtered"


def has_any(tokens: list[str], markers: tuple[str, ...]) -> bool:
    joined = " ".join(tokens)
    return any(marker in joined for marker in markers)


def has_text_hint(value: str | None, hints: tuple[str, ...]) -> bool:
    lowered = (value or "").lower()
    return any(hint in lowered for hint in hints)


def has_suspicious_text(value: str | None) -> bool:
    if not value:
        return False
    issues = local_quality_validator.validate_text(value)["issues"]
    suspicious_codes = {
        "replacement_question_mark_mojibake",
        "mojibake_sequence",
        "spanish_punctuation",
    }
    return any(issue.get("code") in suspicious_codes for issue in issues)


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    missing = parse_json_list(row.get("missing_tokens_json"))
    extra = parse_json_list(row.get("extra_tokens_json"))
    flags = parse_json_list(row.get("issue_flags_json"))
    confirmed_text = row.get("confirmed_text")
    hygiene_flags = text_hygiene_flags(confirmed_text)

    missing_pronoun = has_any(missing, PRONOUN_MARKERS)
    extra_pronoun = has_any(extra, PRONOUN_MARKERS)
    missing_gender = has_any(missing, GENDER_MARKERS)
    extra_gender = has_any(extra, GENDER_MARKERS)
    neutral_hint = has_text_hint(confirmed_text, NEUTRAL_HINTS)
    relationship_hint = has_text_hint(confirmed_text, RELATIONSHIP_HINTS)
    suspicious_text = (
        has_suspicious_text(confirmed_text)
        or "confirmed_text_suspicious_mojibake" in flags
        or bool(hygiene_flags)
    )

    reasons: list[str] = []
    if missing_pronoun:
        reasons.append("missing_pronoun_token")
    if extra_pronoun:
        reasons.append("extra_pronoun_token")
    if missing_gender:
        reasons.append("missing_gender_token")
    if extra_gender:
        reasons.append("extra_gender_token")
    if neutral_hint:
        reasons.append("neutral_ptbr_word_hint")
    if relationship_hint:
        reasons.append("relationship_or_article_context_hint")
    if suspicious_text:
        reasons.append("confirmed_text_suspicious")
    for flag in hygiene_flags:
        reasons.append(f"text_hygiene:{flag}")

    if missing_pronoun and extra_pronoun:
        subtype = "pronoun_form_swap"
    elif extra_pronoun and (missing_gender or extra_gender):
        subtype = "pronoun_added_with_gender_context"
    elif extra_pronoun:
        subtype = "pronoun_added_for_pt_fluency"
    elif missing_pronoun:
        subtype = "pronoun_removed_or_literalized"
    elif missing_gender and extra_gender:
        subtype = "gender_custom_form_swap"
    elif extra_gender:
        subtype = "gender_custom_added"
    elif missing_gender:
        subtype = "gender_custom_removed_or_literalized"
    else:
        subtype = "gender_other"

    if suspicious_text:
        status = "fix_text_before_learning"
        recommendation = "fix confirmed text before this row can teach a gender subpolicy"
        suggested_labels = ["fix_confirmed_text", "reject_policy_candidate", "needs_subpolicy"]
    elif subtype in {"gender_custom_added", "pronoun_added_for_pt_fluency"}:
        status = "subpolicy_candidate_review"
        recommendation = "collect positive/negative evidence for a narrow gender-token release rule"
        suggested_labels = ["needs_subpolicy", "accept_policy_candidate", "keep_manual_exception_only"]
    elif subtype == "gender_custom_removed_or_literalized" and neutral_hint:
        status = "neutralization_candidate_review"
        recommendation = "study neutral PT-BR wording where gender token removal may be intentional"
        suggested_labels = ["needs_subpolicy", "keep_manual_exception_only", "reject_policy_candidate"]
    elif subtype in {"pronoun_added_with_gender_context", "pronoun_form_swap", "gender_custom_form_swap"}:
        status = "needs_split_review"
        recommendation = "split by scope/person before any policy promotion"
        suggested_labels = ["needs_subpolicy", "keep_manual_exception_only", "reject_policy_candidate"]
    else:
        status = "manual_exception_review"
        recommendation = "too contextual for direct policy; keep as evidence boundary"
        suggested_labels = ["keep_manual_exception_only", "needs_subpolicy", "reject_policy_candidate"]

    return {
        **row,
        "missing_tokens": missing,
        "extra_tokens": extra,
        "issue_flags": flags,
        "gender_subtype": subtype,
        "subpolicy_status": status,
        "recommended_next_action": recommendation,
        "suggested_review_labels": suggested_labels,
        "subpolicy_reasons": reasons,
        "neutral_hint": 1 if neutral_hint else 0,
        "relationship_hint": 1 if relationship_hint else 0,
        "suspicious_text": 1 if suspicious_text else 0,
        "text_hygiene_flags": hygiene_flags,
        "apply_allowed": 0,
    }


def fetch_rows(
    conn,
    *,
    policy_run_id: int,
    pending_apply_only: bool,
    skip_apply_approved: bool,
    undecided_only: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [policy_run_id, SOURCE_BUCKET]
    joins = [
        "JOIN source_segments s ON s.id = i.segment_id",
        "LEFT JOIN output_segments o ON o.segment_id = i.segment_id",
        "LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id",
    ]
    where = ["i.run_id = ?", "i.policy_bucket = ?"]

    if pending_apply_only:
        joins.append(
            """
            JOIN segment_state_items state_i
              ON state_i.segment_id = i.segment_id
             AND state_i.run_id = i.state_run_id
             AND state_i.apply_state = 'needs_apply'
            """
        )
    if skip_apply_approved:
        joins.append(
            """
            LEFT JOIN segment_token_policy_decisions approved_d
              ON approved_d.policy_run_id = i.run_id
             AND approved_d.policy_item_id = i.id
             AND approved_d.approved_for_apply = 1
            """
        )
        where.append("approved_d.policy_item_id IS NULL")
    if undecided_only:
        joins.append(
            """
            LEFT JOIN segment_token_policy_decisions any_d
              ON any_d.policy_run_id = i.run_id
             AND any_d.policy_item_id = i.id
            """
        )
        where.append("any_d.id IS NULL")

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
        {" ".join(joins)}
        WHERE {" AND ".join(where)}
        ORDER BY
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
    pending_apply_only: bool,
    skip_apply_approved: bool,
    undecided_only: bool,
    status_filter: list[str],
    subtype_filter: list[str],
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filter_parts: list[str] = []
    if status_filter:
        filter_parts.append("status_" + "_".join(slugify(item) for item in status_filter[:3]))
    if subtype_filter:
        filter_parts.append("subtype_" + "_".join(slugify(item) for item in subtype_filter[:3]))
    filter_slug = "_" + "_".join(filter_parts) if filter_parts else ""
    base = reports_dir / f"{timestamp}_segment_token_gender_subpolicy{filter_slug}"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    template_path = Path(str(base) + "_decisions_template.jsonl")

    fieldnames = [
        "policy_item_id",
        "policy_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "gender_subtype",
        "subpolicy_status",
        "risk_level",
        "neutral_hint",
        "relationship_hint",
        "suspicious_text",
        "text_hygiene_flags",
        "missing_tokens",
        "extra_tokens",
        "confirmed_text",
        "output_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"missing_tokens", "extra_tokens", "text_hygiene_flags"}
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    with template_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            default_decision = "fix_confirmed_text" if row["suspicious_text"] else "needs_subpolicy"
            payload = {
                "policy_item_id": row["policy_item_id"],
                "policy_run_id": row["policy_run_id"],
                "decision": default_decision,
                "reviewer": "codex_gender_subpolicy_triage",
                "notes": (
                    f"{row['gender_subtype']} | {row['subpolicy_status']} | "
                    f"{';'.join(row['subpolicy_reasons'])}"
                ),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    subtype_counts = Counter(row["gender_subtype"] for row in rows)
    status_counts = Counter(row["subpolicy_status"] for row in rows)
    candidate_rows = [row for row in rows if row["subpolicy_status"] in {"subpolicy_candidate_review", "neutralization_candidate_review"}]
    split_rows = [row for row in rows if row["subpolicy_status"] == "needs_split_review"]
    suspicious_rows = [row for row in rows if row["suspicious_text"]]
    hygiene_counts = Counter(flag for row in rows for flag in row.get("text_hygiene_flags") or [])

    lines = [
        "Segment token gender subpolicy diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Source bucket: {SOURCE_BUCKET}",
        f"Pending apply only: {pending_apply_only}",
        f"Skip apply-approved: {skip_apply_approved}",
        f"Undecided only: {undecided_only}",
        f"Status filter: {', '.join(status_filter) if status_filter else 'none'}",
        f"Subtype filter: {', '.join(subtype_filter) if subtype_filter else 'none'}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Candidate/neutralization rows: {len(candidate_rows)}",
        f"- Needs split review: {len(split_rows)}",
        f"- Suspicious text rows: {len(suspicious_rows)}",
        "- Apply allowed: 0",
        "",
        "Subtypes:",
        *[f"- {key}: {value}" for key, value in subtype_counts.most_common()],
        "",
        "Statuses:",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Text hygiene flags:",
        *([f"- {key}: {value}" for key, value in hygiene_counts.most_common()] or ["- none"]),
        "",
        "Priority samples:",
    ]
    for row in rows[:40]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['gender_subtype']} | {row['subpolicy_status']}"
                ),
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  REASONS: {', '.join(row['subpolicy_reasons']) or 'none'}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This is a learning/triage report only; it does not approve apply and does not write output.",
            "- The decisions template defaults to needs_subpolicy so it records evidence without releasing production writes.",
            "- Split-review subtypes should become narrower neuron rules before promotion.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, template_path


def main(
    *,
    policy_run_id: int | None = None,
    pending_apply_only: bool = True,
    skip_apply_approved: bool = True,
    undecided_only: bool = False,
    limit: int | None = None,
    statuses_value: str | None = None,
    subtypes_value: str | None = None,
) -> None:
    settings = db.load_settings()
    status_filter = split_csv(statuses_value)
    subtype_filter = split_csv(subtypes_value)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        rows = [
            classify_row(row)
            for row in fetch_rows(
                conn,
                policy_run_id=selected_policy_run_id,
                pending_apply_only=pending_apply_only,
                skip_apply_approved=skip_apply_approved,
                undecided_only=undecided_only,
                limit=None,
            )
        ]
    if status_filter:
        wanted = set(status_filter)
        rows = [row for row in rows if row["subpolicy_status"] in wanted]
    if subtype_filter:
        wanted = set(subtype_filter)
        rows = [row for row in rows if row["gender_subtype"] in wanted]
    if limit is not None:
        rows = rows[:limit]

    txt_path, csv_path, jsonl_path, template_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=rows,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
        undecided_only=undecided_only,
        status_filter=status_filter,
        subtype_filter=subtype_filter,
    )
    subtype_counts = Counter(row["gender_subtype"] for row in rows)
    status_counts = Counter(row["subpolicy_status"] for row in rows)
    print("[segment_token_gender_subpolicy] Diagnostic generated")
    print(f"[segment_token_gender_subpolicy] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_subpolicy] Policy run id: {selected_policy_run_id}")
    print(
        "[segment_token_gender_subpolicy] Status filter: "
        f"{', '.join(status_filter) if status_filter else 'none'}"
    )
    print(
        "[segment_token_gender_subpolicy] Subtype filter: "
        f"{', '.join(subtype_filter) if subtype_filter else 'none'}"
    )
    print(f"[segment_token_gender_subpolicy] Rows inspected: {len(rows)}")
    for key, value in subtype_counts.most_common():
        print(f"[segment_token_gender_subpolicy] subtype {key}: {value}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_gender_subpolicy] status {key}: {value}")
    print("[segment_token_gender_subpolicy] Apply allowed: 0")
    print(f"[segment_token_gender_subpolicy] Report: {txt_path}")
    print(f"[segment_token_gender_subpolicy] CSV: {csv_path}")
    print(f"[segment_token_gender_subpolicy] JSONL: {jsonl_path}")
    print(f"[segment_token_gender_subpolicy] Decisions template: {template_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify gender-token policy rows into narrower learning subtypes.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--include-apply-approved", action="store_true")
    parser.add_argument("--undecided-only", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--statuses", default=None)
    parser.add_argument("--subtypes", default=None)
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        pending_apply_only=not args.include_all,
        skip_apply_approved=not args.include_apply_approved,
        undecided_only=args.undecided_only,
        limit=args.limit,
        statuses_value=args.statuses,
        subtypes_value=args.subtypes,
    )
