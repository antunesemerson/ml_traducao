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
from apply_segment_state_updates import short


RULE_VERSION = "issue_review_short_label_positive_checkpoint_v1"
POLICY_NAME = "short_label_positive_release"
CHECKPOINT_NAME = "short_label_positive_release_guarded_checkpoint_v1"
CHECKPOINT_ACTION = "guarded_lifecycle_candidate_short_label_positive"
AGENT_KEY = "micro_short_label_style"
RELEASE_ACTION = "short_label_positive_release_shadow"
ALLOWED_DECISION_PAIRS = {
    ("safe_short_label", "positive_evidence"),
    ("false_positive_reopen", "false_positive_reopen"),
}
ALLOWED_FINAL_STATES = {"reopen_auto_confirmed", "reopen_auto_confirmed_autofix"}
ALLOWED_TOKEN_STATUSES = {"", "ok", "none", "unknown"}
ALLOWED_TOKEN_IMPACTS = {"none_or_unknown", "usually_same_tokens", "same_tokens"}


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


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


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_positive_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_release_run_id(conn, *, agent_key: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_release_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND agent_key = ?
          AND released_shadow_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (POLICY_NAME, agent_key),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed short-label release run found for {agent_key!r}.")
    return int(row["id"])


def fetch_release_run(conn, *, release_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_release_runs
        WHERE id = ?
        """,
        (release_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Short-label release run not found: {release_run_id}")
    return dict(row)


def fetch_rows(conn, *, release_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            latest_state.id AS current_state_item_id,
            latest_state.run_id AS current_state_run_id,
            latest_state.final_state AS current_final_state,
            latest_state.review_state AS current_review_state,
            latest_state.apply_state AS current_apply_state,
            latest_state.is_closed AS current_is_closed,
            latest_state.needs_human AS current_needs_human,
            latest_state.locked AS current_locked,
            confirmation.id AS current_confirmation_id,
            confirmation.confirmed_text AS current_confirmed_text,
            confirmation.locked AS current_confirmation_locked
        FROM ml_issue_short_label_release_items item
        LEFT JOIN segment_state_items latest_state
          ON latest_state.id = (
              SELECT s2.id
              FROM segment_state_items s2
              WHERE s2.segment_id = item.segment_id
              ORDER BY s2.run_id DESC, s2.id DESC
              LIMIT 1
          )
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = item.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (release_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(
    release_run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    min_released_required: int,
    max_blocked_allowed: int,
) -> list[str]:
    reasons: list[str] = []
    if release_run.get("policy_name") != POLICY_NAME:
        reasons.append("wrong_policy_name")
    if release_run.get("policy_status") != "shadow":
        reasons.append("release_run_not_shadow")
    if release_run.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_agent_key")
    if int(release_run.get("candidate_count") or 0) != len(rows):
        reasons.append("release_item_count_mismatch")
    if int(release_run.get("released_shadow_count") or 0) < min_released_required:
        reasons.append("min_released_not_met")
    if int(release_run.get("blocked_count") or 0) > max_blocked_allowed:
        reasons.append("too_many_release_blocks")
    if int(release_run.get("blocked_count") or 0) != 0:
        reasons.append("release_has_blocked_rows")
    if not rows:
        reasons.append("no_release_items")
    return reasons


def row_block_reason(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current_hash = stable_hash(row.get("current_confirmed_text"))
    issue_codes = parse_json_list(row.get("issue_codes_json"))
    reasons = {
        "release_item_id": int(row["id"]),
        "release_confirmed_text_hash": row.get("current_confirmed_text_hash") or "",
        "current_confirmed_text_hash": current_hash,
        "issue_codes": issue_codes,
        "release_state_item_id": row.get("state_item_id"),
        "current_state_item_id": row.get("current_state_item_id"),
    }
    if int(row.get("policy_allowed") or 0) != 1:
        return row.get("block_reason") or "release_item_not_allowed", reasons
    if row.get("block_reason"):
        return "release_item_has_block_reason", reasons
    if row.get("policy_action") != RELEASE_ACTION:
        return "wrong_release_action", reasons
    if (row.get("normalized_decision"), row.get("evidence_label")) not in ALLOWED_DECISION_PAIRS:
        return "decision_pair_not_allowed", reasons
    if row.get("token_status") not in ALLOWED_TOKEN_STATUSES:
        return "token_status_not_allowed", reasons
    if row.get("token_impact") not in ALLOWED_TOKEN_IMPACTS:
        return "token_impact_not_allowed", reasons
    if issue_codes:
        return "issue_codes_present", reasons
    if not row.get("current_confirmation_id"):
        return "missing_current_confirmation", reasons
    if int(row.get("current_confirmation_locked") or 0):
        return "current_confirmation_locked", reasons
    if current_hash != (row.get("current_confirmed_text_hash") or ""):
        return "stale_confirmation_hash_changed", reasons
    if not row.get("current_state_item_id"):
        return "missing_current_segment_state", reasons
    if row.get("current_final_state") not in ALLOWED_FINAL_STATES:
        return "current_state_not_reopen_auto_confirmed", reasons
    if row.get("current_review_state") != "auto_confirmed":
        return "current_review_state_not_auto_confirmed", reasons
    if int(row.get("current_locked") or 0):
        return "current_state_locked", reasons
    if int(row.get("current_is_closed") or 0):
        return "already_closed", reasons
    return "", reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    release_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    checkpoint_status: str,
    promotion_status: str,
    global_reasons: list[str],
) -> None:
    fieldnames = [
        "checkpoint_item_id",
        "release_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "normalized_decision",
        "evidence_label",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "current_final_state",
        "current_review_state",
        "current_apply_state",
        "text_length",
        "token_count",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "confirmed_preview": short(row.get("current_confirmed_text")),
                "reasons": row.get("checkpoint_reasons") or {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    by_bucket = Counter(row["queue_bucket"] for row in rows if row["checkpoint_allowed"])
    by_decision = Counter(row["normalized_decision"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Issue-review short-label positive checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        "Production release allowed: 0",
        f"Release run id: {release_run['id']}",
        f"Policy name: {release_run['policy_name']}",
        f"Agent: {release_run['agent_key']}",
        f"Decision run id: {release_run['decision_run_id']}",
        f"Queue run id: {release_run['queue_run_id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Governance gates:",
        f"- Release candidates: {int(release_run['candidate_count'] or 0):,}",
        f"- Release shadow allowed: {int(release_run['released_shadow_count'] or 0):,}",
        f"- Release blocked: {int(release_run['blocked_count'] or 0):,}",
        f"- Estimated release gain: {int(release_run['estimated_closed_gain'] or 0):,}",
        f"- Checkpoint allowed: {counts['checkpoint_allowed']:,}",
        f"- Checkpoint blocked: {len(rows) - counts['checkpoint_allowed']:,}",
        "",
        "Checkpoint items:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Allowed by decision:",
        *[f"- {key}: {value:,}" for key, value in by_decision.most_common()],
        "",
        "Allowed by bucket:",
        *[f"- {key}: {value:,}" for key, value in by_bucket.most_common()],
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Allowed sample:",
    ]
    allowed = [row for row in rows if row["checkpoint_allowed"]]
    if allowed:
        for row in allowed[:25]:
            lines.extend(
                [
                    f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  decision={row['normalized_decision']}; bucket={row['queue_bucket']}; text={short(row.get('current_confirmed_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [row for row in rows if not row["checkpoint_allowed"]]
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  state={row.get('current_final_state')}; text={short(row.get('current_confirmed_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no output writes, no confirmation updates and no segment-state closure.",
            "- It only allowlists rows that remained unchanged since the shadow release.",
            "- Production consumption still requires a separate lifecycle/production-side step.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    release_run_id: int | None = None,
    min_released_required: int = 1,
    max_blocked_allowed: int = 0,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_release_run_id = release_run_id or latest_release_run_id(conn, agent_key=AGENT_KEY)
        release_run = fetch_release_run(conn, release_run_id=selected_release_run_id)
        rows = fetch_rows(conn, release_run_id=selected_release_run_id)
        global_reasons = global_block_reasons(
            release_run,
            rows,
            min_released_required=min_released_required,
            max_blocked_allowed=max_blocked_allowed,
        )
        for row in rows:
            block_reason, reasons = row_block_reason(row)
            if global_reasons and not block_reason:
                block_reason = "global_gate:" + ",".join(global_reasons)
            row["checkpoint_action"] = CHECKPOINT_ACTION
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            row["checkpoint_reasons"] = reasons

        allowed_count = sum(int(row["checkpoint_allowed"]) for row in rows)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = (
            "ready_for_guarded_lifecycle_policy"
            if allowed_count == len(rows) and allowed_count > 0 and not global_reasons
            else "blocked_by_checkpoint_guard"
        )
        promotion_status = "guarded_candidate" if checkpoint_status == "ready_for_guarded_lifecycle_policy" else "blocked"
        blocker_counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
        bucket_counts = Counter(row["queue_bucket"] for row in rows if row["checkpoint_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_release_checkpoint_runs (
                rule_version,
                release_run_id,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                min_released_required,
                max_blocked_allowed,
                total_candidates,
                release_allowed_count,
                release_blocked_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                estimated_closed_gain,
                safe_short_label_count,
                false_positive_reopen_count,
                promotion_status,
                production_release_allowed,
                blocker_counts_json,
                bucket_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_release_run_id,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                "shadow",
                AGENT_KEY,
                release_run.get("decision_run_id"),
                release_run.get("queue_run_id"),
                min_released_required,
                max_blocked_allowed,
                len(rows),
                int(release_run.get("released_shadow_count") or 0),
                int(release_run.get("blocked_count") or 0),
                allowed_count,
                blocked_count,
                sum(1 for row in rows if row["checkpoint_allowed"] and not int(row.get("current_is_closed") or 0)),
                int(release_run.get("safe_short_label_count") or 0),
                int(release_run.get("false_positive_reopen_count") or 0),
                promotion_status,
                0,
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_short_label_release_checkpoint_items (
                    checkpoint_run_id,
                    release_run_id,
                    release_item_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    state_run_id,
                    state_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    queue_bucket,
                    normalized_decision,
                    evidence_label,
                    release_policy_action,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    final_state,
                    review_state,
                    apply_state,
                    is_closed,
                    needs_human,
                    locked,
                    token_impact,
                    token_status,
                    text_length,
                    token_count,
                    issue_codes_json,
                    release_confirmed_text_hash,
                    current_confirmed_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_release_run_id,
                    row["id"],
                    row["decision_id"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row.get("current_state_run_id"),
                    row.get("current_state_item_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    AGENT_KEY,
                    row["queue_bucket"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["policy_action"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row.get("current_final_state"),
                    row.get("current_review_state"),
                    row.get("current_apply_state"),
                    int(row.get("current_is_closed") or 0),
                    int(row.get("current_needs_human") or 0),
                    int(row.get("current_locked") or 0),
                    row.get("token_impact"),
                    row.get("token_status"),
                    int(row.get("text_length") or 0),
                    int(row.get("token_count") or 0),
                    row.get("issue_codes_json"),
                    row.get("current_confirmed_text_hash"),
                    row["checkpoint_reasons"]["current_confirmed_text_hash"],
                    json.dumps(row["checkpoint_reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cursor.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            release_run=release_run,
            rows=rows,
            started_at=started_at,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_review_short_label_positive_checkpoint] Checkpoint generated")
    print(f"[issue_review_short_label_positive_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_review_short_label_positive_checkpoint] Release run id: {selected_release_run_id}")
    print(f"[issue_review_short_label_positive_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_review_short_label_positive_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_review_short_label_positive_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_review_short_label_positive_checkpoint] Report: {txt_path}")
    print(f"[issue_review_short_label_positive_checkpoint] CSV: {csv_path}")
    print(f"[issue_review_short_label_positive_checkpoint] JSONL: {jsonl_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "release_run_id": selected_release_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a governed checkpoint for short-label positive release rows.")
    parser.add_argument("--release-run-id", type=int, default=None)
    parser.add_argument("--min-released", type=int, default=1)
    parser.add_argument("--max-blocked", type=int, default=0)
    args = parser.parse_args()
    main(
        release_run_id=args.release_run_id,
        min_released_required=args.min_released,
        max_blocked_allowed=args.max_blocked,
    )
