from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import structural_tokens
from publication_token_mismatch_old_safe_fix_queue import (
    fetch_rows,
    latest_policy_run_id,
    latest_state_run_id,
)


RULE_VERSION = "publication_token_mismatch_remaining_rewrite_queue_v1"


def reports_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_publication_token_mismatch_remaining_rewrite_queue"


def route(row: dict[str, Any]) -> str:
    bucket = row.get("policy_bucket") or ""
    diff_kind = row.get("diff_kind") or ""
    if "select_cstring" in bucket or "select_cstring" in diff_kind:
        return "rewrite_select_cstring_current_tokens"
    if "dynamic_scope" in bucket or diff_kind == "bracket_token_changed":
        return "rewrite_dynamic_scope_current_tokens"
    return "rewrite_mixed_current_tokens"


def token_delta(row: dict[str, Any]) -> dict[str, list[str]]:
    source_tokens = structural_tokens(row.get("spanish_text"))
    confirmed_tokens = structural_tokens(row.get("confirmed_text"))
    old_tokens = structural_tokens(row.get("old_text"))
    return {
        "confirmed_missing_from_source": [token for token in source_tokens if token not in confirmed_tokens],
        "confirmed_extra_vs_source": [token for token in confirmed_tokens if token not in source_tokens],
        "old_missing_from_source": [token for token in source_tokens if token not in old_tokens],
        "old_extra_vs_source": [token for token in old_tokens if token not in source_tokens],
    }


def build_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if structural_tokens(row.get("old_text")) == structural_tokens(row.get("spanish_text")):
            continue
        payload = dict(row)
        payload["rewrite_route"] = route(row)
        payload["token_delta"] = token_delta(row)
        out.append(payload)
    return out


def write_outputs(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    state_run_id: int,
    policy_run_id: int,
) -> tuple[Path, Path, Path, Path]:
    base = reports_base(settings)
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    csv_path = base.with_suffix(".csv")
    summary_path = base.parent / f"{base.name}_summary.json"

    route_counts = Counter(row["rewrite_route"] for row in rows)
    bucket_counts = Counter(row.get("policy_bucket") or "<none>" for row in rows)
    diff_counts = Counter(row.get("diff_kind") or "<none>" for row in rows)
    review_counts = Counter(row.get("review_state") or "<none>" for row in rows)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                "segment_id": row["segment_id"],
                "policy_item_id": row["policy_item_id"],
                "policy_run_id": policy_run_id,
                "state_run_id": state_run_id,
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "review_state": row["review_state"],
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_label": row.get("confirmation_label"),
                "policy_bucket": row.get("policy_bucket"),
                "risk_level": row.get("risk_level"),
                "diff_kind": row.get("diff_kind"),
                "rewrite_route": row["rewrite_route"],
                "spanish_text": row.get("spanish_text"),
                "old_text": row.get("old_text"),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "token_delta": row["token_delta"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    fieldnames = [
        "segment_id",
        "policy_item_id",
        "rewrite_route",
        "policy_bucket",
        "risk_level",
        "diff_kind",
        "relative_path",
        "source_key",
        "old_text",
        "confirmed_text",
        "output_text",
        "spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})

    summary = {
        "source": RULE_VERSION,
        "state_run_id": state_run_id,
        "policy_run_id": policy_run_id,
        "remaining_rewrite_count": len(rows),
        "route_counts": dict(route_counts),
        "policy_bucket_counts": dict(bucket_counts),
        "diff_kind_counts": dict(diff_counts),
        "review_state_counts": dict(review_counts),
        "recommendation": (
            "Do not apply these automatically. Rewrite confirmed_text against current source token "
            "signature, then rerun token-policy decisions and protected apply dry-run."
        ),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# Publication Token Mismatch Remaining Rewrite Queue\n\n")
        handle.write(f"- Source: `{RULE_VERSION}`\n")
        handle.write(f"- Segment-state run: `{state_run_id}`\n")
        handle.write(f"- Token policy run: `{policy_run_id}`\n")
        handle.write(f"- Remaining rewrite count: `{len(rows)}`\n\n")
        handle.write("## Routes\n\n")
        for name, count in route_counts.most_common():
            handle.write(f"- `{name}`: `{count}`\n")
        handle.write("\n## Policy Buckets\n\n")
        for name, count in bucket_counts.most_common():
            handle.write(f"- `{name}`: `{count}`\n")
        handle.write("\n## Next Step\n\n")
        handle.write(
            "These rows are not safe for output apply yet. The stable old text does not match the "
            "current source token signature, so each row needs a current-token rewrite before it can "
            "enter the protected apply queue.\n\n"
        )
        for route_name, _ in route_counts.most_common():
            handle.write(f"## {route_name}\n\n")
            route_rows = [row for row in rows if row["rewrite_route"] == route_name]
            for row in route_rows[:50]:
                handle.write(f"### Segment `{row['segment_id']}`\n\n")
                handle.write(f"- File/key: `{row['relative_path']} :: {row['source_key']}`\n")
                handle.write(f"- Bucket: `{row.get('policy_bucket')}` / `{row.get('diff_kind')}`\n")
                handle.write(f"- Old: `{row.get('old_text')}`\n")
                handle.write(f"- Confirmed: `{row.get('confirmed_text')}`\n")
                handle.write(f"- Source: `{row.get('spanish_text')}`\n\n")
            if len(route_rows) > 50:
                handle.write(f"_Truncated in Markdown: {len(route_rows) - 50} more rows in JSONL/CSV._\n\n")

    return md_path, jsonl_path, csv_path, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-run-id", type=int)
    parser.add_argument("--policy-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    conn = db.connect(settings)
    try:
        state_run_id = args.state_run_id or latest_state_run_id(conn)
        policy_run_id = args.policy_run_id or latest_policy_run_id(conn, state_run_id)
        rows = build_rows(fetch_rows(conn, state_run_id=state_run_id, policy_run_id=policy_run_id))
        md_path, jsonl_path, csv_path, summary_path = write_outputs(
            settings,
            rows=rows,
            state_run_id=state_run_id,
            policy_run_id=policy_run_id,
        )
    finally:
        conn.close()

    print(f"Remaining rewrite rows: {len(rows)}")
    print(f"Markdown: {md_path}")
    print(f"JSONL: {jsonl_path}")
    print(f"CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
