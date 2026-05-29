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
from segment_token_tutorial_concept_policy import (
    RULE_VERSION as SOURCE_RULE_VERSION,
    classify_row,
    fetch_rows,
    latest_policy_run_id,
)


RULE_VERSION = "segment_token_tutorial_concept_promotion_v1"
POSITIVE_DECISIONS = {"accept_policy_candidate", "needs_subpolicy", "keep_manual_exception_only"}


def rule_key_for(row: dict[str, Any]) -> str:
    bucket = row["subpolicy_bucket"]
    if bucket == "tutorial_game_concept_addition_candidate":
        return "tutorial_game_concept_addition"
    if bucket == "tutorial_concept_placeholder_rewrite_candidate":
        return "tutorial_concept_placeholder_rewrite"
    if bucket == "tutorial_named_variable_review":
        return "tutorial_named_variable_review"
    if bucket == "non_tutorial_variable_or_icon_blocked":
        return "non_tutorial_out_of_scope"
    return bucket


def fetch_decisions(conn, *, policy_run_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            policy_item_id,
            decision,
            approved_for_apply,
            corrected_text,
            notes,
            reviewer,
            created_at,
            updated_at
        FROM segment_token_policy_decisions
        WHERE policy_run_id = ?
        """,
        (policy_run_id,),
    ).fetchall()
    return {int(row["policy_item_id"]): dict(row) for row in rows}


def promotion_status(*, evidence_count: int, pending_count: int, blocker_count: int, min_evidence: int) -> str:
    if blocker_count:
        return "blocked"
    if evidence_count >= min_evidence and pending_count == 0:
        return "ready_for_policy_review"
    if evidence_count > 0:
        return "collect_more_evidence"
    return "needs_human_review"


def build_rows(
    *,
    classified_rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    min_evidence: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    item_rows: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in classified_rows:
        decision = decisions.get(int(row["policy_item_id"]))
        decision_label = decision["decision"] if decision else ""
        positive = row["subpolicy_status"] == "subpolicy_candidate_review" and decision_label in POSITIVE_DECISIONS
        pending = row["subpolicy_status"] == "subpolicy_candidate_review" and not decision_label
        blocker = row["subpolicy_status"] not in {"subpolicy_candidate_review"}
        rule_key = rule_key_for(row)
        item = {
            **row,
            "rule_key": rule_key,
            "decision": decision_label,
            "decision_notes": decision.get("notes") if decision else "",
            "positive_evidence": 1 if positive else 0,
            "pending_decision": 1 if pending else 0,
            "blocker": 1 if blocker else 0,
            "dry_run_action": (
                "would_accept_by_subpolicy"
                if positive
                else "needs_human_decision"
                if pending
                else "blocked_or_out_of_scope"
            ),
            "apply_allowed": 0,
        }
        item_rows.append(item)
        grouped[rule_key].append(item)

    summary_rows: list[dict[str, Any]] = []
    for rule_key, rows in sorted(grouped.items()):
        evidence_count = sum(row["positive_evidence"] for row in rows)
        pending_count = sum(row["pending_decision"] for row in rows)
        blocker_count = sum(row["blocker"] for row in rows)
        candidate_count = sum(1 for row in rows if row["subpolicy_status"] == "subpolicy_candidate_review")
        status = promotion_status(
            evidence_count=evidence_count,
            pending_count=pending_count,
            blocker_count=blocker_count,
            min_evidence=min_evidence,
        )
        summary_rows.append(
            {
                "rule_key": rule_key,
                "promotion_status": status,
                "rows": len(rows),
                "candidate_count": candidate_count,
                "positive_evidence_count": evidence_count,
                "pending_decision_count": pending_count,
                "blocker_count": blocker_count,
                "min_evidence": min_evidence,
                "apply_allowed": 0,
            }
        )
    return summary_rows, item_rows


def write_outputs(
    settings: dict,
    *,
    policy_run_id: int,
    summary_rows: list[dict[str, Any]],
    item_rows: list[dict[str, Any]],
    min_evidence: int,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_tutorial_concept_promotion"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "rule_key",
            "promotion_status",
            "rows",
            "candidate_count",
            "positive_evidence_count",
            "pending_decision_count",
            "blocker_count",
            "min_evidence",
            "apply_allowed",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in item_rows:
            payload = {
                "policy_item_id": row["policy_item_id"],
                "policy_run_id": row["policy_run_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "rule_key": row["rule_key"],
                "subpolicy_bucket": row["subpolicy_bucket"],
                "subpolicy_status": row["subpolicy_status"],
                "decision": row["decision"],
                "positive_evidence": row["positive_evidence"],
                "pending_decision": row["pending_decision"],
                "blocker": row["blocker"],
                "dry_run_action": row["dry_run_action"],
                "apply_allowed": row["apply_allowed"],
                "missing_tokens": row["missing_tokens"],
                "extra_tokens": row["extra_tokens"],
                "confirmed_text": row["confirmed_text"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    status_counts = Counter(row["promotion_status"] for row in summary_rows)
    ready_rules = [row for row in summary_rows if row["promotion_status"] == "ready_for_policy_review"]
    lines = [
        "Segment token tutorial concept promotion dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Source rule version: {SOURCE_RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Minimum evidence per rule: {min_evidence}",
        "",
        "Summary:",
        f"- Rule families inspected: {len(summary_rows)}",
        f"- Ready for policy review: {len(ready_rules)}",
        "- Apply allowed: 0",
        "",
        "Promotion statuses:",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Rule families:",
    ]
    for row in summary_rows:
        lines.append(
            "- {rule_key}: status={status}, rows={rows}, candidates={candidates}, "
            "positive={positive}, pending={pending}, blockers={blockers}, apply=0".format(
                rule_key=row["rule_key"],
                status=row["promotion_status"],
                rows=row["rows"],
                candidates=row["candidate_count"],
                positive=row["positive_evidence_count"],
                pending=row["pending_decision_count"],
                blockers=row["blocker_count"],
            )
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- ready_for_policy_review means evidence is strong enough to design a promoted rule, not to apply output yet.",
            "- blocked/out-of-scope families remain under manual review or a different specialist.",
            "- apply_allowed stays 0 until we build an explicit apply gate and test it separately.",
            "",
            "Positive evidence sample:",
        ]
    )
    for row in [item for item in item_rows if item["positive_evidence"]][:40]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['source_key']} | {row['rule_key']} | {row['dry_run_action']}"
                ),
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, policy_run_id: int | None = None, min_evidence: int = 5) -> None:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        raw_rows = fetch_rows(
            conn,
            policy_run_id=selected_policy_run_id,
            tutorial_only=False,
            limit=None,
        )
        decisions = fetch_decisions(conn, policy_run_id=selected_policy_run_id)

    classified = [classify_row(row) for row in raw_rows]
    summary_rows, item_rows = build_rows(
        classified_rows=classified,
        decisions=decisions,
        min_evidence=min_evidence,
    )
    txt_path, csv_path, jsonl_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        summary_rows=summary_rows,
        item_rows=item_rows,
        min_evidence=min_evidence,
    )

    status_counts = Counter(row["promotion_status"] for row in summary_rows)
    print("[segment_token_tutorial_concept_promotion] Promotion dry-run generated")
    print(f"[segment_token_tutorial_concept_promotion] Rule version: {RULE_VERSION}")
    print(f"[segment_token_tutorial_concept_promotion] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_tutorial_concept_promotion] Rule families: {len(summary_rows)}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_tutorial_concept_promotion] status {key}: {value}")
    print("[segment_token_tutorial_concept_promotion] Apply allowed: 0")
    print(f"[segment_token_tutorial_concept_promotion] Report: {txt_path}")
    print(f"[segment_token_tutorial_concept_promotion] CSV: {csv_path}")
    print(f"[segment_token_tutorial_concept_promotion] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run promotion report for tutorial concept token subpolicy.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--min-evidence", type=int, default=5)
    args = parser.parse_args()
    main(policy_run_id=args.policy_run_id, min_evidence=args.min_evidence)
