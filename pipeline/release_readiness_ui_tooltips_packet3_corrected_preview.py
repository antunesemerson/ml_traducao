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


SOURCE = "release_readiness_ui_tooltips_packet3_corrected_preview_v1"
PACKET_JSONL = Path("reports/20260703_122244_761708_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
SEGMENT_STATE_RUN_ID = 572
LEDGER_RUN_ID = 76

CORRECTIONS: dict[int, str] = {
    10465: "A [faith|lE] do seu [liege|lE] permite que seu povo pratique os sepultamentos tradicionais sem necessidade de uma $building_type_event_tower_of_silence_01$",
    30294: "#WEAK [powerful_vassals|lE] que apoiarem seu golpe se recusarão a representar seu [liege|lE]#!",
    49542: "Seu [realm|lE] é composto por #EMP mais#! de #V [EmptyScope.ScriptValue('small_empire_size_value')|0]#! [counties|lE] #! #weak (#color_white [root_scope.MakeScope.ScriptValue('current_size_empire_value')|0]#!)#!",
    50666: "@alert_icon! #alert_trial Se você morrer antes que um herdeiro seja designado, sua dinastia pode acabar!#!",
    69330: "Visite a [realm_capital|lE] do [son_of_heaven|E] ou do [minister|lE] e apresente uma questão urgente que seu [movement|lE] queira mudar",
    75698: "Contrate um artesão local para criar um [artifact|lE] de [Glossary( 'bunga mas', 'BUNGA_MAS_GLOSS' )] para você",
    155526: "Sua [stewardship|lE] extremamente alta permite que você drene o [development_growth|lE] dos [counties|lE] do seu [liege|lE]",
    160251: "#WEAK Este desafio é, no geral, um pouco mais fácil do que testar [stewardship|lE]#!",
    160493: "@alert_icon! #alert_trial Esta opção é forçada porque você alcançou o melhor resultado que pode obter com o número de falhas que acumulou#!",
    60232: "Você e seu oponente devem ter [domains|lE] adjacentes que não sejam suas [realm_capitals|lE]",
}

HOLD_REASONS: dict[int, str] = {
    50666: "canonical_l10n_no_change",
    69330: "canonical_l10n_no_change",
    160251: "canonical_l10n_no_change",
    160493: "canonical_l10n_no_change",
    60232: "canonical_l10n_no_change",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for UI/tooltips packet3 corrected_text rows.")
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


def issue_class(row: dict[str, Any], will_change: bool, blocked: bool) -> str:
    if blocked and not will_change:
        return "unrelated_or_superseded"
    if blocked:
        return "needs_human_context"
    if int(row.get("open_issue_count") or 0) == 0:
        return "unrelated_or_superseded"
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
        if packet.get("suggested_human_decision") != "corrected_text":
            reasons.append("packet_not_corrected_text")
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
            reasons.append(HOLD_REASONS.get(segment_id, "canonical_l10n_no_change"))
        if segment_id in HOLD_REASONS and canonical_change:
            reasons.append(HOLD_REASONS[segment_id])
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
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
                "issue_classification": issue_class(row, canonical_change, bool(reasons)),
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
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "ready_segment_ids": [int(record["segment_id"]) for record in ready],
        "blocked_segment_ids": [int(record["segment_id"]) for record in blocked],
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_changes_count": sum(1 for record in records if record["canonical_l10n_changes"]),
        "open_issue_count_total": sum(int(record["open_issue_count"]) for record in records),
        "high_issue_count_total": sum(int(record["high_issue_count"]) for record in records),
        "issue_class_counts": dict(Counter(record["issue_classification"] for record in records).most_common()),
        "false_safe_risk_count": len(blocked),
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
            "Apply only ready_for_protected_apply rows after explicit approval; keep blocked rows for approve_already_ok/hold triage."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet3_corrected_preview"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# UI/tooltips Packet3 Corrected Text Diff Preview",
        "",
        f"- record_count: {summary['record_count']}",
        f"- ready_count: {summary['ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        f"- block_reason_counts: `{json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- issue_class_counts: `{json.dumps(summary['issue_class_counts'], ensure_ascii=False, sort_keys=True)}`",
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
                f"- path: `{record['relative_path']}`",
                f"- token_integrity_ok: `{record['token_integrity_ok']}`",
                f"- structure_integrity_ok: `{record['structure_integrity_ok']}`",
                f"- canonical_l10n_changes: `{record['canonical_l10n_changes']}`",
                f"- open/high issues: `{record['open_issue_count']}/{record['high_issue_count']}`",
                f"- issue_classification: `{record['issue_classification']}`",
                f"- block_reasons: `{json.dumps(record['block_reasons'], ensure_ascii=False)}`",
                "",
                "```diff",
                *record["diff_preview"],
                "```",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(args)
    md_path, jsonl_path, summary_path = write_reports(records, args)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"ready_segment_ids={summary['ready_segment_ids']}")
    print(f"blocked_segment_ids={summary['blocked_segment_ids']}")
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
