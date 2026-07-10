from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "segment_token_policy_manual_exception_lifecycle_materializer_v1"
POLICY_NAME = "segment_token_policy_manual_exception_equal_output_lifecycle_bridge"
LABEL_FAMILY = "segment_token_policy_manual_exception_equal_output"
POLICY_ACTION = "close_reopen_token_policy_manual_exception_equal_output_lifecycle"
DEFAULT_SEGMENT_IDS = (107702, 159267, 243970, 286388)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_segment_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return DEFAULT_SEGMENT_IDS
    segment_ids = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not segment_ids:
        raise SystemExit("empty --segment-ids")
    return segment_ids


def latest_finished_segment_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM segment_state_runs WHERE finished_at IS NOT NULL"
    ).fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing finished segment-state run")
    return int(row["id"])


def canonical_l10n(text: str | None) -> str:
    value = str(text or "")
    value = value.replace('\\"', '"')
    value = value.replace("\\n", "\n")
    return value.strip()


def fetch_candidates(
    conn: sqlite3.Connection,
    *,
    segment_state_run_id: int,
    decision_run_id: int | None,
    segment_ids: tuple[int, ...],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    decision_filter = ""
    params: list[Any] = []
    if decision_run_id is not None:
        decision_filter = "AND decision.run_id = ?"
        params.append(decision_run_id)
    params.extend([segment_state_run_id, *segment_ids])
    rows = conn.execute(
        f"""
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.source_line_number,
          state.final_state,
          state.state_group,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.lifecycle_policy_allowed,
          state.lifecycle_policy_action,
          confirmation.confirmation_label,
          confirmation.confirmation_level,
          confirmation.confirmed_text,
          output.portuguese_text AS output_text,
          decision.id AS decision_id,
          decision.run_id AS decision_run_id,
          decision.policy_run_id,
          decision.policy_item_id,
          decision.policy_bucket,
          decision.risk_level,
          decision.decision,
          decision.approved_for_apply
        FROM segment_state_items state
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
        JOIN output_segments output
          ON output.segment_id = state.segment_id
        JOIN segment_token_policy_decisions decision
          ON decision.segment_id = state.segment_id
         AND decision.approved_for_apply = 1
         AND decision.decision = 'keep_manual_exception_only'
         {decision_filter}
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id, decision.run_id DESC, decision.id DESC
        """,
        params,
    ).fetchall()

    by_segment: dict[int, dict[str, Any]] = {}
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id not in by_segment:
            by_segment[segment_id] = dict(row)
    return [by_segment[segment_id] for segment_id in sorted(by_segment)]


def existing_allowed_item(conn: sqlite3.Connection, segment_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs policy_run
          ON policy_run.id = item.run_id
        WHERE item.segment_id = ?
          AND policy_run.policy_name = ?
          AND policy_run.policy_status IN ('active', 'released')
          AND item.policy_action = ?
          AND item.policy_allowed = 1
        LIMIT 1
        """,
        (segment_id, POLICY_NAME, POLICY_ACTION),
    ).fetchone()
    return row is not None


def validate_candidate(conn: sqlite3.Connection, row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if row["state_group"] != "pending":
        failures.append("state_group_not_pending")
    if row["final_state"] != "reopen_auto_confirmed_autofix":
        failures.append("final_state_not_reopen_auto_confirmed_autofix")
    if int(row["confirmed_matches_output"] or 0) != 1:
        failures.append("confirmed_matches_output_not_1")
    if int(row["needs_output_apply"] or 0) != 0:
        failures.append("needs_output_apply_not_0")
    if canonical_l10n(row["confirmed_text"]) != canonical_l10n(row["output_text"]):
        failures.append("canonical_output_confirmed_mismatch")
    if int(row["approved_for_apply"] or 0) != 1:
        failures.append("decision_not_approved_for_apply")
    if row["decision"] != "keep_manual_exception_only":
        failures.append("decision_not_manual_exception")
    if existing_allowed_item(conn, int(row["segment_id"])):
        failures.append("duplicate_allowed_policy_item")
    return failures


def build_rows(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    released: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in candidates:
        failures = validate_candidate(conn, row)
        if failures:
            blocked.append({**row, "block_reason": ";".join(failures)})
            continue
        released.append(
            {
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "label_family": LABEL_FAMILY,
                "confirmation_label": row["confirmation_label"],
                "policy_action": POLICY_ACTION,
                "policy_allowed": 1,
                "block_reason": None,
                "output_match_kind": "canonical_equal",
                "token_status": "ok",
                "issue_count": 0,
                "high_issue_count": 0,
                "model_safe_probability": 1.0,
                "review_priority": 0.0,
                "reasons_json": json.dumps(
                    {
                        "source": RULE_VERSION,
                        "decision_run_id": row["decision_run_id"],
                        "policy_run_id": row["policy_run_id"],
                        "policy_item_id": row["policy_item_id"],
                        "policy_bucket": row["policy_bucket"],
                        "risk_level": row["risk_level"],
                        "decision": row["decision"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )
    return released, blocked


def write_reports(
    *,
    mode: str,
    summary: dict[str, Any],
    released: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> None:
    base = reports_dir() / f"{stamp()}_segment_token_policy_manual_exception_lifecycle_materializer_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    csv_path = base.with_suffix(".csv")

    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
        "csv": str(csv_path),
    }

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in released:
            handle.write(json.dumps({"record_type": "released", **item}, ensure_ascii=False, sort_keys=True) + "\n")
        for item in blocked:
            handle.write(json.dumps({"record_type": "blocked", **item}, ensure_ascii=False, sort_keys=True) + "\n")

    csv_path.write_text(
        "segment_id,policy_allowed,block_reason,relative_path,source_key\n"
        + "\n".join(
            f"{row['segment_id']},{row.get('policy_allowed', 0)},{row.get('block_reason') or ''},{row['relative_path']},{row['source_key']}"
            for row in [*released, *blocked]
        )
        + "\n",
        encoding="utf-8",
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Segment token-policy manual exception lifecycle materializer",
        f"mode={mode}",
        f"policy_name={POLICY_NAME}",
        f"policy_action={POLICY_ACTION}",
        f"policy_run_id={summary.get('policy_run_id')}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"candidate_count={summary['candidate_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        "",
        "Guards:",
        "decision=keep_manual_exception_only",
        "approved_for_apply=1",
        "current_state=reopen_auto_confirmed_autofix",
        "confirmed_matches_output=1",
        "needs_output_apply=0",
        "canonical output == confirmed",
        "issue_count=0",
        "high_issue_count=0",
        "",
        "Operation counters:",
        "candidate_generation_count=0",
        "output_apply_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"csv={csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialize lifecycle policy for token-policy manual exceptions already equal to output."
    )
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--decision-run-id", type=int, default=111)
    parser.add_argument("--segment-ids")
    parser.add_argument("--expected-count", type=int, default=len(DEFAULT_SEGMENT_IDS))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        conn.row_factory = sqlite3.Row
        segment_state_run_id = args.segment_state_run_id or latest_finished_segment_state_run_id(conn)
        segment_ids = parse_segment_ids(args.segment_ids)
        candidates = fetch_candidates(
            conn,
            segment_state_run_id=segment_state_run_id,
            decision_run_id=args.decision_run_id,
            segment_ids=segment_ids,
        )
        released, blocked = build_rows(conn, candidates)
        if len(candidates) != args.expected_count:
            raise SystemExit(f"candidate count guard failed: {len(candidates)} != {args.expected_count}")
        if len(released) != args.expected_count:
            raise SystemExit(f"released count guard failed: {len(released)} != {args.expected_count}; blocked={len(blocked)}")

        policy_run_id: int | None = None
        mode = "apply" if args.apply else "dry_run"
        if args.apply:
            started = now_iso()
            cur = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
                  rule_version, queue_run_id, audit_run_id, policy_name, label_family,
                  policy_status, candidate_count, released_count, blocked_count,
                  manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
                  started_at, finished_at, updated_at
                ) VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, ?, 0, 0, NULL, NULL, NULL, ?, ?, ?)
                """,
                (RULE_VERSION, POLICY_NAME, LABEL_FAMILY, len(candidates), len(released), len(blocked), started, started, started),
            )
            policy_run_id = int(cur.lastrowid)
            for item in released:
                conn.execute(
                    """
                    INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                      run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
                      segment_id, relative_path, source_key, source_line_number,
                      label_family, confirmation_label, policy_action, policy_allowed,
                      block_reason, output_match_kind, token_status, issue_count,
                      high_issue_count, model_safe_probability, review_priority,
                      reasons_json, created_at
                    ) VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        policy_run_id,
                        item["segment_id"],
                        item["relative_path"],
                        item["source_key"],
                        item["source_line_number"],
                        item["label_family"],
                        item["confirmation_label"],
                        item["policy_action"],
                        item["policy_allowed"],
                        item["block_reason"],
                        item["output_match_kind"],
                        item["token_status"],
                        item["issue_count"],
                        item["high_issue_count"],
                        item["model_safe_probability"],
                        item["review_priority"],
                        item["reasons_json"],
                        started,
                    ),
                )
            conn.commit()

    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "apply": bool(args.apply),
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "policy_run_id": policy_run_id,
        "decision_run_id": args.decision_run_id,
        "segment_state_run_id": segment_state_run_id,
        "target_segment_ids": list(segment_ids),
        "candidate_count": len(candidates),
        "released_count": len(released),
        "blocked_count": len(blocked),
        "expected_delta_after_segment_state": {
            "closed_count": len(released),
            "pending_count": -len(released),
            "reopen_count": -len(released),
            "output_apply_pending_count": 0,
        },
        "candidate_generation_count": 0,
        "output_apply_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_production_full_now": False,
    }
    write_reports(mode=mode, summary=summary, released=released, blocked=blocked)
    print(f"policy_run_id={policy_run_id}")
    print(f"candidate_count={len(candidates)}")
    print(f"released_count={len(released)}")
    print(f"blocked_count={len(blocked)}")
    print("output_apply_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
