from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "short_label_guarded_lifecycle_bridge_dry_run_v1"
AGENT_KEY = "micro_short_label_style"
CHECKPOINT_STATUS = "ready_for_guarded_lifecycle_policy"
PROMOTION_STATUS = "guarded_candidate"
LIFECYCLE_ACTION = "close_reopen_short_label_guarded_lifecycle"
FINAL_STATE = "closed_auto_confirmed_short_label_guarded_lifecycle"
LIFECYCLE_POLICY_NAME = "short_label_guarded_lifecycle_bridge"
LIFECYCLE_LABEL_FAMILY = "short_label_guarded_lifecycle"
ALLOWED_NORMALIZED_DECISION = "safe_short_label"
ALLOWED_EVIDENCE_LABEL = "positive_evidence"
BLOCKED_SURFACE_MARKERS = {
    "#bold No#!",
    "SelectLocalization",
}


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def canonical_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No segment-state run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_guarded_lifecycle_bridge_dry_run"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item).strip()]


def fetch_rows(conn, *, state_run_id: int) -> tuple[dict[str, int], list[dict[str, Any]]]:
    raw = conn.execute(
        """
        SELECT
          COUNT(*) AS raw_candidates,
          COUNT(DISTINCT item.segment_id) AS distinct_segments,
          COUNT(DISTINCT run.id) AS checkpoint_runs
        FROM ml_issue_short_label_release_checkpoint_items item
        JOIN ml_issue_short_label_release_checkpoint_runs run
          ON run.id = item.checkpoint_run_id
        WHERE run.finished_at IS NOT NULL
          AND run.checkpoint_status = ?
          AND run.promotion_status = ?
          AND run.agent_key = ?
          AND item.checkpoint_allowed = 1
        """,
        (CHECKPOINT_STATUS, PROMOTION_STATUS, AGENT_KEY),
    ).fetchone()
    rows = conn.execute(
        """
        WITH latest_checkpoint_item AS (
          SELECT item.*
          FROM ml_issue_short_label_release_checkpoint_items item
          JOIN ml_issue_short_label_release_checkpoint_runs run
            ON run.id = item.checkpoint_run_id
          JOIN (
            SELECT
              item.segment_id,
              MAX(printf('%012d', item.checkpoint_run_id) || ':' || printf('%012d', item.id)) AS latest_key
            FROM ml_issue_short_label_release_checkpoint_items item
            JOIN ml_issue_short_label_release_checkpoint_runs run
              ON run.id = item.checkpoint_run_id
            WHERE run.finished_at IS NOT NULL
              AND run.checkpoint_status = ?
              AND run.promotion_status = ?
              AND run.agent_key = ?
              AND item.checkpoint_allowed = 1
            GROUP BY item.segment_id
          ) latest
            ON latest.segment_id = item.segment_id
           AND latest.latest_key = printf('%012d', item.checkpoint_run_id) || ':' || printf('%012d', item.id)
        ),
        issue_summary AS (
          SELECT
            segment_id,
            COUNT(*) AS issue_count,
            SUM(CASE WHEN lower(severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
            GROUP_CONCAT(DISTINCT issue_type) AS issue_types
          FROM issues
          GROUP BY segment_id
        )
        SELECT
          item.id AS checkpoint_item_id,
          item.checkpoint_run_id,
          item.release_run_id,
          item.release_item_id,
          item.decision_id,
          item.queue_item_id,
          item.ledger_item_id,
          item.segment_id,
          item.relative_path,
          item.source_key,
          item.source_line_number,
          item.agent_key,
          item.queue_bucket,
          item.normalized_decision,
          item.evidence_label,
          item.checkpoint_action,
          item.checkpoint_allowed,
          item.block_reason AS checkpoint_block_reason,
          item.token_impact,
          item.token_status,
          item.issue_codes_json,
          item.release_confirmed_text_hash,
          item.current_confirmed_text_hash AS checkpoint_current_confirmed_text_hash,
          run.checkpoint_status,
          run.promotion_status,
          run.production_release_allowed,
          source.is_active,
          state.id AS current_state_item_id,
          state.final_state AS current_final_state,
          state.review_state AS current_review_state,
          state.apply_state AS current_apply_state,
          state.is_closed AS current_is_closed,
          state.locked AS current_locked,
          confirmation.id AS confirmation_id,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.confirmed_text,
          confirmation.locked AS confirmation_locked,
          output.portuguese_text AS output_text,
          COALESCE(issue_summary.issue_count, 0) AS issue_count,
          COALESCE(issue_summary.high_issue_count, 0) AS high_issue_count,
          issue_summary.issue_types
        FROM latest_checkpoint_item item
        JOIN ml_issue_short_label_release_checkpoint_runs run
          ON run.id = item.checkpoint_run_id
        JOIN source_segments source
          ON source.id = item.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        LEFT JOIN issue_summary
          ON issue_summary.segment_id = item.segment_id
        ORDER BY item.checkpoint_run_id DESC, item.relative_path, item.source_line_number, item.source_key
        """,
        (CHECKPOINT_STATUS, PROMOTION_STATUS, AGENT_KEY, state_run_id),
    ).fetchall()
    return dict(raw), [dict(row) for row in rows]


def evaluate(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    confirmed = row.get("confirmed_text")
    output = row.get("output_text")
    issue_codes = parse_json_list(row.get("issue_codes_json"))
    confirmed_hash = stable_hash(confirmed)

    if row.get("checkpoint_status") != CHECKPOINT_STATUS:
        reasons.append("checkpoint_status_not_ready")
    if row.get("promotion_status") != PROMOTION_STATUS:
        reasons.append("promotion_status_not_guarded_candidate")
    if int(row.get("checkpoint_allowed") or 0) != 1:
        reasons.append("checkpoint_item_not_allowed")
    if str(row.get("checkpoint_block_reason") or "").strip():
        reasons.append("checkpoint_item_blocked")
    if row.get("normalized_decision") != ALLOWED_NORMALIZED_DECISION:
        reasons.append("decision_not_safe_short_label")
    if row.get("evidence_label") != ALLOWED_EVIDENCE_LABEL:
        reasons.append("evidence_not_positive")
    if row.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_agent_key")
    if int(row.get("is_active") or 0) != 1:
        reasons.append("source_not_active")
    if row.get("current_final_state") in {"closed_human_locked", "closed_human_confirmed"}:
        reasons.append("human_closed_state")
    if row.get("current_review_state") != "auto_confirmed":
        reasons.append("review_state_not_auto_confirmed")
    if int(row.get("current_locked") or 0) == 1 or int(row.get("confirmation_locked") or 0) == 1:
        reasons.append("locked_state_or_confirmation")
    if not confirmed:
        reasons.append("missing_confirmed_text")
    if not output:
        reasons.append("missing_output_text")
    if canonical_text(confirmed) != canonical_text(output):
        reasons.append("confirmed_output_canonical_mismatch")
    if protected_tokens(confirmed) != protected_tokens(output):
        reasons.append("token_mismatch")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_signal")
    if issue_codes:
        reasons.append("checkpoint_issue_codes_present")
    text_for_surface = f"{confirmed or ''}\n{output or ''}"
    if any(marker in text_for_surface for marker in BLOCKED_SURFACE_MARKERS):
        reasons.append("blocked_surface_marker")
    if row.get("release_confirmed_text_hash"):
        if confirmed_hash != row.get("release_confirmed_text_hash"):
            reasons.append("release_hash_mismatch")
    else:
        reasons.append("hash_field_missing_canonical_fallback")
    if row.get("checkpoint_current_confirmed_text_hash"):
        if confirmed_hash != row.get("checkpoint_current_confirmed_text_hash"):
            reasons.append("checkpoint_hash_mismatch")
    else:
        reasons.append("hash_field_missing_canonical_fallback")
    if any(reason.endswith("_mismatch") for reason in reasons):
        return "blocked", reasons
    blocking_reasons = [reason for reason in reasons if reason != "hash_field_missing_canonical_fallback"]
    if blocking_reasons:
        return "blocked", reasons
    return "eligible", reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    state_run_id: int,
    raw: dict[str, int],
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "bridge_status",
        "block_reasons",
        "checkpoint_run_id",
        "checkpoint_item_id",
        "release_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "normalized_decision",
        "evidence_label",
        "current_final_state",
        "current_review_state",
        "current_apply_state",
        "issue_count",
        "high_issue_count",
        "release_confirmed_text_hash",
        "current_confirmed_text_hash",
        "output_text_hash",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["bridge_status"] for row in rows)
    by_checkpoint = Counter(row["checkpoint_run_id"] for row in rows if row["bridge_status"] == "eligible")
    by_bucket = Counter(row["queue_bucket"] for row in rows if row["bridge_status"] == "eligible")
    blockers = Counter()
    for row in rows:
        if row["bridge_status"] == "eligible":
            continue
        reasons = str(row.get("block_reasons") or "unknown").split(";")
        for reason in reasons:
            blockers[reason or "unknown"] += 1

    eligible = [row for row in rows if row["bridge_status"] == "eligible"]
    blocked = [row for row in rows if row["bridge_status"] != "eligible"]
    lines = [
        "Short-label guarded lifecycle bridge dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Lifecycle action candidate: {LIFECYCLE_ACTION}",
        f"Final state candidate: {FINAL_STATE}",
        f"Latest segment-state run id: {state_run_id}",
        "",
        "Summary:",
        f"- checkpoint_runs_considered: {raw.get('checkpoint_runs', 0):,}",
        f"- raw_checkpoint_candidates: {raw.get('raw_candidates', 0):,}",
        f"- raw_distinct_segments: {raw.get('distinct_segments', 0):,}",
        f"- deduped_candidates: {len(rows):,}",
        f"- eligible: {counts['eligible']:,}",
        f"- blocked: {len(rows) - counts['eligible']:,}",
        f"- estimated_closed_gain: {sum(1 for row in eligible if not int(row.get('current_is_closed') or 0)):,}",
        "",
        "Eligible by checkpoint run:",
        *[f"- {key}: {value:,}" for key, value in by_checkpoint.most_common(20)],
        "",
        "Eligible by bucket:",
        *[f"- {key}: {value:,}" for key, value in by_bucket.most_common(20)],
        "",
        "Blocked by reason:",
        *[f"- {key}: {value:,}" for key, value in blockers.most_common(30)],
        "",
        "Eligible sample:",
    ]
    if eligible:
        for row in eligible[:30]:
            lines.extend(
                [
                    f"- segment={row['segment_id']} | checkpoint_run={row['checkpoint_run_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                    f"  state={row.get('current_final_state')}; bucket={row.get('queue_bucket')}; text={short(row.get('confirmed_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['block_reasons']} | segment={row['segment_id']} | checkpoint_run={row['checkpoint_run_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                    f"  state={row.get('current_final_state')}; confirmed={short(row.get('confirmed_text'))}; output={short(row.get('output_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Dry-run only: no output writes, no source writes, no confirmation changes and no segment-state run.",
            "- This report estimates what a guarded lifecycle bridge could close after explicit integration.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_bridge_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS short_label_guarded_lifecycle_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            state_run_id INTEGER NOT NULL,
            checkpoint_runs_considered INTEGER NOT NULL DEFAULT 0,
            raw_candidates INTEGER NOT NULL DEFAULT 0,
            raw_distinct_segments INTEGER NOT NULL DEFAULT 0,
            deduped_candidates INTEGER NOT NULL DEFAULT 0,
            eligible_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS short_label_guarded_lifecycle_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            release_run_id INTEGER NOT NULL,
            release_item_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT,
            normalized_decision TEXT,
            evidence_label TEXT,
            bridge_status TEXT NOT NULL,
            block_reasons TEXT,
            current_final_state TEXT,
            current_review_state TEXT,
            current_apply_state TEXT,
            current_is_closed INTEGER NOT NULL DEFAULT 0,
            current_confirmed_text_hash TEXT,
            output_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES short_label_guarded_lifecycle_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_guarded_lifecycle_bridge_runs_latest
        ON short_label_guarded_lifecycle_bridge_runs(finished_at, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_guarded_lifecycle_bridge_items_run_status_segment
        ON short_label_guarded_lifecycle_bridge_items(run_id, bridge_status, segment_id)
        """
    )


def write_bridge_snapshot(
    conn,
    *,
    state_run_id: int,
    raw: dict[str, int],
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: str,
    finished_at: str,
) -> int:
    ensure_bridge_tables(conn)
    eligible_count = sum(1 for row in rows if row["bridge_status"] == "eligible")
    estimated_closed_gain = sum(
        1 for row in rows if row["bridge_status"] == "eligible" and not int(row.get("current_is_closed") or 0)
    )
    cursor = conn.execute(
        """
        INSERT INTO short_label_guarded_lifecycle_bridge_runs (
            rule_version,
            state_run_id,
            checkpoint_runs_considered,
            raw_candidates,
            raw_distinct_segments,
            deduped_candidates,
            eligible_count,
            blocked_count,
            estimated_closed_gain,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            state_run_id,
            int(raw.get("checkpoint_runs") or 0),
            int(raw.get("raw_candidates") or 0),
            int(raw.get("distinct_segments") or 0),
            len(rows),
            eligible_count,
            len(rows) - eligible_count,
            estimated_closed_gain,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at,
            finished_at,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in rows:
        conn.execute(
            """
            INSERT INTO short_label_guarded_lifecycle_bridge_items (
                run_id,
                segment_id,
                checkpoint_run_id,
                checkpoint_item_id,
                release_run_id,
                release_item_id,
                relative_path,
                source_key,
                source_line_number,
                queue_bucket,
                normalized_decision,
                evidence_label,
                bridge_status,
                block_reasons,
                current_final_state,
                current_review_state,
                current_apply_state,
                current_is_closed,
                current_confirmed_text_hash,
                output_text_hash,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["segment_id"],
                row["checkpoint_run_id"],
                row["checkpoint_item_id"],
                row["release_run_id"],
                row["release_item_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row.get("queue_bucket"),
                row.get("normalized_decision"),
                row.get("evidence_label"),
                row["bridge_status"],
                row.get("block_reasons"),
                row.get("current_final_state"),
                row.get("current_review_state"),
                row.get("current_apply_state"),
                int(row.get("current_is_closed") or 0),
                row.get("current_confirmed_text_hash"),
                row.get("output_text_hash"),
                finished_at,
            ),
        )
    return run_id


def lifecycle_policy_allows_bridge(row: dict[str, Any]) -> bool:
    return (
        row["bridge_status"] == "eligible"
        and row.get("current_final_state") == "reopen_auto_confirmed_autofix"
        and row.get("current_review_state") == "auto_confirmed"
        and row.get("current_apply_state") == "needs_review"
        and not int(row.get("current_is_closed") or 0)
    )


def lifecycle_policy_block_reason(row: dict[str, Any]) -> str:
    if row["bridge_status"] != "eligible":
        return row.get("block_reasons") or "bridge_status_blocked"
    if int(row.get("current_is_closed") or 0):
        return "already_closed_not_integrated"
    if row.get("current_final_state") != "reopen_auto_confirmed_autofix":
        return "current_state_not_reopen_auto_confirmed_autofix"
    if row.get("current_review_state") != "auto_confirmed":
        return "current_review_state_not_auto_confirmed"
    if row.get("current_apply_state") != "needs_review":
        return "current_apply_state_not_needs_review"
    return "policy_release_blocked"


def write_lifecycle_policy(
    conn,
    *,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: str,
    finished_at: str,
) -> int:
    released_count = sum(1 for row in rows if lifecycle_policy_allows_bridge(row))
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version,
            queue_run_id,
            audit_run_id,
            policy_name,
            label_family,
            policy_status,
            candidate_count,
            released_count,
            blocked_count,
            manual_boundary_count,
            invalid_count,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            LIFECYCLE_POLICY_NAME,
            LIFECYCLE_LABEL_FAMILY,
            len(rows),
            released_count,
            len(rows) - released_count,
            len(rows) - released_count,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at,
            finished_at,
            finished_at,
        ),
    )
    policy_run_id = int(cursor.lastrowid)
    for row in rows:
        allowed = lifecycle_policy_allows_bridge(row)
        block_reason = "" if allowed else lifecycle_policy_block_reason(row)
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                run_id,
                queue_run_id,
                queue_item_id,
                audit_run_id,
                audit_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                label_family,
                confirmation_label,
                policy_action,
                policy_allowed,
                block_reason,
                output_match_kind,
                token_status,
                issue_count,
                high_issue_count,
                model_safe_probability,
                review_priority,
                reasons_json,
                created_at
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, 0.0, ?, ?)
            """,
            (
                policy_run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                LIFECYCLE_LABEL_FAMILY,
                LIFECYCLE_ACTION,
                int(allowed),
                block_reason,
                "dry_run_hash_guarded",
                row.get("token_status") or "ok",
                int(row.get("issue_count") or 0),
                int(row.get("high_issue_count") or 0),
                json.dumps(
                    {
                        "bridge_status": row.get("bridge_status"),
                        "block_reasons": row.get("block_reasons"),
                        "checkpoint_run_id": row.get("checkpoint_run_id"),
                        "checkpoint_item_id": row.get("checkpoint_item_id"),
                        "current_confirmed_text_hash": row.get("current_confirmed_text_hash"),
                        "current_apply_state": row.get("current_apply_state"),
                        "current_final_state": row.get("current_final_state"),
                        "output_text_hash": row.get("output_text_hash"),
                        "policy_release_block_reason": block_reason,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                finished_at,
            ),
        )
    return policy_run_id


def main(*, state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        raw, fetched_rows = fetch_rows(conn, state_run_id=selected_state_run_id)
    rows: list[dict[str, Any]] = []
    for row in fetched_rows:
        status, reasons = evaluate(row)
        rows.append(
            {
                **row,
                "bridge_status": status,
                "block_reasons": ";".join(reasons),
                "current_confirmed_text_hash": stable_hash(row.get("confirmed_text")),
                "output_text_hash": stable_hash(row.get("output_text")),
            }
        )
    txt_path, csv_path, jsonl_path = report_paths(settings)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        state_run_id=selected_state_run_id,
        raw=raw,
        rows=rows,
    )
    finished_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        bridge_run_id = write_bridge_snapshot(
            conn,
            state_run_id=selected_state_run_id,
            raw=raw,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
            finished_at=finished_at,
        )
        policy_run_id = write_lifecycle_policy(
            conn,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
            finished_at=finished_at,
        )
        conn.commit()
    counts = Counter(row["bridge_status"] for row in rows)
    print("[short_label_guarded_lifecycle_bridge_dry_run] Dry-run generated")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Bridge run id: {bridge_run_id}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Lifecycle policy run id: {policy_run_id}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] State run id: {selected_state_run_id}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Checkpoint runs considered: {raw.get('checkpoint_runs', 0)}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Raw candidates: {raw.get('raw_candidates', 0)}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Deduped candidates: {len(rows)}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Eligible: {counts['eligible']}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Blocked: {len(rows) - counts['eligible']}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] Report: {txt_path}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] CSV: {csv_path}")
    print(f"[short_label_guarded_lifecycle_bridge_dry_run] JSONL: {jsonl_path}")
    return {
        "state_run_id": selected_state_run_id,
        "bridge_run_id": bridge_run_id,
        "policy_run_id": policy_run_id,
        "checkpoint_runs_considered": raw.get("checkpoint_runs", 0),
        "raw_candidates": raw.get("raw_candidates", 0),
        "deduped_candidates": len(rows),
        "eligible": counts["eligible"],
        "blocked": len(rows) - counts["eligible"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run the governed short-label lifecycle bridge.")
    parser.add_argument("--state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(state_run_id=args.state_run_id)
