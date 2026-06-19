from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "ui_compact_label_override_apply_v1"
SEGMENT_ID = 161186
RELATIVE_PATH = "laws_l_spanish.yml"
SOURCE_KEY = "border_wars_laws"
CURRENT_TEXT = "Política de Guerras de Fronteira"
PROPOSED_TEXT = "Guerras de Fronteira"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "ui_compact_label_override_production"
CONFIRMATION_LABEL = "border_wars_laws:compact_display_v1"
REVIEWER = "learning_front_ui_compact_label_override"


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


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


def fetch_live_row(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.is_active,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.output_line_number,
            o.portuguese_text AS output_text,
            o.output_raw_line
        FROM source_segments s
        JOIN output_segments o
          ON o.segment_id = s.id
        WHERE s.id = ?
        LIMIT 1
        """,
        (SEGMENT_ID,),
    ).fetchone()
    return dict(row) if row else None


def file_current(output_root: Path, row: dict[str, Any]) -> tuple[Path, list[str], int, str, str]:
    output_path = output_root / Path(RELATIVE_PATH)
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError("Output line out of range.")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError("Output line has no quoted localization value.")
    text = raw_line[first_quote + 1 : last_quote].replace('\\"', '"')
    return output_path, lines, line_index, raw_line, text


def evaluate(conn, output_root: Path) -> tuple[str, list[str], dict[str, Any] | None, tuple | None]:
    row = fetch_live_row(conn)
    reasons: list[str] = []
    file_payload = None

    if row is None:
        return "blocked", ["missing_live_row"], None, None
    if int(row["is_active"] or 0) != 1:
        reasons.append("source_not_active")
    if row["relative_path"] != RELATIVE_PATH:
        reasons.append("relative_path_mismatch")
    if row["source_key"] != SOURCE_KEY:
        reasons.append("source_key_mismatch")
    if as_text(row["english_text"]) != "Border Wars Policy":
        reasons.append("english_text_mismatch")
    if as_text(row["spanish_text"]) != "Política de guerras fronterizas":
        reasons.append("spanish_text_mismatch")
    if as_text(row["old_text"]) != CURRENT_TEXT:
        reasons.append("old_text_mismatch")
    if protected_tokens(CURRENT_TEXT) != protected_tokens(PROPOSED_TEXT):
        reasons.append("protected_tokens_changed")

    if as_text(row["output_text"]) == PROPOSED_TEXT:
        confirmation = latest_confirmation(conn, SEGMENT_ID)
        if (
            confirmation
            and confirmation.get("confirmation_source") == CONFIRMATION_SOURCE
            and confirmation.get("confirmation_label") == CONFIRMATION_LABEL
            and confirmation.get("confirmed_text") == PROPOSED_TEXT
        ):
            return "already_applied", [], row, None
        return "ready_confirmation_only", [], row, None

    if as_text(row["output_text"]) != CURRENT_TEXT:
        reasons.append("db_output_text_unexpected")

    try:
        file_payload = file_current(output_root, row)
    except RuntimeError as exc:
        reasons.append(str(exc))
    else:
        _path, _lines, _line_index, _raw_line, file_text = file_payload
        if file_text == PROPOSED_TEXT and as_text(row["output_text"]) == PROPOSED_TEXT:
            return "ready_confirmation_only", [], row, file_payload
        if file_text != CURRENT_TEXT:
            reasons.append("file_output_text_unexpected")

    if reasons:
        return "blocked", reasons, row, file_payload
    return "ready", [], row, file_payload


def upsert_confirmation(conn, now: str) -> bool:
    confirmation = latest_confirmation(conn, SEGMENT_ID)
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == PROPOSED_TEXT
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
            updated_at = excluded.updated_at
        """,
        (
            SEGMENT_ID,
            CONFIRMATION_LEVEL,
            PROPOSED_TEXT,
            CONFIRMATION_SOURCE,
            CONFIRMATION_LABEL,
            REVIEWER,
            now,
            now,
        ),
    )
    return not bool(already)


def write_report(settings: dict[str, Any], lines: list[str]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{now_stamp()}_ui_compact_label_override_apply.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(*, apply: bool) -> Path:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    conn = db.connect(settings)
    now = datetime.now().isoformat(timespec="seconds")
    applied = 0
    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    try:
        status, reasons, row, file_payload = evaluate(conn, output_root)
        if apply and status not in {"ready", "ready_confirmation_only", "already_applied"}:
            raise RuntimeError(f"Refusing apply: status={status}, reasons={reasons}")

        if apply and status in {"ready", "ready_confirmation_only"}:
            if status == "ready":
                assert file_payload is not None
                output_path, lines, line_index, raw_line, _file_text = file_payload
                lines[line_index] = replace_quoted_text(raw_line, PROPOSED_TEXT)
                output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
                conn.execute(
                    """
                    UPDATE output_segments
                    SET portuguese_text = ?,
                        output_raw_line = ?,
                        portuguese_hash = ?,
                        last_indexed_at = ?
                    WHERE segment_id = ?
                    """,
                    (
                        PROPOSED_TEXT,
                        lines[line_index],
                        sha256_text(PROPOSED_TEXT),
                        now,
                        SEGMENT_ID,
                    ),
                )
                applied = 1
                output_written = 1
                files_touched = 1
            if upsert_confirmation(conn, now):
                confirmation_promoted = 1
            conn.commit()
            status, reasons, row, file_payload = evaluate(conn, output_root)
        else:
            conn.rollback()

        lines = [
            "UI compact label override apply",
            f"Rule version: {RULE_VERSION}",
            f"Mode: {'apply' if apply else 'dry_run'}",
            "",
            f"segment_id: {SEGMENT_ID}",
            f"source_key: {SOURCE_KEY}",
            f"before: {CURRENT_TEXT}",
            f"after: {PROPOSED_TEXT}",
            f"status: {status}",
            f"reasons: {', '.join(reasons) if reasons else 'none'}",
            "",
            "Summary:",
            "- candidates: 1",
            f"- ready: {1 if status in {'ready', 'ready_confirmation_only'} else 0}",
            f"- already_applied: {1 if status == 'already_applied' else 0}",
            f"- blocked: {1 if status == 'blocked' else 0}",
            f"- applied: {applied}",
            f"- confirmation_promoted: {confirmation_promoted}",
            f"- output_written: {output_written}",
            f"- files_touched: {files_touched}",
            "",
            "Safety note:",
            "- This script targets only laws_l_spanish.yml::border_wars_laws.",
            "- It does not alter source files.",
        ]
        report = write_report(settings, lines)
    finally:
        conn.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = run(apply=args.apply)
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
