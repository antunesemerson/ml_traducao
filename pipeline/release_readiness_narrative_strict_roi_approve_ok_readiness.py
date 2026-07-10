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
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_narrative_strict_roi_approve_ok_readiness_v1"
DEFAULT_PACKET_JSONL = Path("reports/20260703_145023_784855_release_readiness_narrative_strict_roi_human_packet.jsonl")
DEFAULT_SEGMENT_STATE_RUN_ID = 578
DEFAULT_LEDGER_RUN_ID = 76
EXPECTED_COUNT = 30

DYNAMIC_OR_PARSER_LATER_RE = re.compile(
    r"(Concept\(|Glossary\(|SelectLocalization|Select_CString|MakeScope|ScriptValue|CustomLoc|ROOT\.|\.Get[A-Za-z_]*\(|\$EFFECT_LIST_BULLET\$)"
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only readiness validation for narrative strict ROI approve_already_ok rows."
    )
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
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
            if row.get("suggested_human_decision") in {"approve_already_ok", "approve_already_ok_ready"}:
                rows.append(row)
    return rows


def canonical(value: str | None) -> str:
    return canonical_localization_text(value or "")


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical(left) == canonical(right)


def structure_ok(text: str | None, token_surface: str | None) -> bool:
    value = text or ""
    if token_surface in {"plain_text", "light_token"}:
        return "\n" not in value and "\r" not in value
    return False


def has_dynamic_or_parser_later_surface(*values: str | None) -> bool:
    blob = "\n".join(value or "" for value in values)
    return bool(DYNAMIC_OR_PARSER_LATER_RE.search(blob))


def fetch_live(
    conn: sqlite3.Connection,
    segment_ids: list[int],
    state_run_id: int,
    ledger_run_id: int,
) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        WITH latest_confirmation AS (
          SELECT c.*
          FROM segment_confirmations c
          JOIN (
            SELECT segment_id, MAX(id) AS max_id
            FROM segment_confirmations
            WHERE segment_id IN ({placeholders})
            GROUP BY segment_id
          ) latest ON latest.segment_id = c.segment_id AND latest.max_id = c.id
        ),
        open_issues AS (
            SELECT
              segment_id,
              COUNT(*) AS open_issue_count,
              SUM(CASE WHEN lower(COALESCE(issue_severity,'')) IN ('high','critical','error') THEN 1 ELSE 0 END) AS high_issue_count,
              GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
              GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds,
              GROUP_CONCAT(DISTINCT issue_severity) AS issue_severities
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
          output.portuguese_text AS output_text,
          confirmation.confirmed_text,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked,
          state.final_state,
          state.review_state,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.lifecycle_policy_allowed,
          state.lifecycle_policy_action,
          COALESCE(open_issues.open_issue_count, 0) AS open_issue_count,
          COALESCE(open_issues.high_issue_count, 0) AS high_issue_count,
          COALESCE(open_issues.issue_families, '') AS issue_families,
          COALESCE(open_issues.issue_kinds, '') AS issue_kinds,
          COALESCE(open_issues.issue_severities, '') AS issue_severities
        FROM source_segments s
        LEFT JOIN output_segments output ON output.segment_id = s.id
        LEFT JOIN latest_confirmation confirmation ON confirmation.segment_id = s.id
        LEFT JOIN segment_state_items state ON state.segment_id = s.id AND state.run_id = ?
        LEFT JOIN open_issues ON open_issues.segment_id = s.id
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        [*segment_ids, ledger_run_id, *segment_ids, state_run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def issue_closure_class(row: dict[str, Any]) -> str:
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
    packet_by_id = {int(row["segment_id"]): row for row in packet_rows}
    with connect_readonly() as conn:
        live_by_id = fetch_live(conn, segment_ids, args.segment_state_run_id, args.ledger_run_id)

    records: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        packet = packet_by_id[segment_id]
        live = live_by_id.get(segment_id) or {}
        token_surface = packet.get("token_surface")
        output_text = live.get("output_text")
        packet_output_text = packet.get("output_text") or ""
        packet_confirmed_text = packet.get("confirmed_text") or packet_output_text
        existing_confirmed_text = live.get("confirmed_text")
        effective_confirmed_text = existing_confirmed_text or packet_confirmed_text

        output_exists = output_text is not None
        output_matches_packet = canonical_equal(output_text, packet_output_text)
        output_matches_effective_confirmed = canonical_equal(output_text, effective_confirmed_text)
        can_create_already_ok_confirmation = not existing_confirmed_text and canonical_equal(output_text, packet_confirmed_text)
        existing_confirmation_ok = bool(existing_confirmed_text) and output_matches_effective_confirmed
        token_ok = Counter(protected_tokens(output_text or "")) == Counter(protected_tokens(effective_confirmed_text or ""))
        struct_ok = structure_ok(output_text, token_surface) and structure_ok(effective_confirmed_text, token_surface)
        canonical_ok = output_matches_packet and output_matches_effective_confirmed
        dynamic_surface = has_dynamic_or_parser_later_surface(
            live.get("spanish_text"),
            live.get("english_text"),
            output_text,
            effective_confirmed_text,
            packet.get("source_text"),
        )

        reasons: list[str] = []
        if not live:
            reasons.append("missing_live_row")
        if not output_exists:
            reasons.append("missing_output")
        if token_surface not in {"plain_text", "light_token"}:
            reasons.append("not_plain_or_light_token")
        if dynamic_surface:
            reasons.append("dynamic_or_parser_later_surface")
        if not output_matches_packet:
            reasons.append("output_differs_from_packet")
        if not (existing_confirmation_ok or can_create_already_ok_confirmation):
            reasons.append("no_safe_already_ok_confirmation_path")
        if not output_matches_effective_confirmed:
            reasons.append("output_differs_from_confirmed")
        if int(live.get("needs_output_apply") or 0) != 0:
            reasons.append("needs_output_apply")
        if int(live.get("high_issue_count") or 0) != 0:
            reasons.append("high_issue_present")
        if not token_ok:
            reasons.append("token_integrity_mismatch")
        if not struct_ok:
            reasons.append("structure_integrity_mismatch")
        if not canonical_ok:
            reasons.append("canonical_l10n_mismatch")

        status = "ready" if not reasons else "blocked"
        records.append(
            {
                "source": SOURCE,
                "record_type": "narrative_strict_roi_approve_already_ok_readiness",
                "segment_id": segment_id,
                "review_index": packet.get("review_index"),
                "relative_path": live.get("relative_path") or packet.get("relative_path"),
                "source_key": live.get("source_key") or packet.get("source_key"),
                "release_class": packet.get("release_class"),
                "token_surface": token_surface,
                "output_text": output_text,
                "packet_output_text": packet_output_text,
                "existing_confirmed_text": existing_confirmed_text,
                "packet_confirmed_text": packet_confirmed_text,
                "effective_confirmed_text": effective_confirmed_text,
                "confirmation_level": live.get("confirmation_level"),
                "confirmation_source": live.get("confirmation_source"),
                "confirmation_label": live.get("confirmation_label"),
                "locked": int(live.get("locked") or 0),
                "can_create_already_ok_confirmation": can_create_already_ok_confirmation,
                "existing_confirmation_ok": existing_confirmation_ok,
                "final_state": live.get("final_state"),
                "review_state": live.get("review_state"),
                "confirmed_matches_output": int(live.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(live.get("needs_output_apply") or 0),
                "open_issue_count": int(live.get("open_issue_count") or 0),
                "high_issue_count": int(live.get("high_issue_count") or 0),
                "issue_families": live.get("issue_families") or "",
                "issue_kinds": live.get("issue_kinds") or "",
                "issue_severities": live.get("issue_severities") or "",
                "issue_closure_class": issue_closure_class(live),
                "output_exists": output_exists,
                "output_matches_packet": output_matches_packet,
                "output_matches_confirmed": output_matches_effective_confirmed,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": struct_ok,
                "canonical_l10n_ok": canonical_ok,
                "dynamic_or_parser_later_surface": dynamic_surface,
                "status": status,
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
    block_reason_counts = Counter(reason for record in blocked for reason in record["block_reasons"])
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
        "block_reason_counts": dict(block_reason_counts.most_common()),
        "issue_closure_class_counts": dict(Counter(record["issue_closure_class"] for record in records).most_common()),
        "release_class_counts": dict(Counter(record["release_class"] for record in records).most_common()),
        "token_surface_counts": dict(Counter(record["token_surface"] for record in records).most_common()),
        "open_issue_count_total": sum(int(record["open_issue_count"]) for record in records),
        "high_issue_count_total": sum(int(record["high_issue_count"]) for record in records),
        "can_create_already_ok_confirmation_count": sum(
            1 for record in records if record["can_create_already_ok_confirmation"]
        ),
        "existing_confirmation_ok_count": sum(1 for record in records if record["existing_confirmation_ok"]),
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
            "Proceed in a separate cycle with human approve ingest and issue-closure dry-run/apply for ready IDs only; keep blocked IDs out."
            if ready
            else "No safe approve_already_ok tranche found; return to a human correction packet or another release group."
        ),
    }

    base = reports_dir() / f"{stamp()}_release_readiness_narrative_strict_roi_approve_ok_readiness"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Narrative strict ROI approve_already_ok readiness",
        f"record_count={summary['record_count']}",
        f"ready_count={summary['ready_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        f"issue_closure_class_counts={json.dumps(summary['issue_closure_class_counts'], ensure_ascii=False, sort_keys=True)}",
        f"ready_segment_ids={json.dumps(summary['ready_segment_ids'])}",
        f"blocked_segment_ids={json.dumps(summary['blocked_segment_ids'])}",
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
    print(f"ready_segment_ids={json.dumps(summary['ready_segment_ids'])}")
    print(f"blocked_segment_ids={json.dumps(summary['blocked_segment_ids'])}")
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
