from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "general_pipe_article_gender_batch1_output_apply_v1"
SEGMENT_IDS = [20762, 60278, 230843]
EXPECTED_TEXTS = {
    20762: {
        "before": "Sua [dynasty|lE] não possui a [dynasty_perk|lE] $glory_legacy_2_name$",
        "after": "Sua [dynasty|lE] não possui o [dynasty_perk|lE] $glory_legacy_2_name$",
    },
    60278: {
        "before": "Mais provável de atrair [knight|lE] [characters|lE] estrangeiros",
        "after": "Mais provável de atrair [characters|lE] estrangeiros que sejam [knight|lE]",
    },
    230843: {
        "before": "[GetVassalStance( 'glory_hound' ).GetTextIcon][GetVassalStance( 'glory_hound' ).GetName] Aprova a [victory|lE] Ofensiva",
        "after": "[GetVassalStance( 'glory_hound' ).GetTextIcon][GetVassalStance( 'glory_hound' ).GetName] Aprova a [victory|lE] ofensiva",
    },
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def reports_dir(settings: dict[str, Any]) -> Path:
    path = db.project_path(settings["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_rows(conn) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.is_active,
            s.english_text,
            s.spanish_text,
            o.output_line_number,
            o.portuguese_text AS output_text,
            o.output_raw_line,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_label,
            sc.confirmation_source,
            sc.locked,
            sc.candidate_id
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        tuple(SEGMENT_IDS),
    ).fetchall()
    return [dict(row) for row in rows]


def read_file_value(output_root: Path, row: dict[str, Any]) -> tuple[Path, list[str], int, str, str]:
    output_path = output_root / Path(row["relative_path"])
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError("output_line_number_out_of_range")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError("output_line_has_no_quoted_value")
    value = raw_line[first_quote + 1 : last_quote].replace('\\"', '"')
    return output_path, lines, line_index, raw_line, value


def token_counter(value: str) -> dict[str, int]:
    return dict(sorted(Counter(protected_tokens(value)).items()))


def classify_row(output_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    expected = EXPECTED_TEXTS[segment_id]
    reasons: list[str] = []
    file_payload: tuple[Path, list[str], int, str, str] | None = None
    before = expected["before"]
    after = expected["after"]
    output_text = as_text(row["output_text"])
    confirmed_text = as_text(row["confirmed_text"])

    if int(row.get("is_active") or 0) != 1:
        reasons.append("source_not_active")
    if int(row.get("locked") or 0) != 1:
        reasons.append("confirmation_not_locked")
    if row.get("confirmation_source") != "local_learning":
        reasons.append("confirmation_source_not_local_learning")
    if confirmed_text != after:
        reasons.append("confirmed_text_mismatch")
    if output_text not in {before, after}:
        reasons.append("db_output_text_unexpected")
    if protected_tokens(before) != protected_tokens(after):
        reasons.append("protected_tokens_changed")

    try:
        file_payload = read_file_value(output_root, row)
    except RuntimeError as exc:
        reasons.append(str(exc))
        file_text = ""
        file_raw_line = ""
    else:
        _path, _lines, _line_index, file_raw_line, file_text = file_payload
        if file_text not in {before, after}:
            reasons.append("file_output_text_unexpected")
        if file_text != output_text:
            reasons.append("db_file_text_mismatch")

    if reasons:
        status = "blocked"
    elif output_text == after and file_text == after:
        status = "already_applied"
    else:
        status = "ready"

    return {
        "segment_id": segment_id,
        "status": status,
        "block_reasons": reasons,
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "output_line_number": row["output_line_number"],
        "candidate_id": row["candidate_id"],
        "confirmation_label": row["confirmation_label"],
        "before": before,
        "after": after,
        "db_output_text": output_text,
        "file_output_text": file_text,
        "file_raw_line_before": file_raw_line,
        "file_raw_line_after": replace_quoted_text(file_raw_line, after) if file_raw_line else "",
        "protected_tokens_before": token_counter(before),
        "protected_tokens_after": token_counter(after),
    }


def snapshot(conn, rows: list[dict[str, Any]], output_root: Path) -> dict[str, Any]:
    file_entries = []
    for row in rows:
        try:
            path, _lines, _line_index, raw_line, file_text = read_file_value(output_root, row)
            file_entries.append(
                {
                    "segment_id": row["segment_id"],
                    "path": str(path),
                    "output_line_number": row["output_line_number"],
                    "raw_line": raw_line,
                    "file_text": file_text,
                }
            )
        except RuntimeError as exc:
            file_entries.append({"segment_id": row["segment_id"], "error": str(exc)})
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    return {
        "source_output_confirmation_rows": rows,
        "file_entries": file_entries,
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(SEGMENT_IDS),
            )
        ],
    }


def write_reports(
    settings: dict[str, Any],
    *,
    mode: str,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    before_snapshot: dict[str, Any],
) -> tuple[Path, Path, Path, Path]:
    base = reports_dir(settings) / f"{stamp()}_general_pipe_article_gender_batch1_output_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir(settings) / f"{base.name}_summary.json"
    snapshot_path = reports_dir(settings) / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "general pipe article/gender batch1 output apply",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"created_at={summary['created_at']}",
        "",
        "summary:",
        f"- candidates={summary['candidate_count']}",
        f"- ready={summary['ready_count']}",
        f"- already_applied={summary['already_applied_count']}",
        f"- blocked={summary['blocked_count']}",
        f"- applied={summary['applied_count']}",
        f"- files_touched={summary['files_touched']}",
        f"- source_changed={str(summary['source_changed']).lower()}",
        f"- output_changed={str(summary['output_changed']).lower()}",
        "",
        "diff preview:",
    ]
    for row in rows:
        lines.extend(
            [
                f"- segment_id={row['segment_id']} status={row['status']} path={row['relative_path']}:{row['output_line_number']}",
                f"  before: {row['before']}",
                f"  after:  {row['after']}",
                f"  block_reasons: {', '.join(row['block_reasons']) if row['block_reasons'] else 'none'}",
            ]
        )
    lines.extend(
        [
            "",
            "rollback path:",
            f"- Use snapshot: {snapshot_path}",
            "- Restore each file raw_line from file_entries and restore output_segments.portuguese_text from source_output_confirmation_rows.output_text.",
            "- No source files are part of this operation.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def run(*, apply: bool) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    timestamp = now()
    mode = "apply" if apply else "dry_run"
    applied_count = 0
    files_touched: set[str] = set()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn)
        found = {int(row["segment_id"]) for row in rows}
        missing = sorted(set(SEGMENT_IDS) - found)
        if missing:
            raise RuntimeError(f"missing output rows: {missing}")
        before_snapshot = snapshot(conn, rows, output_root)
        classified = [classify_row(output_root, row) for row in rows]
        blocked = [row for row in classified if row["status"] == "blocked"]
        ready = [row for row in classified if row["status"] == "ready"]
        already_applied = [row for row in classified if row["status"] == "already_applied"]

        if apply and blocked:
            raise RuntimeError(f"refusing apply with blocked rows: {[row['segment_id'] for row in blocked]}")

        if apply:
            live_rows = {int(row["segment_id"]): row for row in fetch_rows(conn)}
            for row in ready:
                live_row = live_rows[int(row["segment_id"])]
                path, lines, line_index, raw_line, file_text = read_file_value(output_root, live_row)
                if file_text != row["before"] or as_text(live_row["output_text"]) != row["before"]:
                    raise RuntimeError(f"live text changed before apply for segment {row['segment_id']}")
                new_raw_line = replace_quoted_text(raw_line, row["after"])
                lines[line_index] = new_raw_line
                path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
                conn.execute(
                    """
                    UPDATE output_segments
                    SET portuguese_text = ?,
                        output_raw_line = ?,
                        portuguese_hash = ?,
                        last_indexed_at = ?
                    WHERE segment_id = ?
                    """,
                    (row["after"], new_raw_line, sha256_text(row["after"]), timestamp, row["segment_id"]),
                )
                applied_count += 1
                files_touched.add(str(path))
            conn.commit()
            rows = fetch_rows(conn)
            classified = [classify_row(output_root, row) for row in rows]
        else:
            conn.rollback()

    status_counts = Counter(row["status"] for row in classified)
    summary = {
        "rule_version": RULE_VERSION,
        "created_at": timestamp,
        "mode": mode,
        "candidate_count": len(classified),
        "ready_count": int(status_counts["ready"]),
        "already_applied_count": int(status_counts["already_applied"]),
        "blocked_count": int(status_counts["blocked"]),
        "applied_count": applied_count,
        "files_touched": len(files_touched),
        "source_changed": False,
        "output_changed": bool(apply and applied_count),
        "requires_post_validation": bool(apply and applied_count),
        "next_action": "approve_diff_preview_before_apply" if not apply else "post_validate_output_apply",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_reports(
        settings,
        mode=mode,
        rows=classified,
        summary=summary,
        before_snapshot=before_snapshot,
    )
    summary.update(
        {
            "txt_path": str(txt_path),
            "jsonl_path": str(jsonl_path),
            "summary_path": str(summary_path),
            "snapshot_path": str(snapshot_path),
        }
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    summary = run(apply=args.apply)
    for key in [
        "mode",
        "candidate_count",
        "ready_count",
        "already_applied_count",
        "blocked_count",
        "applied_count",
        "files_touched",
        "source_changed",
        "output_changed",
        "txt_path",
        "jsonl_path",
        "summary_path",
        "snapshot_path",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
