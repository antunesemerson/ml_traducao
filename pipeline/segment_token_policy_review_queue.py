from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "segment_token_policy_review_queue_v1"

DEFAULT_BUCKET_ORDER = (
    "blocked_suspicious_confirmed_text",
    "blocked_variable_or_icon_change",
    "manual_exception_candidate_select_cstring",
    "manual_exception_candidate_gender_token",
    "review_select_cstring_change",
    "review_dynamic_scope_change",
    "review_mixed_token_change",
    "review_token_removed",
    "review_concept_simplification",
    "review_token_added",
    "review_gender_token_change",
    "policy_candidate_format_tag",
)

RISK_RANK = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


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


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


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


def review_labels_for(row: dict[str, Any]) -> list[str]:
    bucket = row["policy_bucket"]
    if bucket == "blocked_suspicious_confirmed_text":
        return ["fix_confirmed_text", "reject_policy_candidate", "encoding_cleanup_required"]
    if bucket == "blocked_variable_or_icon_change":
        labels = ["reject_policy_candidate", "manual_token_rewrite_required", "needs_subpolicy"]
        if "confirmed_text_suspicious_mojibake" in parse_json_list(row.get("issue_flags_json")):
            labels.insert(1, "fix_confirmed_text")
        return labels
    if bucket in {"manual_exception_candidate_select_cstring", "manual_exception_candidate_gender_token"}:
        return ["keep_manual_exception_only", "accept_policy_candidate", "needs_subpolicy"]
    if bucket == "review_gender_token_change":
        return ["accept_policy_candidate", "keep_manual_exception_only", "reject_policy_candidate"]
    if bucket == "review_select_cstring_change":
        return ["keep_manual_exception_only", "reject_policy_candidate", "needs_subpolicy"]
    if bucket == "review_concept_simplification":
        return ["accept_policy_candidate", "keep_manual_exception_only", "reject_policy_candidate"]
    if bucket == "policy_candidate_format_tag":
        return ["accept_policy_candidate", "reject_policy_candidate"]
    return ["keep_manual_exception_only", "reject_policy_candidate", "needs_subpolicy"]


def fetch_rows(
    conn,
    *,
    policy_run_id: int,
    buckets: list[str],
    risks: list[str],
    path_like: str | None,
    pending_apply_only: bool,
    skip_apply_approved: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = [policy_run_id]
    where = ["i.run_id = ?"]
    joins = [
        "JOIN source_segments s ON s.id = i.segment_id",
        "LEFT JOIN output_segments o ON o.segment_id = i.segment_id",
        "LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id",
    ]

    if buckets:
        placeholders = ", ".join("?" for _ in buckets)
        where.append(f"i.policy_bucket IN ({placeholders})")
        params.extend(buckets)
    if risks:
        placeholders = ", ".join("?" for _ in risks)
        where.append(f"i.risk_level IN ({placeholders})")
        params.extend(risks)
    if path_like:
        where.append("i.relative_path LIKE ?")
        params.append(path_like)
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
        LEFT JOIN segment_token_policy_decisions current_d
          ON current_d.policy_run_id = i.run_id
         AND current_d.policy_item_id = i.id
         AND current_d.approved_for_apply = 1
            """
        )
        where.append("current_d.id IS NULL")

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
            i.auto_apply_allowed,
            i.needs_human_review,
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
            CASE i.risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
            END,
            i.policy_bucket,
            i.relative_path,
            i.source_line_number,
            i.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_balanced(rows: list[dict[str, Any]], *, per_bucket: int, limit: int | None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["policy_bucket"]].append(row)

    ordered_buckets = [bucket for bucket in DEFAULT_BUCKET_ORDER if bucket in grouped]
    ordered_buckets.extend(sorted(bucket for bucket in grouped if bucket not in DEFAULT_BUCKET_ORDER))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for bucket in ordered_buckets:
        for row in grouped[bucket][:per_bucket]:
            if limit is not None and len(selected) >= limit:
                return selected
            selected.append(row)
            selected_ids.add(int(row["policy_item_id"]))

    if limit is not None and len(selected) < limit:
        for row in rows:
            if int(row["policy_item_id"]) in selected_ids:
                continue
            selected.append(row)
            if len(selected) >= limit:
                break

    return selected


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["missing_tokens"] = parse_json_list(row.get("missing_tokens_json"))
    payload["extra_tokens"] = parse_json_list(row.get("extra_tokens_json"))
    payload["issue_flags"] = parse_json_list(row.get("issue_flags_json"))
    payload["suggested_review_labels"] = review_labels_for(row)
    return payload


def write_outputs(
    settings: dict,
    *,
    policy_run_id: int,
    all_rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]],
    per_bucket: int,
    limit: int | None,
    pending_apply_only: bool,
    skip_apply_approved: bool,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bucket_values = sorted({str(row["policy_bucket"]) for row in selected_rows})
    risk_values = sorted({str(row["risk_level"]) for row in selected_rows})
    filter_slug = ""
    if len(bucket_values) == 1:
        filter_slug = "_" + bucket_values[0]
    elif len(risk_values) == 1:
        filter_slug = "_" + risk_values[0]
    base = reports_dir / f"{timestamp}_segment_token_policy_review_queue{filter_slug}"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    selected = [enrich(row) for row in selected_rows]
    all_risk_counts = Counter(row["risk_level"] for row in all_rows)
    all_bucket_counts = Counter(row["policy_bucket"] for row in all_rows)
    selected_bucket_counts = Counter(row["policy_bucket"] for row in selected)

    fieldnames = [
        "policy_item_id",
        "policy_run_id",
        "state_run_id",
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
        "suggested_review_labels",
        "output_text",
        "confirmed_text",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "policy_item_id": row["policy_item_id"],
                    "policy_run_id": row["policy_run_id"],
                    "state_run_id": row["state_run_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "review_state": row["review_state"],
                    "diff_kind": row["diff_kind"],
                    "policy_bucket": row["policy_bucket"],
                    "risk_level": row["risk_level"],
                    "recommendation": row["recommendation"],
                    "issue_flags": json.dumps(row["issue_flags"], ensure_ascii=False),
                    "missing_tokens": json.dumps(row["missing_tokens"], ensure_ascii=False),
                    "extra_tokens": json.dumps(row["extra_tokens"], ensure_ascii=False),
                    "suggested_review_labels": json.dumps(row["suggested_review_labels"], ensure_ascii=False),
                    "output_text": row["output_text"],
                    "confirmed_text": row["confirmed_text"],
                    "spanish_text": row["spanish_text"],
                    "english_text": row["english_text"],
                    "old_text": row["old_text"],
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "policy_run_id": row["policy_run_id"],
                        "state_run_id": row["state_run_id"],
                        "segment_id": row["segment_id"],
                        "relative_path": row["relative_path"],
                        "source_line_number": row["source_line_number"],
                        "source_key": row["source_key"],
                        "review_state": row["review_state"],
                        "diff_kind": row["diff_kind"],
                        "policy_bucket": row["policy_bucket"],
                        "risk_level": row["risk_level"],
                        "recommendation": row["recommendation"],
                        "issue_flags": row["issue_flags"],
                        "missing_tokens": row["missing_tokens"],
                        "extra_tokens": row["extra_tokens"],
                        "suggested_review_labels": row["suggested_review_labels"],
                        "spanish_text": row["spanish_text"],
                        "english_text": row["english_text"],
                        "old_text": row["old_text"],
                        "output_text": row["output_text"],
                        "confirmed_text": row["confirmed_text"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    lines = [
        "Segment token policy review queue",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Candidates in source policy run: {len(all_rows)}",
        f"Selected for review: {len(selected)}",
        f"Per bucket: {per_bucket}",
        f"Global limit: {limit if limit is not None else 'none'}",
        f"Pending apply only: {pending_apply_only}",
        f"Skip apply-approved decisions: {skip_apply_approved}",
        "",
        "Decision labels for human review:",
        "- accept_policy_candidate: pattern can feed a future safe policy after enough similar samples.",
        "- keep_manual_exception_only: valid human choice, but too contextual to generalize.",
        "- reject_policy_candidate: confirmed text should not replace output as-is.",
        "- fix_confirmed_text: confirmed text is semantically useful but needs correction before use.",
        "- needs_subpolicy: pattern deserves a smaller specialist rule before promotion.",
        "",
        "Risk distribution in policy run:",
        *[f"- {key}: {value}" for key, value in all_risk_counts.most_common()],
        "",
        "Policy buckets in policy run:",
        *[f"- {key}: {value}" for key, value in all_bucket_counts.most_common()],
        "",
        "Selected by bucket:",
        *[f"- {key}: {value}" for key, value in selected_bucket_counts.most_common()],
        "",
        "Priority sample:",
    ]
    for row in selected[:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['risk_level']} | {row['policy_bucket']}"
                ),
                f"  LABELS: {', '.join(row['suggested_review_labels'])}",
                f"  FLAGS: {', '.join(row['issue_flags'])}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  OUTPUT: {short(row['output_text'])}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(
    *,
    policy_run_id: int | None = None,
    per_bucket: int = 25,
    limit: int | None = None,
    buckets_csv: str | None = None,
    risks_csv: str | None = None,
    path_like: str | None = None,
    pending_apply_only: bool = False,
    skip_apply_approved: bool = False,
) -> None:
    settings = db.load_settings()
    buckets = split_csv(buckets_csv)
    risks = split_csv(risks_csv)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        rows = fetch_rows(
            conn,
            policy_run_id=selected_policy_run_id,
            buckets=buckets,
            risks=risks,
            path_like=path_like,
            pending_apply_only=pending_apply_only,
            skip_apply_approved=skip_apply_approved,
        )
    selected = select_balanced(rows, per_bucket=per_bucket, limit=limit)
    txt_path, csv_path, jsonl_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        all_rows=rows,
        selected_rows=selected,
        per_bucket=per_bucket,
        limit=limit,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
    )

    print("[segment_token_policy_review_queue] Queue generated")
    print(f"[segment_token_policy_review_queue] Rule version: {RULE_VERSION}")
    print(f"[segment_token_policy_review_queue] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_policy_review_queue] Candidates in source policy run: {len(rows)}")
    print(f"[segment_token_policy_review_queue] Selected for review: {len(selected)}")
    print(f"[segment_token_policy_review_queue] Report: {txt_path}")
    print(f"[segment_token_policy_review_queue] CSV: {csv_path}")
    print(f"[segment_token_policy_review_queue] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a balanced review queue from segment token policy items.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--per-bucket", type=int, default=25)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--buckets", default=None)
    parser.add_argument("--risks", default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--pending-apply-only", action="store_true")
    parser.add_argument("--skip-apply-approved", action="store_true")
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        per_bucket=args.per_bucket,
        limit=args.limit,
        buckets_csv=args.buckets,
        risks_csv=args.risks,
        path_like=args.path_like,
        pending_apply_only=args.pending_apply_only,
        skip_apply_approved=args.skip_apply_approved,
    )
