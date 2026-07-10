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
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "publication_token_mismatch_old_safe_fix_queue_v1"
DECISION = "manual_token_rewrite_required"
REVIEWER = "codex_publication_token_mismatch_old_safe_fix"


def reports_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_publication_token_mismatch_old_safe_fix_queue"


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE total_segments > 1000
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_state run found.")
    return int(row["id"])


def latest_policy_run_id(conn, state_run_id: int) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_runs
        WHERE state_run_id = ?
          AND finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (state_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No complete token policy run found for segment-state {state_run_id}.")
    return int(row["id"])


def fetch_rows(conn, *, state_run_id: int, policy_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            i.id AS state_item_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.review_state,
            i.final_state,
            i.confirmation_level,
            i.confirmation_label,
            i.locked,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text AS output_text,
            sc.confirmed_text,
            sc.confirmation_source,
            sc.candidate_id,
            pi.id AS policy_item_id,
            pi.policy_bucket,
            pi.risk_level,
            pi.diff_kind,
            pi.recommendation,
            pi.missing_tokens_json,
            pi.extra_tokens_json,
            pi.issue_flags_json,
            d.id AS decision_id,
            d.decision,
            d.approved_for_apply
        FROM segment_state_items i
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        JOIN segment_token_policy_items pi
          ON pi.segment_id = i.segment_id
         AND pi.state_run_id = i.run_id
         AND pi.run_id = ?
        LEFT JOIN segment_token_policy_decisions d
          ON d.policy_run_id = pi.run_id
         AND d.policy_item_id = pi.id
        WHERE i.run_id = ?
          AND i.needs_output_apply = 1
        ORDER BY
            CASE pi.risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
            END,
            pi.policy_bucket,
            i.relative_path,
            i.source_line_number,
            i.segment_id
        """,
        (policy_run_id, state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def issue_codes(text: str | None) -> list[str]:
    if text is None:
        return []
    issues = local_quality_validator.validate_text(text)["issues"]
    return sorted({str(issue.get("code")) for issue in issues if issue.get("code")})


def classify(row: dict[str, Any]) -> tuple[str, list[str]]:
    source_tokens = structural_tokens(row.get("spanish_text"))
    confirmed_tokens = structural_tokens(row.get("confirmed_text"))
    old_tokens = structural_tokens(row.get("old_text"))
    output_tokens = structural_tokens(row.get("output_text"))
    reasons: list[str] = []

    if confirmed_tokens == source_tokens:
        return "skip_confirmed_now_token_safe", ["confirmed_text_already_matches_source_tokens"]
    if row.get("decision_id"):
        return "skip_existing_token_policy_decision", ["token_policy_decision_already_exists"]
    if not (row.get("old_text") or "").strip():
        return "blocked_missing_old_text", ["old_text_missing_or_blank"]
    if old_tokens != source_tokens:
        return "blocked_old_token_mismatch", ["old_text_does_not_match_current_source_tokens"]
    if old_tokens != output_tokens:
        reasons.append("old_text_matches_source_tokens_but_output_signature_differs")
    else:
        reasons.append("old_text_matches_current_output_token_signature")
    codes = issue_codes(row.get("old_text"))
    if "replacement_question_mark_mojibake" in codes:
        return "blocked_old_quality_issue", ["old_text_has_replacement_question_mark_mojibake"]
    if codes:
        reasons.append("old_text_has_quality_warnings")
    return "ready_old_stable_token_safe_fix", reasons


def write_outputs(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    state_run_id: int,
    policy_run_id: int,
) -> tuple[Path, Path, Path, Path, Path]:
    base = reports_base(settings)
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    csv_path = base.with_suffix(".csv")
    decisions_path = base.parent / f"{base.name}_decisions.jsonl"
    summary_path = base.parent / f"{base.name}_summary.json"

    counts = Counter(row["resolution_status"] for row in rows)
    bucket_counts = Counter(row.get("policy_bucket") or "<none>" for row in rows)
    ready_rows = [row for row in rows if row["resolution_status"] == "ready_old_stable_token_safe_fix"]

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
                "resolution_status": row["resolution_status"],
                "resolution_reasons": row["resolution_reasons"],
                "old_quality_issue_codes": row["old_quality_issue_codes"],
                "old_text": row.get("old_text"),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "spanish_text": row.get("spanish_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in ready_rows:
            payload = {
                "policy_item_id": row["policy_item_id"],
                "segment_id": row["segment_id"],
                "decision": DECISION,
                "corrected_text": row.get("old_text"),
                "reviewer": REVIEWER,
                "notes": (
                    "old_text from stable baseline matches current source token signature; "
                    "use it to replace stale confirmed_text before protected apply"
                ),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    fieldnames = [
        "segment_id",
        "policy_item_id",
        "relative_path",
        "source_key",
        "review_state",
        "policy_bucket",
        "risk_level",
        "resolution_status",
        "resolution_reasons",
        "old_quality_issue_codes",
        "old_text",
        "output_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "segment_id": row["segment_id"],
                    "policy_item_id": row["policy_item_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "review_state": row["review_state"],
                    "policy_bucket": row.get("policy_bucket"),
                    "risk_level": row.get("risk_level"),
                    "resolution_status": row["resolution_status"],
                    "resolution_reasons": json.dumps(row["resolution_reasons"], ensure_ascii=False),
                    "old_quality_issue_codes": json.dumps(row["old_quality_issue_codes"], ensure_ascii=False),
                    "old_text": row.get("old_text"),
                    "output_text": row.get("output_text"),
                    "confirmed_text": row.get("confirmed_text"),
                }
            )

    summary = {
        "source": RULE_VERSION,
        "state_run_id": state_run_id,
        "policy_run_id": policy_run_id,
        "total_rows": len(rows),
        "ready_old_stable_token_safe_fix": len(ready_rows),
        "status_counts": dict(counts),
        "policy_bucket_counts": dict(bucket_counts),
        "decisions_path": str(decisions_path),
        "outputs": {
            "markdown": str(md_path),
            "jsonl": str(jsonl_path),
            "csv": str(csv_path),
            "decisions_jsonl": str(decisions_path),
        },
        "guards": {
            "read_only": True,
            "db_writes": 0,
            "output_writes": 0,
            "segment_state": 0,
        },
        "next_steps": [
            "review decisions_jsonl",
            "apply decisions with segment_token_policy_decisions.py only if ready count is approved",
            "run segment_token_policy_confirmation_fixes.py to update confirmed_text",
            "rerun segment-state and publication preflight",
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Publication Token Mismatch Old-Safe Fix Queue",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Segment-state run: `{state_run_id}`",
        f"- Token policy run: `{policy_run_id}`",
        f"- Rows inspected: `{len(rows)}`",
        f"- Ready old-stable token-safe fixes: `{len(ready_rows)}`",
        "- Guard: read-only; no DB writes, no output writes, no segment-state.",
        "",
        "## Status Counts",
        "",
        *[f"- `{key}`: `{value}`" for key, value in counts.most_common()],
        "",
        "## Policy Buckets",
        "",
        *[f"- `{key}`: `{value}`" for key, value in bucket_counts.most_common()],
        "",
        "## Ready Sample",
        "",
    ]
    for row in ready_rows[:60]:
        lines.extend(
            [
                f"### Segment `{row['segment_id']}` | item `{row['policy_item_id']}`",
                "",
                f"- Path: `{row['relative_path']}:{row['source_line_number']}`",
                f"- Key: `{row['source_key']}`",
                f"- Bucket: `{row.get('policy_bucket')}` / `{row.get('risk_level')}`",
                f"- Reasons: `{json.dumps(row['resolution_reasons'], ensure_ascii=False)}`",
                f"- Old stable: `{short(row.get('old_text'), 240)}`",
                f"- Current output: `{short(row.get('output_text'), 240)}`",
                f"- Current confirmed: `{short(row.get('confirmed_text'), 240)}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Blocked Sample",
            "",
        ]
    )
    for row in [item for item in rows if item["resolution_status"] != "ready_old_stable_token_safe_fix"][:40]:
        lines.extend(
            [
                f"- Segment `{row['segment_id']}` | `{row['resolution_status']}` | `{row.get('policy_bucket')}` | `{row['source_key']}`",
                f"  - Reasons: `{json.dumps(row['resolution_reasons'], ensure_ascii=False)}`",
            ]
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, jsonl_path, csv_path, decisions_path, summary_path


def main(*, state_run_id: int | None = None, policy_run_id: int | None = None) -> None:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn, selected_state_run_id)
        rows = fetch_rows(conn, state_run_id=selected_state_run_id, policy_run_id=selected_policy_run_id)

    analyzed: list[dict[str, Any]] = []
    for row in rows:
        status, reasons = classify(row)
        analyzed.append(
            {
                **row,
                "resolution_status": status,
                "resolution_reasons": reasons,
                "old_quality_issue_codes": issue_codes(row.get("old_text")),
            }
        )
    md_path, jsonl_path, csv_path, decisions_path, summary_path = write_outputs(
        settings,
        rows=analyzed,
        state_run_id=selected_state_run_id,
        policy_run_id=selected_policy_run_id,
    )
    counts = Counter(row["resolution_status"] for row in analyzed)
    print("[publication_token_mismatch_old_safe_fix_queue] Read-only queue generated")
    print(f"[publication_token_mismatch_old_safe_fix_queue] State run: {selected_state_run_id}")
    print(f"[publication_token_mismatch_old_safe_fix_queue] Policy run: {selected_policy_run_id}")
    print(f"[publication_token_mismatch_old_safe_fix_queue] Rows: {len(analyzed)}")
    print(f"[publication_token_mismatch_old_safe_fix_queue] Ready old-stable token-safe: {counts['ready_old_stable_token_safe_fix']}")
    print(f"[publication_token_mismatch_old_safe_fix_queue] Markdown: {md_path}")
    print(f"[publication_token_mismatch_old_safe_fix_queue] Decisions JSONL: {decisions_path}")
    print(f"[publication_token_mismatch_old_safe_fix_queue] Summary: {summary_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build a read-only decisions queue for token mismatches fixable from stable old_text."
    )
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--policy-run-id", type=int, default=None)
    args = parser.parse_args()
    main(state_run_id=args.state_run_id, policy_run_id=args.policy_run_id)
