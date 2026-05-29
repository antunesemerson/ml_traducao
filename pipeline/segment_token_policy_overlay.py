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
from segment_token_tutorial_concept_candidate_policy import (
    RULE_VERSION as CANDIDATE_POLICY_RULE_VERSION,
    build_candidate_rows,
    build_rule_summary,
)
from segment_token_tutorial_concept_policy import (
    RULE_VERSION as TUTORIAL_POLICY_RULE_VERSION,
    SOURCE_BUCKET,
    classify_row,
    fetch_rows as fetch_tutorial_source_rows,
    latest_policy_run_id,
)
from segment_token_tutorial_concept_promotion import POSITIVE_DECISIONS, fetch_decisions, rule_key_for


RULE_VERSION = "segment_token_policy_overlay_v1"
OVERLAY_NAME = "tutorial_concept_candidate_overlay"


RISK_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def risk_rank(value: str) -> int:
    return RISK_ORDER.get(value, 99)


def fetch_policy_run(conn, policy_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_policy_runs
        WHERE id = ?
          AND finished_at IS NOT NULL
        """,
        (policy_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment token policy run {policy_run_id} is missing or unfinished.")
    return dict(row)


def fetch_policy_items(conn, policy_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
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
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        WHERE i.run_id = ?
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
        (policy_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_tutorial_release_map(
    conn,
    *,
    policy_run_id: int,
    min_evidence: int,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    raw_rows = fetch_tutorial_source_rows(
        conn,
        policy_run_id=policy_run_id,
        tutorial_only=False,
        limit=None,
    )
    classified_rows = [classify_row(row) for row in raw_rows]
    decisions = fetch_reusable_tutorial_decisions(
        conn,
        policy_run_id=policy_run_id,
        classified_rows=classified_rows,
    )
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
    release_map = {
        int(row["policy_item_id"]): row
        for row in candidate_rows
        if int(row["would_release_from_critical_block"])
    }
    return release_map, rule_summary_rows


def fetch_historical_tutorial_evidence(
    conn,
    *,
    exclude_policy_run_id: int,
) -> dict[tuple[int, str], dict[str, Any]]:
    rows = conn.execute(
        """
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
            sc.locked,
            d.decision,
            d.notes,
            d.policy_run_id AS evidence_policy_run_id,
            d.policy_item_id AS evidence_policy_item_id,
            d.updated_at AS evidence_updated_at
        FROM segment_token_policy_decisions d
        JOIN segment_token_policy_items i ON i.id = d.policy_item_id
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        WHERE d.policy_run_id <> ?
          AND d.decision IN ({positive_placeholders})
          AND i.policy_bucket = ?
        ORDER BY d.updated_at DESC, d.id DESC
        """.format(
            positive_placeholders=", ".join("?" for _ in POSITIVE_DECISIONS)
        ),
        (exclude_policy_run_id, *sorted(POSITIVE_DECISIONS), SOURCE_BUCKET),
    ).fetchall()
    evidence: dict[tuple[int, str], dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        classified = classify_row(row)
        if classified["subpolicy_status"] != "subpolicy_candidate_review":
            continue
        key = (int(classified["segment_id"]), rule_key_for(classified))
        evidence.setdefault(
            key,
            {
                "decision": row["decision"],
                "notes": (
                    f"{row.get('notes') or ''}; "
                    f"reused_from_policy_run={row['evidence_policy_run_id']}; "
                    f"reused_from_policy_item={row['evidence_policy_item_id']}"
                ).strip("; "),
                "reused_from_policy_run_id": row["evidence_policy_run_id"],
                "reused_from_policy_item_id": row["evidence_policy_item_id"],
            },
        )
    return evidence


def fetch_reusable_tutorial_decisions(
    conn,
    *,
    policy_run_id: int,
    classified_rows: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    decisions = fetch_decisions(conn, policy_run_id=policy_run_id)
    historical = fetch_historical_tutorial_evidence(
        conn,
        exclude_policy_run_id=policy_run_id,
    )
    for row in classified_rows:
        policy_item_id = int(row["policy_item_id"])
        if policy_item_id in decisions:
            continue
        if row["subpolicy_status"] != "subpolicy_candidate_review":
            continue
        evidence = historical.get((int(row["segment_id"]), rule_key_for(row)))
        if evidence:
            decisions[policy_item_id] = evidence
    return decisions


def build_overlay_rows(
    *,
    policy_items: list[dict[str, Any]],
    tutorial_release_map: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in policy_items:
        source_item_id = int(item["policy_item_id"])
        release = tutorial_release_map.get(source_item_id)
        if release:
            overlay_policy_bucket = "candidate_tutorial_concept_exception"
            overlay_risk_level = "high"
            overlay_action = "experimental_release_from_critical"
            overlay_agent_key = "token_tutorial_concept_subpolicy"
            decision = release.get("decision") or ""
            decision_notes = release.get("decision_notes") or ""
            rule_key = release.get("rule_key") or ""
            reasons = [
                f"rule:{RULE_VERSION}",
                f"candidate_rule:{CANDIDATE_POLICY_RULE_VERSION}",
                f"source_subpolicy:{TUTORIAL_POLICY_RULE_VERSION}",
                f"source_bucket:{SOURCE_BUCKET}",
                f"rule_key:{rule_key}",
                f"decision:{decision}",
                "apply_allowed:0",
            ]
            if "reused_from_policy_run" in decision_notes:
                reasons.append("evidence_reused_from_previous_policy_run")
            would_release = 1
            apply_allowed = 0
        else:
            overlay_policy_bucket = item["policy_bucket"]
            overlay_risk_level = item["risk_level"]
            overlay_action = "unchanged"
            overlay_agent_key = "base_segment_token_policy"
            decision = ""
            rule_key = ""
            reasons = [
                f"rule:{RULE_VERSION}",
                "base_policy_unchanged",
                f"source_bucket:{item['policy_bucket']}",
            ]
            would_release = 0
            apply_allowed = int(item.get("auto_apply_allowed") or 0)

        rows.append(
            {
                **item,
                "source_policy_item_id": source_item_id,
                "original_policy_bucket": item["policy_bucket"],
                "original_risk_level": item["risk_level"],
                "overlay_policy_bucket": overlay_policy_bucket,
                "overlay_risk_level": overlay_risk_level,
                "overlay_action": overlay_action,
                "overlay_agent_key": overlay_agent_key,
                "would_release_critical": would_release,
                "apply_allowed": apply_allowed if would_release == 0 else 0,
                "decision": decision,
                "rule_key": rule_key,
                "reasons": reasons,
            }
        )
    return rows


def insert_overlay_run(
    conn,
    *,
    source_policy_run: dict[str, Any],
    min_evidence: int,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_policy_overlay_runs (
            rule_version,
            source_policy_run_id,
            source_state_run_id,
            source_rule_version,
            overlay_name,
            min_evidence,
            notes_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            source_policy_run["id"],
            source_policy_run.get("state_run_id"),
            source_policy_run.get("rule_version"),
            OVERLAY_NAME,
            min_evidence,
            json.dumps(
                {
                    "candidate_policy_rule_version": CANDIDATE_POLICY_RULE_VERSION,
                    "tutorial_policy_rule_version": TUTORIAL_POLICY_RULE_VERSION,
                    "source_bucket": SOURCE_BUCKET,
                    "dry_run_only": True,
                    "apply_allowed": 0,
                },
                ensure_ascii=False,
            ),
            started_at,
            started_at,
        ),
    )
    return int(cur.lastrowid)


def insert_overlay_items(
    conn,
    *,
    overlay_run_id: int,
    source_policy_run_id: int,
    rows: list[dict[str, Any]],
    created_at: str,
) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO segment_token_policy_overlay_items (
            run_id,
            source_policy_run_id,
            source_policy_item_id,
            state_run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            original_policy_bucket,
            original_risk_level,
            overlay_policy_bucket,
            overlay_risk_level,
            overlay_action,
            overlay_agent_key,
            would_release_critical,
            apply_allowed,
            decision,
            rule_key,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                overlay_run_id,
                source_policy_run_id,
                row["source_policy_item_id"],
                row["state_run_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["original_policy_bucket"],
                row["original_risk_level"],
                row["overlay_policy_bucket"],
                row["overlay_risk_level"],
                row["overlay_action"],
                row["overlay_agent_key"],
                row["would_release_critical"],
                row["apply_allowed"],
                row["decision"],
                row["rule_key"],
                json.dumps(row["reasons"], ensure_ascii=False),
                created_at,
            )
            for row in rows
        ],
    )


def update_overlay_run(
    conn,
    *,
    overlay_run_id: int,
    rows: list[dict[str, Any]],
    enabled_rule_count: int,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    finished_at: str,
) -> None:
    original_critical = sum(1 for row in rows if row["original_risk_level"] == "critical")
    overlay_critical = sum(1 for row in rows if row["overlay_risk_level"] == "critical")
    released = sum(1 for row in rows if row["would_release_critical"])
    remaining_blocked = sum(1 for row in rows if row["overlay_policy_bucket"].startswith("blocked"))
    apply_allowed = sum(1 for row in rows if row["apply_allowed"])
    conn.execute(
        """
        UPDATE segment_token_policy_overlay_runs
        SET
            total_candidates = ?,
            original_critical_count = ?,
            overlay_critical_count = ?,
            released_critical_count = ?,
            remaining_blocked_count = ?,
            enabled_rule_count = ?,
            apply_allowed_count = ?,
            report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len(rows),
            original_critical,
            overlay_critical,
            released,
            remaining_blocked,
            enabled_rule_count,
            apply_allowed,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            finished_at,
            finished_at,
            overlay_run_id,
        ),
    )


def write_outputs(
    settings: dict,
    *,
    overlay_run_id: int,
    source_policy_run: dict[str, Any],
    min_evidence: int,
    rule_summary_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_policy_overlay"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    selected_rows = sorted(
        rows,
        key=lambda row: (
            risk_rank(row["overlay_risk_level"]),
            -int(row["would_release_critical"]),
            row["overlay_policy_bucket"],
            row["relative_path"],
            row["source_line_number"] or 0,
        ),
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "source_policy_item_id",
            "segment_id",
            "relative_path",
            "source_line_number",
            "source_key",
            "original_policy_bucket",
            "original_risk_level",
            "overlay_policy_bucket",
            "overlay_risk_level",
            "overlay_action",
            "overlay_agent_key",
            "would_release_critical",
            "apply_allowed",
            "decision",
            "rule_key",
            "reasons_json",
            "output_text",
            "confirmed_text",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected_rows:
            writer.writerow(
                {
                    "source_policy_item_id": row["source_policy_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_line_number": row["source_line_number"],
                    "source_key": row["source_key"],
                    "original_policy_bucket": row["original_policy_bucket"],
                    "original_risk_level": row["original_risk_level"],
                    "overlay_policy_bucket": row["overlay_policy_bucket"],
                    "overlay_risk_level": row["overlay_risk_level"],
                    "overlay_action": row["overlay_action"],
                    "overlay_agent_key": row["overlay_agent_key"],
                    "would_release_critical": row["would_release_critical"],
                    "apply_allowed": row["apply_allowed"],
                    "decision": row["decision"],
                    "rule_key": row["rule_key"],
                    "reasons_json": json.dumps(row["reasons"], ensure_ascii=False),
                    "output_text": row["output_text"],
                    "confirmed_text": row["confirmed_text"],
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in selected_rows:
            payload = {
                "overlay_run_id": overlay_run_id,
                "source_policy_run_id": source_policy_run["id"],
                "source_policy_item_id": row["source_policy_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "original_policy_bucket": row["original_policy_bucket"],
                "original_risk_level": row["original_risk_level"],
                "overlay_policy_bucket": row["overlay_policy_bucket"],
                "overlay_risk_level": row["overlay_risk_level"],
                "overlay_action": row["overlay_action"],
                "overlay_agent_key": row["overlay_agent_key"],
                "would_release_critical": row["would_release_critical"],
                "apply_allowed": row["apply_allowed"],
                "decision": row["decision"],
                "rule_key": row["rule_key"],
                "reasons": row["reasons"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    original_risk_counts = Counter(row["original_risk_level"] for row in rows)
    overlay_risk_counts = Counter(row["overlay_risk_level"] for row in rows)
    original_bucket_counts = Counter(row["original_policy_bucket"] for row in rows)
    overlay_bucket_counts = Counter(row["overlay_policy_bucket"] for row in rows)
    overlay_action_counts = Counter(row["overlay_action"] for row in rows)
    enabled_rules = [row for row in rule_summary_rows if row["candidate_policy_enabled"]]
    released_rows = [row for row in selected_rows if row["would_release_critical"]]
    remaining_critical_rows = [row for row in selected_rows if row["overlay_risk_level"] == "critical"]

    lines = [
        "Segment token policy experimental overlay",
        f"Rule version: {RULE_VERSION}",
        f"Overlay name: {OVERLAY_NAME}",
        f"Overlay run id: {overlay_run_id}",
        f"Source policy run id: {source_policy_run['id']}",
        f"Source state run id: {source_policy_run.get('state_run_id')}",
        f"Minimum evidence per candidate rule: {min_evidence}",
        "",
        "Summary:",
        f"- rows evaluated: {len(rows)}",
        f"- original critical: {original_risk_counts.get('critical', 0)}",
        f"- overlay critical: {overlay_risk_counts.get('critical', 0)}",
        f"- critical released by candidate overlay: {len(released_rows)}",
        f"- enabled candidate rules: {len(enabled_rules)}",
        "- apply allowed: 0",
        "",
        "Original risk:",
        *[f"- {key}: {value}" for key, value in original_risk_counts.most_common()],
        "",
        "Overlay risk:",
        *[f"- {key}: {value}" for key, value in overlay_risk_counts.most_common()],
        "",
        "Overlay actions:",
        *[f"- {key}: {value}" for key, value in overlay_action_counts.most_common()],
        "",
        "Candidate rule readiness:",
    ]
    for row in rule_summary_rows:
        lines.append(
            "- {rule_key}: enabled={enabled}, status={status}, rows={rows}, positive={positive}, "
            "pending={pending}, blockers={blockers}".format(
                rule_key=row["rule_key"],
                enabled=row["candidate_policy_enabled"],
                status=row["promotion_status"],
                rows=row["rows"],
                positive=row["positive_evidence_count"],
                pending=row["pending_decision_count"],
                blockers=row["blocker_count"],
            )
        )
    lines.extend(
        [
            "",
            "Original buckets:",
            *[f"- {key}: {value}" for key, value in original_bucket_counts.most_common()],
            "",
            "Overlay buckets:",
            *[f"- {key}: {value}" for key, value in overlay_bucket_counts.most_common()],
            "",
            "Released critical sample:",
        ]
    )
    for row in released_rows[:40]:
        lines.extend(
            [
                (
                    f"- item {row['source_policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['rule_key']}"
                ),
                f"  ACTION: {row['overlay_action']}",
                f"  RISK: {row['original_risk_level']} -> {row['overlay_risk_level']}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    lines.extend(["", "Remaining critical sample:"])
    for row in remaining_critical_rows[:40]:
        lines.extend(
            [
                (
                    f"- item {row['source_policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['overlay_policy_bucket']}"
                ),
                f"  ACTION: {row['overlay_action']}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This overlay is a dry-run comparison layer; it does not update the base token policy, confirmations, ML scores, or output files.",
            "- Released critical rows become high-risk policy candidates, not auto-apply rows.",
            "- Critical rows that remain critical still need correction, specialist work, or explicit manual review.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, policy_run_id: int | None = None, min_evidence: int = 5) -> None:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        source_policy_run = fetch_policy_run(conn, selected_policy_run_id)
        policy_items = fetch_policy_items(conn, selected_policy_run_id)
        tutorial_release_map, rule_summary_rows = build_tutorial_release_map(
            conn,
            policy_run_id=selected_policy_run_id,
            min_evidence=min_evidence,
        )
        overlay_rows = build_overlay_rows(
            policy_items=policy_items,
            tutorial_release_map=tutorial_release_map,
        )
        overlay_run_id = insert_overlay_run(
            conn,
            source_policy_run=source_policy_run,
            min_evidence=min_evidence,
            started_at=started_at,
        )
        insert_overlay_items(
            conn,
            overlay_run_id=overlay_run_id,
            source_policy_run_id=selected_policy_run_id,
            rows=overlay_rows,
            created_at=started_at,
        )
        conn.commit()

        txt_path, csv_path, jsonl_path = write_outputs(
            settings,
            overlay_run_id=overlay_run_id,
            source_policy_run=source_policy_run,
            min_evidence=min_evidence,
            rule_summary_rows=rule_summary_rows,
            rows=overlay_rows,
            started_at=started_at_dt,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        enabled_rule_count = sum(1 for row in rule_summary_rows if row["candidate_policy_enabled"])
        update_overlay_run(
            conn,
            overlay_run_id=overlay_run_id,
            rows=overlay_rows,
            enabled_rule_count=enabled_rule_count,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            finished_at=finished_at,
        )
        conn.commit()

    original_risk_counts = Counter(row["original_risk_level"] for row in overlay_rows)
    overlay_risk_counts = Counter(row["overlay_risk_level"] for row in overlay_rows)
    action_counts = Counter(row["overlay_action"] for row in overlay_rows)
    print("[segment_token_policy_overlay] Experimental overlay generated")
    print(f"[segment_token_policy_overlay] Rule version: {RULE_VERSION}")
    print(f"[segment_token_policy_overlay] Overlay run id: {overlay_run_id}")
    print(f"[segment_token_policy_overlay] Source policy run id: {selected_policy_run_id}")
    print(f"[segment_token_policy_overlay] Rows evaluated: {len(overlay_rows)}")
    print(f"[segment_token_policy_overlay] Original critical: {original_risk_counts.get('critical', 0)}")
    print(f"[segment_token_policy_overlay] Overlay critical: {overlay_risk_counts.get('critical', 0)}")
    for key, value in action_counts.most_common():
        print(f"[segment_token_policy_overlay] action {key}: {value}")
    print("[segment_token_policy_overlay] Apply allowed: 0")
    print(f"[segment_token_policy_overlay] Report: {txt_path}")
    print(f"[segment_token_policy_overlay] CSV: {csv_path}")
    print(f"[segment_token_policy_overlay] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Materialize an experimental overlay over segment token policy.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--min-evidence", type=int, default=5)
    args = parser.parse_args()
    main(policy_run_id=args.policy_run_id, min_evidence=args.min_evidence)
