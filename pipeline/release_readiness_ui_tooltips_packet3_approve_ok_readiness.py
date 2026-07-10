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
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_packet3_approve_ok_readiness_v1"
PACKET_JSONL = Path("reports/20260703_122244_761708_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 574
LEDGER_RUN_ID = 76
EXPECTED_COUNT = 30


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only readiness for UI/tooltips packet3 approve_already_ok rows.")
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    parser.add_argument("--segment-state-run-id", type=int, default=SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def read_packet(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if row.get("suggested_human_decision") == "approve_already_ok":
                rows.append(row)
    return rows


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical_localization_text(left or "") == canonical_localization_text(right or "")


def structure_ok(text: str | None) -> bool:
    return "\n" not in (text or "") and "\r" not in (text or "")


def fetch_live(conn: sqlite3.Connection, segment_ids: list[int], state_run_id: int, ledger_run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        WITH open_issues AS (
            SELECT
              segment_id,
              COUNT(*) AS open_issue_count,
              SUM(CASE WHEN lower(COALESCE(issue_severity,'')) IN ('high','critical','error') THEN 1 ELSE 0 END) AS high_issue_count,
              GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
              GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            GROUP BY segment_id
        )
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_key,
          s.source_line_number,
          s.spanish_text,
          s.english_text,
          o.portuguese_text AS output_text,
          c.confirmed_text,
          c.confirmation_level,
          c.confirmation_source,
          c.confirmation_label,
          c.locked,
          state.final_state,
          state.review_state,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.lifecycle_policy_allowed,
          state.lifecycle_policy_action,
          COALESCE(open_issues.open_issue_count, 0) AS open_issue_count,
          COALESCE(open_issues.high_issue_count, 0) AS high_issue_count,
          COALESCE(open_issues.issue_families, '') AS issue_families,
          COALESCE(open_issues.issue_kinds, '') AS issue_kinds
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        JOIN segment_state_items state ON state.segment_id = s.id AND state.run_id = ?
        LEFT JOIN open_issues ON open_issues.segment_id = s.id
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        [ledger_run_id, *segment_ids, state_run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def issue_close_class(row: dict[str, Any]) -> str:
    if int(row.get("high_issue_count") or 0) > 0:
        return "blocked_high_issue"
    if int(row.get("open_issue_count") or 0) == 0:
        return "no_open_issue"
    return "closable_by_human_approve_already_ok"


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    packet_rows = read_packet(args.packet_jsonl)
    if len(packet_rows) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count} approve_already_ok rows, got {len(packet_rows)}")
    segment_ids = sorted(int(row["segment_id"]) for row in packet_rows)
    by_id = {int(row["segment_id"]): row for row in packet_rows}
    with connect_readonly() as conn:
        live = fetch_live(conn, segment_ids, args.segment_state_run_id, args.ledger_run_id)
    records: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        packet = by_id[segment_id]
        row = live.get(segment_id) or {}
        output_text = row.get("output_text") or ""
        confirmed_text = row.get("confirmed_text") or packet.get("confirmed_text") or output_text
        packet_text = packet.get("output_text") or packet.get("confirmed_text") or ""
        reasons: list[str] = []
        output_matches_packet = canonical_equal(output_text, packet_text)
        output_matches_confirmed = canonical_equal(output_text, confirmed_text)
        token_ok = Counter(protected_tokens(output_text)) == Counter(protected_tokens(confirmed_text))
        struct_ok = structure_ok(output_text) and structure_ok(confirmed_text)
        if not row:
            reasons.append("missing_live_row")
        if not output_matches_packet:
            reasons.append("output_differs_from_packet")
        if not output_matches_confirmed:
            reasons.append("output_differs_from_confirmed")
        if int(row.get("needs_output_apply") or 0) != 0:
            reasons.append("needs_output_apply")
        if int(row.get("high_issue_count") or 0) != 0:
            reasons.append("high_issue_present")
        if not token_ok:
            reasons.append("token_integrity_mismatch")
        if not struct_ok:
            reasons.append("structure_integrity_mismatch")
        records.append(
            {
                "source": SOURCE,
                "record_type": "approve_already_ok_readiness",
                "segment_id": segment_id,
                "review_index": packet.get("review_index"),
                "relative_path": row.get("relative_path") or packet.get("relative_path"),
                "source_key": row.get("source_key") or packet.get("source_key"),
                "token_surface": packet.get("token_surface"),
                "packet_risk_type": packet.get("packet_risk_type"),
                "output_text": output_text,
                "confirmed_text": row.get("confirmed_text"),
                "packet_output_text": packet_text,
                "final_state": row.get("final_state"),
                "review_state": row.get("review_state"),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
                "issue_closure_class": issue_close_class(row),
                "output_matches_packet": output_matches_packet,
                "output_matches_confirmed": output_matches_confirmed,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": struct_ok,
                "canonical_l10n_ok": output_matches_packet and output_matches_confirmed,
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "learning_ingest_count": 0,
                "issue_closure_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def write_reports(records: list[dict[str, Any]], args: argparse.Namespace) -> tuple[Path, Path, Path]:
    ready = [record for record in records if record["status"] == "ready"]
    blocked = [record for record in records if record["status"] != "ready"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_approve_already_ok_readiness",
        "packet_jsonl": str(args.packet_jsonl),
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "ready_segment_ids": [int(record["segment_id"]) for record in ready],
        "blocked_segment_ids": [int(record["segment_id"]) for record in blocked],
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "issue_closure_class_counts": dict(Counter(record["issue_closure_class"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "risk_type_counts": dict(Counter(record["packet_risk_type"] for record in records).most_common()),
        "open_issue_count_total": sum(int(record["open_issue_count"]) for record in records),
        "high_issue_count_total": sum(int(record["high_issue_count"]) for record in records),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_ok_count": sum(1 for record in records if record["canonical_l10n_ok"]),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "If ready_count equals record_count, proceed in a separate cycle with learning ingest, learn-feedback, issue closure, materializer dry-run/apply, then segment-state + delta."
            if not blocked
            else "Resolve blocked rows before any learning/lifecycle action."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet3_approve_ok_readiness"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "UI/tooltips packet3 approve_already_ok readiness",
        f"record_count={summary['record_count']}",
        f"ready_count={summary['ready_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        f"issue_closure_class_counts={json.dumps(summary['issue_closure_class_counts'], ensure_ascii=False, sort_keys=True)}",
        f"open_issue_count_total={summary['open_issue_count_total']}",
        f"high_issue_count_total={summary['high_issue_count_total']}",
        f"token_integrity_ok_count={summary['token_integrity_ok_count']}",
        f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}",
        f"canonical_l10n_ok_count={summary['canonical_l10n_ok_count']}",
        "candidate_generation_count=0",
        "apply_count=0",
        "learning_ingest_count=0",
        "issue_closure_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(args)
    txt_path, jsonl_path, summary_path = write_reports(records, args)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"issue_closure_class_counts={json.dumps(summary['issue_closure_class_counts'], ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
