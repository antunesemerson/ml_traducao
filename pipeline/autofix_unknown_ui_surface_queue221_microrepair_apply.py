from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "autofix_unknown_ui_surface_queue221_microrepair_apply_v1"
DECISIONS_JSONL = Path("reports/20260617_233004_autofix_unknown_ui_surface_queue_221_reviewed_chat.jsonl")
EXPECTED_CANDIDATES = 5
EXPECTED_READY = 5
EXPECTED_BLOCKED = 0
EXPECTED_STALE = 0
EXPECTED_ALREADY_APPLIED = 0
WRITABLE_IDS = {108426, 14288, 287689, 153798, 153812}
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "autofix_unknown_ui_surface_queue221_microrepair_production"
CONFIRMATION_LABEL = "autofix_unknown_ui_surface_queue221_microrepair"
REVIEWER = "production_autofix_unknown_ui_surface_queue221_microrepair_apply"

EXPECTED_CURRENT = {
    108426: '\\"Agora, escute aqui!\\" [stolen_clothes.GetFirstNameNoTooltip] diz, segurando firme a minha orelha. \\"Você não pode sair por aí roubando roupas! Acho que você está atrasad para uma lição de etiqueta...\\"\\n\\nOh não, não a #EMP etiqueta!#!',
    14288: "Probabilidade de remover a corrupção do condado: [SCOPE.ScriptValue('marshal_remove_county_corruption_chance')|P]#P %#!/mês\\n$TAB$ Conselheiro: #P +#![SCOPE.ScriptValue('marshal_councillor_county_corruption_value')|P]\\n$TAB$ Controle do condado: #P +#![SCOPE.ScriptValue('marshal_county_county_corruption_value')|P]\\n\\nEnquanto o controle estiver aumentando, o condado não adquirirá corrupção.",
    287689: "\\n\\n#X You may only attempt one invasion [war|lE] against a [kingdom|lE]-tier [title|lE] per lifetime#!",
    153798: "@warning_icon! #X Por estar acima do [domain_limit|lE], posses recém-adquiridas não fornecem levies#! $EXPIRES$",
    153812: "@warning_icon! #X Por ser o tipo de posse errado para [CHARACTER.GetShortUIName], a posse não produz levies nem impostos#!",
}

EXPECTED_CORRECTED = {
    108426: '\\"Agora, escute aqui!\\" [stolen_clothes.GetFirstNameNoTooltip] diz, segurando firme a minha orelha. \\"Você não pode sair por aí roubando roupas! Já passou da hora de você ter uma lição de etiqueta...\\"\\n\\nOh não, #EMP etiqueta#! não!',
    14288: "Probabilidade de remover a corrupção do condado: [SCOPE.ScriptValue('marshal_remove_county_corruption_chance')|P]#P%#!/mês\\n$TAB$ Conselheiro: #P +#![SCOPE.ScriptValue('marshal_councillor_county_corruption_value')|P]\\n$TAB$ Controle do condado: #P +#![SCOPE.ScriptValue('marshal_county_county_corruption_value')|P]\\n\\nEnquanto o controle estiver aumentando, o condado não adquirirá corrupção.",
    287689: "\\n\\n#X Você só pode tentar uma [war|lE] de invasão contra um [title|lE] de nível [kingdom|lE] uma vez na vida#!",
    153798: "@warning_icon! #X Por estar acima do [domain_limit|lE], posses recém-adquiridas não fornecem levas#! $EXPIRES$",
    153812: "@warning_icon! #X Por ser o tipo de posse errado para [CHARACTER.GetShortUIName], a posse não produz levas nem impostos#!",
}

EXPECTED_PATHS = {
    108426: "event_localization/childhood/childhood_events_oltner_l_spanish.yml",
    14288: "council_tasks_l_spanish.yml",
    287689: "wars_l_spanish.yml",
    153798: "holdings_l_spanish.yml",
    153812: "holdings_l_spanish.yml",
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any], *, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_autofix_unknown_ui_surface_queue221_microrepair_{mode}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE total_segments > 1000
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else 0


def latest_confirmation(conn, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM segment_confirmations
        WHERE segment_id = ?
        ORDER BY updated_at DESC, confirmed_at DESC, id DESC
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def file_text_at(output_root: Path, row: dict[str, Any]) -> str:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"Line without quoted value for segment {row['segment_id']}")
    return raw_line[first_quote + 1 : last_quote]


def load_decisions(queue_run_id: int) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    rows = [json.loads(line) for line in DECISIONS_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [
        dict(row)
        for row in rows
        if int(row.get("queue_run_id") or 0) == queue_run_id
        and row.get("requires_apply_later") is True
        and row.get("tokens_preserved") is True
    ]
    selected_ids = {int(row["segment_id"]) for row in selected}
    if selected_ids != WRITABLE_IDS:
        errors.append(f"selected_ids_mismatch:{sorted(selected_ids)}")
    if len(selected) != EXPECTED_CANDIDATES:
        errors.append(f"candidate_count_mismatch:{len(selected)}")
    for row in selected:
        segment_id = int(row["segment_id"])
        row["expected_current_text"] = EXPECTED_CURRENT[segment_id]
        row["corrected_text"] = EXPECTED_CORRECTED[segment_id]
        if as_text(row["relative_path"]) != EXPECTED_PATHS[segment_id]:
            errors.append(f"relative_path_mismatch:{segment_id}")
        if "?" in row["corrected_text"]:
            errors.append(f"corrected_text_contains_question_mark:{segment_id}")
        if structural_tokens(row["expected_current_text"]) != structural_tokens(row["corrected_text"]):
            errors.append(f"structural_tokens_changed:{segment_id}")
    return sorted(selected, key=lambda item: int(item["segment_id"])), errors


def fetch_live_rows(conn, state_run_id: int, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in decisions)
    by_id = {int(row["segment_id"]): row for row in decisions}
    rows = conn.execute(
        f"""
        SELECT
            source.id AS segment_id,
            source.relative_path,
            source.source_key,
            source.source_line_number,
            source.is_active,
            output.output_line_number,
            output.portuguese_text AS live_output_text,
            output.output_raw_line AS live_output_raw_line,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.confirmed_text,
            COALESCE(confirmation.locked, 0) AS confirmation_locked,
            state.final_state AS live_final_state,
            COALESCE(state.needs_output_apply, 0) AS live_needs_output_apply
        FROM source_segments source
        JOIN output_segments output
          ON output.segment_id = source.id
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = source.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = source.id
         AND state.run_id = ?
        WHERE source.id IN ({placeholders})
        ORDER BY source.id
        """,
        (state_run_id, *sorted(by_id)),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.update(by_id[int(item["segment_id"])])
        result.append(item)
    return result


def classify_row(output_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["status"] = "ready"
    result["block_reason_detail"] = ""

    def mark(status: str, reason: str = "") -> dict[str, Any]:
        result["status"] = status
        result["block_reason_detail"] = reason
        return result

    segment_id = int(row["segment_id"])
    if segment_id not in WRITABLE_IDS:
        return mark("blocked", "segment_not_permitted")
    if int(row.get("is_active") or 0) != 1:
        return mark("stale", "source_not_active")
    if int(row.get("confirmation_locked") or 0) == 1:
        return mark("blocked", "locked_confirmation")
    if int(row.get("live_needs_output_apply") or 0) != 0:
        return mark("blocked", "needs_output_apply_not_0")
    live_output = as_text(row.get("live_output_text"))
    expected_current = as_text(row.get("expected_current_text"))
    corrected = as_text(row.get("corrected_text"))
    confirmed = as_text(row.get("confirmed_text"))
    if live_output == corrected and row.get("confirmation_source") == CONFIRMATION_SOURCE and row.get("confirmation_label") == CONFIRMATION_LABEL:
        return mark("already_applied", "")
    if live_output != expected_current:
        return mark("stale", "live_output_not_expected_current")
    if confirmed != live_output:
        return mark("stale", "confirmation_not_current_output")
    if structural_tokens(live_output) != structural_tokens(corrected):
        return mark("blocked", "structural_tokens_changed")
    try:
        file_text = file_text_at(output_root, row)
    except RuntimeError as exc:
        return mark("blocked", str(exc))
    if file_text != live_output:
        return mark("stale", "file_output_db_output_mismatch")
    return result


def validate_counts(rows: list[dict[str, Any]]) -> Counter:
    counts = Counter(row["status"] for row in rows)
    if len(rows) != EXPECTED_CANDIDATES:
        raise RuntimeError(f"Expected candidates={EXPECTED_CANDIDATES}, got {len(rows)}")
    if counts["ready"] != EXPECTED_READY:
        raise RuntimeError(f"Expected ready={EXPECTED_READY}, got {counts['ready']}")
    if counts["blocked"] != EXPECTED_BLOCKED:
        raise RuntimeError(f"Expected blocked={EXPECTED_BLOCKED}, got {counts['blocked']}")
    if counts["stale"] != EXPECTED_STALE:
        raise RuntimeError(f"Expected stale={EXPECTED_STALE}, got {counts['stale']}")
    if counts["already_applied"] != EXPECTED_ALREADY_APPLIED:
        raise RuntimeError(f"Expected already_applied={EXPECTED_ALREADY_APPLIED}, got {counts['already_applied']}")
    return counts


def upsert_confirmation(conn, *, segment_id: int, confirmed_text: str, now: str) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        raise RuntimeError(f"Refusing locked confirmation for segment {segment_id}")
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == confirmed_text
    )
    conn.execute(
        """
        INSERT INTO segment_confirmations (
            segment_id,
            confirmation_level,
            confirmed_text,
            confirmation_source,
            confirmation_label,
            locked,
            confidence_score,
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, 1.0, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level ELSE excluded.confirmation_level END,
            confirmed_text = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmed_text ELSE excluded.confirmed_text END,
            confirmation_source = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_source ELSE excluded.confirmation_source END,
            confirmation_label = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_label ELSE excluded.confirmation_label END,
            locked = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.locked ELSE excluded.locked END,
            confidence_score = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score ELSE excluded.confidence_score END,
            reviewer = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer ELSE excluded.reviewer END,
            confirmed_at = COALESCE(segment_confirmations.confirmed_at, excluded.confirmed_at),
            updated_at = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.updated_at ELSE excluded.updated_at END
        """,
        (segment_id, CONFIRMATION_LEVEL, confirmed_text, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not bool(already)


def apply_ready(conn, *, output_root: Path, rows: list[dict[str, Any]], now: str) -> dict[str, int]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(row["segment_id"]) not in WRITABLE_IDS:
            raise RuntimeError(f"Refusing non-allowlisted segment {row['segment_id']}")
        by_file[as_text(row["relative_path"])].append(row)
    applied = confirmation_promoted = output_written = files_touched = 0
    for relative_path, file_rows in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row in sorted(file_rows, key=lambda item: int(item["output_line_number"])):
            segment_id = int(row["segment_id"])
            line_index = int(row["output_line_number"]) - 1
            raw_line = lines[line_index]
            if file_text_at(output_root, row) != as_text(row["live_output_text"]):
                raise RuntimeError(f"Live file output drift for segment {segment_id}")
            corrected = as_text(row["corrected_text"])
            lines[line_index] = replace_quoted_text(raw_line, corrected)
            if upsert_confirmation(conn, segment_id=segment_id, confirmed_text=corrected, now=now):
                confirmation_promoted += 1
            conn.execute(
                """
                UPDATE output_segments
                SET portuguese_text = ?,
                    output_raw_line = ?,
                    portuguese_hash = ?,
                    last_indexed_at = ?
                WHERE segment_id = ?
                """,
                (corrected, lines[line_index], sha256_text(corrected), now, segment_id),
            )
            applied += 1
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return {
        "applied": applied,
        "confirmation_promoted": confirmation_promoted,
        "output_written": output_written,
        "files_touched": files_touched,
    }


def write_reports(
    settings: dict[str, Any],
    *,
    queue_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter,
    apply: bool,
    apply_counts: dict[str, int],
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, apply=apply)
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "output_line_number",
        "decision",
        "status",
        "block_reason_detail",
        "live_final_state",
        "live_needs_output_apply",
        "current_text",
        "expected_current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Autofix unknown UI surface queue 221 protected microrepair" if apply else "Autofix unknown UI surface queue 221 protected microrepair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Decisions JSONL: {DECISIONS_JSONL}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Apply: {apply}",
        "",
        "Counts:",
        f"- candidates: {len(rows)}",
        f"- ready: {counts['ready']}",
        f"- blocked: {counts['blocked']}",
        f"- stale: {counts['stale']}",
        f"- already_applied: {counts['already_applied']}",
        f"- applied: {apply_counts.get('applied', 0)}",
        f"- confirmation_promoted: {apply_counts.get('confirmation_promoted', 0)}",
        f"- output_written: {apply_counts.get('output_written', 0)}",
        f"- files_touched: {apply_counts.get('files_touched', 0)}",
        "",
        "Rows:",
    ]
    for row in rows:
        lines.append(f"- {row['segment_id']} | {row['source_key']} | status={row['status']} | {row['block_reason_detail']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def run(*, queue_run_id: int, apply: bool) -> tuple[Counter, dict[str, int], tuple[Path, Path, Path]]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    decisions, errors = load_decisions(queue_run_id)
    if errors:
        raise RuntimeError("; ".join(errors))
    apply_counts = {"applied": 0, "confirmation_promoted": 0, "output_written": 0, "files_touched": 0}
    with db.connect(settings) as conn:
        state_run_id = latest_state_run_id(conn)
        rows = [classify_row(output_root, row) for row in fetch_live_rows(conn, state_run_id, decisions)]
        counts = validate_counts(rows)
        if apply:
            apply_counts = apply_ready(
                conn,
                output_root=output_root,
                rows=[row for row in rows if row["status"] == "ready"],
                now=db.utc_now(),
            )
            conn.commit()
        paths = write_reports(settings, queue_run_id=queue_run_id, rows=rows, counts=counts, apply=apply, apply_counts=apply_counts)
    return counts, apply_counts, paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-run-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        counts, apply_counts, paths = run(queue_run_id=args.queue_run_id, apply=args.apply)
    except Exception as exc:
        print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[autofix_unknown_ui_surface_queue221_microrepair_apply] Completed")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Apply: {args.apply}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Candidates: {EXPECTED_CANDIDATES}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Ready: {counts['ready']}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Blocked: {counts['blocked']}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Stale: {counts['stale']}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Already applied: {counts['already_applied']}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Applied: {apply_counts.get('applied', 0)}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Confirmation promoted: {apply_counts.get('confirmation_promoted', 0)}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Output written: {apply_counts.get('output_written', 0)}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Files touched: {apply_counts.get('files_touched', 0)}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] Report: {paths[0]}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] CSV: {paths[1]}")
    print(f"[autofix_unknown_ui_surface_queue221_microrepair_apply] JSONL: {paths[2]}")


if __name__ == "__main__":
    main()
