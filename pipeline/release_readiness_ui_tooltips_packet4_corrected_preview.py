from __future__ import annotations

import argparse
import difflib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_packet4_corrected_preview_v1"
PACKET_JSONL = Path("reports/20260703_134652_257558_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 575
LEDGER_RUN_ID = 77

CORRECTIONS: dict[int, str] = {
    33279: "Você rapidamente tira o capuz dele e o solta na classe para estudar o caos que se segue!",
    46950: "As crianças que acompanharem você às [holdings|lE] do seu [domain|lE] poderão aumentar suas habilidades",
    49535: "Você deixa claro que #BOL não#! deseja ser eleito para um [title|lE] mais alto #WEAK (possuir uma parte grande demais do senhorio pode fazer com que você seja eleito de qualquer maneira)#!",
    49943: "Seu suserano avaliará seu pedido. Quanto mais forte for sua posição política e religiosa, mais provável será a aceitação. Aumente sua [influence|lE] e o número de [vassals|lE] e [counties|lE] no [realm|lE] de seu [top_liege|lE] que seguem sua mesma [faith|lE] para aumentar sua chance de sucesso.",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for UI/tooltips packet4 corrected_text rows.")
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    parser.add_argument("--segment-state-run-id", type=int, default=SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    return parser.parse_args()


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


def issue_class(row: dict[str, Any], blocked: bool) -> str:
    if int(row.get("open_issue_count") or 0) == 0:
        return "unrelated_or_superseded"
    if blocked:
        return "needs_human_context"
    return "resolved_by_corrected_text"


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    packet_rows = {int(row["segment_id"]): row for row in read_jsonl(args.packet_jsonl)}
    missing = sorted(set(CORRECTIONS) - set(packet_rows))
    if missing:
        raise SystemExit(f"missing correction ids from packet: {missing}")
    with connect_readonly() as conn:
        live = fetch_live(conn, sorted(CORRECTIONS), args.segment_state_run_id, args.ledger_run_id)

    records: list[dict[str, Any]] = []
    for segment_id, target_text in sorted(CORRECTIONS.items()):
        packet = packet_rows[segment_id]
        row = live.get(segment_id) or {}
        output_text = row.get("output_text") or ""
        current_raw_line = row.get("output_raw_line") or ""
        new_raw_line = replace_quoted_text(current_raw_line, target_text) if current_raw_line else ""
        token_ok = protected_tokens(output_text) == protected_tokens(target_text)
        structure_ok = "\n" not in target_text and "\r" not in target_text
        canonical_change = not canonical_equal(output_text, target_text)
        reasons: list[str] = []
        if not row:
            reasons.append("missing_live_row")
        if int(row.get("high_issue_count") or 0) != 0:
            reasons.append("high_issue_present")
        if int(row.get("needs_output_apply") or 0) != 0:
            reasons.append("state_needs_output_apply")
        if not token_ok:
            reasons.append("structure_or_token_mismatch")
        if not structure_ok:
            reasons.append("structure_or_token_mismatch")
        if not current_raw_line:
            reasons.append("missing_output_raw_line")
        if not canonical_change:
            reasons.append("canonical_l10n_no_change")
        status = "ready_for_protected_apply" if not reasons else "blocked"
        records.append(
            {
                "source": SOURCE,
                "record_type": "corrected_text_diff_preview",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or packet.get("relative_path"),
                "source_key": row.get("source_key") or packet.get("source_key"),
                "source_text": packet.get("source_text"),
                "english_text": row.get("english_text") or packet.get("english_text"),
                "current_output_text": output_text,
                "confirmed_text": row.get("confirmed_text"),
                "proposed_corrected_text": target_text,
                "old_raw_line": current_raw_line,
                "new_raw_line": new_raw_line,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": structure_ok,
                "canonical_l10n_changes": canonical_change,
                "canonical_l10n_ok": canonical_change,
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
                "issue_classification": issue_class(row, bool(reasons)),
                "final_state": row.get("final_state"),
                "review_state": row.get("review_state"),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "status": status,
                "block_reasons": reasons,
                "diff_preview": list(
                    difflib.unified_diff(
                        [output_text],
                        [target_text],
                        fromfile="current_output",
                        tofile="proposed_corrected",
                        lineterm="",
                    )
                ),
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
    ready = [record for record in records if record["status"] == "ready_for_protected_apply"]
    blocked = [record for record in records if record["status"] != "ready_for_protected_apply"]
    block_counts = Counter(reason for record in blocked for reason in record.get("block_reasons", []))
    issue_class_counts = Counter(record["issue_classification"] for record in records)
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
        "ready_segment_ids": [int(record["segment_id"]) for record in ready],
        "blocked_segment_ids": [int(record["segment_id"]) for record in blocked],
        "block_reason_counts": dict(block_counts.most_common()),
        "issue_classification_counts": dict(issue_class_counts.most_common()),
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
            "If ready_count=record_count, proceed in a separate cycle with protected apply, post-validation, learning ingest, issue closure, materializer dry-run/apply, and segment-state delta."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet4_corrected_preview"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# UI/tooltips packet4 corrected_text diff preview",
        "",
        f"- record_count: {summary['record_count']}",
        f"- ready_count: {summary['ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        f"- block_reason_counts: {json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- issue_classification_counts: {json.dumps(summary['issue_classification_counts'], ensure_ascii=False, sort_keys=True)}",
        "- candidate_generation_count: 0",
        "- apply_count: 0",
        "- learning_ingest_count: 0",
        "- issue_closure_count: 0",
        "- lifecycle_count: 0",
        "- segment_state_count: 0",
        "- reindex_count: 0",
        "- production_full_count: 0",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## Segment {record['segment_id']} - {record['source_key']}",
                "",
                f"- status: `{record['status']}`",
                f"- block_reasons: `{record['block_reasons']}`",
                f"- token_integrity_ok: `{record['token_integrity_ok']}`",
                f"- structure_integrity_ok: `{record['structure_integrity_ok']}`",
                f"- canonical_l10n_ok: `{record['canonical_l10n_ok']}`",
                f"- issue_classification: `{record['issue_classification']}`",
                "",
                "```diff",
                *record["diff_preview"],
                "```",
                "",
            ]
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(args)
    md, jsonl, summary = write_reports(records, args)
    ready = [record for record in records if record["status"] == "ready_for_protected_apply"]
    blocked = [record for record in records if record["status"] != "ready_for_protected_apply"]
    print(f"markdown={md}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary}")
    print(f"record_count={len(records)}")
    print(f"ready_count={len(ready)}")
    print(f"blocked_count={len(blocked)}")
    print(f"ready_segment_ids={[int(record['segment_id']) for record in ready]}")
    print(f"blocked_segment_ids={[int(record['segment_id']) for record in blocked]}")
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
