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
from segment_token_gender_simple_evidence_queue import simple_agent_for, token_method
from segment_token_gender_subpolicy import classify_row as classify_gender_row
from segment_token_policy_decision_rebase import (
    fetch_needs_apply_rows,
    latest_policy_run_id,
    latest_state_run_id,
)
from segment_token_policy_review_queue import parse_json_list, review_labels_for


RULE_VERSION = "segment_token_needs_apply_diagnostic_v1"
DEFAULT_QUEUE_STATUS = "no_approved_token_policy_decision"


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def reports_base(settings: dict[str, Any], suffix: str) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_{suffix}"


def fetch_source_context(conn, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ", ".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT id AS segment_id, english_text, spanish_text, old_text
        FROM source_segments
        WHERE id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_policy_context(conn, policy_item_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not policy_item_ids:
        return {}
    placeholders = ", ".join("?" for _ in policy_item_ids)
    rows = conn.execute(
        f"""
        SELECT
            id AS policy_item_id,
            diff_kind,
            recommendation,
            missing_tokens_json,
            extra_tokens_json,
            issue_flags_json
        FROM segment_token_policy_items
        WHERE id IN ({placeholders})
        """,
        tuple(policy_item_ids),
    ).fetchall()
    return {int(row["policy_item_id"]): dict(row) for row in rows}


def enrich_rows(conn, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_by_segment = fetch_source_context(conn, [int(row["segment_id"]) for row in rows])
    policy_ids = [int(row["new_policy_item_id"]) for row in rows if row.get("new_policy_item_id")]
    policy_by_item = fetch_policy_context(conn, policy_ids)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload.update(source_by_segment.get(int(row["segment_id"]), {}))
        policy_context = policy_by_item.get(int(row.get("new_policy_item_id") or 0), {})
        payload.update(policy_context)
        payload["policy_bucket"] = payload.get("new_policy_bucket") or payload.get("old_policy_bucket") or ""
        payload["risk_level"] = payload.get("new_risk_level") or payload.get("old_risk_level") or ""
        payload["missing_tokens"] = parse_json_list(payload.get("missing_tokens_json"))
        payload["extra_tokens"] = parse_json_list(payload.get("extra_tokens_json"))
        payload["issue_flags"] = parse_json_list(payload.get("issue_flags_json"))
        payload["suggested_review_labels"] = review_labels_for(payload)
        if payload["policy_bucket"] == "review_gender_token_change":
            gender_payload = classify_gender_row(payload)
            split_agent, split_maturity, split_next_action, split_reasons = simple_agent_for(gender_payload)
            missing_methods = [token_method(token) for token in gender_payload["missing_tokens"]]
            extra_methods = [token_method(token) for token in gender_payload["extra_tokens"]]
            payload.update(
                {
                    "gender_subtype": gender_payload["gender_subtype"],
                    "gender_subpolicy_status": gender_payload["subpolicy_status"],
                    "gender_split_agent": split_agent,
                    "gender_split_maturity": split_maturity,
                    "gender_split_next_action": split_next_action,
                    "gender_split_reasons": split_reasons,
                    "gender_method_signature": f"{','.join(missing_methods)} -> {','.join(extra_methods)}",
                }
            )
        else:
            payload.update(
                {
                    "gender_subtype": "",
                    "gender_subpolicy_status": "",
                    "gender_split_agent": "",
                    "gender_split_maturity": "",
                    "gender_split_next_action": "",
                    "gender_split_reasons": [],
                    "gender_method_signature": "",
                }
            )
        enriched.append(payload)
    return enriched


def select_review_rows(
    rows: list[dict[str, Any]],
    *,
    statuses: set[str],
    limit: int | None,
    per_bucket: int | None,
) -> list[dict[str, Any]]:
    filtered = [row for row in rows if row["rebase_status"] in statuses]
    if per_bucket is None:
        selected = filtered
    else:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in filtered:
            grouped[row.get("policy_bucket") or "<none>"].append(row)
        selected = []
        for bucket in sorted(grouped):
            selected.extend(grouped[bucket][:per_bucket])
    if limit is not None:
        selected = selected[:limit]
    return selected


def write_outputs(
    settings: dict[str, Any],
    *,
    state_run_id: int,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]],
    statuses: set[str],
) -> tuple[Path, Path, Path]:
    base = reports_base(settings, "segment_token_needs_apply_diagnostic")
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    status_counts = Counter(row["rebase_status"] for row in rows)
    bucket_counts = Counter((row["rebase_status"], row.get("policy_bucket") or "", row.get("risk_level") or "") for row in rows)
    selected_bucket_counts = Counter((row.get("policy_bucket") or "", row.get("risk_level") or "") for row in selected_rows)
    selected_gender_agent_counts = Counter(
        (row.get("gender_split_agent") or "", row.get("gender_split_maturity") or "")
        for row in selected_rows
        if row.get("gender_split_agent")
    )
    package_counts = Counter(row["relative_path"] for row in selected_rows)

    fieldnames = [
        "rebase_status",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "review_state",
        "confirmation_label",
        "policy_bucket",
        "risk_level",
        "diff_kind",
        "recommendation",
        "gender_subtype",
        "gender_subpolicy_status",
        "gender_split_agent",
        "gender_split_maturity",
        "gender_method_signature",
        "issue_flags",
        "missing_tokens",
        "extra_tokens",
        "suggested_review_labels",
        "current_output_text",
        "confirmed_text",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected_rows:
            writer.writerow(
                {
                    "rebase_status": row["rebase_status"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "review_state": row["review_state"],
                    "confirmation_label": row.get("confirmation_label"),
                    "policy_bucket": row.get("policy_bucket"),
                    "risk_level": row.get("risk_level"),
                    "diff_kind": row.get("diff_kind"),
                    "recommendation": row.get("recommendation"),
                    "gender_subtype": row.get("gender_subtype"),
                    "gender_subpolicy_status": row.get("gender_subpolicy_status"),
                    "gender_split_agent": row.get("gender_split_agent"),
                    "gender_split_maturity": row.get("gender_split_maturity"),
                    "gender_method_signature": row.get("gender_method_signature"),
                    "issue_flags": json.dumps(row["issue_flags"], ensure_ascii=False),
                    "missing_tokens": json.dumps(row["missing_tokens"], ensure_ascii=False),
                    "extra_tokens": json.dumps(row["extra_tokens"], ensure_ascii=False),
                    "suggested_review_labels": json.dumps(row["suggested_review_labels"], ensure_ascii=False),
                    "current_output_text": row.get("current_output_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "spanish_text": row.get("spanish_text"),
                    "english_text": row.get("english_text"),
                    "old_text": row.get("old_text"),
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in selected_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Segment token needs-apply diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"State run id: {state_run_id}",
        f"Policy run id: {policy_run_id}",
        f"Rows inspected: {len(rows):,}",
        f"Review statuses selected: {', '.join(sorted(statuses))}",
        f"Selected for review: {len(selected_rows):,}",
        "",
        "Interpretation:",
        f"- Existing token decisions still current by output hash: {status_counts['already_current']:,}",
        f"- Missing approved token policy decision: {status_counts['no_approved_token_policy_decision']:,}",
        f"- Safe automatic rebases available: {status_counts['safe_rebase_same_token_signature']:,}",
        "",
        "Status counts:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Top status/bucket/risk:",
    ]
    for (status, bucket, risk), value in bucket_counts.most_common(30):
        lines.append(f"- {status} | {bucket or '<none>'} | {risk or '<none>'}: {value:,}")
    lines.extend(["", "Selected bucket/risk:"])
    for (bucket, risk), value in selected_bucket_counts.most_common(30):
        lines.append(f"- {bucket or '<none>'} | {risk or '<none>'}: {value:,}")
    lines.extend(["", "Selected gender split agents:"])
    if selected_gender_agent_counts:
        for (agent, maturity), value in selected_gender_agent_counts.most_common(30):
            lines.append(f"- {agent} | {maturity}: {value:,}")
    else:
        lines.append("- none")
    lines.extend(["", "Selected packages:"])
    for package, value in package_counts.most_common(30):
        lines.append(f"- {package}: {value:,}")
    lines.extend(["", "Priority sample:"])
    for row in selected_rows[:80]:
        lines.extend(
            [
                (
                    f"- {row['rebase_status']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row.get('risk_level')} | {row.get('policy_bucket')}"
                ),
                f"  LABELS: {', '.join(row['suggested_review_labels'])}",
                f"  FLAGS: {', '.join(row['issue_flags'])}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  OUTPUT: {short(row.get('current_output_text'))}",
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Diagnostic only: no output files are read or written.",
            "- This report uses database text fields and token-policy metadata only.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(
    *,
    state_run_id: int | None = None,
    policy_run_id: int | None = None,
    queue_statuses_csv: str | None = DEFAULT_QUEUE_STATUS,
    limit: int | None = None,
    per_bucket: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn, selected_state_run_id)
        rows = fetch_needs_apply_rows(
            conn,
            state_run_id=selected_state_run_id,
            policy_run_id=selected_policy_run_id,
        )
        rows = enrich_rows(conn, rows)
        statuses = set(split_csv(queue_statuses_csv) or [DEFAULT_QUEUE_STATUS])
        selected_rows = select_review_rows(rows, statuses=statuses, limit=limit, per_bucket=per_bucket)

    txt_path, csv_path, jsonl_path = write_outputs(
        settings,
        state_run_id=selected_state_run_id,
        policy_run_id=selected_policy_run_id,
        rows=rows,
        selected_rows=selected_rows,
        statuses=statuses,
    )
    counts = Counter(row["rebase_status"] for row in rows)
    print("[segment_token_needs_apply_diagnostic] Diagnostic generated")
    print(f"[segment_token_needs_apply_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[segment_token_needs_apply_diagnostic] State run id: {selected_state_run_id}")
    print(f"[segment_token_needs_apply_diagnostic] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_needs_apply_diagnostic] Rows inspected: {len(rows):,}")
    print(f"[segment_token_needs_apply_diagnostic] Existing decisions current: {counts['already_current']:,}")
    print(f"[segment_token_needs_apply_diagnostic] Missing approved decisions: {counts['no_approved_token_policy_decision']:,}")
    print(f"[segment_token_needs_apply_diagnostic] Selected for review: {len(selected_rows):,}")
    print(f"[segment_token_needs_apply_diagnostic] Report: {txt_path}")
    print(f"[segment_token_needs_apply_diagnostic] CSV: {csv_path}")
    print(f"[segment_token_needs_apply_diagnostic] JSONL: {jsonl_path}")
    return {
        "state_run_id": selected_state_run_id,
        "policy_run_id": selected_policy_run_id,
        "rows": len(rows),
        "already_current": counts["already_current"],
        "missing_decisions": counts["no_approved_token_policy_decision"],
        "selected": len(selected_rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose needs-apply token-policy blockers and build a focused review queue.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--queue-statuses", default=DEFAULT_QUEUE_STATUS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-bucket", type=int, default=None)
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        policy_run_id=args.policy_run_id,
        queue_statuses_csv=args.queue_statuses,
        limit=args.limit,
        per_bucket=args.per_bucket,
    )
