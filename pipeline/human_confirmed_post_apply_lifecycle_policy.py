from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "human_confirmed_post_apply_lifecycle_policy_v1"
POLICY_NAME = "human_confirmed_post_apply_repair_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_post_apply_repair_lifecycle"
LABEL_FAMILY = "human_confirmed_post_apply_repair"
APPROVED_DECISIONS = {"human_approved_for_protected_apply"}
ALLOWED_CONFIRMATION_SOURCES = {"local_learning", "post_apply_human_correction"}
ALLOWED_CONFIRMATION_LABELS = {"correct"}
ALLOWED_ISSUE_SHAPES = {
    ("gender_token_microagent", "gender_token_usage"),
    ("semantic_review_router", "needs_human_or_semantic_conflict"),
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def readonly_conn() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def latest_run_id(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT MAX(id) AS id FROM {table_name}").fetchone()
    if not row or row["id"] is None:
        raise SystemExit(f"missing latest run in {table_name}")
    return int(row["id"])


def norm_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def token_signature(value: str) -> tuple[str, ...]:
    return tuple(protected_tokens(value))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def fetch_segment_context(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    state_run_id: int,
    ledger_run_id: int,
) -> dict[str, Any]:
    state = fetch_one(
        conn,
        """
        SELECT *
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (state_run_id, segment_id),
    )
    output = fetch_one(
        conn,
        """
        SELECT segment_id, relative_path, output_line_number, portuguese_text
        FROM output_segments
        WHERE segment_id = ?
        """,
        (segment_id,),
    )
    confirmation = fetch_one(
        conn,
        """
        SELECT *
        FROM segment_confirmations
        WHERE segment_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (segment_id,),
    )
    source = fetch_one(
        conn,
        """
        SELECT id AS segment_id, relative_path, source_key, source_line_number
        FROM source_segments
        WHERE id = ?
        """,
        (segment_id,),
    )
    issues = [
        dict(row)
        for row in conn.execute(
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
    ]
    return {"state": state, "output": output, "confirmation": confirmation, "source": source, "issues": issues}


def evaluate(decision: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(decision["segment_id"])
    state = context["state"] or {}
    output = context["output"] or {}
    confirmation = context["confirmation"] or {}
    source = context["source"] or {}
    issues = context["issues"]

    candidate_text = norm_text(decision.get("candidate_text"))
    output_text = norm_text(output.get("portuguese_text"))
    confirmed_text = norm_text(confirmation.get("confirmed_text"))
    issue_shape = {(issue["issue_family"], issue["issue_kind"]) for issue in issues}

    block_reasons: list[str] = []
    if decision.get("human_decision") not in APPROVED_DECISIONS:
        block_reasons.append("human_decision_not_approved")
    if decision.get("audit_source") != "gender_semantic_literal_residue_audit_v1":
        block_reasons.append("unexpected_audit_source")
    if not candidate_text:
        block_reasons.append("missing_candidate_text")
    if not state:
        block_reasons.append("missing_segment_state")
    if state and state.get("state_group") != "pending":
        block_reasons.append("state_not_pending")
    if state and state.get("final_state") != "reopen_auto_confirmed_autofix":
        block_reasons.append("final_state_not_reopen_auto_confirmed_autofix")
    if state and int(state.get("needs_output_apply") or 0) != 0:
        block_reasons.append("state_needs_output_apply")
    if state and int(state.get("confirmed_matches_output") or 0) != 1:
        block_reasons.append("state_confirmation_output_mismatch")
    if not output_text:
        block_reasons.append("missing_output_text")
    if not confirmed_text:
        block_reasons.append("missing_confirmation_text")
    if output_text != candidate_text:
        block_reasons.append("output_not_equal_candidate")
    if confirmed_text != candidate_text:
        block_reasons.append("confirmation_not_equal_candidate")
    if int(confirmation.get("locked") or 0) != 1:
        block_reasons.append("confirmation_not_locked")
    if confirmation.get("confirmation_level") != "human_confirmed":
        block_reasons.append("confirmation_level_not_human_confirmed")
    if confirmation.get("confirmation_source") not in ALLOWED_CONFIRMATION_SOURCES:
        block_reasons.append("confirmation_source_not_allowed")
    if confirmation.get("confirmation_label") not in ALLOWED_CONFIRMATION_LABELS:
        block_reasons.append("confirmation_label_not_correct")
    if token_signature(output_text) != token_signature(candidate_text):
        block_reasons.append("output_candidate_token_signature_mismatch")
    if token_signature(confirmed_text) != token_signature(candidate_text):
        block_reasons.append("confirmation_candidate_token_signature_mismatch")
    if issue_shape and issue_shape != ALLOWED_ISSUE_SHAPES:
        block_reasons.append("open_issue_shape_not_allowed")
    if source and decision.get("relative_path") and source.get("relative_path") != decision.get("relative_path"):
        block_reasons.append("source_relative_path_mismatch")
    if source and decision.get("source_key") and source.get("source_key") != decision.get("source_key"):
        block_reasons.append("source_key_mismatch")

    return {
        "segment_id": segment_id,
        "relative_path": decision.get("relative_path") or source.get("relative_path") or state.get("relative_path"),
        "source_key": decision.get("source_key") or source.get("source_key") or state.get("source_key"),
        "source_line_number": decision.get("source_line_number") or source.get("source_line_number"),
        "candidate_text": candidate_text,
        "output_text": output_text,
        "confirmed_text": confirmed_text,
        "confirmation_id": confirmation.get("id"),
        "candidate_id": confirmation.get("candidate_id"),
        "confirmation_level": confirmation.get("confirmation_level"),
        "confirmation_source": confirmation.get("confirmation_source"),
        "confirmation_label": confirmation.get("confirmation_label"),
        "confirmation_locked": int(confirmation.get("locked") or 0),
        "state_run_final_state": state.get("final_state"),
        "state_run_state_group": state.get("state_group"),
        "state_run_needs_output_apply": int(state.get("needs_output_apply") or 0) if state else None,
        "state_run_confirmed_matches_output": int(state.get("confirmed_matches_output") or 0) if state else None,
        "issue_shape": sorted(f"{family}:{kind}" for family, kind in issue_shape),
        "issue_count": len(issues),
        "high_issue_count": sum(
            1
            for issue in issues
            if str(issue.get("issue_severity") or "").lower() in {"high", "error", "critical"}
        ),
        "policy_action": POLICY_ACTION,
        "policy_allowed": 0 if block_reasons else 1,
        "block_reason": ";".join(block_reasons),
        "human_reason": decision.get("human_reason"),
        "audit_source": decision.get("audit_source"),
    }


def collect_rows(
    conn: sqlite3.Connection,
    *,
    decisions_path: Path,
    state_run_id: int,
    ledger_run_id: int,
) -> tuple[list[dict[str, Any]], int, int]:
    decisions = read_jsonl(decisions_path)
    approved = [row for row in decisions if row.get("human_decision") in APPROVED_DECISIONS]
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for decision in approved:
        segment_id = int(decision["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        context = fetch_segment_context(
            conn,
            segment_id=segment_id,
            state_run_id=state_run_id,
            ledger_run_id=ledger_run_id,
        )
        rows.append(evaluate(decision, context))
    return rows, len(decisions), len(approved)


def output_paths() -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_human_confirmed_post_apply_lifecycle_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    rows: list[dict[str, Any]],
    mode: str,
    decisions_path: Path,
    state_run_id: int,
    ledger_run_id: int,
    policy_run_id: int | None,
    total_decisions: int,
    approved_decisions: int,
) -> None:
    fieldnames = [
        "policy_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "confirmation_level",
        "confirmation_source",
        "confirmation_label",
        "confirmation_locked",
        "issue_count",
        "high_issue_count",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
    lines = [
        "Human-confirmed post-apply lifecycle policy",
        f"rule_version={RULE_VERSION}",
        f"policy_name={POLICY_NAME}",
        f"policy_action={POLICY_ACTION}",
        f"mode={mode}",
        f"policy_run_id={policy_run_id or ''}",
        f"decisions_path={decisions_path}",
        f"segment_state_run_id={state_run_id}",
        f"ledger_run_id={ledger_run_id}",
        "",
        "Summary:",
        f"- human_decision_rows: {total_decisions}",
        f"- approved_decision_rows: {approved_decisions}",
        f"- evaluated: {len(rows)}",
        f"- released: {sum(1 for row in rows if row['policy_allowed'])}",
        f"- blocked: {sum(1 for row in rows if not row['policy_allowed'])}",
        *[f"- {key}: {value}" for key, value in counts.most_common()],
        "",
        "Released:",
    ]
    released = [row for row in rows if row["policy_allowed"]]
    if released:
        for row in released:
            lines.append(f"- {row['segment_id']} | {row['relative_path']} | {row['source_key']}")
    else:
        lines.append("- none")
    blocked = [row for row in rows if not row["policy_allowed"]]
    lines.append("")
    lines.append("Blocked:")
    if blocked:
        for row in blocked:
            lines.append(f"- {row['segment_id']} | {row['block_reason']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This policy does not write source/output files and does not modify confirmations.",
            "- It only materializes lifecycle closure evidence for protected repairs already human-confirmed and locked.",
            "- Segment-state snapshot/reindex must be planned separately.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_policy(
    conn: sqlite3.Connection,
    *,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    state_run_id: int,
    ledger_run_id: int,
    policy_status: str,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    released = sum(1 for row in rows if row["policy_allowed"])
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
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            POLICY_NAME,
            LABEL_FAMILY,
            policy_status,
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
        item_cursor = conn.execute(
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
                "output_confirmation_candidate_exact_match" if row["policy_allowed"] else "blocked",
                "ok" if row["policy_allowed"] else "not_evaluated",
                int(row["issue_count"] or 0),
                int(row["high_issue_count"] or 0),
                0.0,
                json.dumps(
                    {
                        "candidate_id": row.get("candidate_id"),
                        "confirmation_id": row.get("confirmation_id"),
                        "confirmation_source": row.get("confirmation_source"),
                        "human_reason": row.get("human_reason"),
                        "audit_source": row.get("audit_source"),
                        "issue_shape": row.get("issue_shape"),
                        "source_segment_state_run_id": state_run_id,
                        "source_ledger_run_id": ledger_run_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        row["policy_item_id"] = int(item_cursor.lastrowid)
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions-jsonl")
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--status", choices=["active", "shadow"], default="active")
    args = parser.parse_args()

    decisions_path = Path(args.decisions_jsonl) if args.decisions_jsonl else latest_file("*gender_semantic_literal_residue_human_decisions.jsonl")
    if not decisions_path:
        raise SystemExit("missing gender semantic literal residue human decisions JSONL")

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            state_run_id = args.segment_state_run_id or latest_run_id(conn, "segment_state_runs")
            ledger_run_id = args.ledger_run_id or latest_run_id(conn, "ml_issue_ledger_runs")
            rows, total_decisions, approved_decisions = collect_rows(
                conn,
                decisions_path=decisions_path,
                state_run_id=state_run_id,
                ledger_run_id=ledger_run_id,
            )
            txt_path, csv_path, jsonl_path = output_paths()
            policy_run_id = apply_policy(
                conn,
                rows=rows,
                txt_path=txt_path,
                csv_path=csv_path,
                jsonl_path=jsonl_path,
                state_run_id=state_run_id,
                ledger_run_id=ledger_run_id,
                policy_status=args.status,
            )
            write_reports(
                txt_path=txt_path,
                csv_path=csv_path,
                jsonl_path=jsonl_path,
                rows=rows,
                mode="apply",
                decisions_path=decisions_path,
                state_run_id=state_run_id,
                ledger_run_id=ledger_run_id,
                policy_run_id=policy_run_id,
                total_decisions=total_decisions,
                approved_decisions=approved_decisions,
            )
            conn.commit()
    else:
        with readonly_conn() as conn:
            state_run_id = args.segment_state_run_id or latest_run_id(conn, "segment_state_runs")
            ledger_run_id = args.ledger_run_id or latest_run_id(conn, "ml_issue_ledger_runs")
            rows, total_decisions, approved_decisions = collect_rows(
                conn,
                decisions_path=decisions_path,
                state_run_id=state_run_id,
                ledger_run_id=ledger_run_id,
            )
            txt_path, csv_path, jsonl_path = output_paths()
            policy_run_id = None
            write_reports(
                txt_path=txt_path,
                csv_path=csv_path,
                jsonl_path=jsonl_path,
                rows=rows,
                mode="dry_run",
                decisions_path=decisions_path,
                state_run_id=state_run_id,
                ledger_run_id=ledger_run_id,
                policy_run_id=policy_run_id,
                total_decisions=total_decisions,
                approved_decisions=approved_decisions,
            )

    released = sum(1 for row in rows if row["policy_allowed"])
    blocked = len(rows) - released
    print(f"txt={txt_path}")
    print(f"csv={csv_path}")
    print(f"jsonl={jsonl_path}")
    print(f"mode={'apply' if args.apply else 'dry_run'}")
    print(f"policy_run_id={policy_run_id or ''}")
    print(f"evaluated={len(rows)}")
    print(f"released={released}")
    print(f"blocked={blocked}")
    print("writes_output=false")
    print("runs_lifecycle=false")


if __name__ == "__main__":
    main()
