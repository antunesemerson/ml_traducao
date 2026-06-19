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


RULE_VERSION = "issue_review_short_label_positive_release_v1"
POLICY_NAME = "short_label_positive_release"
POLICY_ACTION = "short_label_positive_release_shadow"
AGENT_KEY = "micro_short_label_style"
ALLOWED_DECISION_PAIRS = {
    ("safe_short_label", "positive_evidence"),
    ("false_positive_reopen", "false_positive_reopen"),
}
ALLOWED_TOKEN_IMPACTS = {"none_or_unknown", "usually_same_tokens", "same_tokens"}
ALLOWED_TOKEN_STATUSES = {"", "ok", "none", "unknown"}
ALLOWED_FINAL_STATES = {"reopen_auto_confirmed", "reopen_auto_confirmed_autofix"}
SURFACE_BLOCKERS = (" :", " ,", "«", "»", "¿", "¡")


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_positive_release"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_decision_run_id(conn, *, agent_key: str, queue_run_id: int | None) -> int:
    params: list[Any] = [agent_key]
    queue_filter = ""
    if queue_run_id is not None:
        queue_filter = "AND queue_run_id = ?"
        params.append(queue_run_id)
    row = conn.execute(
        f"""
        SELECT id
        FROM ml_issue_review_decision_runs
        WHERE agent_key = ?
          AND finished_at IS NOT NULL
          AND accepted_count > 0
          {queue_filter}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed issue-review decision run found for {agent_key!r}.")
    return int(row["id"])


def fetch_decision_run(conn, *, decision_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_decision_runs
        WHERE id = ?
        """,
        (decision_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Issue-review decision run not found: {decision_run_id}")
    return dict(row)


def fetch_rows(
    conn,
    *,
    agent_key: str,
    decision_run_id: int,
    queue_run_id: int | None,
) -> list[dict[str, Any]]:
    params: list[Any] = [decision_run_id, agent_key]
    queue_filter = ""
    if queue_run_id is not None:
        queue_filter = "AND d.queue_run_id = ?"
        params.append(queue_run_id)
    rows = conn.execute(
        f"""
        SELECT
            d.id AS decision_id,
            d.run_id AS decision_run_id,
            d.queue_run_id,
            d.queue_item_id,
            d.ledger_run_id,
            d.ledger_item_id,
            d.segment_id,
            d.relative_path,
            d.source_key,
            d.source_line_number,
            d.agent_key,
            d.issue_family,
            d.issue_kind,
            d.queue_bucket,
            d.normalized_decision,
            d.evidence_label,
            d.corrected_text AS decision_corrected_text,
            d.notes AS decision_notes,
            d.valid AS decision_valid,
            d.validation_status AS decision_validation_status,
            q.review_status AS queue_review_status,
            q.reviewer_decision AS queue_reviewer_decision,
            q.corrected_text AS queue_corrected_text,
            q.confirmed_text AS queue_confirmed_text,
            q.evidence_json AS queue_evidence_json,
            l.issue_family AS ledger_issue_family,
            l.issue_kind AS ledger_issue_kind,
            l.status AS ledger_status,
            l.route_status AS ledger_route_status,
            l.token_impact,
            l.token_status AS ledger_token_status,
            l.evidence_json AS ledger_evidence_json,
            s.id AS state_item_id,
            s.run_id AS state_run_id,
            s.final_state,
            s.state_group,
            s.output_state,
            s.review_state,
            s.apply_state,
            s.is_closed,
            s.needs_human,
            s.needs_output_apply,
            s.locked AS state_locked,
            c.id AS confirmation_id,
            c.confirmation_level,
            c.confirmation_label,
            c.confirmed_text AS current_confirmed_text,
            c.locked AS confirmation_locked,
            source.english_text,
            source.spanish_text,
            output.portuguese_text
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        JOIN ml_issue_ledger_items l ON l.id = d.ledger_item_id
        JOIN source_segments source ON source.id = d.segment_id
        LEFT JOIN output_segments output ON output.segment_id = d.segment_id
        LEFT JOIN segment_state_items s
          ON s.id = (
              SELECT s2.id
              FROM segment_state_items s2
              WHERE s2.segment_id = d.segment_id
              ORDER BY s2.run_id DESC, s2.id DESC
              LIMIT 1
          )
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = d.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE d.run_id = ?
          AND d.agent_key = ?
          AND d.valid = 1
          AND d.normalized_decision IN ('safe_short_label', 'false_positive_reopen')
          {queue_filter}
        ORDER BY d.relative_path, d.source_line_number, d.source_key, d.id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(*, policy_status: str, decision_run: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if policy_status != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if decision_run.get("agent_key") != AGENT_KEY:
        reasons.append("decision_run_agent_mismatch")
    if int(decision_run.get("accepted_count") or 0) <= 0:
        reasons.append("decision_run_has_no_accepted_rows")
    if not rows:
        reasons.append("no_candidate_rows")
    return reasons


def evidence_numbers(row: dict[str, Any]) -> tuple[int, int, int]:
    queue_evidence = parse_json(row.get("queue_evidence_json"))
    ledger_evidence = parse_json(row.get("ledger_evidence_json"))
    text_length = as_int(queue_evidence.get("text_length"), as_int(ledger_evidence.get("text_length")))
    token_count = as_int(queue_evidence.get("token_count"), as_int(ledger_evidence.get("token_count")))
    word_count = as_int(queue_evidence.get("word_count"), as_int(ledger_evidence.get("word_count")))
    if not text_length:
        text_length = len(row.get("current_confirmed_text") or row.get("queue_confirmed_text") or "")
    return text_length, token_count, word_count


def evidence_issue_codes(row: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for payload in (parse_json(row.get("queue_evidence_json")), parse_json(row.get("ledger_evidence_json"))):
        raw = payload.get("issue_codes")
        if isinstance(raw, list):
            codes.extend(str(item) for item in raw if str(item).strip())
    return sorted(set(codes))


def evaluate_row(
    row: dict[str, Any],
    *,
    global_reasons: list[str],
    max_text_length: int,
    max_token_count: int,
) -> tuple[int, str, dict[str, Any]]:
    queue_text = row.get("queue_confirmed_text") or ""
    current_text = row.get("current_confirmed_text") or ""
    text_length, token_count, word_count = evidence_numbers(row)
    issue_codes = evidence_issue_codes(row)
    token_status = str(row.get("ledger_token_status") or "").strip().lower()
    token_impact = str(row.get("token_impact") or "").strip().lower()
    reasons = {
        "queue_confirmed_text_hash": stable_hash(queue_text),
        "current_confirmed_text_hash": stable_hash(current_text),
        "text_length": text_length,
        "token_count": token_count,
        "word_count": word_count,
        "issue_codes": issue_codes,
    }

    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons), reasons
    if (row.get("normalized_decision"), row.get("evidence_label")) not in ALLOWED_DECISION_PAIRS:
        return 0, "decision_pair_not_allowed", reasons
    if row.get("queue_review_status") != "reviewed":
        return 0, "queue_item_not_reviewed", reasons
    if row.get("queue_reviewer_decision") != row.get("normalized_decision"):
        return 0, "queue_decision_mismatch", reasons
    if row.get("decision_validation_status") != "accepted":
        return 0, "decision_not_accepted", reasons
    if (row.get("decision_corrected_text") or "").strip() or (row.get("queue_corrected_text") or "").strip():
        return 0, "corrected_text_present", reasons
    if row.get("ledger_issue_family") != "short_label_style_microagent":
        return 0, "ledger_family_mismatch", reasons
    if row.get("ledger_issue_kind") != "short_or_compact_label_reopened":
        return 0, "ledger_kind_mismatch", reasons
    if token_impact not in ALLOWED_TOKEN_IMPACTS:
        return 0, "token_impact_not_allowed", reasons
    if token_status not in ALLOWED_TOKEN_STATUSES:
        return 0, "token_status_not_allowed", reasons
    if issue_codes:
        return 0, "surface_issue_codes_present", reasons
    if any(marker in queue_text for marker in SURFACE_BLOCKERS):
        return 0, "surface_marker_in_queue_text", reasons
    if any(marker in current_text for marker in SURFACE_BLOCKERS):
        return 0, "surface_marker_in_current_text", reasons
    if not current_text:
        return 0, "missing_current_confirmation", reasons
    if current_text != queue_text:
        return 0, "stale_confirmation_text_changed", reasons
    if row.get("final_state") not in ALLOWED_FINAL_STATES:
        return 0, "state_not_reopen_auto_confirmed", reasons
    if row.get("review_state") != "auto_confirmed":
        return 0, "review_state_not_auto_confirmed", reasons
    if int(row.get("state_locked") or 0) or int(row.get("confirmation_locked") or 0):
        return 0, "locked_confirmation", reasons
    if int(row.get("is_closed") or 0) == 1:
        return 0, "already_closed", reasons
    if text_length > max_text_length:
        return 0, "text_length_above_guard", reasons
    if token_count > max_token_count:
        return 0, "token_count_above_guard", reasons
    return 1, "", reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    policy_status: str,
    global_reasons: list[str],
) -> None:
    fieldnames = [
        "policy_item_id",
        "decision_id",
        "decision_run_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "state_run_id",
        "state_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "normalized_decision",
        "evidence_label",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "final_state",
        "review_state",
        "apply_state",
        "text_length",
        "token_count",
        "word_count",
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
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "issue_codes": row.get("issue_codes"),
                "reasons": row.get("reasons"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
    by_decision = Counter(row["normalized_decision"] for row in rows)
    by_bucket = Counter(row["queue_bucket"] for row in rows)
    estimated_gain = sum(1 for row in rows if row["policy_allowed"] and not int(row.get("is_closed") or 0))
    lines = [
        "Issue-review short-label positive release",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {policy_status}",
        f"Policy run id: {run_id}",
        f"Decision run id: {decision_run['id']}",
        f"Queue run id: {decision_run.get('queue_run_id')}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        f"- Estimated closed gain: {estimated_gain:,}",
        f"- By decision: {json.dumps(dict(by_decision), ensure_ascii=False, sort_keys=True)}",
        f"- By queue bucket: {json.dumps(dict(by_bucket), ensure_ascii=False, sort_keys=True)}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Shadow released sample:",
    ]
    released = [row for row in rows if row["policy_allowed"]]
    if released:
        for row in released[:30]:
            lines.extend(
                [
                    f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  decision={row['normalized_decision']}; bucket={row['queue_bucket']}; text={short(row.get('current_confirmed_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [row for row in rows if not row["policy_allowed"]]
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  decision={row['normalized_decision']}; state={row.get('final_state')}; text={short(row.get('current_confirmed_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow policy only: no source reads, no output writes, no confirmation updates, no segment-state closure.",
            "- It converts reviewed short-label evidence into a governed release candidate for the coordinator.",
            "- Production can only consume this after a separate checkpoint/allowlist step.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    decision_run_id: int | None = None,
    queue_run_id: int | None = None,
    agent_key: str = AGENT_KEY,
    policy_status: str = "shadow",
    max_text_length: int = 160,
    max_token_count: int = 12,
) -> dict[str, Any]:
    if policy_status != "shadow":
        raise ValueError("Short-label positive release is intentionally shadow-only for now.")
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(
            conn,
            agent_key=agent_key,
            queue_run_id=queue_run_id,
        )
        decision_run = fetch_decision_run(conn, decision_run_id=selected_decision_run_id)
        rows = fetch_rows(
            conn,
            agent_key=agent_key,
            decision_run_id=selected_decision_run_id,
            queue_run_id=queue_run_id,
        )
        global_reasons = global_block_reasons(
            policy_status=policy_status,
            decision_run=decision_run,
            rows=rows,
        )
        for row in rows:
            allowed, block_reason, reasons = evaluate_row(
                row,
                global_reasons=global_reasons,
                max_text_length=max_text_length,
                max_token_count=max_token_count,
            )
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
            row["text_length"] = reasons["text_length"]
            row["token_count"] = reasons["token_count"]
            row["word_count"] = reasons["word_count"]
            row["issue_codes"] = reasons["issue_codes"]
            row["reasons"] = reasons

        counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
        by_bucket = Counter(row["queue_bucket"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        run_cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_release_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                candidate_count,
                released_shadow_count,
                blocked_count,
                estimated_closed_gain,
                safe_short_label_count,
                false_positive_reopen_count,
                blocker_counts_json,
                bucket_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                policy_status,
                agent_key,
                selected_decision_run_id,
                decision_run.get("queue_run_id"),
                len(rows),
                counts["released_shadow"],
                len(rows) - counts["released_shadow"],
                sum(1 for row in rows if row["policy_allowed"] and not int(row.get("is_closed") or 0)),
                sum(1 for row in rows if row["normalized_decision"] == "safe_short_label"),
                sum(1 for row in rows if row["normalized_decision"] == "false_positive_reopen"),
                json.dumps(dict(counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_bucket), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        policy_run_id = int(run_cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_short_label_release_items (
                    run_id,
                    decision_id,
                    decision_run_id,
                    queue_run_id,
                    queue_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    state_run_id,
                    state_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_bucket,
                    normalized_decision,
                    evidence_label,
                    policy_action,
                    policy_allowed,
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
                    word_count,
                    issue_codes_json,
                    queue_confirmed_text_hash,
                    current_confirmed_text_hash,
                    evidence_json,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    row["decision_id"],
                    row["decision_run_id"],
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row.get("state_run_id"),
                    row.get("state_item_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["queue_bucket"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    row.get("final_state"),
                    row.get("review_state"),
                    row.get("apply_state"),
                    int(row.get("is_closed") or 0),
                    int(row.get("needs_human") or 0),
                    int(row.get("state_locked") or 0),
                    row.get("token_impact"),
                    row.get("ledger_token_status"),
                    row["text_length"],
                    row["token_count"],
                    row["word_count"],
                    json.dumps(row["issue_codes"], ensure_ascii=False, sort_keys=True),
                    row["reasons"]["queue_confirmed_text_hash"],
                    row["reasons"]["current_confirmed_text_hash"],
                    row.get("queue_evidence_json") or row.get("ledger_evidence_json"),
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row["policy_item_id"] = int(item_cursor.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=policy_run_id,
            decision_run=decision_run,
            rows=rows,
            started_at=started_at,
            policy_status=policy_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_review_short_label_positive_release] Shadow release generated")
    print(f"[issue_review_short_label_positive_release] Run id: {policy_run_id}")
    print(f"[issue_review_short_label_positive_release] Decision run id: {selected_decision_run_id}")
    print(f"[issue_review_short_label_positive_release] Candidates: {len(rows):,}")
    print(f"[issue_review_short_label_positive_release] Released shadow: {counts['released_shadow']:,}")
    print(f"[issue_review_short_label_positive_release] Blocked: {len(rows) - counts['released_shadow']:,}")
    print(f"[issue_review_short_label_positive_release] Estimated closed gain: {sum(1 for row in rows if row['policy_allowed'] and not int(row.get('is_closed') or 0)):,}")
    print(f"[issue_review_short_label_positive_release] Report: {txt_path}")
    print(f"[issue_review_short_label_positive_release] CSV: {csv_path}")
    print(f"[issue_review_short_label_positive_release] JSONL: {jsonl_path}")
    return {
        "run_id": policy_run_id,
        "decision_run_id": selected_decision_run_id,
        "queue_run_id": decision_run.get("queue_run_id"),
        "candidates": len(rows),
        "released_shadow": counts["released_shadow"],
        "blocked": len(rows) - counts["released_shadow"],
        "estimated_closed_gain": sum(1 for row in rows if row["policy_allowed"] and not int(row.get("is_closed") or 0)),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a guarded shadow release for reviewed short-label positives.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default=AGENT_KEY)
    parser.add_argument("--status", choices=["shadow"], default="shadow")
    parser.add_argument("--max-text-length", type=int, default=160)
    parser.add_argument("--max-token-count", type=int, default=12)
    args = parser.parse_args()
    main(
        decision_run_id=args.decision_run_id,
        queue_run_id=args.queue_run_id,
        agent_key=args.agent_key,
        policy_status=args.status,
        max_text_length=args.max_text_length,
        max_token_count=args.max_token_count,
    )
