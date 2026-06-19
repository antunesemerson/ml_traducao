from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import escape_localization_value, replace_quoted_text
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "short_label_single_issue_residual_queue225_apply_v1"
DEFAULT_DECISIONS_JSONL = Path("reports/20260618_123935_802713_short_label_single_issue_residual_queue225_review.jsonl")
EXPECTED_CANDIDATES = 15
EXPECTED_BLOCKED = 0
EXPECTED_STALE = 0
ALLOWLIST_IDS = {
    2140,
    79796,
    69453,
    78556,
    43207,
    2303,
    284330,
    2306,
    283461,
    144513,
    283532,
    284058,
    284332,
    283689,
    283459,
}
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "short_label_single_issue_residual_queue225_repair_production"
CONFIRMATION_LABEL = "short_label_single_issue_residual_queue225_repair"
REVIEWER = "production_short_label_single_issue_residual_queue225_apply"
BAD_TEXT_RE = re.compile(r"Ãƒ|Ã‚|Ã¯Â¿Â½|ï¿½|Â¿|Â¡")
QUESTION_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\?(?=[A-Za-zÀ-ÖØ-öø-ÿ])")

EXPECTED_CORRECTED = {
    2140: '"Olhos indiscretos devem ser — #EMP serão cegados!#!"',
    79796: '"Bem, deveríamos nomear vocês #EMP imediatamente...#!"',
    69453: '"Fora da minha vista, #emp imediatamente!#!"',
    78556: '"A #EMP mim#! me agradariam tais bênçãos".',
    43207: '"Como #EMP ousais#!"',
    2303: "#N #bold Redução importante#!#! da sua #V probabilidade de sucesso#!",
    284330: "[CHARACTER.GetShortUIName|U] #bold não#! tem o nome régio de $NAME$",
    2306: "#P #bold Aumento importante#!#! da sua #V probabilidade de sucesso#!",
    283461: "[CHARACTER.GetShortUIName|V] #bold não#! possui [treasury|lE]",
    144513: "A [disaster|lE] chegou à fase de #bold Recuperação#!",
    283532: "#bold Não#! você não tem o [modifier|El]; $MODIFIER|V$",
    284058: "#bold Não#! é [spouse|lE] de [liege.GetShortUIName]",
    284332: "#bold Não#! você não tem o nome régio de $NAME$",
    283689: "#bold Não#! não se permite ter mais cônjuges",
    283459: "#bold Não#! você não tem [treasury|lE]",
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
    base = reports_dir / f"{stamp}_short_label_single_issue_residual_queue225_{mode}"
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


def has_bad_text(value: str) -> bool:
    return bool(BAD_TEXT_RE.search(value))


def question_inside_word(value: str) -> bool:
    return bool(QUESTION_INSIDE_WORD_RE.search(value))


def load_decisions(decisions_jsonl: Path) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    rows = [json.loads(line) for line in decisions_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [dict(row) for row in rows if row.get("requires_apply_later") is True]
    selected_ids = {int(row.get("segment_id") or 0) for row in selected}
    if selected_ids != ALLOWLIST_IDS:
        errors.append(f"selected_ids_mismatch:{sorted(selected_ids)}")
    if len(selected) != EXPECTED_CANDIDATES:
        errors.append(f"candidate_count_mismatch:{len(selected)}")
    for row in selected:
        segment_id = int(row["segment_id"])
        if segment_id not in ALLOWLIST_IDS:
            errors.append(f"segment_not_allowlisted:{segment_id}")
            continue
        if row.get("decision") not in {"safe_spanish_residual_repair", "safe_markup_no_repair"}:
            errors.append(f"decision_not_allowed:{segment_id}:{row.get('decision')}")
        if row.get("subpolicy") not in {"dialogue_spanish_residual_microrepair", "bold_no_ptbr_microrepair"}:
            errors.append(f"subpolicy_not_allowed:{segment_id}:{row.get('subpolicy')}")
        corrected = as_text(row.get("corrected_text"))
        expected = EXPECTED_CORRECTED[segment_id]
        if corrected != expected:
            errors.append(f"corrected_text_mismatch:{segment_id}")
        if has_bad_text(corrected):
            errors.append(f"corrected_text_mojibake:{segment_id}")
        if question_inside_word(corrected):
            errors.append(f"corrected_text_question_inside_word:{segment_id}")
        current = as_text(row.get("current_text"))
        if structural_tokens(current) != structural_tokens(corrected):
            errors.append(f"structural_tokens_changed:{segment_id}")
        row["display_current_text"] = current
        row["display_corrected_text"] = corrected
        row["expected_current_text"] = escape_localization_value(current)
        row["corrected_text"] = escape_localization_value(corrected)
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
    if segment_id not in ALLOWLIST_IDS:
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
    if confirmed != live_output and escape_localization_value(confirmed) != live_output:
        return mark("stale", "confirmation_not_current_output")
    if has_bad_text(corrected):
        return mark("blocked", "corrected_text_mojibake")
    if question_inside_word(corrected):
        return mark("blocked", "corrected_text_question_inside_word")
    if structural_tokens(as_text(row.get("display_current_text"))) != structural_tokens(as_text(row.get("display_corrected_text"))):
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
    if counts["blocked"] != EXPECTED_BLOCKED:
        raise RuntimeError(f"Expected blocked={EXPECTED_BLOCKED}, got {counts['blocked']}")
    if counts["stale"] != EXPECTED_STALE:
        raise RuntimeError(f"Expected stale={EXPECTED_STALE}, got {counts['stale']}")
    if counts["ready"] + counts["already_applied"] != EXPECTED_CANDIDATES:
        raise RuntimeError(
            "Expected ready + already_applied="
            f"{EXPECTED_CANDIDATES}, got ready={counts['ready']} already_applied={counts['already_applied']}"
        )
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
        if int(row["segment_id"]) not in ALLOWLIST_IDS:
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
            display_corrected = as_text(row["display_corrected_text"])
            lines[line_index] = replace_quoted_text(raw_line, display_corrected)
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
    decisions_jsonl: Path,
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
        "subpolicy",
        "status",
        "block_reason_detail",
        "live_final_state",
        "live_needs_output_apply",
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
    title = "Short-label single-issue residual queue 225 protected apply" if apply else "Short-label single-issue residual queue 225 protected apply dry-run"
    lines = [
        title,
        f"Rule version: {RULE_VERSION}",
        f"Decisions JSONL: {decisions_jsonl}",
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


def run(*, decisions_jsonl: Path, apply: bool) -> tuple[Counter, dict[str, int], tuple[Path, Path, Path]]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    decisions, errors = load_decisions(decisions_jsonl)
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
        paths = write_reports(settings, decisions_jsonl=decisions_jsonl, rows=rows, counts=counts, apply=apply, apply_counts=apply_counts)
    return counts, apply_counts, paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions-jsonl", default=DEFAULT_DECISIONS_JSONL, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        counts, apply_counts, paths = run(decisions_jsonl=args.decisions_jsonl, apply=args.apply)
    except Exception as exc:
        print(f"[short_label_single_issue_residual_queue225_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[short_label_single_issue_residual_queue225_apply] Completed")
    print(f"[short_label_single_issue_residual_queue225_apply] Apply: {args.apply}")
    print(f"[short_label_single_issue_residual_queue225_apply] Candidates: {EXPECTED_CANDIDATES}")
    print(f"[short_label_single_issue_residual_queue225_apply] Ready: {counts['ready']}")
    print(f"[short_label_single_issue_residual_queue225_apply] Blocked: {counts['blocked']}")
    print(f"[short_label_single_issue_residual_queue225_apply] Stale: {counts['stale']}")
    print(f"[short_label_single_issue_residual_queue225_apply] Already applied: {counts['already_applied']}")
    print(f"[short_label_single_issue_residual_queue225_apply] Applied: {apply_counts.get('applied', 0)}")
    print(f"[short_label_single_issue_residual_queue225_apply] Confirmation promoted: {apply_counts.get('confirmation_promoted', 0)}")
    print(f"[short_label_single_issue_residual_queue225_apply] Output written: {apply_counts.get('output_written', 0)}")
    print(f"[short_label_single_issue_residual_queue225_apply] Files touched: {apply_counts.get('files_touched', 0)}")
    print(f"[short_label_single_issue_residual_queue225_apply] Report: {paths[0]}")
    print(f"[short_label_single_issue_residual_queue225_apply] CSV: {paths[1]}")
    print(f"[short_label_single_issue_residual_queue225_apply] JSONL: {paths[2]}")


if __name__ == "__main__":
    main()
