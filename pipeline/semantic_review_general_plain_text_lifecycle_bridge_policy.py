from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "semantic_review_general_plain_text_lifecycle_bridge_policy_v1"
POLICY_NAME = "semantic_review_general_plain_text_reopen_lifecycle"
POLICY_ACTION = "close_reopen_semantic_review_general_plain_text_lifecycle"
LABEL_FAMILY = "semantic_review_general_plain_text_reopen"
SEGMENT_STATE_RUN_ID = 404
LEDGER_RUN_ID = 76
DEFAULT_REVIEW_JSONL = "reports/20260624_215106_626147_semantic_review_general_plain_text_policy_review.jsonl"
ALLOWED_DECISION = "plain_text_lifecycle_candidate"

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|(?:Get|Build|Add|LessThan|StringIsEmpty|SelectLocalization)[A-Za-z0-9_]*\("
)
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:adem[aá]s|ahora|alg[uú]n|aunque|caballero|cielos|consejo|coste|cualquier|"
    r"elige|eres|hacerte|hacerle|maravilloso|mientras|ning[uú]n|puede|pueden|"
    r"quieres|siguiente|tambi[eé]n|tus|vuestro|vuestra|vuestras|vuestros)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_path(value: str | Path) -> Path:
    return db.project_path(str(value))


def output_paths(mode: str) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_general_plain_text_lifecycle_bridge_policy_{mode}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
    )


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


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


def has_token_or_dynamic(value: str) -> bool:
    return bool(TOKEN_RE.search(value) or "[" in value or "]" in value or "$" in value or "#!" in value)


def has_spanish_residue(value: str) -> bool:
    return bool(SPANISH_RESIDUE_RE.search(value))


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any]:
    state = fetch_one(
        conn,
        """
        SELECT segment_id, state_group, final_state, review_state, needs_output_apply,
               confirmed_matches_output, is_closed, locked
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (SEGMENT_STATE_RUN_ID, segment_id),
    )
    source = fetch_one(
        conn,
        """
        SELECT id AS segment_id, relative_path, source_key, source_line_number, old_text
        FROM source_segments
        WHERE id = ?
        """,
        (segment_id,),
    )
    output = fetch_one(
        conn,
        """
        SELECT segment_id, portuguese_text
        FROM output_segments
        WHERE segment_id = ?
        """,
        (segment_id,),
    )
    issues = [
        dict(row)
        for row in conn.execute(
            """
            SELECT issue_family, issue_kind, issue_severity, agent_key, route_status, proposed_action, status
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id = ?
              AND status = 'open'
            ORDER BY issue_family, issue_kind, agent_key
            """,
            (LEDGER_RUN_ID, segment_id),
        )
    ]
    return {"state": state, "source": source, "output": output, "issues": issues}


def evaluate(review: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(review["segment_id"])
    state = context["state"] or {}
    source = context["source"] or {}
    output = context["output"] or {}
    issues = context["issues"]
    review_current = str(review.get("current_output_text") or "")
    old_text = str(source.get("old_text") or "")
    output_text = str(output.get("portuguese_text") or "")
    issue_shape = {(issue.get("issue_family"), issue.get("issue_kind"), issue.get("agent_key")) for issue in issues}
    high_issue_count = sum(
        1
        for issue in issues
        if str(issue.get("issue_severity") or "").lower() in {"high", "error", "critical"}
    )
    block_reasons: list[str] = []
    if review.get("decision") != ALLOWED_DECISION:
        block_reasons.append("review_decision_not_lifecycle_candidate")
    if review.get("requires_lifecycle_later") is not True:
        block_reasons.append("review_did_not_require_lifecycle_later")
    if review.get("requires_apply_later") is True:
        block_reasons.append("review_requires_apply_later")
    if review.get("false_safe_risk") is True:
        block_reasons.append("review_false_safe_risk")
    if not state:
        block_reasons.append("missing_segment_state")
    if state and state.get("state_group") != "pending":
        block_reasons.append("state_not_pending")
    if state and state.get("final_state") != "reopen_auto_confirmed_autofix":
        block_reasons.append("state_not_reopen_auto_confirmed_autofix")
    if state and int(state.get("needs_output_apply") or 0) != 0:
        block_reasons.append("state_needs_output_apply")
    if state and int(state.get("confirmed_matches_output") or 0) != 1:
        block_reasons.append("state_confirmation_output_mismatch")
    if int(review.get("needs_output_apply") or 0) != 0:
        block_reasons.append("review_needs_output_apply")
    if int(review.get("confirmed_matches_output") or 0) != 1:
        block_reasons.append("review_confirmation_output_mismatch")
    if not output_text:
        block_reasons.append("missing_output_text")
    if output_text != old_text:
        block_reasons.append("output_not_equal_source_old_text")
    if has_token_or_dynamic(output_text):
        block_reasons.append("token_or_dynamic_marker_found")
    if has_spanish_residue(output_text) or review.get("has_spanish_residue_signal") is True:
        block_reasons.append("spanish_residue_signal")
    allowed_issue_shape = {
        ("semantic_review_router", "needs_human_or_semantic_conflict", "micro_semantic_review_router"),
        ("autofix_unknown_microagent", "needs_autofix_unclassified", "micro_autofix_unknown_router"),
    }
    if not any(issue[0] == "semantic_review_router" for issue in issue_shape):
        block_reasons.append("missing_semantic_review_router_issue")
    if any(issue not in allowed_issue_shape for issue in issue_shape):
        block_reasons.append("unexpected_open_issue_shape")
    if high_issue_count:
        block_reasons.append("high_issue_present")
    if source and review.get("relative_path") != source.get("relative_path"):
        block_reasons.append("relative_path_mismatch")
    if source and review.get("source_key") != source.get("source_key"):
        block_reasons.append("source_key_mismatch")

    allowed = not block_reasons
    return {
        "segment_id": segment_id,
        "relative_path": review.get("relative_path") or source.get("relative_path"),
        "source_key": review.get("source_key") or source.get("source_key"),
        "source_line_number": review.get("source_line_number") or source.get("source_line_number"),
        "decision": review.get("decision"),
        "policy_action": POLICY_ACTION,
        "policy_allowed": 1 if allowed else 0,
        "block_reason": ";".join(block_reasons),
        "output_match_kind": "output_matches_source_old_plain_text" if allowed else "blocked",
        "token_status": "plain_text_no_tokens" if allowed else "not_evaluated",
        "issue_count": len(issues),
        "high_issue_count": high_issue_count,
        "review_reason": review.get("reason"),
        "text_length": len(output_text or review_current),
        "requires_lifecycle_later": bool(review.get("requires_lifecycle_later")),
        "requires_apply_later": bool(review.get("requires_apply_later")),
        "false_safe_risk": bool(review.get("false_safe_risk")),
    }


def collect_rows(conn: sqlite3.Connection, review_path: Path) -> tuple[list[dict[str, Any]], int]:
    review_rows = read_jsonl(review_path)
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for review in review_rows:
        segment_id = int(review["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(evaluate(review, fetch_context(conn, segment_id)))
    return rows, len(review_rows)


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    summary_path: Path,
    rows: list[dict[str, Any]],
    mode: str,
    review_path: Path,
    policy_run_id: int | None,
    raw_review_rows: int,
) -> None:
    fields = [
        "policy_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "decision",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "output_match_kind",
        "token_status",
        "issue_count",
        "high_issue_count",
        "requires_lifecycle_later",
        "requires_apply_later",
        "false_safe_risk",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
    released = sum(1 for row in rows if row["policy_allowed"])
    blocked = len(rows) - released
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "policy_run_id": policy_run_id,
        "review_jsonl": str(review_path),
        "raw_review_rows": raw_review_rows,
        "evaluated_rows": len(rows),
        "released_count": released,
        "blocked_count": blocked,
        "policy_allowed_all_guards": blocked == 105 and released == 135,
        "apply_ready_now": 0,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "production_full_recommended_now": False,
        "segment_state_recommended_next": mode == "apply" and released > 0 and blocked == 105,
        "counts": [{"key": key, "count": value} for key, value in counts.most_common()],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Semantic review general plain-text lifecycle bridge policy",
        f"rule_version={RULE_VERSION}",
        f"policy_name={POLICY_NAME}",
        f"policy_action={POLICY_ACTION}",
        f"mode={mode}",
        f"policy_run_id={policy_run_id or ''}",
        f"review_jsonl={review_path}",
        f"raw_review_rows={raw_review_rows}",
        "",
        "Summary:",
        f"- evaluated: {len(rows)}",
        f"- released: {released}",
        f"- blocked: {blocked}",
        f"- apply_ready_now: 0",
        f"- production_full_recommended_now: false",
        "",
        "Counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in counts.most_common())
    lines.extend(
        [
            "",
            "Safety note:",
            "- This policy does not write source/output files and does not modify confirmations.",
            "- It only materializes lifecycle evidence for reviewed plain-text false reopens.",
            "- Segment-state must be run separately after architecture approval.",
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
    policy_status: str,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    released = sum(1 for row in rows if row["policy_allowed"])
    blocked = len(rows) - released
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
            blocked,
            blocked,
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
                row["decision"],
                POLICY_ACTION,
                int(row["policy_allowed"]),
                row["block_reason"],
                row["output_match_kind"],
                row["token_status"],
                int(row["issue_count"] or 0),
                int(row["high_issue_count"] or 0),
                0.0,
                json.dumps(
                    {
                        "review_reason": row.get("review_reason"),
                        "text_length": row.get("text_length"),
                        "source_segment_state_run_id": SEGMENT_STATE_RUN_ID,
                        "source_ledger_run_id": LEDGER_RUN_ID,
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
    parser.add_argument("--review-jsonl", default=DEFAULT_REVIEW_JSONL)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--status", choices=["active", "shadow"], default="active")
    args = parser.parse_args()

    review_path = project_path(args.review_jsonl)
    if not review_path.exists():
        raise SystemExit(f"missing review JSONL: {review_path}")
    mode = "apply" if args.apply else "dry_run"
    txt_path, csv_path, jsonl_path, summary_path = output_paths(mode)

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            rows, raw_count = collect_rows(conn, review_path)
            released = sum(1 for row in rows if row["policy_allowed"])
            blocked = len(rows) - released
            if released != 135 or blocked != 105:
                raise SystemExit(f"guard count mismatch: released={released}, blocked={blocked}")
            policy_run_id = apply_policy(
                conn,
                rows=rows,
                txt_path=txt_path,
                csv_path=csv_path,
                jsonl_path=jsonl_path,
                policy_status=args.status,
            )
            write_reports(
                txt_path=txt_path,
                csv_path=csv_path,
                jsonl_path=jsonl_path,
                summary_path=summary_path,
                rows=rows,
                mode=mode,
                review_path=review_path,
                policy_run_id=policy_run_id,
                raw_review_rows=raw_count,
            )
            conn.commit()
    else:
        with connect_readonly() as conn:
            rows, raw_count = collect_rows(conn, review_path)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            summary_path=summary_path,
            rows=rows,
            mode=mode,
            review_path=review_path,
            policy_run_id=None,
            raw_review_rows=raw_count,
        )

    print(txt_path)
    print(csv_path)
    print(jsonl_path)
    print(summary_path)


if __name__ == "__main__":
    main()
