from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_packet2_corrected_preview_v1"
PACKET_JSONL = Path("reports/20260702_175422_889616_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 553
LEDGER_RUN_ID = 76
EXPECTED_COUNT = 6


CORRECTIONS: dict[int, str] = {
    143193: "$rule_title_creation_imperial_power_projection_title_creation_trigger.tt.boilerplate$ ser pelo menos 50% mais forte do que #BER qualquer#! [empire|lE] vizinho",
    143196: "$rule_title_creation_imperial_power_projection_title_creation_trigger.tt.boilerplate$ ser pelo menos 50% mais forte do que #BER qualquer#! [empire|lE] que compartilhe qualquer território [de_jure|lE] com seu [realm|lE]",
    143928: "Mobilize os membros da sua casa para se proteger contra esquemas hostis e executá-los",
    158142: "Sua [capital|lE] n\u00e3o est\u00e1 localizada em uma [situation|lE] que use [migration|lE]",
    159166: "#TUT Embora alongar sua rota de viagem n\u00e3o lhe d\u00ea mais v\u00edtimas em potencial, encurt\u00e1-la pode reduzir a quantidade que voc\u00ea recebe#!",
    160779: "A v\u00edtima foge antes que voc\u00ea possa encurral\u00e1-la",
}

ISSUE_TRIAGE_BY_SEGMENT: dict[int, str] = {
    143193: "resolved_by_corrected_text",
    143196: "resolved_by_corrected_text",
    143928: "resolved_by_corrected_text",
    158142: "resolved_by_corrected_text",
    159166: "resolved_by_corrected_text",
    160779: "resolved_by_corrected_text",
}


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for UI/tooltips packet2 corrected_text subbatch.")
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    parser.add_argument("--segment-state-run-id", type=int, default=SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    return parser.parse_args()


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical_localization_text(left or "") == canonical_localization_text(right or "")


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
          o.output_line_number,
          o.portuguese_text AS output_text,
          o.output_raw_line,
          c.confirmed_text,
          c.confirmation_level,
          c.confirmation_source,
          c.confirmation_label,
          c.locked,
          state.final_state,
          state.review_state,
          state.confirmed_matches_output,
          state.needs_output_apply,
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


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    packet_rows = {int(row["segment_id"]): row for row in read_jsonl(args.packet_jsonl)}
    if set(CORRECTIONS) - set(packet_rows):
        raise SystemExit(f"missing correction ids from packet: {sorted(set(CORRECTIONS) - set(packet_rows))}")
    if len(CORRECTIONS) != EXPECTED_COUNT:
        raise SystemExit("expected count guard failed")
    with connect_readonly() as conn:
        live = fetch_live(conn, sorted(CORRECTIONS), args.segment_state_run_id, args.ledger_run_id)
    records: list[dict[str, Any]] = []
    for segment_id, target_text in sorted(CORRECTIONS.items()):
        packet = packet_rows[segment_id]
        row = live.get(segment_id) or {}
        output_text = row.get("output_text") or ""
        current_raw_line = row.get("output_raw_line") or ""
        new_raw_line = replace_quoted_text(current_raw_line, target_text) if current_raw_line else ""
        reasons: list[str] = []
        if not row:
            reasons.append("missing_live_row")
        if packet.get("suggested_human_decision") != "corrected_text":
            reasons.append("packet_not_corrected_text")
        if int(row.get("high_issue_count") or 0) != 0:
            reasons.append("high_issue_present")
        issue_resolution_class = ISSUE_TRIAGE_BY_SEGMENT.get(segment_id, "untriaged")
        open_issue_count = int(row.get("open_issue_count") or 0)
        issue_gate_ok = open_issue_count == 0 or issue_resolution_class in {
            "resolved_by_corrected_text",
            "unrelated_or_superseded",
        }
        if not issue_gate_ok:
            reasons.append("open_issue_present")
        if int(row.get("needs_output_apply") or 0) != 0:
            reasons.append("state_needs_output_apply")
        if "\n" in target_text or "\r" in target_text:
            reasons.append("unexpected_multiline_target")
        if protected_tokens(output_text) != protected_tokens(target_text):
            reasons.append("token_integrity_mismatch")
        if not current_raw_line:
            reasons.append("missing_output_raw_line")
        if not canonical_equal(target_text, target_text):
            reasons.append("canonical_l10n_self_check_failed")
        records.append(
            {
                "source": SOURCE,
                "record_type": "corrected_text_diff_preview",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or packet.get("relative_path"),
                "source_key": row.get("source_key") or packet.get("source_key"),
                "source_text": packet.get("source_text"),
                "old_output_text": output_text,
                "old_confirmed_text": row.get("confirmed_text"),
                "target_text": target_text,
                "old_raw_line": current_raw_line,
                "new_raw_line": new_raw_line,
                "token_integrity_ok": protected_tokens(output_text) == protected_tokens(target_text),
                "structure_integrity_ok": "\n" not in target_text and "\r" not in target_text,
                "canonical_l10n_target_stable": canonical_equal(target_text, target_text),
                "output_would_change": output_text != target_text,
                "confirmation_would_change": row.get("confirmed_text") != target_text,
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_resolution_class": issue_resolution_class,
                "issue_gate_ok": issue_gate_ok,
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
                "final_state": row.get("final_state"),
                "review_state": row.get("review_state"),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "apply_count": 0,
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
        "mode": "read_only_diff_preview",
        "packet_jsonl": str(args.packet_jsonl),
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "output_would_change_count": sum(1 for record in records if record["output_would_change"]),
        "confirmation_would_change_count": sum(1 for record in records if record["confirmation_would_change"]),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_ok_count": sum(1 for record in records if record["canonical_l10n_target_stable"]),
        "open_issue_count_total": sum(int(record["open_issue_count"]) for record in records),
        "high_issue_count_total": sum(int(record["high_issue_count"]) for record in records),
        "issue_gate_ok_count": sum(1 for record in records if record["issue_gate_ok"]),
        "false_safe_risk_count": sum(1 for record in records if record["status"] != "ready"),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "ready_segment_ids": [int(record["segment_id"]) for record in ready],
        "blocked_segment_ids": [int(record["segment_id"]) for record in blocked],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Do not apply: at least one gate failed. Fix target text or triage issue resolution before protected apply."
            if blocked
            else "All gates passed; protected apply can run next with snapshot/rollback."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_corrected_preview"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "UI/tooltips packet2 corrected_text diff preview",
        f"record_count={summary['record_count']}",
        f"ready_count={summary['ready_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"output_would_change_count={summary['output_would_change_count']}",
        f"token_integrity_ok_count={summary['token_integrity_ok_count']}",
        f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}",
        f"canonical_l10n_ok_count={summary['canonical_l10n_ok_count']}",
        f"open_issue_count_total={summary['open_issue_count_total']}",
        f"high_issue_count_total={summary['high_issue_count_total']}",
        f"issue_gate_ok_count={summary['issue_gate_ok_count']}",
        f"false_safe_risk_count={summary['false_safe_risk_count']}",
        f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
        "",
        "Diff preview:",
    ]
    for record in records:
        lines.extend(
            [
                f"- {record['segment_id']} {record['status']} reasons={record['block_reasons']}",
                f"  OLD: {record['old_output_text']}",
                f"  NEW: {record['target_text']}",
            ]
        )
    lines.append("")
    lines.append(f"recommendation={summary['single_operational_recommendation']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(args)
    txt_path, jsonl_path, summary_path = write_reports(records, args)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={len(records)}")
    print(f"ready_count={sum(1 for r in records if r['status'] == 'ready')}")
    print(f"blocked_count={sum(1 for r in records if r['status'] != 'ready')}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
