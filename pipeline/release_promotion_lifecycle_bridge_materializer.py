from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import release_promotion_lifecycle_bridge_dry_run as bridge


RULE_VERSION = "release_promotion_lifecycle_bridge_materializer_v1"
LABEL_FAMILY = "validated_release_promotion"
OUTPUT_MATCH_KIND = "output_confirmation_canonical_match"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize the exact audited release-promotion lifecycle bridge."
    )
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--policy-name", default=bridge.POLICY_NAME)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def latest_segment_state_run_id(conn: sqlite3.Connection) -> int:
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
    if not row:
        raise SystemExit("No completed segment-state run found.")
    return int(row["id"])


def validate_current(
    validation_rows: list[dict[str, Any]], expected_count: int
) -> tuple[list[dict[str, Any]], int, int | None]:
    if len(validation_rows) != expected_count:
        raise SystemExit(
            f"Validation count mismatch: found {len(validation_rows)}, expected {expected_count}."
        )
    segment_ids = [int(row["segment_id"]) for row in validation_rows]
    if len(set(segment_ids)) != expected_count:
        raise SystemExit("Validation set contains duplicate segment IDs.")

    with bridge.connect_readonly() as conn:
        state_run_id = latest_segment_state_run_id(conn)
        ledger_run_id = bridge.latest_ledger_run_id(conn)
        current = bridge.fetch_current(conn, state_run_id, ledger_run_id, segment_ids)

    records = [
        bridge.evaluate(row, current.get(int(row["segment_id"])))
        for row in validation_rows
    ]
    return records, state_run_id, ledger_run_id


def existing_exact_policy_run(
    conn: sqlite3.Connection, segment_ids: list[int], policy_name: str
) -> int | None:
    rows = conn.execute(
        """
        SELECT run.id
        FROM auto_confirmation_reopen_lifecycle_policy_runs run
        WHERE run.policy_name = ?
          AND run.policy_status = 'active'
          AND run.finished_at IS NOT NULL
          AND run.released_count = ?
        ORDER BY run.id DESC
        """,
        (policy_name, len(segment_ids)),
    ).fetchall()
    expected = set(segment_ids)
    for row in rows:
        run_id = int(row["id"])
        actual = {
            int(item["segment_id"])
            for item in conn.execute(
                """
                SELECT segment_id
                FROM auto_confirmation_reopen_lifecycle_policy_items
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
    validation_rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    state_run_id: int,
    ledger_run_id: int | None,
    validation_path: Path,
    md_path: Path,
    jsonl_path: Path,
    policy_name: str,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version, queue_run_id, audit_run_id, policy_name, label_family,
            policy_status, candidate_count, released_count, blocked_count,
            manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
            started_at, finished_at, updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, 0, 0, 0, ?, NULL, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            policy_name,
            LABEL_FAMILY,
            len(records),
            len(records),
            str(md_path),
            str(jsonl_path),
            now,
            now,
            now,
        ),
    )
    policy_run_id = int(cursor.lastrowid)
    validation_by_id = {int(row["segment_id"]): row for row in validation_rows}

    for record in records:
        segment_id = int(record["segment_id"])
        validation = validation_by_id[segment_id]
        source = conn.execute(
            """
            SELECT relative_path, source_key, source_line_number
            FROM source_segments
            WHERE id = ?
            """,
            (segment_id,),
        ).fetchone()
        if not source:
            raise RuntimeError(f"Missing source segment during policy insert: {segment_id}")
        reasons = {
            "source": RULE_VERSION,
            "validation_jsonl": str(validation_path),
            "source_segment_state_run_id": int(validation.get("segment_state_run_id") or 0),
            "materialized_segment_state_run_id": state_run_id,
            "source_ledger_run_id": ledger_run_id,
            "lane": record["lane"],
            "promotion_gate": record["promotion_gate"],
            "confirmation_source": record.get("confirmation_source"),
            "expected_final_state": bridge.EXPECTED_FINAL_STATE,
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
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, 1, '', ?, 'ok', 0, 0, ?, 0.0, ?, ?)
            """,
            (
                policy_run_id,
                segment_id,
                source["relative_path"],
                source["source_key"],
                source["source_line_number"],
                LABEL_FAMILY,
                validation.get("confirmation_label"),
                bridge.POLICY_ACTION,
                OUTPUT_MATCH_KIND,
                validation.get("effective_new_score"),
                json.dumps(reasons, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
    return policy_run_id


def write_reports(
    records: list[dict[str, Any]],
    *,
    mode: str,
    state_run_id: int,
    ledger_run_id: int | None,
    policy_run_id: int | None,
    already_materialized: bool,
    policy_name: str,
    md_path: Path,
    jsonl_path: Path,
    summary_path: Path,
) -> dict[str, Any]:
    decisions = Counter(record["dry_run_decision"] for record in records)
    block_counts = Counter(
        reason for record in records for reason in record["block_reasons"]
    )
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "policy_name": policy_name,
        "policy_action": bridge.POLICY_ACTION,
        "policy_run_id": policy_run_id,
        "already_materialized": already_materialized,
        "segment_state_run_id": state_run_id,
        "ledger_run_id": ledger_run_id,
        "candidate_count": len(records),
        "released_count": decisions["released"],
        "blocked_count": decisions["blocked"],
        "block_reason_counts": dict(block_counts),
        "expected_final_state": bridge.EXPECTED_FINAL_STATE,
        "expected_delta_after_segment_state": {
            "closed": decisions["released"],
            "pending": -decisions["released"],
            "reopen": -decisions["released"],
            "needs_output_apply": 0,
        },
        "policy_write_count": 0 if mode == "dry_run" or already_materialized else len(records),
        "source_changed": False,
        "output_changed": False,
        "segment_state_executed": False,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# Validated Release Promotion Lifecycle Materializer",
        "",
        f"- mode: `{mode}`",
        f"- policy run: `{policy_run_id or '-'}`",
        f"- segment-state source: `{state_run_id}`",
        f"- ledger: `{ledger_run_id}`",
        f"- candidates: `{len(records)}`",
        f"- released: `{decisions['released']}`",
        f"- blocked: `{decisions['blocked']}`",
        f"- already materialized: `{str(already_materialized).lower()}`",
        "- source/output changed: `false`",
        "- segment-state executed: `false`",
        "",
        "| ID | lane | gate | decision | reasons |",
        "|---:|---|---|---|---|",
    ]
    for record in records:
        reasons = ", ".join(record["block_reasons"]) or "-"
        lines.append(
            f"| {record['segment_id']} | `{record['lane']}` | `{record['promotion_gate']}` | "
            f"`{record['dry_run_decision']}` | {reasons} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    args = parse_args()
    validation_path = db.project_path(args.validation_jsonl)
    validation_rows = bridge.read_jsonl(args.validation_jsonl)
    records, state_run_id, ledger_run_id = validate_current(
        validation_rows, args.expected_count
    )
    released = sum(1 for record in records if record["policy_allowed"])
    if released != args.expected_count:
        raise SystemExit(
            f"Materializer blocked: released {released}, expected {args.expected_count}."
        )

    mode = "apply" if args.apply else "dry_run"
    base = bridge.reports_dir() / f"{stamp()}_release_promotion_lifecycle_bridge_materializer_{mode}"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    policy_run_id: int | None = None
    already_materialized = False

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            segment_ids = [int(record["segment_id"]) for record in records]
            policy_run_id = existing_exact_policy_run(conn, segment_ids, args.policy_name)
            if policy_run_id is not None:
                already_materialized = True
            else:
                policy_run_id = insert_policy_run(
                    conn,
                    validation_rows,
                    records,
                    state_run_id,
                    ledger_run_id,
                    validation_path,
                    md_path,
                    jsonl_path,
                    args.policy_name,
                )
                conn.commit()

    summary = write_reports(
        records,
        mode=mode,
        state_run_id=state_run_id,
        ledger_run_id=ledger_run_id,
        policy_run_id=policy_run_id,
        already_materialized=already_materialized,
        policy_name=args.policy_name,
        md_path=md_path,
        jsonl_path=jsonl_path,
        summary_path=summary_path,
    )
    print(
        json.dumps(
            {
                "summary": summary,
                "artifacts": [str(md_path), str(jsonl_path), str(summary_path)],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
