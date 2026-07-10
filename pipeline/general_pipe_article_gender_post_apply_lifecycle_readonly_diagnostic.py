from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "general_pipe_article_gender_post_apply_lifecycle_readonly_diagnostic_v1"
TARGET_SEGMENT_IDS = [20762, 60278, 230843]
BASELINE_RUN_ID = 406
CURRENT_RUN_ID = 408
POLICY_NAME = "human_confirmed_post_apply_repair_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_post_apply_repair_lifecycle"
EXPECTED_FINAL_STATE = "closed_auto_confirmed_human_confirmed_post_apply_repair_lifecycle"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
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


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(Counter(protected_tokens(value or "")).items()))


def latest_finished_ledger_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL").fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing finished issue ledger run")
    return int(row["id"])


def run_row(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM segment_state_runs WHERE id = ?", (run_id,))
    if not row:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    return row


def segment_state(conn: sqlite3.Connection, run_id: int, segment_id: int) -> dict[str, Any] | None:
    return one(
        conn,
        """
        SELECT segment_id, relative_path, source_key, source_line_number, final_state,
               state_group, review_state, apply_state, confirmation_level,
               confirmation_label, locked, confirmed_matches_output,
               needs_human, needs_output_apply, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (run_id, segment_id),
    )


def segment_probe(conn: sqlite3.Connection, segment_id: int, ledger_run_id: int) -> dict[str, Any]:
    source_output_confirmation = one(
        conn,
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
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
            ll.match_type,
            ll.source_language,
            ll.learned_at,
            ll.confirmation_synced_at
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN local_learning_candidates ll ON ll.id = sc.candidate_id
        WHERE s.id = ?
        """,
        (segment_id,),
    )
    before_state = segment_state(conn, BASELINE_RUN_ID, segment_id)
    current_state = segment_state(conn, CURRENT_RUN_ID, segment_id)
    policy_items = all_rows(
        conn,
        """
        SELECT item.*, run.policy_name, run.policy_status, run.finished_at
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
        WHERE item.segment_id = ?
        ORDER BY item.run_id DESC, item.id DESC
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

    soc = source_output_confirmation or {}
    output_text = str(soc.get("output_text") or "")
    confirmed_text = str(soc.get("confirmed_text") or "")
    token_integrity = token_counts(output_text) == token_counts(confirmed_text)
    guards = {
        "current_state_pending_reopen": bool(
            current_state
            and current_state.get("state_group") == "pending"
            and current_state.get("final_state") == "reopen_auto_confirmed_autofix"
        ),
        "needs_output_apply_zero": bool(current_state and int(current_state.get("needs_output_apply") or 0) == 0),
        "confirmed_matches_output": bool(current_state and int(current_state.get("confirmed_matches_output") or 0) == 1),
        "confirmation_human_confirmed": soc.get("confirmation_level") == "human_confirmed",
        "confirmation_source_allowed": soc.get("confirmation_source") in {"local_learning", "post_apply_human_correction"},
        "confirmation_label_correct": soc.get("confirmation_label") == "correct",
        "confirmation_locked": int(soc.get("locked") or 0) == 1,
        "output_equals_confirmation": bool(output_text and output_text == confirmed_text),
        "token_integrity": token_integrity,
        "latest_open_issue_count_zero": len(open_issues) == 0,
        "policy_item_present": any(
            item.get("policy_name") == POLICY_NAME
            and item.get("policy_status") == "active"
            and item.get("policy_action") == POLICY_ACTION
            for item in policy_items
        ),
    }
    architecture_bridge_evidence_ready = all(value for key, value in guards.items() if key != "policy_item_present")
    return {
        "segment_id": segment_id,
        "relative_path": soc.get("relative_path"),
        "source_key": soc.get("source_key"),
        "source_line_number": soc.get("source_line_number"),
        "learning_run_id": soc.get("learning_run_id"),
        "candidate_id": soc.get("candidate_id"),
        "origin": soc.get("origin"),
        "match_type": soc.get("match_type"),
        "before_state": before_state,
        "current_state": current_state,
        "confirmation": {
            "confirmation_id": soc.get("confirmation_id"),
            "confirmation_level": soc.get("confirmation_level"),
            "confirmation_source": soc.get("confirmation_source"),
            "confirmation_label": soc.get("confirmation_label"),
            "locked": soc.get("locked"),
            "confirmed_text": confirmed_text,
        },
        "output_text": output_text,
        "token_counts_output": token_counts(output_text),
        "token_counts_confirmation": token_counts(confirmed_text),
        "latest_ledger_run_id": ledger_run_id,
        "latest_open_issues": open_issues,
        "existing_policy_items": policy_items,
        "guards": guards,
        "architecture_bridge_evidence_ready": architecture_bridge_evidence_ready,
        "current_blocker": "missing_policy_item" if architecture_bridge_evidence_ready and not guards["policy_item_present"] else "",
        "expected_policy_action": POLICY_ACTION,
        "expected_final_state_if_policy_released": EXPECTED_FINAL_STATE if architecture_bridge_evidence_ready else None,
    }


def build_summary() -> dict[str, Any]:
    with connect_readonly() as conn:
        ledger_run_id = latest_finished_ledger_run_id(conn)
        before_run = run_row(conn, BASELINE_RUN_ID)
        current_run = run_row(conn, CURRENT_RUN_ID)
        probes = [segment_probe(conn, segment_id, ledger_run_id) for segment_id in TARGET_SEGMENT_IDS]
    ready = [probe for probe in probes if probe["architecture_bridge_evidence_ready"]]
    missing_policy = [probe for probe in ready if probe["current_blocker"] == "missing_policy_item"]
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_segment_state_run_id": BASELINE_RUN_ID,
        "current_segment_state_run_id": CURRENT_RUN_ID,
        "latest_ledger_run_id": ledger_run_id,
        "baseline_run": before_run,
        "current_run": current_run,
        "target_segment_count": len(TARGET_SEGMENT_IDS),
        "architecture_bridge_evidence_ready_count": len(ready),
        "missing_policy_item_count": len(missing_policy),
        "expected_delta_if_policy_released": {
            "closed_count": len(missing_policy),
            "pending_count": -len(missing_policy),
            "reopen_count": -len(missing_policy),
            "output_apply_pending_count": 0,
        },
        "probes": probes,
        "requires_architecture_review": bool(missing_policy),
        "recommended_architecture_adjustment": (
            "materialize lifecycle policy items for local_learning human-confirmed protected output corrections "
            "that already have output=confirmation, token integrity ok, no open latest ledger issues, "
            "locked human confirmation, and current segment-state pending reopen with needs_output_apply=0"
            if missing_policy
            else ""
        ),
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_lifecycle_now": False,
        "run_production_full_now": False,
        "discovery_apply_hold": True,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_general_pipe_article_gender_post_apply_lifecycle_readonly_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for probe in summary["probes"]:
            handle.write(json.dumps(probe, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "general pipe article/gender post-apply lifecycle read-only diagnostic",
        f"source={RULE_VERSION}",
        f"baseline_segment_state_run_id={summary['baseline_segment_state_run_id']}",
        f"current_segment_state_run_id={summary['current_segment_state_run_id']}",
        f"latest_ledger_run_id={summary['latest_ledger_run_id']}",
        "",
        "summary:",
        f"- target_segment_count={summary['target_segment_count']}",
        f"- architecture_bridge_evidence_ready_count={summary['architecture_bridge_evidence_ready_count']}",
        f"- missing_policy_item_count={summary['missing_policy_item_count']}",
        f"- requires_architecture_review={str(summary['requires_architecture_review']).lower()}",
        "",
        "expected_delta_if_policy_released:",
        *[f"- {key}: {value}" for key, value in summary["expected_delta_if_policy_released"].items()],
        "",
        "segments:",
    ]
    for probe in summary["probes"]:
        state = probe["current_state"] or {}
        lines.extend(
            [
                f"- segment_id={probe['segment_id']} {probe['relative_path']}::{probe['source_key']}",
                f"  current={state.get('state_group')} / {state.get('final_state')}",
                f"  evidence_ready={str(probe['architecture_bridge_evidence_ready']).lower()}",
                f"  blocker={probe['current_blocker'] or 'none'}",
                f"  expected_final_state_if_policy_released={probe['expected_final_state_if_policy_released']}",
            ]
        )
    lines.extend(
        [
            "",
            "recommended_architecture_adjustment:",
            f"- {summary['recommended_architecture_adjustment'] or 'none'}",
            "",
            "execution_flags:",
            "- read_only=true",
            f"- run_segment_state_now={str(summary['run_segment_state_now']).lower()}",
            f"- run_reindex_now={str(summary['run_reindex_now']).lower()}",
            f"- run_lifecycle_now={str(summary['run_lifecycle_now']).lower()}",
            f"- run_production_full_now={str(summary['run_production_full_now']).lower()}",
            f"- discovery_apply_hold={str(summary['discovery_apply_hold']).lower()}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    summary = build_summary()
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "target_segment_count",
        "architecture_bridge_evidence_ready_count",
        "missing_policy_item_count",
        "expected_delta_if_policy_released",
        "requires_architecture_review",
        "run_segment_state_now",
        "run_reindex_now",
        "run_production_full_now",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
