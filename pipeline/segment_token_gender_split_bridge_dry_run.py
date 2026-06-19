from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "segment_token_gender_split_bridge_dry_run_v1"
BRIDGE_ACTION = "would_route_to_guarded_low_review_without_apply"


def latest_guarded_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_gender_split_guarded_policy_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished segment_token_gender_split_guarded_policy_runs entry found.")
    return int(row["id"])


def fetch_guarded_run(conn, guarded_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_gender_split_guarded_policy_runs
        WHERE id = ?
        """,
        (guarded_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded policy run {guarded_run_id} was not found.")
    return dict(row)


def fetch_rows(conn, *, guarded_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            g.id AS guarded_item_id,
            g.run_id AS guarded_run_id,
            g.policy_run_id,
            g.policy_item_id,
            g.segment_id,
            g.relative_path,
            g.source_key,
            g.source_line_number,
            g.split_agent,
            g.split_maturity,
            g.method_signature,
            g.guarded_status,
            g.guarded_action,
            g.target_policy_bucket,
            g.target_risk_level,
            g.reasons_json AS guarded_reasons_json,
            i.policy_bucket AS current_policy_bucket,
            i.risk_level AS current_risk_level,
            i.recommendation AS current_recommendation,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            d.id AS current_decision_id,
            d.decision AS current_decision,
            d.approved_for_apply AS current_approved_for_apply
        FROM segment_token_gender_split_guarded_policy_items g
        JOIN segment_token_policy_items i ON i.id = g.policy_item_id
        LEFT JOIN segment_token_policy_decisions d
          ON d.policy_run_id = g.policy_run_id
         AND d.policy_item_id = g.policy_item_id
        WHERE g.run_id = ?
        ORDER BY
            g.split_agent,
            g.relative_path,
            g.source_line_number,
            g.policy_item_id
        """,
        (guarded_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    reasons = [
        f"rule:{RULE_VERSION}",
        f"guarded_status:{row['guarded_status']}",
        f"guarded_action:{row['guarded_action']}",
        "would_create_policy_decision:0",
        "approved_for_apply:0",
        "apply_allowed:0",
    ]
    if row.get("current_decision_id"):
        bridge_status = "blocked_existing_policy_decision"
        bridge_action = "keep_existing_decision"
        reasons.append(f"existing_decision:{row.get('current_decision')}")
    elif row["guarded_status"] != "ready_for_guarded_dry_run_release":
        bridge_status = "blocked_by_guarded_policy"
        bridge_action = "keep_heavy_review"
    else:
        bridge_status = "would_bridge_to_guarded_low_review"
        bridge_action = BRIDGE_ACTION
        reasons.extend(
            [
                f"from:{row['current_policy_bucket']}/{row['current_risk_level']}",
                f"to:{row['target_policy_bucket']}/{row['target_risk_level']}",
            ]
        )
    return {
        **row,
        "bridge_status": bridge_status,
        "bridge_action": bridge_action,
        "would_create_policy_decision": 0,
        "approved_for_apply": 0,
        "apply_allowed": 0,
        "bridge_reasons": reasons,
    }


def insert_run(conn, *, guarded_run_id: int, policy_run_id: int, started_at: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_gender_split_bridge_dry_run_runs (
            rule_version,
            guarded_run_id,
            policy_run_id,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, guarded_run_id, policy_run_id, started_at, started_at),
    )
    return int(cur.lastrowid)


def insert_items(conn, *, run_id: int, rows: list[dict[str, Any]], now: str) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO segment_token_gender_split_bridge_dry_run_items (
                run_id,
                guarded_run_id,
                policy_run_id,
                policy_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                split_agent,
                current_policy_bucket,
                current_risk_level,
                target_policy_bucket,
                target_risk_level,
                bridge_status,
                bridge_action,
                would_create_policy_decision,
                approved_for_apply,
                apply_allowed,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["guarded_run_id"],
                row["policy_run_id"],
                row["policy_item_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["split_agent"],
                row["current_policy_bucket"],
                row["current_risk_level"],
                row["target_policy_bucket"],
                row["target_risk_level"],
                row["bridge_status"],
                row["bridge_action"],
                row["would_create_policy_decision"],
                row["approved_for_apply"],
                row["apply_allowed"],
                json.dumps(row["bridge_reasons"], ensure_ascii=False),
                now,
            ),
        )


def write_outputs(
    settings: dict[str, Any],
    *,
    run_id: int,
    guarded_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_gender_split_bridge_dry_run"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "split_agent",
        "current_policy_bucket",
        "current_risk_level",
        "target_policy_bucket",
        "target_risk_level",
        "bridge_status",
        "bridge_action",
        "would_create_policy_decision",
        "approved_for_apply",
        "apply_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    status_counts = Counter(row["bridge_status"] for row in rows)
    agent_counts = Counter(row["split_agent"] for row in rows if row["bridge_status"] == "would_bridge_to_guarded_low_review")
    source_counts = Counter(
        (row["current_policy_bucket"], row["current_risk_level"])
        for row in rows
        if row["bridge_status"] == "would_bridge_to_guarded_low_review"
    )
    lines = [
        "Segment token gender split bridge dry-run",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Bridge run id: {run_id}",
        f"Guarded policy run id: {guarded_run['id']}",
        f"Policy run id: {guarded_run['policy_run_id']}",
        "",
        "Safety contract:",
        "- Operational bridge simulation only.",
        "- Creates segment_token_policy_decisions: no",
        "- Approved for apply: 0",
        "- Apply allowed: 0",
        "- Writes source/output: no",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Would bridge by agent:",
        *[f"- {key}: {value}" for key, value in agent_counts.most_common()],
        "",
        "Would bridge from buckets:",
        *[f"- {bucket}/{risk}: {count}" for (bucket, risk), count in source_counts.most_common()],
        "",
        "Interpretation:",
        "- These rows could leave the heavy gender-token review lane and move to guarded low review.",
        "- They still would not become automatic apply approvals.",
        "- A production integration must explicitly consume this dry-run or a future promoted bridge checkpoint.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def update_run(
    conn,
    *,
    run_id: int,
    rows: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    finished_at: str,
) -> None:
    status_counts = Counter(row["bridge_status"] for row in rows)
    conn.execute(
        """
        UPDATE segment_token_gender_split_bridge_dry_run_runs
        SET
            total_candidates = ?,
            would_bridge_count = ?,
            blocked_count = ?,
            would_create_policy_decisions = 0,
            apply_allowed_count = 0,
            report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len(rows),
            status_counts["would_bridge_to_guarded_low_review"],
            len(rows) - status_counts["would_bridge_to_guarded_low_review"],
            str(report_path),
            str(csv_path),
            str(jsonl_path),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(*, guarded_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_guarded_run_id = guarded_run_id or latest_guarded_run_id(conn)
        guarded_run = fetch_guarded_run(conn, selected_guarded_run_id)
        raw_rows = fetch_rows(conn, guarded_run_id=selected_guarded_run_id)
        rows = [classify(row) for row in raw_rows]
        run_id = insert_run(
            conn,
            guarded_run_id=selected_guarded_run_id,
            policy_run_id=int(guarded_run["policy_run_id"]),
            started_at=started_at.isoformat(timespec="seconds"),
        )
        now = db.utc_now()
        insert_items(conn, run_id=run_id, rows=rows, now=now)
        report_path, csv_path, jsonl_path = write_outputs(
            settings,
            run_id=run_id,
            guarded_run=guarded_run,
            rows=rows,
            started_at=started_at,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        update_run(
            conn,
            run_id=run_id,
            rows=rows,
            report_path=report_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            finished_at=finished_at,
        )
        conn.commit()

    status_counts = Counter(row["bridge_status"] for row in rows)
    print("[segment_token_gender_split_bridge_dry_run] Bridge dry-run generated")
    print(f"[segment_token_gender_split_bridge_dry_run] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_bridge_dry_run] Guarded policy run id: {selected_guarded_run_id}")
    print(f"[segment_token_gender_split_bridge_dry_run] Bridge run id: {run_id}")
    print(f"[segment_token_gender_split_bridge_dry_run] Rows inspected: {len(rows)}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_gender_split_bridge_dry_run] {key}: {value}")
    print("[segment_token_gender_split_bridge_dry_run] Creates policy decisions: 0")
    print("[segment_token_gender_split_bridge_dry_run] Apply allowed: 0")
    print(f"[segment_token_gender_split_bridge_dry_run] Report: {report_path}")
    print(f"[segment_token_gender_split_bridge_dry_run] CSV: {csv_path}")
    print(f"[segment_token_gender_split_bridge_dry_run] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "report_path": str(report_path),
        "rows": len(rows),
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run operational bridge for gender split guarded policies.")
    parser.add_argument("--guarded-run-id", type=int, default=None)
    args = parser.parse_args()
    main(guarded_run_id=args.guarded_run_id)
