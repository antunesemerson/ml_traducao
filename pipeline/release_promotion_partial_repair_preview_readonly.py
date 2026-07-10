from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "release_promotion_partial_repair_preview_readonly_v1"
CORRECTIONS = {
    30553: "@alert_icon! #alert_trial [host.GetPlayerHeir.GetShortUIName] não [Select_CString( host.GetPlayerHeir.IsLocalPlayer, 'herdará', 'herdará' )] [crowning_artifact.GetName]#!",
    30554: "@alert_icon! #alert_trial [host.GetPlayerHeir.GetShortUIName] não [Select_CString( host.GetPlayerHeir.IsLocalPlayer, 'herdará', 'herdará' )] [crowning_artifact_2.GetName]#!",
    30555: "@alert_icon! #alert_trial [host.GetPlayerHeir.GetShortUIName] não [Select_CString( host.GetPlayerHeir.IsLocalPlayer, 'herdará', 'herdará' )] [crowning_artifact_3.GetName]#!",
    30556: "@alert_icon! #alert_trial [host.GetPlayerHeir.GetShortUIName] não [Select_CString( host.GetPlayerHeir.IsLocalPlayer, 'herdará', 'herdará' )] [crowning_artifact_4.GetName]#!",
    30557: "@alert_icon! #alert_trial [host.GetPlayerHeir.GetShortUIName] não [Select_CString( host.GetPlayerHeir.IsLocalPlayer, 'herdará', 'herdará' )] [crowning_artifact_5.GetName]#!",
    58592: "Se [actor.GetShortUINameNoTooltip] [Select_CString( actor.IsLocalPlayer, 'vencer', 'vencer' )]",
    58593: "Se [recipient.GetShortUINameNoTooltip] [Select_CString( recipient.IsLocalPlayer, 'vencer', 'vencer' )]",
    229510: "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] simplesmente #EMP [Select_CString( CHARACTER.IsLocalPlayer, 'não consegue', 'não consegue' )]#! evitar que se fale de um certo cheiro de peixe ao redor de [Select_CString( CHARACTER.IsLocalPlayer, 'você', CHARACTER.GetSheHe)].",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview the eight remaining promotion repairs.")
    parser.add_argument("--audit-jsonl", type=Path, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def latest_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT id FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("No completed segment-state run found.")
    return int(row["id"])


def fetch_live_rows(conn: sqlite3.Connection, run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in CORRECTIONS)
    rows = conn.execute(
        f"""
        SELECT state.segment_id, state.final_state, state.needs_output_apply,
               output.relative_path, output.output_line_number,
               output.portuguese_text AS output_text,
               confirm.confirmed_text
        FROM segment_state_items state
        JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations confirm ON confirm.segment_id = state.segment_id
        WHERE state.run_id = ? AND state.segment_id IN ({placeholders})
        """,
        [run_id, *sorted(CORRECTIONS)],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def file_matches_db(row: dict[str, Any]) -> bool:
    root = db.project_path(db.load_settings()["output_spanish"])
    path = root / Path(str(row["relative_path"]))
    if not path.exists() or row["output_line_number"] is None:
        return False
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(row["output_line_number"]) - 1
    if index < 0 or index >= len(lines):
        return False
    try:
        return replace_quoted_text(lines[index], str(row["output_text"] or "")) == lines[index]
    except ValueError:
        return False


def preview_record(audit: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    segment_id = int(audit["segment_id"])
    corrected = CORRECTIONS[segment_id]
    failures: list[str] = []
    current = ""
    if live is None:
        failures.append("missing_live_segment")
    else:
        current = str(live.get("output_text") or "")
        if canonical_localization_text(current) != canonical_localization_text(str(audit.get("output_text") or "")):
            failures.append("live_output_differs_from_audit")
        if canonical_localization_text(current) != canonical_localization_text(str(live.get("confirmed_text") or "")):
            failures.append("output_differs_from_confirmation")
        if int(live.get("needs_output_apply") or 0) != 0:
            failures.append("unexpected_needs_output_apply")
        if not file_matches_db(live):
            failures.append("file_differs_from_output_db")
        if not str(live.get("final_state") or "").startswith("reopen_"):
            failures.append("current_state_not_reopen")
    token_ok = structural_tokens(current) == structural_tokens(corrected)
    if not token_ok:
        failures.append("structural_token_signature_changed")
    canonical_change = canonical_localization_text(current) != canonical_localization_text(corrected)
    if not canonical_change:
        failures.append("canonical_no_change")

    return {
        "schema_version": 1,
        "source": SOURCE,
        "record_type": "release_promotion_partial_repair_preview",
        "segment_id": segment_id,
        "relative_path": audit.get("relative_path"),
        "source_key": audit.get("source_key"),
        "current_text": current,
        "corrected_text": corrected,
        "repair_class": (
            "spanish_literal_no_to_nao"
            if segment_id in {30553, 30554, 30555, 30556, 30557}
            else "conditional_victory_grammar"
            if segment_id in {58592, 58593}
            else "duplicate_auxiliary_cleanup"
        ),
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": token_ok,
        "canonical_l10n_change": canonical_change,
        "dynamic_literal_surface_changed": "Select_CString" in current and current != corrected,
        "validation_failures": failures,
        "readiness": "ready_for_protected_apply_preview" if not failures else "blocked",
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
    }


def main() -> int:
    args = parse_args()
    audit_rows = {
        int(row["segment_id"]): row
        for row in read_jsonl(args.audit_jsonl)
        if int(row["segment_id"]) in CORRECTIONS
    }
    if set(audit_rows) != set(CORRECTIONS):
        raise SystemExit(f"Audit mismatch: found={sorted(audit_rows)}, expected={sorted(CORRECTIONS)}")
    with connect_readonly() as conn:
        run_id = latest_state_run_id(conn)
        live_rows = fetch_live_rows(conn, run_id)
    records = [preview_record(audit_rows[segment_id], live_rows.get(segment_id)) for segment_id in sorted(CORRECTIONS)]
    readiness = Counter(record["readiness"] for record in records)

    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_release_promotion_partial_repair_preview_readonly"
    jsonl_path = base.with_suffix(".jsonl")
    md_path = base.with_suffix(".md")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "segment_state_run_id": run_id,
        "record_count": len(records),
        "ready_count": readiness["ready_for_protected_apply_preview"],
        "blocked_count": readiness["blocked"],
        "token_integrity_ok_count": sum(record["token_integrity_ok"] for record in records),
        "canonical_change_count": sum(record["canonical_l10n_change"] for record in records),
        "ready_segment_ids": [record["segment_id"] for record in records if record["readiness"].startswith("ready_")],
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": "Review exact PT-BR text, then run a separate protected apply only for explicitly approved records.",
    }
    lines = [
        "# Release Promotion Partial Repair Preview (read-only)", "",
        f"- segment-state: `{run_id}`", f"- records: `{len(records)}`",
        f"- ready: `{summary['ready_count']}`", f"- blocked: `{summary['blocked_count']}`",
        f"- token/structure ok: `{summary['token_integrity_ok_count']}/{len(records)}`",
        "- source/output changed: `false`", "",
        "| ID | class | current | corrected | status |", "|---:|---|---|---|---|",
    ]
    for record in records:
        current = str(record["current_text"]).replace("|", "\\|")
        corrected = str(record["corrected_text"]).replace("|", "\\|")
        lines.append(f"| {record['segment_id']} | `{record['repair_class']}` | {current} | {corrected} | `{record['readiness']}` |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary, "artifacts": [str(md_path), str(jsonl_path), str(summary_path)]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
