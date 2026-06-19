from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_context_lane_partial_promotion_dry_run_v1"
DRY_RUN_NAME = "micro_custom_localization_fragment_partial_file_profile_promotion"
AGENT_KEY = "micro_custom_localization_fragment"
SAFE_DECISION = "safe_short_label"
PROMOTION_CANDIDATE_STATUS = "partial_file_profile_promotion_candidate"
PARTIAL_PROMOTION_DECISION = "partial_file_profile_promotion_dry_run_recommended"


def report_paths(settings: dict[str, Any], audit_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_partial_promotion_dry_run_audit_{audit_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_partial_promotion_dry_run_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            dry_run_name TEXT NOT NULL,
            dry_run_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            audit_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            eligible_file_count INTEGER NOT NULL DEFAULT 0,
            evaluated_count INTEGER NOT NULL DEFAULT 0,
            promotion_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_issue_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_partial_promotion_dry_run_items (
            id INTEGER PRIMARY KEY,
            dry_run_id INTEGER NOT NULL,
            audit_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            classifier_decision TEXT NOT NULL,
            classifier_reason TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            block_reason TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(dry_run_id) REFERENCES ml_issue_short_label_context_lane_partial_promotion_dry_run_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_context_partial_promotion_items_run
        ON ml_issue_short_label_context_lane_partial_promotion_dry_run_items(dry_run_id, promotion_status, relative_path);

        CREATE INDEX IF NOT EXISTS idx_short_label_context_partial_promotion_items_ledger
        ON ml_issue_short_label_context_lane_partial_promotion_dry_run_items(ledger_item_id, promotion_status);
        """
    )


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_context_lane_promotion_audit_runs
        WHERE audit_status = 'completed'
          AND agent_key = ?
          AND partial_promotion_decision = ?
          AND min_reviewed_per_file = 16
          AND max_blocked_per_file = 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, PARTIAL_PROMOTION_DECISION),
    ).fetchone()
    if row is None:
        raise RuntimeError("No conservative partial-promotion audit run found.")
    return int(row["id"])


def fetch_audit_run(conn, *, audit_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_context_lane_promotion_audit_runs
        WHERE id = ?
        """,
        (audit_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Promotion audit run not found: {audit_run_id}")
    return dict(row)


def fetch_eligible_files(conn, *, audit_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_context_lane_promotion_audit_items
        WHERE audit_run_id = ?
          AND promotion_status = ?
        ORDER BY estimated_remaining_gain DESC, relative_path
        """,
        (audit_run_id, PROMOTION_CANDIDATE_STATUS),
    ).fetchall()
    return [dict(row) for row in rows]


def source_key_is_sensitive(source_key: str) -> bool:
    normalized = source_key.strip().lower()
    return normalized in {"adjective_khanal", "adjective_khaganal"}


def fetch_candidate_rows(conn, *, shadow_run_id: int, eligible_paths: list[str]) -> list[dict[str, Any]]:
    if not eligible_paths:
        return []
    placeholders = ", ".join("?" for _ in eligible_paths)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            EXISTS (
                SELECT 1
                FROM ml_issue_review_queue_items queued
                WHERE queued.segment_id = item.segment_id
                  AND queued.agent_key = ?
            ) AS has_existing_queue,
            EXISTS (
                SELECT 1
                FROM ml_issue_review_decisions decision
                WHERE decision.segment_id = item.segment_id
                  AND decision.agent_key = ?
                  AND decision.valid = 1
                  AND decision.validation_status = 'accepted'
            ) AS has_existing_decision
        FROM ml_issue_short_label_context_lane_shadow_policy_items item
        WHERE item.run_id = ?
          AND item.shadow_status = 'shadow_ready'
          AND item.relative_path IN ({placeholders})
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (AGENT_KEY, AGENT_KEY, shadow_run_id, *eligible_paths),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any], eligible_paths: set[str]) -> tuple[str, str]:
    if str(row["relative_path"]) not in eligible_paths:
        return "blocked", "file_not_eligible"
    if str(row.get("classifier_decision") or "") != SAFE_DECISION:
        return "blocked", "classifier_not_safe"
    if source_key_is_sensitive(str(row.get("source_key") or "")):
        return "blocked", "sensitive_domain_key"
    if int(row.get("has_existing_decision") or 0):
        return "blocked", "already_has_accepted_decision"
    if int(row.get("has_existing_queue") or 0):
        return "blocked", "already_queued_for_agent"
    return "promotion_ready", ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    dry_run_id: int,
    audit_run: dict[str, Any],
    eligible_files: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "promotion_status",
        "block_reason",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "classifier_decision",
        "classifier_reason",
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

    ready_rows = [row for row in rows if row["promotion_status"] == "promotion_ready"]
    ready_by_file = Counter(str(row["relative_path"]) for row in ready_rows)
    blocked_by_reason = Counter(str(row["block_reason"] or "none") for row in rows if row["promotion_status"] != "promotion_ready")
    eligible_by_path = {str(item["relative_path"]): item for item in eligible_files}

    lines = [
        "Short-label Context Lane Partial Promotion Dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Dry-run name: {DRY_RUN_NAME}",
        f"Dry-run id: {dry_run_id}",
        f"Audit run id: {audit_run['id']}",
        f"Shadow run id: {audit_run['shadow_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Eligible files from audit: {len(eligible_files):,}",
        f"- Shadow-ready rows evaluated: {len(rows):,}",
        f"- Promotion-ready issue-items: {counts['promotion_ready']:,}",
        f"- Blocked rows: {counts['blocked']:,}",
        f"- Audit estimated gain: {int(audit_run['estimated_remaining_gain']):,}",
        "",
        "Ready by file:",
    ]
    for path, value in ready_by_file.most_common():
        audit_item = eligible_by_path.get(path, {})
        lines.append(
            "- {path}: ready={value:,}, audit_estimate={estimate:,}, reviewed={reviewed}, safe={safe}".format(
                path=path,
                value=value,
                estimate=int(audit_item.get("estimated_remaining_gain") or 0),
                reviewed=int(audit_item.get("validation_reviewed_count") or 0),
                safe=int(audit_item.get("validation_safe_count") or 0),
            )
        )

    lines.extend(["", "Blocked by reason:"])
    if blocked_by_reason:
        for reason, value in blocked_by_reason.most_common():
            lines.append(f"- {reason}: {value:,}")
    else:
        lines.append("- none: 0")

    lines.extend(["", "Ready samples:"])
    for row in ready_rows[:60]:
        lines.append(f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | {row['evidence_text']}")

    lines.extend(
        [
            "",
            "Recommendation:",
            "- This dry-run is ready to become an issue-level checkpoint only if we want to materialize this file-profile policy.",
            "- Do not convert it directly into lifecycle or output application.",
            "- Keep ach_custom_loc, personality_quirks, and signature_weapon outside this promotion path for now.",
            "",
            "Safety note:",
            "- Dry-run only: no checkpoint coverage, no lifecycle policy, no confirmations, no source/output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, audit_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        audit_run = fetch_audit_run(conn, audit_run_id=selected_audit_run_id)
        if audit_run["agent_key"] != AGENT_KEY:
            raise RuntimeError(f"Audit run {selected_audit_run_id} belongs to unexpected agent: {audit_run['agent_key']}")
        if audit_run["partial_promotion_decision"] != PARTIAL_PROMOTION_DECISION:
            raise RuntimeError(f"Audit run {selected_audit_run_id} is not approved for partial promotion dry-run.")

        eligible_files = fetch_eligible_files(conn, audit_run_id=selected_audit_run_id)
        eligible_paths = [str(row["relative_path"]) for row in eligible_files]
        candidates = fetch_candidate_rows(conn, shadow_run_id=int(audit_run["shadow_run_id"]), eligible_paths=eligible_paths)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        eligible_set = set(eligible_paths)
        for candidate in candidates:
            status, reason = classify(candidate, eligible_set)
            counts[status] += 1
            if reason:
                counts[f"block:{reason}"] += 1
            rows.append(
                {
                    "audit_run_id": selected_audit_run_id,
                    "shadow_run_id": int(audit_run["shadow_run_id"]),
                    "diagnostic_item_id": int(candidate["diagnostic_item_id"]),
                    "ledger_item_id": int(candidate["ledger_item_id"]),
                    "segment_id": int(candidate["segment_id"]),
                    "relative_path": str(candidate["relative_path"]),
                    "source_key": str(candidate["source_key"]),
                    "source_line_number": candidate.get("source_line_number"),
                    "classifier_decision": str(candidate.get("classifier_decision") or ""),
                    "classifier_reason": str(candidate.get("classifier_reason") or ""),
                    "promotion_status": status,
                    "block_reason": reason,
                    "evidence_text": candidate.get("evidence_text") or "",
                }
            )

        dry_run_status = "ready_for_checkpoint_dry_run" if counts["promotion_ready"] else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_audit_run_id)
        summary = {
            "audit_run_id": selected_audit_run_id,
            "shadow_run_id": int(audit_run["shadow_run_id"]),
            "eligible_file_count": len(eligible_files),
            "evaluated_count": len(rows),
            "promotion_ready_count": counts["promotion_ready"],
            "blocked_count": counts["blocked"],
            "estimated_issue_gain": counts["promotion_ready"],
            "audit_estimated_remaining_gain": int(audit_run["estimated_remaining_gain"]),
            "production_release_allowed": 0,
            "blockers": {key.removeprefix("block:"): value for key, value in counts.items() if key.startswith("block:")},
        }
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_context_lane_partial_promotion_dry_run_runs (
                rule_version,
                dry_run_name,
                dry_run_status,
                agent_key,
                audit_run_id,
                shadow_run_id,
                eligible_file_count,
                evaluated_count,
                promotion_ready_count,
                blocked_count,
                estimated_issue_gain,
                production_release_allowed,
                summary_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DRY_RUN_NAME,
                dry_run_status,
                AGENT_KEY,
                selected_audit_run_id,
                int(audit_run["shadow_run_id"]),
                len(eligible_files),
                len(rows),
                counts["promotion_ready"],
                counts["blocked"],
                counts["promotion_ready"],
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        dry_run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_context_lane_partial_promotion_dry_run_items (
                dry_run_id,
                audit_run_id,
                shadow_run_id,
                diagnostic_item_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                classifier_decision,
                classifier_reason,
                promotion_status,
                block_reason,
                evidence_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    dry_run_id,
                    row["audit_run_id"],
                    row["shadow_run_id"],
                    row["diagnostic_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["classifier_decision"],
                    row["classifier_reason"],
                    row["promotion_status"],
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
        dry_run_id=dry_run_id,
        audit_run=audit_run,
        eligible_files=eligible_files,
        rows=rows,
        counts=counts,
    )

    print("[issue_short_label_context_lane_partial_promotion_dry_run] Dry-run generated")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Dry-run id: {dry_run_id}")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Audit run id: {selected_audit_run_id}")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Eligible files: {len(eligible_files):,}")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Evaluated: {len(rows):,}")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Ready: {counts['promotion_ready']:,}")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Blocked: {counts['blocked']:,}")
    print(f"[issue_short_label_context_lane_partial_promotion_dry_run] Report: {txt_path}")
    return {
        "dry_run_id": dry_run_id,
        "audit_run_id": selected_audit_run_id,
        "eligible_files": len(eligible_files),
        "evaluated": len(rows),
        "ready": counts["promotion_ready"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run partial file-profile promotion for custom-localization fragments.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    args = parser.parse_args()
    main(audit_run_id=args.audit_run_id)
