from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "general_pipe_article_gender_lifecycle_policy_materializer_v1"
POLICY_NAME = "human_confirmed_post_apply_repair_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_post_apply_repair_lifecycle"
EXPECTED_FINAL_STATE = "closed_auto_confirmed_human_confirmed_post_apply_repair_lifecycle"
LABEL_FAMILY = "human_confirmed_post_apply_repair"
OUTPUT_MATCH_KIND = "output_confirmation_candidate_exact_match"
TARGET_SEGMENT_IDS = (20762, 60278, 230843)
DEFAULT_SEGMENT_STATE_RUN_ID = 408
ALLOWED_CONFIRMATION_SOURCES = {
    "codex_release_promotion_review",
    "codex_release_readiness_human_review",
    "local_learning",
    "post_apply_human_correction",
}
ALLOWED_CONFIRMATION_LABELS = {
    "correct",
    "semantic_error",
    "minor_fix",
    "residual_spanish",
    "structure_error",
    "major_fix",
    "release_promotion_final8_corrected",
}
ALLOWED_CONFIRMATION_LABEL_PREFIXES = (
    "narrative_plain_light_batch",
    "ui_tooltips_packet",
)
ALLOWED_CONFIRMATION_LABEL_SUFFIXES = (
    "_corrected",
)


def confirmation_label_allowed(value: Any) -> bool:
    label = str(value or "")
    if label in ALLOWED_CONFIRMATION_LABELS:
        return True
    return any(
        label.startswith(prefix) and label.endswith(suffix)
        for prefix in ALLOWED_CONFIRMATION_LABEL_PREFIXES
        for suffix in ALLOWED_CONFIRMATION_LABEL_SUFFIXES
    )


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths(mode: str) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_general_pipe_article_gender_lifecycle_policy_materializer_{mode}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
        base.with_suffix(".csv"),
    )


def readonly_conn() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def all_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def latest_finished_ledger_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL").fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing finished issue ledger run")
    return int(row["id"])


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(Counter(protected_tokens(value or "")).items()))


def canonical_output_text(value: str | None) -> str:
    """Compare DB/file-localized text without treating YAML quote escaping as content."""

    return str(value or "").replace("\r\n", "\n").replace('\\"', '"')


def evaluate_segment(conn: sqlite3.Connection, segment_id: int, state_run_id: int, ledger_run_id: int) -> dict[str, Any]:
    state = one(
        conn,
        """
        SELECT *
        FROM segment_state_items
        WHERE run_id = ? AND segment_id = ?
        """,
        (state_run_id, segment_id),
    )
    soc = one(
        conn,
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            o.portuguese_text AS output_text,
            sc.id AS confirmation_id,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked,
            sc.candidate_id,
            ll.run_id AS learning_run_id,
            ll.origin,
            ll.match_type
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN local_learning_candidates ll ON ll.id = sc.candidate_id
        WHERE s.id = ?
        ORDER BY sc.updated_at DESC, sc.id DESC
        LIMIT 1
        """,
        (segment_id,),
    )
    open_issues = all_rows(
        conn,
        """
        SELECT issue_family, issue_kind, issue_severity, agent_key, status,
               route_status, proposed_action, validation_status
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND status = 'open'
        ORDER BY issue_family, issue_kind, issue_severity
        """,
        (ledger_run_id, segment_id),
    )
    existing_items = all_rows(
        conn,
        """
        SELECT item.*, run.policy_name, run.policy_status
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
        WHERE item.segment_id = ?
          AND run.policy_name = ?
          AND run.policy_status = 'active'
          AND item.policy_action = ?
        ORDER BY item.run_id DESC, item.id DESC
        """,
        (segment_id, POLICY_NAME, POLICY_ACTION),
    )
    state = state or {}
    soc = soc or {}
    output_text = str(soc.get("output_text") or "")
    confirmed_text = str(soc.get("confirmed_text") or "")
    output_tokens = token_counts(output_text)
    confirmation_tokens = token_counts(confirmed_text)
    guards = {
        "state_present": bool(state),
        "current_state_pending_reopen": bool(
            state
            and state.get("state_group") == "pending"
            and state.get("final_state") == "reopen_auto_confirmed_autofix"
        ),
        "needs_output_apply_zero": bool(state and int(state.get("needs_output_apply") or 0) == 0),
        "confirmed_matches_output": bool(state and int(state.get("confirmed_matches_output") or 0) == 1),
        "confirmation_present": bool(soc),
        "confirmation_human_confirmed": soc.get("confirmation_level") == "human_confirmed",
        "confirmation_source_allowed": soc.get("confirmation_source") in ALLOWED_CONFIRMATION_SOURCES,
        "confirmation_label_allowed": confirmation_label_allowed(soc.get("confirmation_label")),
        "confirmation_locked": int(soc.get("locked") or 0) == 1,
        "output_equals_confirmation": bool(
            output_text
            and canonical_output_text(output_text) == canonical_output_text(confirmed_text)
        ),
        "token_integrity": output_tokens == confirmation_tokens,
        "latest_open_issue_count_zero": len(open_issues) == 0,
        "no_existing_active_policy_item": len(existing_items) == 0,
    }
    block_reasons = [key for key, value in guards.items() if not value]
    allowed = not block_reasons
    return {
        "segment_id": segment_id,
        "relative_path": soc.get("relative_path") or state.get("relative_path"),
        "source_key": soc.get("source_key") or state.get("source_key"),
        "source_line_number": soc.get("source_line_number") or state.get("source_line_number"),
        "candidate_id": soc.get("candidate_id"),
        "confirmation_id": soc.get("confirmation_id"),
        "confirmation_label": soc.get("confirmation_label"),
        "confirmation_source": soc.get("confirmation_source"),
        "confirmation_level": soc.get("confirmation_level"),
        "confirmation_locked": int(soc.get("locked") or 0),
        "learning_run_id": soc.get("learning_run_id"),
        "origin": soc.get("origin"),
        "match_type": soc.get("match_type"),
        "state_group": state.get("state_group"),
        "final_state": state.get("final_state"),
        "needs_output_apply": int(state.get("needs_output_apply") or 0) if state else None,
        "confirmed_matches_output": int(state.get("confirmed_matches_output") or 0) if state else None,
        "issue_count": len(open_issues),
        "high_issue_count": sum(
            1 for issue in open_issues if str(issue.get("issue_severity") or "").lower() in {"high", "error", "critical"}
        ),
        "output_text": output_text,
        "confirmed_text": confirmed_text,
        "token_counts_output": output_tokens,
        "token_counts_confirmation": confirmation_tokens,
        "existing_active_policy_item_count": len(existing_items),
        "guards": guards,
        "policy_action": POLICY_ACTION,
        "policy_allowed": int(allowed),
        "block_reason": ";".join(block_reasons),
        "expected_final_state_if_released": EXPECTED_FINAL_STATE if allowed else None,
    }


def collect_rows(conn: sqlite3.Connection, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    return [evaluate_segment(conn, segment_id, state_run_id, ledger_run_id) for segment_id in TARGET_SEGMENT_IDS]


def apply_policy_run(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    state_run_id: int,
    ledger_run_id: int,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    released = sum(1 for row in rows if row["policy_allowed"])
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version, queue_run_id, audit_run_id, policy_name, label_family,
            policy_status, candidate_count, released_count, blocked_count,
            manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
            started_at, finished_at, updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            POLICY_NAME,
            LABEL_FAMILY,
            len(rows),
            released,
            len(rows) - released,
            len(rows) - released,
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            now,
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in rows:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
                segment_id, relative_path, source_key, source_line_number,
                label_family, confirmation_label, policy_action, policy_allowed,
                block_reason, output_match_kind, token_status, issue_count,
                high_issue_count, model_safe_probability, review_priority,
                reasons_json, created_at
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                LABEL_FAMILY,
                row["confirmation_label"],
                POLICY_ACTION,
                int(row["policy_allowed"]),
                row["block_reason"],
                OUTPUT_MATCH_KIND if row["policy_allowed"] else "blocked",
                "ok" if row["policy_allowed"] else "not_evaluated",
                int(row["issue_count"] or 0),
                int(row["high_issue_count"] or 0),
                0.0,
                json.dumps(
                    {
                        "source": RULE_VERSION,
                        "candidate_id": row.get("candidate_id"),
                        "confirmation_id": row.get("confirmation_id"),
                        "confirmation_source": row.get("confirmation_source"),
                        "learning_run_id": row.get("learning_run_id"),
                        "origin": row.get("origin"),
                        "match_type": row.get("match_type"),
                        "source_segment_state_run_id": state_run_id,
                        "source_ledger_run_id": ledger_run_id,
                        "expected_final_state_if_released": row.get("expected_final_state_if_released"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
    return run_id


def write_reports(
    rows: list[dict[str, Any]],
    mode: str,
    state_run_id: int,
    ledger_run_id: int,
    policy_run_id: int | None,
    txt_path: Path,
    jsonl_path: Path,
    summary_path: Path,
    csv_path: Path,
) -> dict[str, Any]:
    released = sum(1 for row in rows if row["policy_allowed"])
    blocked = len(rows) - released
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "policy_run_id": policy_run_id,
        "segment_state_run_id": state_run_id,
        "ledger_run_id": ledger_run_id,
        "candidate_count": len(rows),
        "released_count": released,
        "blocked_count": blocked,
        "target_segment_ids": list(TARGET_SEGMENT_IDS),
        "expected_delta_after_segment_state": {
            "closed_count": released,
            "pending_count": -released,
            "reopen_count": -released,
            "output_apply_pending_count": 0,
        },
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_lifecycle_now": False,
        "run_production_full_now": False,
        "writes_source": False,
        "writes_output": False,
        "rows": rows,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("segment_id,policy_allowed,block_reason,relative_path,source_key\n")
        for row in rows:
            handle.write(
                f"{row['segment_id']},{row['policy_allowed']},"
                f"\"{row['block_reason']}\",\"{row['relative_path']}\",\"{row['source_key']}\"\n"
            )
    lines = [
        "General pipe article/gender lifecycle policy materializer",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"policy_name={POLICY_NAME}",
        f"policy_action={POLICY_ACTION}",
        f"policy_run_id={policy_run_id or ''}",
        f"segment_state_run_id={state_run_id}",
        f"ledger_run_id={ledger_run_id}",
        "",
        "Summary:",
        f"- candidate_count: {len(rows)}",
        f"- released_count: {released}",
        f"- blocked_count: {blocked}",
        "- writes_source: false",
        "- writes_output: false",
        "- runs_lifecycle: false",
        "- runs_segment_state: false",
        "",
        "Released:",
    ]
    for row in rows:
        if row["policy_allowed"]:
            lines.append(f"- {row['segment_id']} | {row['relative_path']} | {row['source_key']}")
    if not released:
        lines.append("- none")
    lines.append("")
    lines.append("Blocked:")
    for row in rows:
        if not row["policy_allowed"]:
            lines.append(f"- {row['segment_id']} | {row['block_reason']}")
    if not blocked:
        lines.append("- none")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    txt_path, jsonl_path, summary_path, csv_path = output_paths(mode)

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            ledger_run_id = args.ledger_run_id or latest_finished_ledger_run_id(conn)
            rows = collect_rows(conn, args.segment_state_run_id, ledger_run_id)
            if sum(1 for row in rows if row["policy_allowed"]) != len(TARGET_SEGMENT_IDS):
                write_reports(rows, mode, args.segment_state_run_id, ledger_run_id, None, txt_path, jsonl_path, summary_path, csv_path)
                raise SystemExit("apply blocked: not all target segments are policy_allowed")
            policy_run_id = apply_policy_run(conn, rows, txt_path, csv_path, jsonl_path, args.segment_state_run_id, ledger_run_id)
            summary = write_reports(rows, mode, args.segment_state_run_id, ledger_run_id, policy_run_id, txt_path, jsonl_path, summary_path, csv_path)
            conn.commit()
    else:
        with readonly_conn() as conn:
            ledger_run_id = args.ledger_run_id or latest_finished_ledger_run_id(conn)
            rows = collect_rows(conn, args.segment_state_run_id, ledger_run_id)
        summary = write_reports(rows, mode, args.segment_state_run_id, ledger_run_id, None, txt_path, jsonl_path, summary_path, csv_path)

    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"csv={csv_path}")
    print(f"mode={mode}")
    print(f"policy_run_id={summary['policy_run_id'] or ''}")
    print(f"candidate_count={summary['candidate_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("writes_source=false")
    print("writes_output=false")
    print("runs_lifecycle=false")
    print("runs_segment_state=false")


if __name__ == "__main__":
    main()
