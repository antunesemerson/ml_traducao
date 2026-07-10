from __future__ import annotations

import json
import sqlite3
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_closure_debt_diagnostic_v1"
FROM_RUN_ID = 512
TO_RUN_ID = 526


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
    return conn


def token_surface(text: str) -> str:
    tokens = protected_tokens(text)
    if not tokens:
        return "plain_text"
    token_names = " ".join(tokens)
    if "\\n" in tokens:
        return "multiline"
    if "Select_CString" in token_names or "SelectLocalization" in token_names:
        return "dynamic_select"
    if any(part in token_names for part in [".Get", ".Custom", "ROOT.", "scope:", "GetScriptValue"]):
        return "dynamic_getter"
    return "light_token"


def confirmation_bucket(row: dict[str, Any]) -> str:
    level = str(row.get("confirmation_level") or "")
    source = str(row.get("confirmation_source") or "")
    label = str(row.get("confirmation_label") or "")
    if int(row.get("locked") or 0) == 1 and level == "human_confirmed":
        return "human_locked"
    if level == "human_confirmed":
        return "human_confirmed_unlocked"
    if "auto" in level or "auto" in source or "auto" in label:
        return "auto_confirmed"
    return "confirmation_source_unclear"


def safety_class(row: dict[str, Any]) -> str:
    bucket = confirmation_bucket(row)
    issue_count = int(row.get("open_issue_count") or 0)
    high_issue_count = int(row.get("high_issue_count") or 0)
    lifecycle_allowed = int(row.get("lifecycle_policy_allowed") or 0)
    surface = token_surface(str(row.get("confirmed_text") or row.get("output_text") or ""))
    if bucket in {"human_locked", "human_confirmed_unlocked"} and issue_count == 0:
        return "human_locked_or_confirmed_output_equal_no_open_issue"
    if bucket == "auto_confirmed" and issue_count == 0 and surface in {"plain_text", "light_token"}:
        return "auto_confirmed_trusted_surface_output_equal"
    if lifecycle_allowed == 1:
        return "lifecycle_policy_allowed_but_not_closed_current"
    if high_issue_count > 0:
        return "hold_open_high_issue"
    if issue_count > 0:
        return "hold_open_issue"
    if surface not in {"plain_text", "light_token"}:
        return "hold_structural_or_dynamic_risk"
    return "hold_missing_policy_or_weak_source"


def recommended_bridge(row: dict[str, Any], classification: str) -> str:
    label = str(row.get("confirmation_label") or "unknown_label")
    bucket = confirmation_bucket(row)
    if classification == "human_locked_or_confirmed_output_equal_no_open_issue":
        return f"bridge_human_confirmed_equal_output::{label}"
    if classification == "auto_confirmed_trusted_surface_output_equal":
        return f"bridge_auto_confirmed_equal_output::{label}"
    if classification == "lifecycle_policy_allowed_but_not_closed_current":
        return "debug_existing_lifecycle_policy_not_consumed"
    return f"hold::{bucket}::{token_surface(str(row.get('confirmed_text') or row.get('output_text') or ''))}"


def fetch_issue_summary(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    summaries: dict[int, dict[str, Any]] = {}
    for index in range(0, len(segment_ids), 800):
        chunk = segment_ids[index : index + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                segment_id,
                COUNT(*) AS issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
                GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds
            FROM ml_issue_ledger_items
            WHERE segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            GROUP BY segment_id
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            summaries[int(row["segment_id"])] = {
                "open_issue_count": int(row["issue_count"] or 0),
                "high_issue_count": int(row["high_issue_count"] or 0),
                "issue_families": row["issue_families"],
                "issue_kinds": row["issue_kinds"],
            }
    return summaries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only closure debt diagnostic.")
    parser.add_argument("--from-run-id", type=int, default=FROM_RUN_ID)
    parser.add_argument("--to-run-id", type=int, default=TO_RUN_ID)
    return parser.parse_args()


def fetch_rows(conn: sqlite3.Connection, from_run_id: int, to_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            old.segment_id,
            old.relative_path,
            old.source_key,
            old.source_line_number,
            old.final_state AS from_final_state,
            old.state_group AS from_state_group,
            old.is_closed AS from_is_closed,
            cur.final_state AS to_final_state,
            cur.state_group AS to_state_group,
            cur.is_closed AS to_is_closed,
            cur.confirmed_matches_output,
            cur.needs_output_apply,
            cur.lifecycle_policy_allowed,
            cur.lifecycle_policy_action,
            cur.confirmation_level,
            cur.confirmation_label,
            cur.locked,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS output_text,
            c.confirmed_text,
            c.confirmation_source
        FROM segment_state_items old
        JOIN segment_state_items cur ON cur.segment_id = old.segment_id AND cur.run_id = ?
        JOIN source_segments s ON s.id = old.segment_id
        LEFT JOIN output_segments o ON o.segment_id = old.segment_id
        LEFT JOIN segment_confirmations c ON c.segment_id = old.segment_id
        WHERE old.run_id = ?
          AND old.is_closed = 1
          AND cur.state_group = 'pending'
          AND cur.confirmed_matches_output = 1
          AND cur.needs_output_apply = 0
        ORDER BY cur.confirmation_level, cur.confirmation_label, old.segment_id
        """,
        (to_run_id, from_run_id),
    ).fetchall()
    output = [dict(row) for row in rows]
    issue_summary = fetch_issue_summary(conn, [int(row["segment_id"]) for row in output])
    for row in output:
        row.update(
            issue_summary.get(
                int(row["segment_id"]),
                {"open_issue_count": 0, "high_issue_count": 0, "issue_families": None, "issue_kinds": None},
            )
        )
    return output


def build_records(rows: list[dict[str, Any]], from_run_id: int, to_run_id: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        classification = safety_class(row)
        records.append(
            {
                "source": SOURCE,
                "from_run_id": from_run_id,
                "to_run_id": to_run_id,
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "from_final_state": row["from_final_state"],
                "to_final_state": row["to_final_state"],
                "confirmed_matches_output": int(row["confirmed_matches_output"] or 0),
                "needs_output_apply": int(row["needs_output_apply"] or 0),
                "lifecycle_policy_allowed": int(row["lifecycle_policy_allowed"] or 0),
                "lifecycle_policy_action": row["lifecycle_policy_action"],
                "confirmation_level": row["confirmation_level"],
                "confirmation_source": row["confirmation_source"],
                "confirmation_label": row["confirmation_label"],
                "locked": int(row["locked"] or 0),
                "confirmation_bucket": confirmation_bucket(row),
                "open_issue_count": int(row["open_issue_count"] or 0),
                "high_issue_count": int(row["high_issue_count"] or 0),
                "issue_families": row["issue_families"],
                "issue_kinds": row["issue_kinds"],
                "token_surface": token_surface(str(row.get("confirmed_text") or row.get("output_text") or "")),
                "closure_debt_classification": classification,
                "recommended_bridge_or_policy_family": recommended_bridge(row, classification),
                "english_text": row["english_text"],
                "spanish_text": row["spanish_text"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
                "candidate_generation_allowed": False,
                "apply_allowed": False,
                "lifecycle_run_allowed_now": False,
            }
        )
    return records


def write_reports(records: list[dict[str, Any]], from_run_id: int, to_run_id: int) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_closure_debt_diagnostic_{from_run_id}_{to_run_id}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    classification_counts = Counter(record["closure_debt_classification"] for record in records)
    bridge_counts = Counter(record["recommended_bridge_or_policy_family"] for record in records)
    bucket_counts = Counter(record["confirmation_bucket"] for record in records)
    surface_counts = Counter(record["token_surface"] for record in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_closure_debt_diagnostic",
        "from_run_id": from_run_id,
        "to_run_id": to_run_id,
        "record_count": len(records),
        "classification_counts": dict(sorted(classification_counts.items())),
        "confirmation_bucket_counts": dict(sorted(bucket_counts.items())),
        "token_surface_counts": dict(sorted(surface_counts.items())),
        "top_recommended_bridge_or_policy_families": dict(bridge_counts.most_common(30)),
        "lifecycle_policy_allowed_but_not_closed_count": classification_counts.get("lifecycle_policy_allowed_but_not_closed_current", 0),
        "safe_human_lifecycle_bridge_count": classification_counts.get("human_locked_or_confirmed_output_equal_no_open_issue", 0),
        "safe_auto_lifecycle_bridge_count": classification_counts.get("auto_confirmed_trusted_surface_output_equal", 0),
        "hold_count": sum(
            count for key, count in classification_counts.items() if key.startswith("hold_")
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Materialize lifecycle bridge families only for human/auto confirmed equal-output groups with no open issues; investigate existing lifecycle_policy_allowed rows separately before running lifecycle."
        ),
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"Domain policy vote candidate closure debt diagnostic {from_run_id} -> {to_run_id}",
        f"record_count={len(records)}",
        "",
        "Classification counts:",
    ]
    for key, count in sorted(classification_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Confirmation buckets:"])
    for key, count in sorted(bucket_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Token surfaces:"])
    for key, count in sorted(surface_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Top bridge/policy families:"])
    for key, count in bridge_counts.most_common(20):
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "segment_state_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "source_changed=false",
            "output_changed=false",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    with connect_readonly() as conn:
        rows = fetch_rows(conn, args.from_run_id, args.to_run_id)
    records = build_records(rows, args.from_run_id, args.to_run_id)
    txt_path, jsonl_path, summary_path = write_reports(records, args.from_run_id, args.to_run_id)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={len(records)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
