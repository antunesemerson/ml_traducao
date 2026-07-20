from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text, structural_tokens
from quality_promotion_cycle import load_providers


RULE_VERSION = "pairwise_monotonic_lifecycle_materializer_v1"
POLICY_NAME = "pairwise_monotonic_repair_lifecycle_bridge"
POLICY_ACTION = "close_reopen_pairwise_monotonic_repair_lifecycle"
LABEL_FAMILY = "pairwise_monotonic_repair"
EXPECTED_FINAL_STATE = "closed_auto_confirmed_pairwise_monotonic_repair_lifecycle"
PAIRWISE_SOURCE = "pairwise_monotonic_repair"
LEGACY_SOURCE = "offline_proposals"
LEGACY_LABEL = "remove_space_before_punctuation"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize lifecycle closure for applied pairwise monotonic repairs."
    )
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--segment-ids", default="")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def latest_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run found.")
    return int(row["id"])


def latest_ledger_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL"
    ).fetchone()
    return int(row["id"]) if row and row["id"] is not None else None


def parse_segment_ids(value: str) -> list[int]:
    return sorted({int(item.strip()) for item in value.split(",") if item.strip()})


def registered_pairwise_evidence_types() -> set[str]:
    return {provider.evidence_type for provider in load_providers()}


def deterministic_space_repair(text: str) -> str:
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def fetch_rows(
    conn: sqlite3.Connection,
    state_run_id: int,
    ledger_run_id: int | None,
    segment_ids: list[int],
    evidence_types: set[str],
) -> list[dict[str, Any]]:
    if not evidence_types:
        raise RuntimeError("No registered quality promotion evidence types were found.")
    id_filter = ""
    id_params: list[Any] = []
    if segment_ids:
        placeholders = ",".join("?" for _ in segment_ids)
        id_filter = f"AND source.id IN ({placeholders})"
        id_params.extend(segment_ids)
    ledger_join = ""
    ledger_column = "0 AS open_issue_count"
    ledger_params: list[Any] = []
    if ledger_run_id is not None:
        ledger_join = """
        LEFT JOIN (
          SELECT segment_id, COUNT(*) AS open_issue_count
          FROM ml_issue_ledger_items
          WHERE run_id = ?
            AND lower(COALESCE(status, 'open')) NOT LIKE 'closed%'
          GROUP BY segment_id
        ) ledger ON ledger.segment_id = source.id
        """
        ledger_column = "COALESCE(ledger.open_issue_count, 0) AS open_issue_count"
        ledger_params.append(ledger_run_id)
    ordered_evidence_types = sorted(evidence_types)
    evidence_placeholders = ",".join("?" for _ in ordered_evidence_types)
    rows = conn.execute(
        f"""
        SELECT
          source.id AS segment_id,
          source.relative_path,
          source.source_key,
          source.source_line_number,
          source.old_text,
          state.final_state,
          state.state_group,
          state.needs_output_apply,
          state.confirmed_matches_output,
          output.portuguese_text AS output_text,
          confirmation.confirmed_text,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked AS confirmation_locked,
          {ledger_column},
          evidence.id AS evidence_id,
          evidence.evidence_type,
          evidence.baseline_text,
          evidence.candidate_text,
          evidence.pairwise_score,
          evidence.pairwise_delta,
          evidence.promotion_eligible,
          evidence.token_integrity_ok,
          evidence.post_validation_clean
        FROM source_segments source
        JOIN segment_state_items state
          ON state.segment_id = source.id AND state.run_id = ?
        JOIN output_segments output ON output.segment_id = source.id
        JOIN segment_confirmations confirmation ON confirmation.segment_id = source.id
        LEFT JOIN ml_pairwise_quality_evidence evidence
          ON evidence.id = (
            SELECT MAX(e2.id)
            FROM ml_pairwise_quality_evidence e2
            WHERE e2.segment_id = source.id
              AND e2.evidence_type IN ({evidence_placeholders})
              AND e2.candidate_text = confirmation.confirmed_text
              AND e2.baseline_text = source.old_text
          )
        {ledger_join}
        WHERE source.is_active = 1
          AND state.state_group = 'pending'
          AND state.final_state IN (
            'pending_apply_confirmed',
            'reopen_auto_confirmed_autofix'
          )
          AND (
            (
              confirmation.confirmation_source = ?
            )
            OR
            (confirmation.confirmation_source = ? AND confirmation.confirmation_label = ?)
          )
          {id_filter}
        ORDER BY source.id
        """,
        [
            state_run_id,
            *ordered_evidence_types,
            *ledger_params,
            PAIRWISE_SOURCE,
            LEGACY_SOURCE,
            LEGACY_LABEL,
            *id_params,
        ],
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate(row: dict[str, Any], evidence_types: set[str]) -> dict[str, Any]:
    reasons: list[str] = []
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    old_text = str(row.get("old_text") or "")
    source = str(row.get("confirmation_source") or "")
    label = str(row.get("confirmation_label") or "")
    state_is_post_write = row.get("final_state") == "reopen_auto_confirmed_autofix"
    if state_is_post_write and int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_zero")
    if state_is_post_write and int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_one")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_present")
    if canonical_localization_text(output_text) != canonical_localization_text(confirmed_text):
        reasons.append("output_confirmation_canonical_mismatch")
    if structural_tokens(output_text) != structural_tokens(confirmed_text):
        reasons.append("output_confirmation_token_mismatch")
    if int(row.get("confirmation_locked") or 0) != 0:
        reasons.append("confirmation_unexpectedly_locked")

    evidence_kind = ""
    if source == PAIRWISE_SOURCE:
        evidence_kind = str(row.get("evidence_type") or "")
        if row.get("evidence_id") is None:
            reasons.append("pairwise_evidence_missing")
        elif evidence_kind not in evidence_types:
            reasons.append("pairwise_evidence_type_not_registered")
        if old_text != str(row.get("baseline_text") or ""):
            reasons.append("pairwise_baseline_stale")
        if output_text != str(row.get("candidate_text") or ""):
            reasons.append("pairwise_candidate_stale")
        if int(row.get("token_integrity_ok") or 0) != 1:
            reasons.append("pairwise_token_integrity_failed")
        if int(row.get("post_validation_clean") or 0) != 1:
            reasons.append("pairwise_post_validation_failed")
    elif source == LEGACY_SOURCE and label == LEGACY_LABEL:
        evidence_kind = "legacy_exact_deterministic_repair"
        if deterministic_space_repair(old_text) != output_text or old_text == output_text:
            reasons.append("legacy_repair_not_exact")
        if structural_tokens(old_text) != structural_tokens(output_text):
            reasons.append("legacy_token_integrity_failed")
    else:
        reasons.append("confirmation_source_label_not_allowed")

    allowed = not reasons
    return {
        **row,
        "evidence_kind": evidence_kind,
        "policy_action": POLICY_ACTION,
        "policy_allowed": allowed,
        "dry_run_decision": "released" if allowed else "blocked",
        "block_reasons": reasons,
        "expected_final_state": EXPECTED_FINAL_STATE,
    }


def existing_exact_policy_run(conn: sqlite3.Connection, segment_ids: list[int]) -> int | None:
    expected = set(segment_ids)
    rows = conn.execute(
        """
        SELECT id FROM auto_confirmation_reopen_lifecycle_policy_runs
        WHERE policy_name = ? AND policy_status = 'active'
          AND finished_at IS NOT NULL AND released_count = ?
        ORDER BY id DESC
        """,
        (POLICY_NAME, len(segment_ids)),
    ).fetchall()
    for row in rows:
        run_id = int(row["id"])
        actual = {
            int(item["segment_id"])
            for item in conn.execute(
                """
                SELECT segment_id FROM auto_confirmation_reopen_lifecycle_policy_items
                WHERE run_id = ? AND policy_allowed = 1
                """,
                (run_id,),
            ).fetchall()
        }
        if actual == expected:
            return run_id
    return None


def insert_policy_run(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    report_path: Path,
    jsonl_path: Path,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
          rule_version, queue_run_id, audit_run_id, policy_name, label_family,
          policy_status, candidate_count, released_count, blocked_count,
          manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
          started_at, finished_at, updated_at
        ) VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, 0, 0, 0, ?, NULL, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            POLICY_NAME,
            LABEL_FAMILY,
            len(records),
            len(records),
            str(report_path),
            str(jsonl_path),
            now,
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for record in records:
        reasons = {
            "source": RULE_VERSION,
            "evidence_kind": record["evidence_kind"],
            "evidence_id": record.get("evidence_id"),
            "confirmation_source": record.get("confirmation_source"),
            "confirmation_label": record.get("confirmation_label"),
            "expected_final_state": EXPECTED_FINAL_STATE,
        }
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
              run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
              segment_id, relative_path, source_key, source_line_number,
              label_family, confirmation_label, policy_action, policy_allowed,
              block_reason, output_match_kind, token_status, issue_count,
              high_issue_count, model_safe_probability, review_priority,
              reasons_json, created_at
            ) VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, 1, '',
                      'output_confirmation_exact_match', 'ok', 0, 0, ?, 0.0, ?, ?)
            """,
            (
                run_id,
                int(record["segment_id"]),
                record["relative_path"],
                record["source_key"],
                record["source_line_number"],
                LABEL_FAMILY,
                record["confirmation_label"],
                POLICY_ACTION,
                record.get("pairwise_score"),
                json.dumps(reasons, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
    return run_id


def write_reports(
    records: list[dict[str, Any]],
    *,
    mode: str,
    state_run_id: int,
    ledger_run_id: int | None,
    policy_run_id: int | None,
    already_materialized: bool,
) -> dict[str, Any]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_pairwise_monotonic_lifecycle_materializer_{mode}"
    markdown_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    decisions = Counter(record["dry_run_decision"] for record in records)
    block_counts = Counter(
        reason for record in records for reason in record["block_reasons"]
    )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# Pairwise Monotonic Lifecycle Materializer",
        "",
        f"- mode: `{mode}`",
        f"- state run: `{state_run_id}`",
        f"- candidates: `{len(records)}`",
        f"- released: `{decisions['released']}`",
        f"- blocked: `{decisions['blocked']}`",
        f"- policy run: `{policy_run_id or '-'}`",
        "- output changed: `false`",
        "",
        "| ID | evidence | decision | reasons |",
        "|---:|---|---|---|",
    ]
    for record in records:
        lines.append(
            f"| {record['segment_id']} | `{record['evidence_kind']}` | "
            f"`{record['dry_run_decision']}` | {', '.join(record['block_reasons']) or '-'} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "policy_run_id": policy_run_id,
        "already_materialized": already_materialized,
        "segment_state_run_id": state_run_id,
        "ledger_run_id": ledger_run_id,
        "candidate_count": len(records),
        "released_count": decisions["released"],
        "blocked_count": decisions["blocked"],
        "block_reason_counts": dict(block_counts),
        "expected_final_state": EXPECTED_FINAL_STATE,
        "source_changed": False,
        "output_changed": False,
        "artifacts": [str(markdown_path), str(jsonl_path), str(summary_path)],
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"candidate_count={len(records)}")
    print(f"released_count={decisions['released']}")
    print(f"blocked_count={decisions['blocked']}")
    print(f"policy_run_id={policy_run_id or 0}")
    print(f"Report: {summary_path}")
    return summary


def main() -> int:
    args = parse_args()
    segment_ids = parse_segment_ids(args.segment_ids)
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        conn.row_factory = sqlite3.Row
        state_run_id = args.segment_state_run_id or latest_state_run_id(conn)
        ledger_run_id = latest_ledger_run_id(conn)
        evidence_types = registered_pairwise_evidence_types()
        records = [
            evaluate(row, evidence_types)
            for row in fetch_rows(
                conn,
                state_run_id,
                ledger_run_id,
                segment_ids,
                evidence_types,
            )
        ]
        if args.expected_count is not None and len(records) != args.expected_count:
            raise SystemExit(
                f"Candidate count mismatch: found {len(records)}, expected {args.expected_count}."
            )
        released = [row for row in records if row["policy_allowed"]]
        blocked = [row for row in records if not row["policy_allowed"]]
        policy_run_id: int | None = None
        already_materialized = False
        mode = "apply" if args.apply else "dry_run"
        if args.apply:
            if blocked or len(released) != len(records):
                raise SystemExit(
                    f"Materializer blocked: released {len(released)}, blocked {len(blocked)}."
                )
            report_stub = db.project_path(settings["reports_dir"]) / "pairwise_monotonic_lifecycle_materializer"
            policy_run_id = existing_exact_policy_run(
                conn, [int(row["segment_id"]) for row in released]
            )
            if policy_run_id is not None:
                already_materialized = True
            else:
                policy_run_id = insert_policy_run(
                    conn,
                    released,
                    report_stub.with_suffix(".md"),
                    report_stub.with_suffix(".jsonl"),
                )
                conn.commit()
        write_reports(
            records,
            mode=mode,
            state_run_id=state_run_id,
            ledger_run_id=ledger_run_id,
            policy_run_id=policy_run_id,
            already_materialized=already_materialized,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
