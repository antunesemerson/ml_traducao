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
from apply_safe_output_updates import escape_localization_value, protected_tokens, replace_quoted_text


RULE_VERSION = "single_combat_signature_weapon_batch1_apply_v1"
EXPECTED_IDS = {245948, 246103, 246110, 246111, 246093}
EXPECTED_CANDIDATES = 5
EXPECTED_READY = 5
EXPECTED_STALE = 0
EXPECTED_BLOCKED = 0
EXPECTED_ALREADY_APPLIED = 0
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "single_combat_signature_weapon_composer_batch1_production"
CONFIRMATION_LABEL = "single_combat_signature_weapon_composer_batch1_repair"
REVIEWER = "production_single_combat_signature_weapon_batch1_apply"
BAD_ENCODING_MARKERS = ("\ufffd", "\u00c3\u0192", "\u00c3\u201a", "\u00c3\u00af\u00c2\u00bf\u00c2\u00bd")
TOKEN_DELTA_DECISIONS = {"needs_token_delta_review", "needs_context", "already_good"}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any], *, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_single_combat_signature_weapon_batch1_{mode}"
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


def localization_value(text: str) -> str:
    return escape_localization_value(text)


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


def load_decisions(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for marker in BAD_ENCODING_MARKERS:
        if marker in text:
            errors.append(f"bad_encoding_marker:{marker!r}")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    disallowed_apply = [
        int(row["segment_id"])
        for row in rows
        if row.get("decision") in TOKEN_DELTA_DECISIONS and row.get("requires_apply_later")
    ]
    if disallowed_apply:
        errors.append(f"non_corrected_rows_marked_apply:{disallowed_apply}")

    selected = []
    for row in rows:
        segment_id = int(row.get("segment_id") or 0)
        if (
            row.get("decision") == "corrected"
            and segment_id in EXPECTED_IDS
            and row.get("token_delta_review_required") is False
            and row.get("requires_apply_later") is True
        ):
            selected.append(dict(row))

    selected_ids = {int(row["segment_id"]) for row in selected}
    all_corrected_ids = {int(row["segment_id"]) for row in rows if row.get("decision") == "corrected"}
    if selected_ids != EXPECTED_IDS:
        errors.append(f"selected_ids_mismatch:{sorted(selected_ids)}")
    if all_corrected_ids != EXPECTED_IDS:
        errors.append(f"unexpected_corrected_ids:{sorted(all_corrected_ids)}")
    if len(selected) != EXPECTED_CANDIDATES:
        errors.append(f"candidate_count_mismatch:{len(selected)}")
    for row in selected:
        if not as_text(row.get("corrected_text")):
            errors.append(f"empty_corrected_text:{row['segment_id']}")
        row["corrected_localization_text"] = localization_value(as_text(row["corrected_text"]))
    return sorted(selected, key=lambda item: int(item["segment_id"])), errors


def fetch_live_rows(conn, state_run_id: int, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not decisions:
        return []
    placeholders = ",".join("?" for _ in decisions)
    decision_by_id = {int(row["segment_id"]): row for row in decisions}
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
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = source.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = source.id
         AND state.run_id = ?
        WHERE source.id IN ({placeholders})
        ORDER BY source.id
        """,
        (state_run_id, *sorted(decision_by_id)),
    ).fetchall()
    live = []
    for row in rows:
        item = dict(row)
        decision = decision_by_id[int(item["segment_id"])]
        item.update(decision)
        live.append(item)
    return live


def classify_row(output_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["status"] = "ready"
    result["block_reason_detail"] = ""

    def mark(status: str, reason: str = "") -> dict[str, Any]:
        result["status"] = status
        result["block_reason_detail"] = reason
        return result

    segment_id = int(row["segment_id"])
    live_output = as_text(row.get("live_output_text"))
    corrected = as_text(row.get("corrected_localization_text"))
    confirmed = as_text(row.get("confirmed_text"))

    if segment_id not in EXPECTED_IDS:
        return mark("blocked", "segment_not_permitted")
    if as_text(row.get("decision")) != "corrected":
        return mark("blocked", "decision_not_corrected")
    if row.get("token_delta_review_required") is not False:
        return mark("blocked", "token_delta_review_required")
    if row.get("requires_apply_later") is not True:
        return mark("blocked", "not_marked_for_apply")
    if int(row.get("is_active") or 0) != 1:
        return mark("stale", "source_not_active")
    if int(row.get("confirmation_locked") or 0) == 1:
        return mark("blocked", "locked_confirmation")
    if int(row.get("live_needs_output_apply") or 0) != 0:
        return mark("blocked", "needs_output_apply_not_0")
    if not corrected:
        return mark("blocked", "missing_corrected_text")
    if any(marker in corrected for marker in BAD_ENCODING_MARKERS):
        return mark("blocked", "bad_encoding_marker_in_corrected_text")
    if live_output == corrected and row.get("confirmation_source") == CONFIRMATION_SOURCE and row.get("confirmation_label") == CONFIRMATION_LABEL:
        return mark("already_applied", "")
    if protected_tokens(live_output) != protected_tokens(corrected):
        return mark("blocked", "protected_token_signature_changed")
    if confirmed != live_output:
        return mark("stale", "confirmation_not_current_output")
    try:
        file_text = file_text_at(output_root, row)
    except RuntimeError as exc:
        return mark("blocked", str(exc))
    if file_text != live_output:
        return mark("stale", "file_output_db_output_mismatch")
    return result


def validate_counts(rows: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter(row["status"] for row in rows)
    if len(rows) != EXPECTED_CANDIDATES:
        raise RuntimeError(f"Expected candidates={EXPECTED_CANDIDATES}, got {len(rows)}.")
    if counts["ready"] != EXPECTED_READY:
        raise RuntimeError(f"Expected ready={EXPECTED_READY}, got {counts['ready']}.")
    if counts["stale"] != EXPECTED_STALE:
        raise RuntimeError(f"Expected stale={EXPECTED_STALE}, got {counts['stale']}.")
    if counts["blocked"] != EXPECTED_BLOCKED:
        raise RuntimeError(f"Expected blocked={EXPECTED_BLOCKED}, got {counts['blocked']}.")
    if counts["already_applied"] != EXPECTED_ALREADY_APPLIED:
        raise RuntimeError(f"Expected already_applied={EXPECTED_ALREADY_APPLIED}, got {counts['already_applied']}.")
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
        by_file[as_text(row["relative_path"])].append(row)

    applied = 0
    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, file_rows in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row in sorted(file_rows, key=lambda item: int(item["output_line_number"])):
            segment_id = int(row["segment_id"])
            line_index = int(row["output_line_number"]) - 1
            raw_line = lines[line_index]
            if file_text_at(output_root, row) != as_text(row["live_output_text"]):
                raise RuntimeError(f"Live file output drift for segment {segment_id}")
            corrected_semantic = as_text(row["corrected_text"])
            corrected_localization = as_text(row["corrected_localization_text"])
            lines[line_index] = replace_quoted_text(raw_line, corrected_semantic)
            if upsert_confirmation(conn, segment_id=segment_id, confirmed_text=corrected_localization, now=now):
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
                (corrected_localization, lines[line_index], sha256_text(corrected_localization), now, segment_id),
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
    apply_counts: dict[str, int] | None = None,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, apply=apply)
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "output_line_number",
        "decision",
        "subpattern",
        "status",
        "block_reason_detail",
        "live_final_state",
        "live_needs_output_apply",
        "confirmation_level",
        "confirmation_source",
        "confirmation_label",
        "corrected_localization_text",
        "review_notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    apply_counts = apply_counts or {"applied": 0, "confirmation_promoted": 0, "output_written": 0, "files_touched": 0}
    lines = [
        "Single combat signature weapon batch 1 protected apply"
        if apply
        else "Single combat signature weapon batch 1 protected apply dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Decisions JSONL: {decisions_jsonl}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Apply: {apply}",
        "",
        "Counts:",
        f"- candidates: {len(rows)}",
        f"- ready: {counts['ready']}",
        f"- stale: {counts['stale']}",
        f"- blocked: {counts['blocked']}",
        f"- already_applied: {counts['already_applied']}",
        f"- applied: {apply_counts.get('applied', 0)}",
        f"- confirmation_promoted: {apply_counts.get('confirmation_promoted', 0)}",
        f"- output_written: {apply_counts.get('output_written', 0)}",
        f"- files_touched: {apply_counts.get('files_touched', 0)}",
        "",
        "Permitted repair segments:",
    ]
    for row in rows:
        lines.append(
            f"- {row['segment_id']} | {row['source_key']} | status={row['status']} | path={row['relative_path']}"
        )
        if row.get("block_reason_detail"):
            lines.append(f"  block_reason={row['block_reason_detail']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def run(*, decisions_jsonl: Path, apply: bool) -> tuple[Counter, dict[str, int], tuple[Path, Path, Path]]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    decisions, decision_errors = load_decisions(decisions_jsonl)
    if decision_errors:
        raise RuntimeError("; ".join(decision_errors))
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
        paths = write_reports(
            settings,
            decisions_jsonl=decisions_jsonl,
            rows=rows,
            counts=counts,
            apply=apply,
            apply_counts=apply_counts,
        )
    return counts, apply_counts, paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        counts, apply_counts, paths = run(decisions_jsonl=args.decisions_jsonl, apply=args.apply)
    except Exception as exc:
        print(f"[single_combat_signature_weapon_batch1_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[single_combat_signature_weapon_batch1_apply] Completed")
    print(f"[single_combat_signature_weapon_batch1_apply] Apply: {args.apply}")
    print(f"[single_combat_signature_weapon_batch1_apply] Candidates: {EXPECTED_CANDIDATES}")
    print(f"[single_combat_signature_weapon_batch1_apply] Ready: {counts['ready']}")
    print(f"[single_combat_signature_weapon_batch1_apply] Stale: {counts['stale']}")
    print(f"[single_combat_signature_weapon_batch1_apply] Blocked: {counts['blocked']}")
    print(f"[single_combat_signature_weapon_batch1_apply] Already applied: {counts['already_applied']}")
    print(f"[single_combat_signature_weapon_batch1_apply] Applied: {apply_counts.get('applied', 0)}")
    print(
        "[single_combat_signature_weapon_batch1_apply] Confirmation promoted: "
        f"{apply_counts.get('confirmation_promoted', 0)}"
    )
    print(f"[single_combat_signature_weapon_batch1_apply] Output written: {apply_counts.get('output_written', 0)}")
    print(f"[single_combat_signature_weapon_batch1_apply] Files touched: {apply_counts.get('files_touched', 0)}")
    print(f"[single_combat_signature_weapon_batch1_apply] Report: {paths[0]}")
    print(f"[single_combat_signature_weapon_batch1_apply] CSV: {paths[1]}")
    print(f"[single_combat_signature_weapon_batch1_apply] JSONL: {paths[2]}")


if __name__ == "__main__":
    main()
