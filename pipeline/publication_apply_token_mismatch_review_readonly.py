from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import (
    evaluate_best_candidates,
    fetch_candidates,
    latest_state_run_id,
    parse_review_states,
    short,
    structural_tokens,
)
from segment_token_mismatch_queue import classify_diff
from segment_token_policy import classify_policy, risk_rank


RULE_VERSION = "publication_apply_token_mismatch_review_readonly_v1"


def split_segment_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    segment_ids: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            segment_ids.add(int(token))
        except ValueError as exc:
            raise RuntimeError(f"Invalid segment id: {token}") from exc
    return segment_ids


def reports_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_publication_apply_token_mismatch_review_readonly"


def token_diff_payload(row: dict[str, Any]) -> dict[str, Any]:
    source_tokens = structural_tokens(row.get("spanish_text"))
    confirmed_tokens = structural_tokens(row.get("confirmed_text"))
    missing = sorted((source_tokens - confirmed_tokens).elements())
    extra = sorted((confirmed_tokens - source_tokens).elements())
    diff_kind = classify_diff(missing, extra)
    enriched = dict(row)
    enriched["missing_tokens"] = missing
    enriched["extra_tokens"] = extra
    enriched["diff_kind"] = diff_kind
    enriched["policy"] = classify_policy(enriched)
    return enriched


def suggested_next_action(row: dict[str, Any]) -> str:
    policy = row["policy"]
    bucket = policy.policy_bucket
    if bucket.startswith("blocked_"):
        return "hold_blocked_token_policy"
    if bucket.startswith("manual_exception_candidate"):
        return "review_for_manual_token_policy_exception"
    if bucket.startswith("policy_candidate"):
        return "sample_then_create_token_policy_decision"
    if policy.risk_level in {"critical", "high"}:
        return "manual_token_policy_review"
    return "small_batch_review_before_policy"


def write_outputs(
    settings: dict[str, Any],
    *,
    state_run_id: int,
    review_states: list[str],
    apply_counts: Counter,
    rows: list[dict[str, Any]],
) -> tuple[Path, Path, Path, Path]:
    base = reports_base(settings)
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    csv_path = base.with_suffix(".csv")
    summary_path = base.parent / f"{base.name}_summary.json"

    sorted_rows = sorted(
        rows,
        key=lambda row: (
            risk_rank(row["policy"].risk_level),
            row["policy"].policy_bucket,
            row["relative_path"],
            int(row.get("source_line_number") or 0),
            int(row["segment_id"]),
        ),
    )

    risk_counts = Counter(row["policy"].risk_level for row in rows)
    bucket_counts = Counter(row["policy"].policy_bucket for row in rows)
    diff_counts = Counter(row["diff_kind"] for row in rows)
    review_counts = Counter(row["review_state"] for row in rows)
    action_counts = Counter(suggested_next_action(row) for row in rows)
    label_counts = Counter(row.get("confirmation_label") or "<none>" for row in rows)
    source_counts = Counter(row.get("confirmation_source") or "<none>" for row in rows)
    package_counts = Counter((row.get("relative_path") or "").split("/", 1)[0] for row in rows)

    summary = {
        "source": RULE_VERSION,
        "state_run_id": state_run_id,
        "review_states": review_states,
        "apply_status_counts": dict(apply_counts),
        "token_mismatch_count": len(rows),
        "risk_counts": dict(risk_counts),
        "policy_bucket_counts": dict(bucket_counts),
        "diff_kind_counts": dict(diff_counts),
        "review_state_counts": dict(review_counts),
        "suggested_next_action_counts": dict(action_counts),
        "confirmation_label_counts": dict(label_counts),
        "confirmation_source_counts": dict(source_counts),
        "package_counts": dict(package_counts),
        "outputs": {
            "markdown": str(md_path),
            "jsonl": str(jsonl_path),
            "csv": str(csv_path),
        },
        "guards": {
            "read_only": True,
            "db_writes": 0,
            "output_writes": 0,
            "segment_state": 0,
        },
    }

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in sorted_rows:
            policy = row["policy"]
            payload = {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "review_state": row["review_state"],
                "final_state": row["final_state"],
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "locked": row.get("locked"),
                "diff_kind": row["diff_kind"],
                "policy_bucket": policy.policy_bucket,
                "risk_level": policy.risk_level,
                "recommendation": policy.recommendation,
                "issue_flags": policy.issue_flags,
                "suggested_next_action": suggested_next_action(row),
                "missing_tokens": row["missing_tokens"],
                "extra_tokens": row["extra_tokens"],
                "old_text": row.get("old_text"),
                "output_text": row.get("current_output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "spanish_text": row.get("spanish_text"),
                "english_text": row.get("english_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    fieldnames = [
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "review_state",
        "confirmation_source",
        "confirmation_label",
        "diff_kind",
        "policy_bucket",
        "risk_level",
        "suggested_next_action",
        "missing_tokens",
        "extra_tokens",
        "output_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted_rows:
            policy = row["policy"]
            writer.writerow(
                {
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "review_state": row["review_state"],
                    "confirmation_source": row.get("confirmation_source"),
                    "confirmation_label": row.get("confirmation_label"),
                    "diff_kind": row["diff_kind"],
                    "policy_bucket": policy.policy_bucket,
                    "risk_level": policy.risk_level,
                    "suggested_next_action": suggested_next_action(row),
                    "missing_tokens": json.dumps(row["missing_tokens"], ensure_ascii=False),
                    "extra_tokens": json.dumps(row["extra_tokens"], ensure_ascii=False),
                    "output_text": row.get("current_output_text"),
                    "confirmed_text": row.get("confirmed_text"),
                }
            )

    lines = [
        "# Publication Apply Token Mismatch Review",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Segment-state run: `{state_run_id}`",
        f"- Review states: `{', '.join(review_states)}`",
        f"- Token mismatch rows: `{len(rows)}`",
        "- Guard: read-only; no DB writes, no output writes, no segment-state.",
        "",
        "## Apply Status",
        "",
        *[f"- `{key}`: `{value}`" for key, value in apply_counts.most_common()],
        "",
        "## Risk",
        "",
        *[f"- `{key}`: `{value}`" for key, value in risk_counts.most_common()],
        "",
        "## Policy Buckets",
        "",
        *[f"- `{key}`: `{value}`" for key, value in bucket_counts.most_common()],
        "",
        "## Suggested Next Actions",
        "",
        *[f"- `{key}`: `{value}`" for key, value in action_counts.most_common()],
        "",
        "## Review States",
        "",
        *[f"- `{key}`: `{value}`" for key, value in review_counts.most_common()],
        "",
        "## Priority Sample",
        "",
    ]
    for row in sorted_rows[:80]:
        policy = row["policy"]
        lines.extend(
            [
                (
                    f"### Segment `{row['segment_id']}` | `{policy.risk_level}` | "
                    f"`{policy.policy_bucket}`"
                ),
                "",
                f"- Path: `{row['relative_path']}:{row['source_line_number']}`",
                f"- Key: `{row['source_key']}`",
                f"- Review: `{row['review_state']}` / `{row.get('confirmation_label') or '<none>'}`",
                f"- Next action: `{suggested_next_action(row)}`",
                f"- Missing tokens: `{json.dumps(row['missing_tokens'], ensure_ascii=False)}`",
                f"- Extra tokens: `{json.dumps(row['extra_tokens'], ensure_ascii=False)}`",
                f"- Output: `{short(row.get('current_output_text'))}`",
                f"- Confirmed: `{short(row.get('confirmed_text'))}`",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return md_path, jsonl_path, csv_path, summary_path


def main(
    *,
    state_run_id: int | None = None,
    segment_ids_csv: str | None = None,
    review_states_csv: str | None = None,
    include_auto_confirmed: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    review_states = parse_review_states(review_states_csv, include_auto_confirmed)
    segment_ids = split_segment_ids(segment_ids_csv)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        candidates = fetch_candidates(
            conn,
            state_run_id=selected_state_run_id,
            review_states=review_states,
            limit=limit,
            path_like=None,
            segment_ids=segment_ids,
            include_intentional_blank=False,
            require_token_policy_decision=False,
            token_policy_run_id=None,
        )
        apply_counts, previews = evaluate_best_candidates(
            candidates,
            output_root=output_root,
            allow_locked_token_override=False,
            require_token_policy_decision=False,
            include_intentional_blank=False,
        )
    rows = [token_diff_payload(row) for row, status, _current_line, _new_line in previews if status == "token_mismatch"]
    md_path, jsonl_path, csv_path, summary_path = write_outputs(
        settings,
        state_run_id=selected_state_run_id,
        review_states=review_states,
        apply_counts=apply_counts,
        rows=rows,
    )
    result = {
        "state_run_id": selected_state_run_id,
        "candidates": len(candidates),
        "token_mismatch": len(rows),
        "apply_status_counts": dict(apply_counts),
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "csv": str(csv_path),
        "summary": str(summary_path),
    }
    print("[publication_apply_token_mismatch_review_readonly] Review generated")
    print(f"[publication_apply_token_mismatch_review_readonly] State run id: {selected_state_run_id}")
    print(f"[publication_apply_token_mismatch_review_readonly] Candidates: {len(candidates)}")
    print(f"[publication_apply_token_mismatch_review_readonly] Token mismatch: {len(rows)}")
    print(f"[publication_apply_token_mismatch_review_readonly] Markdown: {md_path}")
    print(f"[publication_apply_token_mismatch_review_readonly] JSONL: {jsonl_path}")
    print(f"[publication_apply_token_mismatch_review_readonly] Summary: {summary_path}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only review for publication apply rows blocked by token mismatch.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--segment-ids", default=None)
    parser.add_argument("--review-states", default=None)
    parser.add_argument("--include-auto-confirmed", action="store_true", default=True)
    parser.add_argument("--exclude-auto-confirmed", action="store_false", dest="include_auto_confirmed")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        segment_ids_csv=args.segment_ids,
        review_states_csv=args.review_states,
        include_auto_confirmed=args.include_auto_confirmed,
        limit=args.limit,
    )
