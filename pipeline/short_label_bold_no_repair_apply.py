from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from issue_short_label_bold_no_repair_shadow import (
    english_hits,
    has_duplicate_negative_after_repair,
    spanish_hits,
    structural_tokens,
    validator_summary,
)


RULE_VERSION = "short_label_bold_no_repair_apply_v1"
CHECKPOINT_RUN_ID = 3
EXPECTED_CANDIDATES = 91
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "short_label_bold_no_repair_production"
CONFIRMATION_LABEL = "short_label_bold_no_repair:checkpoint_3"
REVIEWER = "production_short_label_bold_no_repair_apply"


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


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


def fetch_checkpoint(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_bold_no_repair_checkpoint_runs
        WHERE id = ?
        """,
        (CHECKPOINT_RUN_ID,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run {CHECKPOINT_RUN_ID} not found.")
    checkpoint = dict(row)
    if int(checkpoint.get("checkpoint_allowed_count") or 0) != EXPECTED_CANDIDATES:
        raise RuntimeError(
            f"Checkpoint {CHECKPOINT_RUN_ID} allowed count drifted: "
            f"{checkpoint.get('checkpoint_allowed_count')} != {EXPECTED_CANDIDATES}."
        )
    return checkpoint


def fetch_allowed_items(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_bold_no_repair_checkpoint_items
        WHERE checkpoint_run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY relative_path, source_line_number, source_key, segment_id, id
        """,
        (CHECKPOINT_RUN_ID,),
    ).fetchall()
    items = [dict(row) for row in rows]
    if len(items) != EXPECTED_CANDIDATES:
        raise RuntimeError(f"Expected {EXPECTED_CANDIDATES} allowed checkpoint items, found {len(items)}.")
    if len({int(item["segment_id"]) for item in items}) != EXPECTED_CANDIDATES:
        raise RuntimeError("Allowed checkpoint items are not one unique row per segment.")
    return items


def fetch_live_row(conn, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.is_active,
            o.output_line_number,
            o.portuguese_text AS output_text,
            o.output_raw_line
        FROM source_segments s
        LEFT JOIN output_segments o
          ON o.segment_id = s.id
        WHERE s.id = ?
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def file_current_text(output_root: Path, live_row: dict[str, Any]) -> tuple[str | None, str | None]:
    relative_path = as_text(live_row.get("relative_path"))
    line_number = live_row.get("output_line_number")
    if not relative_path or line_number is None:
        return None, None
    output_path = output_root / Path(relative_path)
    if not output_path.exists():
        return None, None
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(line_number) - 1
    if line_index < 0 or line_index >= len(lines):
        return None, None
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        return raw_line, None
    return raw_line, raw_line[first_quote + 1 : last_quote].replace('\\"', '"')


def validation_reasons(current: str, corrected: str) -> list[str]:
    reasons: list[str] = []
    if not corrected.strip():
        reasons.append("missing_corrected_text")
    if current == corrected:
        reasons.append("no_text_delta")
    if structural_tokens(current) != structural_tokens(corrected):
        reasons.append("structural_tokens_changed")
    if has_duplicate_negative_after_repair(corrected):
        reasons.append("duplicate_negative_after_bold_no_repair")
    remaining_spanish = [hit for hit in spanish_hits(corrected) if "#bold\\s+no#!" not in hit]
    remaining_english = english_hits(corrected)
    if remaining_spanish:
        reasons.append("spanish_residual_after_repair:" + ",".join(remaining_spanish[:4]))
    if remaining_english:
        reasons.append("english_residual_after_repair:" + ",".join(remaining_english[:4]))
    validation = validator_summary(corrected)
    if validation:
        reasons.append("local_validator:" + validation)
    return reasons


def evaluate_item(
    conn,
    *,
    item: dict[str, Any],
    output_root: Path,
) -> tuple[str, list[str], dict[str, Any] | None, str | None]:
    reasons: list[str] = []
    segment_id = int(item["segment_id"])
    live_row = fetch_live_row(conn, segment_id)
    current = as_text(item.get("current_text"))
    corrected = as_text(item.get("corrected_text"))
    new_line: str | None = None

    if int(item.get("checkpoint_run_id") or 0) != CHECKPOINT_RUN_ID:
        reasons.append("wrong_checkpoint_run_id")
    if int(item.get("checkpoint_allowed") or 0) != 1:
        reasons.append("checkpoint_not_allowed")
    if as_text(item.get("block_reason")).strip():
        reasons.append("checkpoint_item_has_block_reason")

    reasons.extend(validation_reasons(current, corrected))

    if live_row is None:
        reasons.append("missing_live_row")
    else:
        if int(live_row.get("is_active") or 0) != 1:
            reasons.append("source_not_active")
        if live_row.get("relative_path") != item.get("relative_path"):
            reasons.append("relative_path_mismatch")
        if live_row.get("source_key") != item.get("source_key"):
            reasons.append("source_key_mismatch")
        if as_text(live_row.get("output_text")) == corrected:
            return "already_applied", [], live_row, None
        if as_text(live_row.get("output_text")) != current:
            reasons.append("stale_db_output_text")

        raw_line, file_text = file_current_text(output_root, live_row)
        if live_row.get("output_line_number") is None:
            reasons.append("missing_output_line_number")
        if raw_line is None:
            reasons.append("missing_output_file_or_line")
        elif file_text is None:
            reasons.append("line_without_quoted_value")
        elif file_text != current:
            reasons.append("stale_file_output_text")
        else:
            new_line = replace_quoted_text(raw_line, corrected)

    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        if as_text(confirmation.get("confirmed_text")) != corrected:
            reasons.append("locked_confirmation_diverges")

    if any(reason.startswith("stale_") for reason in reasons):
        return "stale", reasons, live_row, new_line
    if reasons:
        return "blocked", reasons, live_row, new_line
    return "ready", [], live_row, new_line


def upsert_confirmation(conn, *, segment_id: int, corrected: str, now: str) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        return False
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == corrected
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
        (segment_id, CONFIRMATION_LEVEL, corrected, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not bool(already)


def apply_entries(
    conn,
    *,
    output_root: Path,
    entries: list[tuple[dict[str, Any], dict[str, Any], str]],
    now: str,
) -> tuple[int, int, int]:
    by_file: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    for item, live_row, new_line in entries:
        by_file.setdefault(as_text(live_row["relative_path"]), []).append((item, live_row, new_line))

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, file_entries in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for item, live_row, new_line in sorted(file_entries, key=lambda entry: int(entry[1]["output_line_number"])):
            segment_id = int(item["segment_id"])
            corrected = as_text(item["corrected_text"])
            line_index = int(live_row["output_line_number"]) - 1
            lines[line_index] = new_line
            if upsert_confirmation(conn, segment_id=segment_id, corrected=corrected, now=now):
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
                (corrected, new_line, sha256_text(corrected), now, segment_id),
            )
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return confirmation_promoted, output_written, files_touched


def report_paths(settings: dict[str, Any], *, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{now_stamp()}_short_label_bold_no_repair_{suffix}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    settings: dict[str, Any],
    *,
    apply: bool,
    checkpoint: dict[str, Any],
    rows: list[dict[str, Any]],
    counters: Counter,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, apply=apply)
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "status",
        "reasons",
        "current_text",
        "corrected_text",
        "output_line_number",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short label #bold No#! protected repair dry-run" if not apply else "Short label #bold No#! protected repair apply",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Mode: {'apply' if apply else 'dry_run'}",
        f"Checkpoint run id: {CHECKPOINT_RUN_ID}",
        f"Checkpoint shadow run id: {checkpoint.get('shadow_run_id')}",
        "",
        "Summary:",
        f"- candidates: {counters['candidates']}",
        f"- ready: {counters['ready']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- already_applied: {counters['already_applied']}",
        f"- applied: {counters['applied']}",
        f"- confirmation_promoted: {counters['confirmation_promoted']}",
        f"- output_written: {counters['output_written']}",
        f"- files_touched: {counters['files_touched']}",
        f"- TXT: {txt_path}",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        "",
        "Samples:",
    ]
    for row in rows[:25]:
        lines.extend(
            [
                f"- segment={row['segment_id']} | {row['relative_path']}::{row['source_key']} | status={row['status']}",
                f"  before: {short(row['current_text'])}",
                f"  after:  {short(row['corrected_text'])}",
                f"  reasons: {row['reasons'] or 'none'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This script consumes only checkpoint run id 3 and checkpoint_allowed=1.",
            "- It refuses apply unless exactly 91 rows are live-ready and no stale/blocked rows exist.",
            "- Fase A intentionally runs without --apply.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    counters: Counter = Counter()
    report_rows: list[dict[str, Any]] = []
    ready_entries: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    now = datetime.now().isoformat(timespec="seconds")

    with db.connect(settings) as conn:
        checkpoint = fetch_checkpoint(conn)
        items = fetch_allowed_items(conn)
        for item in items:
            status, reasons, live_row, new_line = evaluate_item(conn, item=item, output_root=output_root)
            counters[status] += 1
            counters["candidates"] += 1
            report_rows.append(
                {
                    "segment_id": item["segment_id"],
                    "relative_path": item["relative_path"],
                    "source_key": item["source_key"],
                    "status": status,
                    "reasons": ";".join(reasons),
                    "current_text": item["current_text"],
                    "corrected_text": item["corrected_text"],
                    "output_line_number": "" if live_row is None else live_row.get("output_line_number"),
                }
            )
            if status == "ready" and live_row is not None and new_line is not None:
                ready_entries.append((item, live_row, new_line))

        if apply:
            if (
                counters["candidates"] != EXPECTED_CANDIDATES
                or counters["ready"] != EXPECTED_CANDIDATES
                or counters["stale"]
                or counters["blocked"]
                or counters["already_applied"]
            ):
                raise RuntimeError(
                    "Refusing --apply: expected candidates=ready=91 and stale=blocked=already_applied=0; "
                    f"got candidates={counters['candidates']}, ready={counters['ready']}, "
                    f"stale={counters['stale']}, blocked={counters['blocked']}, "
                    f"already_applied={counters['already_applied']}."
                )
            promoted, written, files_touched = apply_entries(conn, output_root=output_root, entries=ready_entries, now=now)
            counters["confirmation_promoted"] = promoted
            counters["output_written"] = written
            counters["applied"] = written
            counters["files_touched"] = files_touched
            conn.commit()
        else:
            conn.rollback()

        txt_path, csv_path, jsonl_path = write_reports(
            settings,
            apply=apply,
            checkpoint=checkpoint,
            rows=report_rows,
            counters=counters,
        )

    print("[short_label_bold_no_repair_apply] Done")
    print(f"[short_label_bold_no_repair_apply] Rule version: {RULE_VERSION}")
    print(f"[short_label_bold_no_repair_apply] Mode: {'apply' if apply else 'dry_run'}")
    print(f"[short_label_bold_no_repair_apply] Checkpoint run id: {CHECKPOINT_RUN_ID}")
    print(f"[short_label_bold_no_repair_apply] Candidates: {counters['candidates']}")
    print(f"[short_label_bold_no_repair_apply] Ready: {counters['ready']}")
    print(f"[short_label_bold_no_repair_apply] Stale: {counters['stale']}")
    print(f"[short_label_bold_no_repair_apply] Blocked: {counters['blocked']}")
    print(f"[short_label_bold_no_repair_apply] Already applied: {counters['already_applied']}")
    print(f"[short_label_bold_no_repair_apply] Applied: {counters['applied']}")
    print(f"[short_label_bold_no_repair_apply] Confirmation promoted: {counters['confirmation_promoted']}")
    print(f"[short_label_bold_no_repair_apply] Output written: {counters['output_written']}")
    print(f"[short_label_bold_no_repair_apply] Files touched: {counters['files_touched']}")
    print(f"[short_label_bold_no_repair_apply] TXT: {txt_path}")
    print(f"[short_label_bold_no_repair_apply] CSV: {csv_path}")
    print(f"[short_label_bold_no_repair_apply] JSONL: {jsonl_path}")
    return {
        "apply": apply,
        "counts": dict(counters),
        "txt_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Protected dry-run/apply for short-label #bold No#! repair checkpoint 3.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(apply=args.apply)
