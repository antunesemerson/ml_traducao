from __future__ import annotations

import argparse
import csv
import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text


RULE_VERSION = "nickname_gago_game_evidence_apply_v1"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "nickname_gender_article_game_evidence_production"
CONFIRMATION_LABEL = "nickname_gender_article_gago_select_cstring:strict_v2"
REVIEWER = "production_nickname_gago_game_evidence_apply"
NICKNAME_PATH = "nicknames_l_spanish.yml"

TARGETS = {
    229222: {
        "source_key": "nick_the_lisp_and_lame",
        "current": "o/a Gago[CHARACTER.Custom('ES_OA')] Cox[CHARACTER.Custom('ES_OA')]",
        "proposed": "[Select_CString( CHARACTER.IsFemale, 'a Gaga e Coxa', 'o Gago e Coxo' )]",
    },
    229254: {
        "source_key": "nick_the_stammerer",
        "current": "o/a Gago[CHARACTER.Custom('ES_OA')]",
        "proposed": "[Select_CString( CHARACTER.IsFemale, 'a Gaga', 'o Gago' )]",
    },
    229815: {
        "source_key": "nick_the_stutterer",
        "current": "o/a Gago[CHARACTER.Custom('ES_OA')]",
        "proposed": "[Select_CString( CHARACTER.IsFemale, 'a Gaga', 'o Gago' )]",
    },
}


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_strict_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    matches = sorted(reports_dir.glob("*_nickname_gender_article_repair_dry_run.csv"))
    if not matches:
        raise RuntimeError("No nickname gender/article repair dry-run CSV found.")
    return matches[-1]


def load_strict_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def selected_csv_rows(path: Path) -> list[dict[str, str]]:
    rows = load_strict_rows(path)
    target_ids = {str(segment_id) for segment_id in TARGETS}
    target_keys = {target["source_key"] for target in TARGETS.values()}
    selected = [
        row
        for row in rows
        if row.get("strict_decision") == "ready_known_game_evidence_exact"
        and row.get("source_key") in target_keys
        and row.get("segment_id") in target_ids
    ]
    extra_ready = [
        row
        for row in rows
        if row.get("strict_decision") == "ready_known_game_evidence_exact"
        and (row.get("source_key") not in target_keys or row.get("segment_id") not in target_ids)
    ]
    if extra_ready:
        extras = ", ".join(f"{row.get('segment_id')}:{row.get('source_key')}" for row in extra_ready)
        raise RuntimeError(f"Unexpected extra ready_known_game_evidence_exact rows: {extras}")
    return selected


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
            s.spanish_text,
            s.english_text,
            s.old_text,
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
    index = int(line_number) - 1
    if index < 0 or index >= len(lines):
        return None, None
    raw_line = lines[index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        return raw_line, None
    return raw_line, raw_line[first_quote + 1 : last_quote].replace('\\"', '"')


def evaluate_row(
    conn,
    *,
    csv_row: dict[str, str],
    live_row: dict[str, Any] | None,
    output_root: Path,
    state_run_id: int | None,
) -> tuple[str, list[str], str | None]:
    segment_id = int(csv_row.get("segment_id") or 0)
    target = TARGETS.get(segment_id)
    reasons: list[str] = []
    new_line: str | None = None
    if target is None:
        return "blocked", ["unexpected_segment_id"], None
    if live_row is None:
        return "blocked", ["missing_live_row"], None

    proposed = target["proposed"]
    current = target["current"]
    confirmation = latest_confirmation(conn, segment_id)
    raw_line, file_text = file_current_text(output_root, live_row)

    if int(live_row.get("is_active") or 0) != 1:
        reasons.append("source_not_active")
    if live_row.get("relative_path") != NICKNAME_PATH:
        reasons.append("unexpected_relative_path")
    if live_row.get("source_key") != target["source_key"]:
        reasons.append("unexpected_source_key")
    if csv_row.get("source_key") != target["source_key"]:
        reasons.append("csv_source_key_mismatch")
    if csv_row.get("current_text") != current:
        reasons.append("csv_current_text_mismatch")
    if csv_row.get("proposed_text") != proposed:
        reasons.append("csv_proposed_text_mismatch")
    if csv_row.get("strict_decision") != "ready_known_game_evidence_exact":
        reasons.append("csv_not_strict_game_evidence_ready")
    if csv_row.get("token_delta_kind") != "controlled_select_cstring_replacement":
        reasons.append("csv_token_delta_not_controlled_replacement")
    if csv_row.get("linguistic_guard_status") != "ready_known_game_evidence":
        reasons.append("csv_linguistic_guard_not_game_evidence")
    if as_text(live_row.get("output_text")) != current:
        reasons.append("stale_db_output_text")
    if file_text != current:
        reasons.append("stale_file_output_text")
    if live_row.get("output_line_number") is None:
        reasons.append("missing_output_line_number")
    if raw_line is None:
        reasons.append("missing_output_file_or_line")
    elif file_text is None:
        reasons.append("line_without_quoted_value")
    else:
        new_line = replace_quoted_text(raw_line, proposed)

    if confirmation and int(confirmation.get("locked") or 0) == 1:
        if as_text(confirmation.get("confirmed_text")) != proposed:
            reasons.append("locked_confirmation_diverges")

    if as_text(live_row.get("output_text")) == proposed and (not confirmation or as_text(confirmation.get("confirmed_text")) == proposed):
        return "already_applied", [], new_line
    if any(reason.startswith("stale_") for reason in reasons):
        return "stale", reasons, new_line
    if reasons:
        return "blocked", reasons, new_line
    return "ready", [], new_line


def upsert_confirmation(conn, *, segment_id: int, proposed: str, now: str) -> bool:
    existing = latest_confirmation(conn, segment_id)
    if existing and int(existing.get("locked") or 0) == 1:
        return False
    already = (
        existing
        and as_text(existing.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(existing.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(existing.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(existing.get("confirmed_text")) == proposed
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
            confirmation_level = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level
                ELSE excluded.confirmation_level
            END,
            confirmed_text = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmed_text
                ELSE excluded.confirmed_text
            END,
            confirmation_source = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_source
                ELSE excluded.confirmation_source
            END,
            confirmation_label = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_label
                ELSE excluded.confirmation_label
            END,
            locked = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.locked
                ELSE excluded.locked
            END,
            confidence_score = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score
                ELSE excluded.confidence_score
            END,
            reviewer = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer
                ELSE excluded.reviewer
            END,
            confirmed_at = COALESCE(segment_confirmations.confirmed_at, excluded.confirmed_at),
            updated_at = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.updated_at
                ELSE excluded.updated_at
            END
        """,
        (segment_id, CONFIRMATION_LEVEL, proposed, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not bool(already)


def apply_entries(
    conn,
    *,
    output_root: Path,
    entries: list[tuple[dict[str, str], dict[str, Any], str]],
    now: str,
) -> tuple[int, int, int]:
    by_file: dict[str, list[tuple[dict[str, str], dict[str, Any], str]]] = {}
    for csv_row, live_row, new_line in entries:
        by_file.setdefault(as_text(live_row["relative_path"]), []).append((csv_row, live_row, new_line))

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, file_entries in sorted(by_file.items()):
        if relative_path != NICKNAME_PATH:
            raise RuntimeError(f"Refusing to touch unexpected file: {relative_path}")
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for csv_row, live_row, new_line in sorted(file_entries, key=lambda item: int(item[1]["output_line_number"])):
            segment_id = int(csv_row["segment_id"])
            proposed = TARGETS[segment_id]["proposed"]
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


def report_path(settings: dict[str, Any], *, apply: bool) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    suffix = "apply" if apply else "apply_dry_run"
    return reports_dir / f"{now_stamp()}_nickname_gago_game_evidence_{suffix}.txt"


def write_report(
    settings: dict[str, Any],
    *,
    apply: bool,
    csv_path: Path,
    state_run_id: int | None,
    counters: Counter,
    previews: list[tuple[dict[str, str], dict[str, Any] | None, str, list[str]]],
) -> Path:
    path = report_path(settings, apply=apply)
    lines = [
        "Nickname Gago game-evidence protected apply",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Mode: {'apply' if apply else 'dry_run'}",
        f"CSV source: {csv_path}",
        f"Segment-state run id before apply: {state_run_id}",
        "",
        "Summary:",
        f"- candidates: {counters['candidates']}",
        f"- ready: {counters['ready']}",
        f"- applied: {counters['applied']}",
        f"- confirmation_promoted: {counters['confirmation_promoted']}",
        f"- output_written: {counters['output_written']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- already_applied: {counters['already_applied']}",
        f"- files_touched: {counters['files_touched']}",
        "",
        "Rows:",
    ]
    for csv_row, live_row, status, reasons in previews:
        segment_id = int(csv_row["segment_id"])
        target = TARGETS[segment_id]
        lines.extend(
            [
                f"- segment={segment_id} | {target['source_key']} | status={status}",
                f"  reasons={', '.join(reasons) if reasons else 'none'}",
                f"  current={short(target['current'])}",
                f"  proposed={short(target['proposed'])}",
                f"  output_line={'' if live_row is None else live_row.get('output_line_number')}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Only the 3 hardcoded Gago game-evidence segments are eligible.",
            "- The strict article-only batch is intentionally ignored.",
            "- This script does not touch source/.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(*, apply: bool = False, csv_path: Path | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    selected_csv = csv_path or latest_strict_csv(settings)
    selected_rows = selected_csv_rows(selected_csv)

    if len(selected_rows) != 3:
        raise RuntimeError(f"Expected exactly 3 selected Gago rows, found {len(selected_rows)}.")
    if {int(row["segment_id"]) for row in selected_rows} != set(TARGETS):
        raise RuntimeError("Selected Gago segment IDs do not match the hardcoded allowlist.")

    counters: Counter = Counter()
    counters["candidates"] = len(selected_rows)
    previews: list[tuple[dict[str, str], dict[str, Any] | None, str, list[str]]] = []
    ready_entries: list[tuple[dict[str, str], dict[str, Any], str]] = []
    now = datetime.now().isoformat(timespec="seconds")

    with db.connect(settings) as conn:
        state_run_id = latest_state_run_id(conn)
        for row in sorted(selected_rows, key=lambda item: int(item["segment_id"])):
            live_row = fetch_live_row(conn, segment_id=int(row["segment_id"]), state_run_id=state_run_id)
            status, reasons, new_line = evaluate_row(
                conn,
                csv_row=row,
                live_row=live_row,
                output_root=output_root,
                state_run_id=state_run_id,
            )
            counters[status] += 1
            previews.append((row, live_row, status, reasons))
            if status == "ready" and live_row is not None and new_line is not None:
                ready_entries.append((row, live_row, new_line))

        if apply:
            if counters["ready"] != 3 or counters["stale"] != 0 or counters["blocked"] != 0:
                raise RuntimeError(
                    "Refusing --apply: expected exactly ready=3, stale=0, blocked=0 "
                    f"but got ready={counters['ready']}, stale={counters['stale']}, blocked={counters['blocked']}."
                )
            promoted, written, files_touched = apply_entries(
                conn,
                output_root=output_root,
                entries=ready_entries,
                now=now,
            )
            counters["confirmation_promoted"] = promoted
            counters["output_written"] = written
            counters["applied"] = written
            counters["files_touched"] = files_touched
            conn.commit()
        else:
            conn.rollback()

        report = write_report(
            settings,
            apply=apply,
            csv_path=selected_csv,
            state_run_id=state_run_id,
            counters=counters,
            previews=previews,
        )

    print("[nickname_gago_game_evidence_apply] Done")
    print(f"[nickname_gago_game_evidence_apply] Rule version: {RULE_VERSION}")
    print(f"[nickname_gago_game_evidence_apply] Mode: {'apply' if apply else 'dry_run'}")
    print(f"[nickname_gago_game_evidence_apply] CSV source: {selected_csv}")
    print(f"[nickname_gago_game_evidence_apply] Candidates: {counters['candidates']}")
    print(f"[nickname_gago_game_evidence_apply] Ready: {counters['ready']}")
    print(f"[nickname_gago_game_evidence_apply] Applied: {counters['applied']}")
    print(f"[nickname_gago_game_evidence_apply] Confirmation promoted: {counters['confirmation_promoted']}")
    print(f"[nickname_gago_game_evidence_apply] Output written: {counters['output_written']}")
    print(f"[nickname_gago_game_evidence_apply] Stale: {counters['stale']}")
    print(f"[nickname_gago_game_evidence_apply] Blocked: {counters['blocked']}")
    print(f"[nickname_gago_game_evidence_apply] Files touched: {counters['files_touched']}")
    print(f"[nickname_gago_game_evidence_apply] Report: {report}")
    return {
        "apply": apply,
        "csv_path": str(selected_csv),
        "counts": dict(counters),
        "report_path": str(report),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Protected apply for three Gago nickname game-evidence repairs.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--csv-path", type=Path, default=None)
    args = parser.parse_args()
    main(apply=args.apply, csv_path=args.csv_path)
