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
from apply_safe_output_updates import escape_localization_value, protected_tokens, replace_quoted_text


RULE_VERSION = "v3_deterministic_correction_decision_preview_readonly_v1"
DECISION_SOURCE = "explicit_user_approval_20260711"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_packet() -> Path:
    reports = db.project_path(db.load_settings()["reports_dir"])
    matches = sorted(reports.glob("*_v3_deterministic_correction_human_packet.jsonl"))
    if not matches:
        raise RuntimeError("No V3 deterministic correction packet was found.")
    return matches[-1]


def report_paths() -> dict[str, Path]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    base = reports / f"{stamp()}_v3_deterministic_correction_decision_preview_readonly"
    return {
        "decision_markdown": base.with_name(base.name + "_decisions.md"),
        "decision_jsonl": base.with_name(base.name + "_decisions.jsonl"),
        "preview_markdown": base.with_name(base.name + "_preview.md"),
        "preview_jsonl": base.with_name(base.name + "_preview.jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }


def load_packet(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 11 or len({int(row["segment_id"]) for row in rows}) != 11:
        raise RuntimeError("Expected 11 unique deterministic correction records.")
    for row in rows:
        if not row.get("assisted_suggestion"):
            raise RuntimeError(f"Missing assisted suggestion for segment {row['segment_id']}.")
    return rows


def fetch_live(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    latest_run = conn.execute(
        "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not latest_run:
        raise RuntimeError("No completed segment-state run was found.")
    run_id = int(latest_run[0])
    rows = conn.execute(
        f"""
        SELECT
            source.id AS segment_id,
            source.relative_path,
            source.source_key,
            source.spanish_text,
            output.portuguese_text AS current_output_text,
            output.output_line_number,
            confirmation.confirmed_text,
            confirmation.confirmation_level,
            confirmation.confirmation_label,
            confirmation.locked,
            state.final_state,
            state.state_group,
            state.is_closed,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.lifecycle_policy_action,
            ? AS segment_state_run_id
        FROM source_segments source
        JOIN output_segments output ON output.segment_id = source.id
        JOIN segment_state_items state
          ON state.segment_id = source.id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = source.id
        WHERE source.id IN ({placeholders})
        """,
        (run_id, run_id, *segment_ids),
    ).fetchall()
    if len(rows) != len(segment_ids):
        raise RuntimeError(f"Expected {len(segment_ids)} live rows, got {len(rows)}.")
    return {int(row["segment_id"]): dict(row) for row in rows}


def unified_diff(current: str, corrected: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            [current],
            [corrected],
            fromfile="current_output",
            tofile="approved_correction",
            lineterm="",
        )
    )


def build_records(packet: list[dict[str, Any]], live: dict[int, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output_root = db.project_path(db.load_settings()["output_spanish"])
    decisions: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    for packet_row in packet:
        segment_id = int(packet_row["segment_id"])
        current = live[segment_id]
        corrected = str(packet_row["assisted_suggestion"])
        decision = dict(packet_row)
        decision.update(
            {
                "human_decision": "corrected_text",
                "corrected_text": corrected,
                "decision_source": DECISION_SOURCE,
                "decision_recorded_at": datetime.now().isoformat(timespec="seconds"),
                "ready_for_diff_preview": True,
                "candidate_generation_allowed": False,
                "apply_allowed": False,
            }
        )
        decisions.append(decision)

        relative_path = str(current["relative_path"])
        line_number = int(current.get("output_line_number") or 0)
        output_path = output_root / Path(relative_path)
        disk_line = ""
        disk_line_in_range = False
        disk_current_exact = False
        structure_integrity_ok = False
        if output_path.exists() and line_number > 0:
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            disk_line_in_range = line_number <= len(lines)
            if disk_line_in_range:
                disk_line = lines[line_number - 1]
                try:
                    disk_current_exact = replace_quoted_text(disk_line, str(current["current_output_text"] or "")) == disk_line
                    candidate_line = replace_quoted_text(disk_line, corrected)
                    structure_integrity_ok = candidate_line != disk_line and candidate_line.split('"', 1)[0] == disk_line.split('"', 1)[0]
                except ValueError:
                    disk_current_exact = False
                    structure_integrity_ok = False

        current_text = str(current.get("current_output_text") or "")
        confirmed_text = str(current.get("confirmed_text") or "")
        spanish_text = str(current.get("spanish_text") or "")
        guards = {
            "packet_current_matches_db": str(packet_row.get("output_text") or "") == current_text,
            "confirmation_matches_output": (
                escape_localization_value(confirmed_text) == current_text
                and int(current.get("confirmed_matches_output") or 0) == 1
            ),
            "existing_confirmation_not_human_locked": not (
                str(current.get("confirmation_level") or "") == "human_confirmed"
                and int(current.get("locked") or 0) == 1
            ),
            "segment_is_closed": int(current.get("is_closed") or 0) == 1,
            "needs_output_apply_zero": int(current.get("needs_output_apply") or 0) == 0,
            "canonical_change_present": corrected != current_text,
            "token_integrity_current": protected_tokens(current_text) == protected_tokens(corrected),
            "token_integrity_spanish": protected_tokens(spanish_text) == protected_tokens(corrected),
            "output_file_exists": output_path.exists(),
            "output_line_in_range": disk_line_in_range,
            "output_file_matches_db": disk_current_exact,
            "structure_integrity_ok": structure_integrity_ok,
        }
        ready = all(guards.values())
        previews.append(
            {
                "segment_id": segment_id,
                "relative_path": relative_path,
                "source_key": current["source_key"],
                "output_line_number": line_number,
                "segment_state_run_id": int(current["segment_state_run_id"]),
                "final_state": current["final_state"],
                "lifecycle_policy_action": current["lifecycle_policy_action"],
                "current_output_text": current_text,
                "approved_corrected_text": corrected,
                "diff_preview": unified_diff(current_text, corrected),
                "guards": guards,
                "ready_for_protected_apply": ready,
                "block_reasons": [name for name, passed in guards.items() if not passed],
                "apply_now": False,
                "source_changed": False,
                "output_changed": False,
            }
        )
    return decisions, previews


def write_reports(paths: dict[str, Path], packet_path: Path, decisions: list[dict[str, Any]], previews: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [row for row in previews if row["ready_for_protected_apply"]]
    blocked = [row for row in previews if not row["ready_for_protected_apply"]]
    block_counts = Counter(reason for row in blocked for reason in row["block_reasons"])
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "source_packet": str(packet_path),
        "decision_source": DECISION_SOURCE,
        "record_count": len(previews),
        "corrected_text_decision_count": len(decisions),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "ready_segment_ids": [int(row["segment_id"]) for row in ready],
        "blocked_segment_ids": [int(row["segment_id"]) for row in blocked],
        "block_reason_counts": dict(block_counts),
        "candidate_generation": 0,
        "apply": 0,
        "database_changed": False,
        "source_changed": False,
        "output_changed": False,
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with paths["decision_jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with paths["preview_jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in previews:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    decision_lines = [
        "# V3 deterministic correction decisions",
        "",
        f"- Decision source: `{DECISION_SOURCE}`",
        f"- Corrected text decisions: `{len(decisions)}`",
        "- Apply: `0`",
        "",
    ]
    for row in decisions:
        decision_lines.extend(
            [
                f"## Segment {row['segment_id']}",
                "",
                "- Human decision: `corrected_text`",
                f"- Corrected text: {row['corrected_text']}",
                "",
            ]
        )
    paths["decision_markdown"].write_text("\n".join(decision_lines) + "\n", encoding="utf-8")

    preview_lines = [
        "# V3 deterministic correction diff preview",
        "",
        f"- Records: `{len(previews)}`",
        f"- Ready: `{len(ready)}`",
        f"- Blocked: `{len(blocked)}`",
        "- Apply: `0`",
        "",
    ]
    for row in previews:
        preview_lines.extend(
            [
                f"## Segment {row['segment_id']} - {'ready' if row['ready_for_protected_apply'] else 'blocked'}",
                "",
                f"- Path: `{row['relative_path']}:{row['output_line_number']}`",
                f"- Guards: `{json.dumps(row['guards'], ensure_ascii=False, sort_keys=True)}`",
                "",
                "```diff",
                row["diff_preview"],
                "```",
                "",
            ]
        )
    paths["preview_markdown"].write_text("\n".join(preview_lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Record approved V3 deterministic decisions and preview them read-only.")
    parser.add_argument("--packet", type=Path)
    args = parser.parse_args()
    packet_path = args.packet.resolve() if args.packet else latest_packet()
    packet = load_packet(packet_path)
    settings = db.load_settings()
    with sqlite3.connect(f"file:{db.get_database_path(settings)}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        live = fetch_live(conn, [int(row["segment_id"]) for row in packet])
    decisions, previews = build_records(packet, live)
    paths = report_paths()
    summary = write_reports(paths, packet_path, decisions, previews)
    print("[v3-decision-preview] Read-only preview completed")
    print(f"[v3-decision-preview] Ready: {summary['ready_count']}")
    print(f"[v3-decision-preview] Blocked: {summary['blocked_count']}")
    print(f"[v3-decision-preview] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
