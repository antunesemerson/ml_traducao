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


RULE_VERSION = "nickname_gender_article_held_exceptions_apply_v1"
NICKNAME_PATH = "nicknames_l_spanish.yml"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "nickname_gender_article_held_exception_production"
CONFIRMATION_LABEL = "nickname_gender_article_held_exception:structural_watch_v1"
REVIEWER = "production_nickname_gender_article_held_exceptions_apply"

TARGETS: dict[int, dict[str, str]] = {
    229527: {
        "source_key": "nick_the_deep_minded",
        "current_text": "o/a Dout[CHARACTER.Custom('ES_OA')]",
        "proposed_text": "[Select_CString( CHARACTER.IsFemale, 'a', 'o' )] Dout[CHARACTER.Custom('ES_OA')]",
        "male_preview": "o Douto",
        "female_preview": "a Douta",
        "watch_note": "semantica Douto/Douta fica em watch",
    },
    229645: {
        "source_key": "nick_the_campeador",
        "current_text": "o Campeador[CHARACTER.Custom('ES_XA')]",
        "proposed_text": "[Select_CString( CHARACTER.IsFemale, 'a', 'o' )] Campeador[CHARACTER.Custom('ES_XA')]",
        "male_preview": "o Campeador",
        "female_preview": "a Campeadora",
        "watch_note": "titulo cultural fica em watch",
    },
    229908: {
        "source_key": "nick_the_naysayer",
        "current_text": "o/a Negativ[CHARACTER.Custom('ES_OA')]",
        "proposed_text": "[Select_CString( CHARACTER.IsFemale, 'a', 'o' )] Negativ[CHARACTER.Custom('ES_OA')]",
        "male_preview": "o Negativo",
        "female_preview": "a Negativa",
        "watch_note": "semantica Negativo/Negativa fica em watch",
    },
    229988: {
        "source_key": "nick_the_hermit",
        "current_text": "o/a Eremit[CHARACTER.Custom('ES_OA')]",
        "proposed_text": "[Select_CString( CHARACTER.IsFemale, 'a Eremita', 'o Eremita' )]",
        "male_preview": "o Eremita",
        "female_preview": "a Eremita",
        "watch_note": "Eremita e comum de dois generos; nao usar Eremito",
    },
    230004: {
        "source_key": "nick_the_zealot",
        "current_text": "o/a Fervoros[CHARACTER.Custom('ES_OA')]",
        "proposed_text": "[Select_CString( CHARACTER.IsFemale, 'a', 'o' )] Fervoros[CHARACTER.Custom('ES_OA')]",
        "male_preview": "o Fervoroso",
        "female_preview": "a Fervorosa",
        "watch_note": "semantica Fervoroso/Fervorosa fica em watch",
    },
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_state_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


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


def fetch_live_row(conn, *, segment_id: int, state_run_id: int | None) -> dict[str, Any] | None:
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
            o.output_raw_line,
            state.final_state,
            state.review_state,
            state.apply_state,
            state.needs_output_apply
        FROM source_segments s
        LEFT JOIN output_segments o
          ON o.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id = ?
        LIMIT 1
        """,
        (state_run_id, segment_id),
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


def evaluate_row(
    conn,
    *,
    segment_id: int,
    live_row: dict[str, Any] | None,
    output_root: Path,
) -> tuple[str, list[str], str | None]:
    target = TARGETS[segment_id]
    reasons: list[str] = []
    new_line: str | None = None
    if live_row is None:
        return "blocked", ["missing_live_row"], None

    if int(live_row.get("segment_id") or 0) != segment_id:
        reasons.append("segment_id_mismatch")
    if int(live_row.get("is_active") or 0) != 1:
        reasons.append("source_not_active")
    if live_row.get("relative_path") != NICKNAME_PATH:
        reasons.append("unexpected_relative_path")
    if live_row.get("source_key") != target["source_key"]:
        reasons.append("source_key_mismatch")
    if as_text(live_row.get("output_text")) != target["current_text"]:
        reasons.append("stale_db_output_text")

    raw_line, file_text = file_current_text(output_root, live_row)
    if live_row.get("output_line_number") is None:
        reasons.append("missing_output_line_number")
    if raw_line is None:
        reasons.append("missing_output_file_or_line")
    elif file_text is None:
        reasons.append("line_without_quoted_value")
    elif file_text != target["current_text"]:
        reasons.append("stale_file_output_text")
    else:
        new_line = replace_quoted_text(raw_line, target["proposed_text"])

    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        if as_text(confirmation.get("confirmed_text")) != target["proposed_text"]:
            reasons.append("locked_confirmation_diverges")

    if as_text(live_row.get("output_text")) == target["proposed_text"] and (
        confirmation is None or as_text(confirmation.get("confirmed_text")) == target["proposed_text"]
    ):
        return "already_applied", [], new_line
    if any(reason.startswith("stale_") for reason in reasons):
        return "stale", reasons, new_line
    if reasons:
        return "blocked", reasons, new_line
    return "ready", [], new_line


def upsert_confirmation(conn, *, segment_id: int, proposed: str, now: str) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        return False
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == proposed
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
        (segment_id, CONFIRMATION_LEVEL, proposed, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not bool(already)


def apply_entries(
    conn,
    *,
    output_root: Path,
    entries: list[tuple[int, dict[str, Any], str]],
    now: str,
) -> tuple[int, int, int]:
    by_file: dict[str, list[tuple[int, dict[str, Any], str]]] = {}
    for segment_id, live_row, new_line in entries:
        by_file.setdefault(as_text(live_row["relative_path"]), []).append((segment_id, live_row, new_line))

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, file_entries in sorted(by_file.items()):
        if relative_path != NICKNAME_PATH:
            raise RuntimeError(f"Refusing to touch unexpected file: {relative_path}")
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for segment_id, live_row, new_line in sorted(file_entries, key=lambda item: int(item[1]["output_line_number"])):
            proposed = TARGETS[segment_id]["proposed_text"]
            line_index = int(live_row["output_line_number"]) - 1
            lines[line_index] = new_line
            if upsert_confirmation(conn, segment_id=segment_id, proposed=proposed, now=now):
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
                (proposed, new_line, sha256_text(proposed), now, segment_id),
            )
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return confirmation_promoted, output_written, files_touched


def report_paths(settings: dict[str, Any], *, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = "apply" if apply else "dry_run"
    base = reports_dir / f"{now_stamp()}_nickname_gender_article_held_exceptions_{suffix}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    settings: dict[str, Any],
    *,
    apply: bool,
    state_run_id: int | None,
    rows: list[dict[str, Any]],
    counters: Counter,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, apply=apply)
    fields = [
        "segment_id",
        "source_key",
        "status",
        "reasons",
        "current_text",
        "proposed_text",
        "male_preview",
        "female_preview",
        "watch_note",
        "output_line_number",
        "final_state",
        "review_state",
        "apply_state",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Nickname gender/article held exceptions dry-run" if not apply else "Nickname gender/article held exceptions apply",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Mode: {'apply' if apply else 'dry_run'}",
        f"Segment-state run id before apply: {state_run_id}",
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
        "Previews:",
    ]
    for row in rows:
        lines.extend(
            [
                f"- segment={row['segment_id']} | {row['source_key']} | status={row['status']}",
                f"  M: {row['male_preview']}",
                f"  F: {row['female_preview']}",
                f"  current: {short(row['current_text'])}",
                f"  proposed: {short(row['proposed_text'])}",
                f"  watch: {row['watch_note']}",
                f"  reasons: {row['reasons'] or 'none'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This microqueue is hardcoded to exactly five held nickname exceptions.",
            "- It does not touch output unless --apply is passed.",
            "- Fase A intentionally runs without --apply.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    counters: Counter = Counter()
    rows: list[dict[str, Any]] = []
    ready_entries: list[tuple[int, dict[str, Any], str]] = []
    now = datetime.now().isoformat(timespec="seconds")

    with db.connect(settings) as conn:
        state_run_id = latest_state_run_id(conn)
        for segment_id, target in TARGETS.items():
            live_row = fetch_live_row(conn, segment_id=segment_id, state_run_id=state_run_id)
            status, reasons, new_line = evaluate_row(conn, segment_id=segment_id, live_row=live_row, output_root=output_root)
            counters[status] += 1
            counters["candidates"] += 1
            rows.append(
                {
                    "segment_id": segment_id,
                    "source_key": target["source_key"],
                    "status": status,
                    "reasons": ";".join(reasons),
                    "current_text": target["current_text"],
                    "proposed_text": target["proposed_text"],
                    "male_preview": target["male_preview"],
                    "female_preview": target["female_preview"],
                    "watch_note": target["watch_note"],
                    "output_line_number": "" if live_row is None else live_row.get("output_line_number"),
                    "final_state": "" if live_row is None else live_row.get("final_state"),
                    "review_state": "" if live_row is None else live_row.get("review_state"),
                    "apply_state": "" if live_row is None else live_row.get("apply_state"),
                }
            )
            if status == "ready" and live_row is not None and new_line is not None:
                ready_entries.append((segment_id, live_row, new_line))

        if apply:
            if counters["ready"] != 5 or counters["stale"] or counters["blocked"]:
                raise RuntimeError(
                    "Refusing --apply: expected ready=5, stale=0, blocked=0 "
                    f"but got ready={counters['ready']}, stale={counters['stale']}, blocked={counters['blocked']}."
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
            state_run_id=state_run_id,
            rows=rows,
            counters=counters,
        )

    print("[nickname_gender_article_held_exceptions_apply] Done")
    print(f"[nickname_gender_article_held_exceptions_apply] Rule version: {RULE_VERSION}")
    print(f"[nickname_gender_article_held_exceptions_apply] Mode: {'apply' if apply else 'dry_run'}")
    print(f"[nickname_gender_article_held_exceptions_apply] Candidates: {counters['candidates']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Ready: {counters['ready']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Stale: {counters['stale']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Blocked: {counters['blocked']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Already applied: {counters['already_applied']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Applied: {counters['applied']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Confirmation promoted: {counters['confirmation_promoted']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Output written: {counters['output_written']}")
    print(f"[nickname_gender_article_held_exceptions_apply] Files touched: {counters['files_touched']}")
    print(f"[nickname_gender_article_held_exceptions_apply] TXT: {txt_path}")
    print(f"[nickname_gender_article_held_exceptions_apply] CSV: {csv_path}")
    print(f"[nickname_gender_article_held_exceptions_apply] JSONL: {jsonl_path}")
    return {
        "apply": apply,
        "counts": dict(counters),
        "txt_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Protected dry-run/apply for five held nickname gender/article exceptions.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(apply=args.apply)
