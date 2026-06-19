from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_short_label_context_lane_assisted_review import classify


RULE_VERSION = "issue_short_label_context_lane_shadow_policy_v1"
POLICY_NAME = "short_label_context_lane_safe_fragment_shadow"
AGENT_KEY = "micro_custom_localization_fragment"
DEFAULT_ROUTE_LANE = "custom_localization_fragment_context"


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label shadow blocker diagnostic run found.")
    return int(row["id"])


def latest_reviewed_queue_run_id(conn, *, route_lane: str) -> int:
    bucket = f"short_label_context:{route_lane}"
    row = conn.execute(
        """
        SELECT run.id
        FROM ml_issue_review_queue_runs run
        WHERE run.finished_at IS NOT NULL
          AND run.agent_key = ?
          AND run.reviewed_count > 0
          AND EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items item
              WHERE item.run_id = run.id
                AND item.queue_bucket = ?
          )
        ORDER BY run.id DESC
        LIMIT 1
        """,
        (AGENT_KEY, bucket),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No reviewed queue run found for route lane: {route_lane}")
    return int(row["id"])


def report_paths(settings: dict[str, Any], diagnostic_run_id: int, queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_shadow_policy_diag_{diagnostic_run_id}_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_shadow_policy_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            route_lane TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            calibration_queue_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            shadow_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            reason_counts_json TEXT,
            file_profile_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_shadow_policy_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            route_lane TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            classifier_decision TEXT NOT NULL,
            classifier_reason TEXT NOT NULL,
            file_reviewed_count INTEGER NOT NULL DEFAULT 0,
            file_safe_count INTEGER NOT NULL DEFAULT 0,
            file_context_count INTEGER NOT NULL DEFAULT 0,
            file_profile_status TEXT NOT NULL,
            block_reason TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_context_lane_shadow_policy_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_context_shadow_items_run
        ON ml_issue_short_label_context_lane_shadow_policy_items(run_id, shadow_status, relative_path);

        CREATE INDEX IF NOT EXISTS idx_short_label_context_shadow_items_ledger
        ON ml_issue_short_label_context_lane_shadow_policy_items(ledger_item_id, shadow_status);
        """
    )


def fetch_candidates(conn, *, diagnostic_run_id: int, route_lane: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_items
        WHERE run_id = ?
          AND route_lane = ?
          AND shadow_status != 'shadow_ready'
        ORDER BY relative_path, source_line_number, source_key
        """,
        (diagnostic_run_id, route_lane),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_file_profiles(conn, *, queue_run_id: int) -> dict[str, Counter[str]]:
    rows = conn.execute(
        """
        SELECT
            item.relative_path,
            decision.normalized_decision,
            COUNT(*) AS n
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        GROUP BY item.relative_path, decision.normalized_decision
        """,
        (queue_run_id,),
    ).fetchall()
    profiles: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        profiles[str(row["relative_path"])][str(row["normalized_decision"])] += int(row["n"] or 0)
    return profiles


def profile_status(profile: Counter[str], *, min_reviewed_per_file: int) -> tuple[str, int, int, int]:
    reviewed = sum(profile.values())
    safe = profile["safe_short_label"]
    context = profile["needs_domain_context"]
    if reviewed < min_reviewed_per_file:
        return "insufficient_reviewed_file_profile", reviewed, safe, context
    if reviewed == safe:
        return "file_profile_all_safe", reviewed, safe, context
    return "file_profile_mixed_or_contextual", reviewed, safe, context


def shadow_block_reason(*, decision: str, profile: str) -> str:
    if decision != "safe_short_label":
        return f"classifier_not_safe:{decision}"
    if profile != "file_profile_all_safe":
        return profile
    return ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
    file_counts: Counter[str],
) -> None:
    fields = [
        "shadow_status",
        "block_reason",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "classifier_decision",
        "classifier_reason",
        "file_reviewed_count",
        "file_safe_count",
        "file_context_count",
        "file_profile_status",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short-label Context Lane Shadow Policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Run id: {run_id}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {counts['shadow_ready']:,}",
        f"- Shadow blocked: {counts['shadow_blocked']:,}",
        "",
        "Classifier decisions:",
    ]
    for key, value in counts.most_common():
        if key.startswith("decision:"):
            lines.append(f"- {key.removeprefix('decision:')}: {value:,}")
    lines.extend(["", "File profile status:"])
    for key, value in counts.most_common():
        if key.startswith("profile:"):
            lines.append(f"- {key.removeprefix('profile:')}: {value:,}")
    lines.extend(["", "Blockers:"])
    for key, value in counts.most_common():
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Shadow ready by file:"])
    for key, value in file_counts.most_common(30):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Ready samples:"])
    for row in [item for item in rows if item["shadow_status"] == "shadow_ready"][:50]:
        lines.append(f"- {row['relative_path']}::{row['source_key']} | {row['evidence_text']}")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is shadow only: no coverage checkpoint, no lifecycle, no output write.",
            "- A future checkpoint should require additional validation or explicit approval for file-profile expansion.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    diagnostic_run_id: int | None = None,
    calibration_queue_run_id: int | None = None,
    route_lane: str = DEFAULT_ROUTE_LANE,
    min_reviewed_per_file: int = 8,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        selected_queue_run_id = calibration_queue_run_id or latest_reviewed_queue_run_id(conn, route_lane=route_lane)
        candidates = fetch_candidates(conn, diagnostic_run_id=selected_diagnostic_run_id, route_lane=route_lane)
        profiles = fetch_file_profiles(conn, queue_run_id=selected_queue_run_id)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        ready_file_counts: Counter[str] = Counter()
        for row in candidates:
            decision, reason = classify(row)
            profile, reviewed, safe, context = profile_status(
                profiles.get(str(row["relative_path"]), Counter()),
                min_reviewed_per_file=min_reviewed_per_file,
            )
            block = shadow_block_reason(decision=decision, profile=profile)
            ready = not block
            status = "shadow_ready" if ready else "shadow_blocked"
            counts[status] += 1
            counts[f"decision:{decision}"] += 1
            counts[f"profile:{profile}"] += 1
            if block:
                counts[f"block:{block}"] += 1
            else:
                ready_file_counts[str(row["relative_path"])] += 1
            rows.append(
                {
                    "diagnostic_item_id": row["id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "route_lane": route_lane,
                    "shadow_status": status,
                    "classifier_decision": decision,
                    "classifier_reason": reason,
                    "file_reviewed_count": reviewed,
                    "file_safe_count": safe,
                    "file_context_count": context,
                    "file_profile_status": profile,
                    "block_reason": block,
                    "evidence_text": row.get("evidence_text") or "",
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_diagnostic_run_id, selected_queue_run_id)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_context_lane_shadow_policy_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                route_lane,
                diagnostic_run_id,
                calibration_queue_run_id,
                candidate_count,
                shadow_ready_count,
                shadow_blocked_count,
                production_release_allowed,
                decision_counts_json,
                reason_counts_json,
                file_profile_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                AGENT_KEY,
                route_lane,
                selected_diagnostic_run_id,
                selected_queue_run_id,
                len(rows),
                counts["shadow_ready"],
                counts["shadow_blocked"],
                json.dumps({key.removeprefix("decision:"): value for key, value in counts.items() if key.startswith("decision:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({key.removeprefix("block:"): value for key, value in counts.items() if key.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({key.removeprefix("profile:"): value for key, value in counts.items() if key.startswith("profile:")}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_context_lane_shadow_policy_items (
                run_id,
                diagnostic_item_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                route_lane,
                shadow_status,
                classifier_decision,
                classifier_reason,
                file_reviewed_count,
                file_safe_count,
                file_context_count,
                file_profile_status,
                block_reason,
                evidence_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    row["diagnostic_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["route_lane"],
                    row["shadow_status"],
                    row["classifier_decision"],
                    row["classifier_reason"],
                    row["file_reviewed_count"],
                    row["file_safe_count"],
                    row["file_context_count"],
                    row["file_profile_status"],
                    row["block_reason"],
                    row["evidence_text"],
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        rows=rows,
        counts=counts,
        file_counts=ready_file_counts,
    )

    print("[issue_short_label_context_lane_shadow_policy] Shadow generated")
    print(f"[issue_short_label_context_lane_shadow_policy] Run id: {run_id}")
    print(f"[issue_short_label_context_lane_shadow_policy] Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"[issue_short_label_context_lane_shadow_policy] Calibration queue run id: {selected_queue_run_id}")
    print(f"[issue_short_label_context_lane_shadow_policy] Candidates: {len(rows):,}")
    print(f"[issue_short_label_context_lane_shadow_policy] Shadow ready: {counts['shadow_ready']:,}")
    print(f"[issue_short_label_context_lane_shadow_policy] Shadow blocked: {counts['shadow_blocked']:,}")
    print(f"[issue_short_label_context_lane_shadow_policy] Report: {txt_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "calibration_queue_run_id": selected_queue_run_id,
        "candidate_count": len(rows),
        "shadow_ready_count": counts["shadow_ready"],
        "shadow_blocked_count": counts["shadow_blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a shadow policy for short-label context-lane safe fragments.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--calibration-queue-run-id", type=int, default=None)
    parser.add_argument("--route-lane", default=DEFAULT_ROUTE_LANE)
    parser.add_argument("--min-reviewed-per-file", type=int, default=8)
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        calibration_queue_run_id=args.calibration_queue_run_id,
        route_lane=args.route_lane,
        min_reviewed_per_file=args.min_reviewed_per_file,
    )
