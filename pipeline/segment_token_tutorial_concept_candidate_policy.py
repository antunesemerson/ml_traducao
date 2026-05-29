from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from segment_token_tutorial_concept_policy import (
    RULE_VERSION as SOURCE_RULE_VERSION,
    SOURCE_BUCKET,
    classify_row,
    fetch_rows,
    latest_policy_run_id,
)
from segment_token_tutorial_concept_promotion import (
    POSITIVE_DECISIONS,
    fetch_decisions,
    promotion_status,
    rule_key_for,
)


RULE_VERSION = "segment_token_tutorial_concept_candidate_policy_v1"
PROMOTABLE_RULE_KEYS = {
    "tutorial_game_concept_addition",
    "tutorial_concept_placeholder_rewrite",
}


def build_rule_summary(
    *,
    classified_rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    min_evidence: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in classified_rows:
        grouped.setdefault(rule_key_for(row), []).append(row)

    summary_rows: list[dict[str, Any]] = []
    for rule_key, rows in sorted(grouped.items()):
        evidence_count = 0
        pending_count = 0
        blocker_count = 0
        candidate_count = 0
        for row in rows:
            decision = decisions.get(int(row["policy_item_id"]))
            decision_label = decision["decision"] if decision else ""
            is_candidate = row["subpolicy_status"] == "subpolicy_candidate_review"
            if is_candidate:
                candidate_count += 1
            if is_candidate and decision_label in POSITIVE_DECISIONS:
                evidence_count += 1
            elif is_candidate and not decision_label:
                pending_count += 1
            elif not is_candidate:
                blocker_count += 1

        status = promotion_status(
            evidence_count=evidence_count,
            pending_count=pending_count,
            blocker_count=blocker_count,
            min_evidence=min_evidence,
        )
        candidate_policy_enabled = (
            rule_key in PROMOTABLE_RULE_KEYS
            and status == "ready_for_policy_review"
        )
        summary_rows.append(
            {
                "rule_key": rule_key,
                "promotion_status": status,
                "candidate_policy_enabled": 1 if candidate_policy_enabled else 0,
                "rows": len(rows),
                "candidate_count": candidate_count,
                "positive_evidence_count": evidence_count,
                "pending_decision_count": pending_count,
                "blocker_count": blocker_count,
                "min_evidence": min_evidence,
                "apply_allowed": 0,
            }
        )
    return summary_rows


def build_candidate_rows(
    *,
    classified_rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    enabled_rules: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in classified_rows:
        decision = decisions.get(int(row["policy_item_id"]))
        decision_label = decision["decision"] if decision else ""
        rule_key = rule_key_for(row)
        has_positive_decision = decision_label in POSITIVE_DECISIONS
        is_candidate = row["subpolicy_status"] == "subpolicy_candidate_review"

        if rule_key in enabled_rules and is_candidate and has_positive_decision:
            candidate_status = "would_release_from_critical_block"
            candidate_action = "candidate_tutorial_concept_exception"
            candidate_recommendation = "allow by candidate policy in dry-run only; keep apply disabled"
            would_release = 1
        elif is_candidate and not decision_label:
            candidate_status = "pending_human_decision"
            candidate_action = "keep_blocked_until_reviewed"
            candidate_recommendation = "collect human decision before candidate policy can evaluate this row"
            would_release = 0
        elif is_candidate:
            candidate_status = "candidate_rule_not_enabled_or_decision_not_positive"
            candidate_action = "keep_blocked"
            candidate_recommendation = "rule is not enabled or human decision was not positive"
            would_release = 0
        else:
            candidate_status = "remains_blocked"
            candidate_action = "keep_main_token_block"
            candidate_recommendation = row["recommended_next_action"]
            would_release = 0

        rows.append(
            {
                **row,
                "rule_key": rule_key,
                "decision": decision_label,
                "decision_notes": decision.get("notes") if decision else "",
                "candidate_policy_enabled": 1 if rule_key in enabled_rules else 0,
                "candidate_status": candidate_status,
                "candidate_action": candidate_action,
                "candidate_recommendation": candidate_recommendation,
                "would_release_from_critical_block": would_release,
                "apply_allowed": 0,
            }
        )
    return rows


def write_outputs(
    settings: dict,
    *,
    policy_run_id: int,
    min_evidence: int,
    rule_summary_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_tutorial_concept_candidate_policy"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "policy_item_id",
            "policy_run_id",
            "segment_id",
            "relative_path",
            "source_line_number",
            "source_key",
            "rule_key",
            "subpolicy_bucket",
            "subpolicy_status",
            "decision",
            "candidate_policy_enabled",
            "candidate_status",
            "candidate_action",
            "would_release_from_critical_block",
            "apply_allowed",
            "missing_tokens",
            "extra_tokens",
            "issue_flags",
            "confirmed_text",
            "output_text",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in candidate_rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"missing_tokens", "extra_tokens", "issue_flags"}
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in candidate_rows:
            payload = {
                "policy_item_id": row["policy_item_id"],
                "policy_run_id": row["policy_run_id"],
                "state_run_id": row["state_run_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "rule_key": row["rule_key"],
                "subpolicy_bucket": row["subpolicy_bucket"],
                "subpolicy_status": row["subpolicy_status"],
                "decision": row["decision"],
                "candidate_policy_enabled": row["candidate_policy_enabled"],
                "candidate_status": row["candidate_status"],
                "candidate_action": row["candidate_action"],
                "candidate_recommendation": row["candidate_recommendation"],
                "would_release_from_critical_block": row["would_release_from_critical_block"],
                "apply_allowed": row["apply_allowed"],
                "missing_tokens": row["missing_tokens"],
                "extra_tokens": row["extra_tokens"],
                "issue_flags": row["issue_flags"],
                "confirmed_text": row["confirmed_text"],
                "output_text": row["output_text"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    rule_status_counts = Counter(row["promotion_status"] for row in rule_summary_rows)
    candidate_status_counts = Counter(row["candidate_status"] for row in candidate_rows)
    enabled_rules = [row for row in rule_summary_rows if row["candidate_policy_enabled"]]
    release_rows = [row for row in candidate_rows if row["would_release_from_critical_block"]]
    blocked_rows = [row for row in candidate_rows if not row["would_release_from_critical_block"]]

    lines = [
        "Segment token tutorial concept candidate policy dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Source rule version: {SOURCE_RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Source bucket: {SOURCE_BUCKET}",
        f"Minimum evidence per rule: {min_evidence}",
        "",
        "Summary:",
        f"- Rows inspected: {len(candidate_rows)}",
        f"- Current critical blockers in source bucket: {len(candidate_rows)}",
        f"- Candidate would release from critical block: {len(release_rows)}",
        f"- Remaining blocked: {len(blocked_rows)}",
        f"- Candidate rules enabled: {len(enabled_rules)}",
        "- Apply allowed: 0",
        "",
        "Rule readiness:",
    ]
    for row in rule_summary_rows:
        lines.append(
            "- {rule_key}: enabled={enabled}, status={status}, rows={rows}, positive={positive}, "
            "pending={pending}, blockers={blockers}, min_evidence={min_evidence}, apply=0".format(
                rule_key=row["rule_key"],
                enabled=row["candidate_policy_enabled"],
                status=row["promotion_status"],
                rows=row["rows"],
                positive=row["positive_evidence_count"],
                pending=row["pending_decision_count"],
                blockers=row["blocker_count"],
                min_evidence=row["min_evidence"],
            )
        )
    lines.extend(
        [
            "",
            "Rule readiness statuses:",
            *[f"- {key}: {value}" for key, value in rule_status_counts.most_common()],
            "",
            "Candidate statuses:",
            *[f"- {key}: {value}" for key, value in candidate_status_counts.most_common()],
            "",
            "Interpretation:",
            "- This is a promoted candidate policy simulation only.",
            "- would_release_from_critical_block means the row could stop being treated as a critical token blocker by this subpolicy.",
            "- apply_allowed remains 0; output application needs a separate explicit gate.",
            "- remaining blocked rows stay under the main token policy or a future specialist.",
            "",
            "Would release sample:",
        ]
    )
    for row in release_rows[:40]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | "
                    f"{row['source_key']} | {row['rule_key']}"
                ),
                f"  ACTION: {row['candidate_action']}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    lines.extend(["", "Remaining blocked sample:"])
    for row in blocked_rows[:40]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | "
                    f"{row['source_key']} | {row['candidate_status']}"
                ),
                f"  ACTION: {row['candidate_action']}",
                f"  REASON: {row['candidate_recommendation']}",
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
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

    classified_rows = [classify_row(row) for row in raw_rows]
    rule_summary_rows = build_rule_summary(
        classified_rows=classified_rows,
        decisions=decisions,
        min_evidence=min_evidence,
    )
    enabled_rules = {
        row["rule_key"]
        for row in rule_summary_rows
        if row["candidate_policy_enabled"]
    }
    candidate_rows = build_candidate_rows(
        classified_rows=classified_rows,
        decisions=decisions,
        enabled_rules=enabled_rules,
    )
    txt_path, csv_path, jsonl_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        min_evidence=min_evidence,
        rule_summary_rows=rule_summary_rows,
        candidate_rows=candidate_rows,
    )

    candidate_status_counts = Counter(row["candidate_status"] for row in candidate_rows)
    print("[segment_token_tutorial_concept_candidate_policy] Candidate policy dry-run generated")
    print(f"[segment_token_tutorial_concept_candidate_policy] Rule version: {RULE_VERSION}")
    print(f"[segment_token_tutorial_concept_candidate_policy] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_tutorial_concept_candidate_policy] Rows inspected: {len(candidate_rows)}")
    print(f"[segment_token_tutorial_concept_candidate_policy] Enabled rules: {len(enabled_rules)}")
    for key, value in candidate_status_counts.most_common():
        print(f"[segment_token_tutorial_concept_candidate_policy] status {key}: {value}")
    print("[segment_token_tutorial_concept_candidate_policy] Apply allowed: 0")
    print(f"[segment_token_tutorial_concept_candidate_policy] Report: {txt_path}")
    print(f"[segment_token_tutorial_concept_candidate_policy] CSV: {csv_path}")
    print(f"[segment_token_tutorial_concept_candidate_policy] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run a candidate policy for tutorial concept token exceptions.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--min-evidence", type=int, default=5)
    args = parser.parse_args()
    main(policy_run_id=args.policy_run_id, min_evidence=args.min_evidence)
