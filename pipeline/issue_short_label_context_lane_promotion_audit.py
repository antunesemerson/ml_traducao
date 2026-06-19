from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_context_lane_promotion_audit_v1"
AUDIT_NAME = "micro_custom_localization_fragment_promotion_audit"
AGENT_KEY = "micro_custom_localization_fragment"
SHADOW_VALIDATION_STRATEGY = "short_label_context_lane_shadow_ready_validation"
CALIBRATION_STRATEGY = "short_label_context_lane_stratified_review"
SAFE_DECISION = "safe_short_label"


def report_paths(settings: dict[str, Any], shadow_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_promotion_audit_shadow_{shadow_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_promotion_audit_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            audit_name TEXT NOT NULL,
            audit_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            min_reviewed_per_file INTEGER NOT NULL,
            max_blocked_per_file INTEGER NOT NULL,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            shadow_blocked_count INTEGER NOT NULL DEFAULT 0,
            validation_reviewed_count INTEGER NOT NULL DEFAULT 0,
            validation_safe_count INTEGER NOT NULL DEFAULT 0,
            validation_blocked_count INTEGER NOT NULL DEFAULT 0,
            eligible_file_count INTEGER NOT NULL DEFAULT 0,
            ineligible_file_count INTEGER NOT NULL DEFAULT 0,
            estimated_remaining_gain INTEGER NOT NULL DEFAULT 0,
            broad_promotion_decision TEXT NOT NULL,
            partial_promotion_decision TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_promotion_audit_items (
            id INTEGER PRIMARY KEY,
            audit_run_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            shadow_blocked_count INTEGER NOT NULL DEFAULT 0,
            validation_reviewed_count INTEGER NOT NULL DEFAULT 0,
            validation_safe_count INTEGER NOT NULL DEFAULT 0,
            validation_blocked_count INTEGER NOT NULL DEFAULT 0,
            validation_safe_rate REAL,
            distinct_validation_families INTEGER NOT NULL DEFAULT 0,
            remaining_unreviewed_shadow_ready INTEGER NOT NULL DEFAULT 0,
            latest_checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            promotion_status TEXT NOT NULL,
            promotion_reason TEXT NOT NULL,
            estimated_remaining_gain INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(audit_run_id) REFERENCES ml_issue_short_label_context_lane_promotion_audit_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_context_promotion_audit_items_run
        ON ml_issue_short_label_context_lane_promotion_audit_items(audit_run_id, promotion_status);
        """
    )


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_context_lane_shadow_policy_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND shadow_ready_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished custom-localization shadow policy run found.")
    return int(row["id"])


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_context_lane_shadow_policy_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Shadow policy run not found: {shadow_run_id}")
    return dict(row)


def latest_coverage_run(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def queue_ids(conn, *, strategy: str, reviewed_only: bool = True) -> list[int]:
    reviewed_filter = "AND reviewed_count > 0" if reviewed_only else ""
    rows = conn.execute(
        f"""
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE agent_key = ?
          AND queue_strategy = ?
          {reviewed_filter}
        ORDER BY id
        """,
        (AGENT_KEY, strategy),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def placeholders(values: list[Any]) -> str:
    if not values:
        return "NULL"
    return ", ".join("?" for _ in values)


def validation_by_file(conn, *, queue_run_ids: list[int]) -> dict[str, dict[str, Any]]:
    if not queue_run_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT
            item.relative_path,
            decision.normalized_decision,
            COUNT(*) AS n
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id IN ({placeholders(queue_run_ids)})
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        GROUP BY item.relative_path, decision.normalized_decision
        """,
        tuple(queue_run_ids),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row["relative_path"])
        bucket = result.setdefault(path, {"reviewed": 0, "safe": 0, "blocked": 0, "decisions": Counter()})
        decision = str(row["normalized_decision"] or "unknown")
        n = int(row["n"] or 0)
        bucket["reviewed"] += n
        bucket["decisions"][decision] += n
        if decision == SAFE_DECISION:
            bucket["safe"] += n
        else:
            bucket["blocked"] += n
    return result


def validation_families_by_file(conn, *, queue_run_ids: list[int]) -> dict[str, set[str]]:
    if not queue_run_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT item.relative_path, item.evidence_json
        FROM ml_issue_review_queue_items item
        WHERE item.run_id IN ({placeholders(queue_run_ids)})
          AND item.agent_key = ?
        """,
        (*queue_run_ids, AGENT_KEY),
    ).fetchall()
    result: dict[str, set[str]] = {}
    for row in rows:
        path = str(row["relative_path"])
        family = "unknown"
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
            family = str(evidence.get("key_family") or family)
        except json.JSONDecodeError:
            family = "invalid_json"
        result.setdefault(path, set()).add(family)
    return result


def shadow_by_file(conn, *, shadow_run_id: int) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT
            relative_path,
            SUM(CASE WHEN shadow_status = 'shadow_ready' THEN 1 ELSE 0 END) AS shadow_ready,
            SUM(CASE WHEN shadow_status = 'shadow_blocked' THEN 1 ELSE 0 END) AS shadow_blocked
        FROM ml_issue_short_label_context_lane_shadow_policy_items
        WHERE run_id = ?
        GROUP BY relative_path
        """,
        (shadow_run_id,),
    ).fetchall()
    return {
        str(row["relative_path"]): {
            "shadow_ready": int(row["shadow_ready"] or 0),
            "shadow_blocked": int(row["shadow_blocked"] or 0),
        }
        for row in rows
    }


def remaining_shadow_ready_by_file(conn, *, shadow_run_id: int) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT item.relative_path, COUNT(*) AS n
        FROM ml_issue_short_label_context_lane_shadow_policy_items item
        WHERE item.run_id = ?
          AND item.shadow_status = 'shadow_ready'
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = ?
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.segment_id = item.segment_id
                AND decision.agent_key = ?
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
          )
        GROUP BY item.relative_path
        """,
        (shadow_run_id, AGENT_KEY, AGENT_KEY),
    ).fetchall()
    return {str(row["relative_path"]): int(row["n"] or 0) for row in rows}


def latest_checkpoint_allowed_by_file(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT queue_run_id, MAX(id) AS checkpoint_run_id
            FROM ml_issue_short_label_context_lane_checkpoint_runs
            WHERE agent_key = ?
            GROUP BY queue_run_id
        )
        SELECT item.relative_path, COUNT(*) AS n
        FROM ml_issue_short_label_context_lane_checkpoint_items item
        JOIN latest ON latest.checkpoint_run_id = item.checkpoint_run_id
        WHERE item.checkpoint_allowed = 1
        GROUP BY item.relative_path
        """,
        (AGENT_KEY,),
    ).fetchall()
    return {str(row["relative_path"]): int(row["n"] or 0) for row in rows}


def classify_file(
    *,
    shadow_ready: int,
    reviewed: int,
    blocked: int,
    remaining: int,
    min_reviewed_per_file: int,
    max_blocked_per_file: int,
) -> tuple[str, str, int]:
    if shadow_ready <= 0:
        return "not_applicable_no_shadow_ready", "no_shadow_ready_candidates", 0
    if blocked > max_blocked_per_file:
        return "blocked_domain_context", "validation_found_context_or_non_safe_decisions", 0
    if reviewed < min_reviewed_per_file:
        return "needs_more_validation", "insufficient_file_level_review_depth", 0
    if remaining <= 0:
        return "validated_no_remaining_gain", "all_shadow_ready_candidates_already_queued_or_decided", 0
    return "partial_file_profile_promotion_candidate", "review_depth_met_with_zero_blockers", remaining


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    audit_run_id: int,
    shadow_run: dict[str, Any],
    coverage_run: dict[str, Any] | None,
    calibration_queue_ids: list[int],
    validation_queue_ids: list[int],
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    fields = [
        "promotion_status",
        "promotion_reason",
        "relative_path",
        "shadow_ready_count",
        "shadow_blocked_count",
        "validation_reviewed_count",
        "validation_safe_count",
        "validation_blocked_count",
        "validation_safe_rate",
        "distinct_validation_families",
        "remaining_unreviewed_shadow_ready",
        "latest_checkpoint_allowed",
        "estimated_remaining_gain",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    json_path.write_text(
        json.dumps(
            {
                "audit_run_id": audit_run_id,
                "rule_version": RULE_VERSION,
                "audit_name": AUDIT_NAME,
                "shadow_run": shadow_run,
                "latest_coverage_run": coverage_run,
                "calibration_queue_ids": calibration_queue_ids,
                "validation_queue_ids": validation_queue_ids,
                "summary": summary,
                "files": rows,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    eligible = [row for row in rows if row["promotion_status"] == "partial_file_profile_promotion_candidate"]
    needs_more = [row for row in rows if row["promotion_status"] == "needs_more_validation"]
    blocked = [row for row in rows if row["promotion_status"] == "blocked_domain_context"]

    lines = [
        "Custom Localization Fragment Promotion Audit",
        f"Rule version: {RULE_VERSION}",
        f"Audit run id: {audit_run_id}",
        f"Shadow run id: {shadow_run['id']}",
        "Production release allowed: 0",
        "",
        "Decision:",
        f"- Broad promotion: {summary['broad_promotion_decision']}",
        f"- Partial promotion: {summary['partial_promotion_decision']}",
        "",
        "Summary:",
        f"- Shadow ready candidates: {summary['shadow_ready_count']:,}",
        f"- Shadow blocked candidates: {summary['shadow_blocked_count']:,}",
        f"- Calibration queues: {calibration_queue_ids}",
        f"- Validation queues: {validation_queue_ids}",
        f"- Validation reviewed: {summary['validation_reviewed_count']:,}",
        f"- Validation safe: {summary['validation_safe_count']:,}",
        f"- Validation blocked: {summary['validation_blocked_count']:,}",
        f"- Validation safe rate: {summary['validation_safe_rate']:.4%}",
        f"- Eligible files: {summary['eligible_file_count']:,}",
        f"- Files needing more validation: {len(needs_more):,}",
        f"- Blocked/mixed files: {len(blocked):,}",
        f"- Estimated remaining gain from eligible files: {summary['estimated_remaining_gain']:,} issue-items",
    ]
    if coverage_run:
        projected = int(coverage_run["covered_issue_items"]) + int(summary["estimated_remaining_gain"])
        projected_ratio = projected / max(1, int(coverage_run["total_issue_items"]))
        lines.extend(
            [
                f"- Latest coverage run: {coverage_run['id']}",
                f"- Current covered issue-items: {int(coverage_run['covered_issue_items']):,}/{int(coverage_run['total_issue_items']):,}",
                f"- Projected covered issue-items after eligible promotion: {projected:,}/{int(coverage_run['total_issue_items']):,} ({projected_ratio:.2%})",
            ]
        )

    lines.extend(["", "Eligible file-profile candidates:"])
    for row in sorted(eligible, key=lambda item: item["estimated_remaining_gain"], reverse=True):
        lines.append(
            "- {relative_path}: remaining_gain={estimated_remaining_gain:,}, reviewed={validation_reviewed_count}, safe={validation_safe_count}, families={distinct_validation_families}".format(
                **row
            )
        )

    lines.extend(["", "Needs more validation:"])
    for row in sorted(needs_more, key=lambda item: item["remaining_unreviewed_shadow_ready"], reverse=True):
        lines.append(
            "- {relative_path}: remaining={remaining_unreviewed_shadow_ready:,}, reviewed={validation_reviewed_count}, safe={validation_safe_count}".format(
                **row
            )
        )

    lines.extend(["", "Blocked/mixed files:"])
    for row in sorted(blocked, key=lambda item: item["validation_blocked_count"], reverse=True):
        lines.append(
            "- {relative_path}: blocked={validation_blocked_count}, safe={validation_safe_count}, remaining={remaining_unreviewed_shadow_ready:,}".format(
                **row
            )
        )

    lines.extend(
        [
            "",
            "Recommendation:",
            "- Do not promote the whole shadow-ready universe yet.",
            "- A partial file-profile policy is a reasonable next dry-run target for eligible files only.",
            "- Exclude files with any domain-context blocker, especially ach_custom_loc, until a dedicated title/adjective route handles them.",
            "- Files with high remaining gain but shallow review depth should receive another targeted validation queue before promotion.",
            "",
            "Safety note:",
            "- This audit creates no lifecycle policy, no confirmation, no source/output writes, and no production release.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    shadow_run_id: int | None = None,
    min_reviewed_per_file: int = 16,
    max_blocked_per_file: int = 0,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        coverage_run = latest_coverage_run(conn)
        validation_queue_ids = queue_ids(conn, strategy=SHADOW_VALIDATION_STRATEGY)
        calibration_queue_ids = queue_ids(conn, strategy=CALIBRATION_STRATEGY)

        shadow = shadow_by_file(conn, shadow_run_id=selected_shadow_run_id)
        validation = validation_by_file(conn, queue_run_ids=validation_queue_ids)
        families = validation_families_by_file(conn, queue_run_ids=validation_queue_ids)
        remaining = remaining_shadow_ready_by_file(conn, shadow_run_id=selected_shadow_run_id)
        allowed = latest_checkpoint_allowed_by_file(conn)

        all_files = sorted(set(shadow) | set(validation) | set(remaining) | set(allowed))
        rows: list[dict[str, Any]] = []
        counts = Counter()
        for relative_path in all_files:
            shadow_ready = int(shadow.get(relative_path, {}).get("shadow_ready", 0))
            shadow_blocked = int(shadow.get(relative_path, {}).get("shadow_blocked", 0))
            reviewed = int(validation.get(relative_path, {}).get("reviewed", 0))
            safe = int(validation.get(relative_path, {}).get("safe", 0))
            blocked = int(validation.get(relative_path, {}).get("blocked", 0))
            left = int(remaining.get(relative_path, 0))
            status, reason, gain = classify_file(
                shadow_ready=shadow_ready,
                reviewed=reviewed,
                blocked=blocked,
                remaining=left,
                min_reviewed_per_file=min_reviewed_per_file,
                max_blocked_per_file=max_blocked_per_file,
            )
            safe_rate = (safe / reviewed) if reviewed else None
            row = {
                "relative_path": relative_path,
                "shadow_ready_count": shadow_ready,
                "shadow_blocked_count": shadow_blocked,
                "validation_reviewed_count": reviewed,
                "validation_safe_count": safe,
                "validation_blocked_count": blocked,
                "validation_safe_rate": safe_rate,
                "distinct_validation_families": len(families.get(relative_path, set())),
                "remaining_unreviewed_shadow_ready": left,
                "latest_checkpoint_allowed": int(allowed.get(relative_path, 0)),
                "promotion_status": status,
                "promotion_reason": reason,
                "estimated_remaining_gain": gain,
            }
            rows.append(row)
            counts[f"status:{status}"] += 1

        validation_reviewed = sum(int(row["validation_reviewed_count"]) for row in rows)
        validation_safe = sum(int(row["validation_safe_count"]) for row in rows)
        validation_blocked = sum(int(row["validation_blocked_count"]) for row in rows)
        eligible_file_count = counts["status:partial_file_profile_promotion_candidate"]
        estimated_gain = sum(int(row["estimated_remaining_gain"]) for row in rows)
        broad_promotion_decision = "defer_broad_promotion"
        partial_promotion_decision = (
            "partial_file_profile_promotion_dry_run_recommended"
            if eligible_file_count and estimated_gain
            else "no_partial_promotion_candidate"
        )
        summary = {
            "min_reviewed_per_file": min_reviewed_per_file,
            "max_blocked_per_file": max_blocked_per_file,
            "shadow_ready_count": int(shadow_run["shadow_ready_count"]),
            "shadow_blocked_count": int(shadow_run["shadow_blocked_count"]),
            "validation_reviewed_count": validation_reviewed,
            "validation_safe_count": validation_safe,
            "validation_blocked_count": validation_blocked,
            "validation_safe_rate": (validation_safe / validation_reviewed) if validation_reviewed else 0.0,
            "eligible_file_count": eligible_file_count,
            "ineligible_file_count": len(rows) - eligible_file_count,
            "estimated_remaining_gain": estimated_gain,
            "broad_promotion_decision": broad_promotion_decision,
            "partial_promotion_decision": partial_promotion_decision,
            "status_counts": {key.removeprefix("status:"): value for key, value in counts.items() if key.startswith("status:")},
        }

        txt_path, csv_path, json_path = report_paths(settings, selected_shadow_run_id)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_context_lane_promotion_audit_runs (
                rule_version,
                audit_name,
                audit_status,
                agent_key,
                shadow_run_id,
                min_reviewed_per_file,
                max_blocked_per_file,
                shadow_ready_count,
                shadow_blocked_count,
                validation_reviewed_count,
                validation_safe_count,
                validation_blocked_count,
                eligible_file_count,
                ineligible_file_count,
                estimated_remaining_gain,
                broad_promotion_decision,
                partial_promotion_decision,
                production_release_allowed,
                summary_json,
                report_path,
                csv_path,
                json_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                AUDIT_NAME,
                AGENT_KEY,
                selected_shadow_run_id,
                min_reviewed_per_file,
                max_blocked_per_file,
                summary["shadow_ready_count"],
                summary["shadow_blocked_count"],
                validation_reviewed,
                validation_safe,
                validation_blocked,
                eligible_file_count,
                summary["ineligible_file_count"],
                estimated_gain,
                broad_promotion_decision,
                partial_promotion_decision,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(json_path),
                started_at,
                now,
                now,
            ),
        )
        audit_run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_context_lane_promotion_audit_items (
                audit_run_id,
                relative_path,
                shadow_ready_count,
                shadow_blocked_count,
                validation_reviewed_count,
                validation_safe_count,
                validation_blocked_count,
                validation_safe_rate,
                distinct_validation_families,
                remaining_unreviewed_shadow_ready,
                latest_checkpoint_allowed,
                promotion_status,
                promotion_reason,
                estimated_remaining_gain,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    audit_run_id,
                    row["relative_path"],
                    row["shadow_ready_count"],
                    row["shadow_blocked_count"],
                    row["validation_reviewed_count"],
                    row["validation_safe_count"],
                    row["validation_blocked_count"],
                    row["validation_safe_rate"],
                    row["distinct_validation_families"],
                    row["remaining_unreviewed_shadow_ready"],
                    row["latest_checkpoint_allowed"],
                    row["promotion_status"],
                    row["promotion_reason"],
                    row["estimated_remaining_gain"],
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        json_path=json_path,
        audit_run_id=audit_run_id,
        shadow_run=shadow_run,
        coverage_run=coverage_run,
        calibration_queue_ids=calibration_queue_ids,
        validation_queue_ids=validation_queue_ids,
        rows=rows,
        summary=summary,
    )

    print("[issue_short_label_context_lane_promotion_audit] Audit generated")
    print(f"[issue_short_label_context_lane_promotion_audit] Audit run id: {audit_run_id}")
    print(f"[issue_short_label_context_lane_promotion_audit] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_short_label_context_lane_promotion_audit] Validation reviewed: {validation_reviewed:,}")
    print(f"[issue_short_label_context_lane_promotion_audit] Validation safe: {validation_safe:,}")
    print(f"[issue_short_label_context_lane_promotion_audit] Validation blocked: {validation_blocked:,}")
    print(f"[issue_short_label_context_lane_promotion_audit] Eligible files: {eligible_file_count:,}")
    print(f"[issue_short_label_context_lane_promotion_audit] Estimated remaining gain: {estimated_gain:,}")
    print(f"[issue_short_label_context_lane_promotion_audit] Report: {txt_path}")
    return {
        "audit_run_id": audit_run_id,
        "shadow_run_id": selected_shadow_run_id,
        "validation_reviewed": validation_reviewed,
        "validation_safe": validation_safe,
        "validation_blocked": validation_blocked,
        "eligible_file_count": eligible_file_count,
        "estimated_remaining_gain": estimated_gain,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit promotion readiness for the custom-localization fragment microagent.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--min-reviewed-per-file", type=int, default=16)
    parser.add_argument("--max-blocked-per-file", type=int, default=0)
    args = parser.parse_args()
    main(
        shadow_run_id=args.shadow_run_id,
        min_reviewed_per_file=args.min_reviewed_per_file,
        max_blocked_per_file=args.max_blocked_per_file,
    )
