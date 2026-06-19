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
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "dynamic_acclaimed_knight_requirement_repair_apply_v1"
DECISION = "acclaimed_knight_requirement_repair_ready"
EXPECTED_CANDIDATES = 7
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "dynamic_acclaimed_knight_requirement_repair_production"
CONFIRMATION_LABEL = "dynamic_acclaimed_knight_requirement_repair_ready"
REVIEWER = "production_dynamic_acclaimed_knight_requirement_repair_apply"

BAD_TEXT_PATTERNS = [
    "a tra\u00e7o",
    "a traco",
    "possui a tra\u00e7o",
    "possui a traco",
    "a traÃ§o",
    "possui a traÃ§o",
    "ÃƒÆ’",
    "Ãƒâ€š",
    "Ã¯Â¿Â½",
    "car\u00e1cter",
    "caracter",
    "combata",
    "ocupe",
    "ahora",
    "enamorado",
]
QUESTION_INSIDE_WORD_RE = re.compile(r"\w\?\w", re.UNICODE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any], *, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_dynamic_acclaimed_knight_requirement_repair_{mode}"
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
        raise RuntimeError(f"output_line_out_of_range:{row['segment_id']}")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"line_without_quoted_value:{row['segment_id']}")
    return raw_line[first_quote + 1 : last_quote]


def bad_text_reasons(value: str) -> list[str]:
    text = value.lower()
    reasons = [f"linguistic_guard:{pattern}" for pattern in BAD_TEXT_PATTERNS if pattern.lower() in text]
    if QUESTION_INSIDE_WORD_RE.search(value):
        reasons.append("linguistic_guard:question_inside_word")
    return reasons


def load_candidates(review_jsonl: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in review_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [dict(row) for row in rows if row.get("decision") == DECISION]
    for row in selected:
        row["segment_id"] = int(row["segment_id"])
        row["expected_current_text"] = as_text(row.get("current_text"))
        row["corrected_text"] = as_text(row.get("corrected_text"))
        row["blocked_reason_detail"] = ""
    return sorted(selected, key=lambda item: int(item["segment_id"]))


def fetch_live_rows(conn, state_run_id: int, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    placeholders = ",".join("?" for _ in candidates)
    by_id = {int(row["segment_id"]): row for row in candidates}
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
    result: list[dict[str, Any]] = []
    found_ids: set[int] = set()
    for row in rows:
        item = dict(row)
        segment_id = int(item["segment_id"])
        found_ids.add(segment_id)
        item.update(by_id[segment_id])
        result.append(item)
    for segment_id in sorted(set(by_id) - found_ids):
        missing = dict(by_id[segment_id])
        missing["status"] = "blocked"
        missing["blocked_reason_detail"] = "live_db_row_missing"
        result.append(missing)
    return sorted(result, key=lambda item: int(item["segment_id"]))


def classify_row(output_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["status"] = "ready"
    result["blocked_reason_detail"] = ""

    def mark(status: str, reason: str) -> dict[str, Any]:
        result["status"] = status
        result["blocked_reason_detail"] = reason
        return result

    corrected = as_text(row.get("corrected_text"))
    expected_current = as_text(row.get("expected_current_text"))
    live_output = as_text(row.get("live_output_text"))
    confirmed = as_text(row.get("confirmed_text"))

    if row.get("decision") != DECISION:
        return mark("blocked", "decision_not_allowed")
    if row.get("requires_apply_later") is not True:
        return mark("blocked", "requires_apply_later_not_true")
    if not corrected:
        return mark("blocked", "corrected_text_empty")
    if row.get("token_delta_review_required") is True:
        return mark("blocked", "token_delta_review_required")
    if row.get("tokens_preserved") is not True:
        return mark("blocked", "review_tokens_not_preserved")
    linguistic_reasons = bad_text_reasons(corrected)
    if linguistic_reasons:
        return mark("blocked", ";".join(linguistic_reasons))
    if structural_tokens(expected_current) != structural_tokens(corrected):
        return mark("blocked", "structural_tokens_changed")
    if int(row.get("is_active") or 0) != 1:
        return mark("stale", "source_not_active")
    if int(row.get("confirmation_locked") or 0) == 1:
        return mark("blocked", "locked_confirmation")
    if int(row.get("live_needs_output_apply") or 0) != 0:
        return mark("blocked", "needs_output_apply_not_0")
    if (
        live_output == corrected
        and confirmed == corrected
        and row.get("confirmation_source") == CONFIRMATION_SOURCE
        and row.get("confirmation_label") == CONFIRMATION_LABEL
    ):
        return mark("already_applied", "")
    if live_output != expected_current:
        return mark("stale", "live_output_not_expected_current")
    if confirmed != live_output:
        return mark("stale", "confirmation_not_current_output")
    try:
        file_text = file_text_at(output_root, row)
    except RuntimeError as exc:
        return mark("blocked", str(exc))
    if file_text != live_output:
        return mark("stale", "file_output_db_output_mismatch")
    return result


def count_rows(rows: list[dict[str, Any]]) -> Counter:
    counts = Counter(row["status"] for row in rows)
    for key in ("ready", "stale", "blocked", "already_applied"):
        counts.setdefault(key, 0)
    return counts


def assert_apply_gate(rows: list[dict[str, Any]], counts: Counter) -> None:
    if len(rows) != EXPECTED_CANDIDATES:
        raise RuntimeError(f"Expected candidates={EXPECTED_CANDIDATES}, got {len(rows)}")
    if counts["ready"] != EXPECTED_CANDIDATES:
        raise RuntimeError(f"Expected ready={EXPECTED_CANDIDATES}, got {counts['ready']}")
    if counts["stale"] != 0:
        raise RuntimeError(f"Expected stale=0, got {counts['stale']}")
    if counts["blocked"] != 0:
        raise RuntimeError(f"Expected blocked=0, got {counts['blocked']}")
    if counts["already_applied"] != 0:
        raise RuntimeError(f"Expected already_applied=0, got {counts['already_applied']}")


def upsert_confirmation(conn, *, segment_id: int, confirmed_text: str, now: str) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        raise RuntimeError(f"locked_confirmation:{segment_id}")
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

    applied = confirmation_promoted = output_written = files_touched = 0
    for relative_path, file_rows in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row in sorted(file_rows, key=lambda item: int(item["output_line_number"])):
            segment_id = int(row["segment_id"])
            line_index = int(row["output_line_number"]) - 1
            raw_line = lines[line_index]
            if file_text_at(output_root, row) != as_text(row["live_output_text"]):
                raise RuntimeError(f"live_file_output_drift:{segment_id}")
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
    review_jsonl: Path,
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
        "blocked_reason_detail",
        "live_final_state",
        "live_needs_output_apply",
        "confirmation_source",
        "confirmation_label",
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
        "Dynamic acclaimed knight requirement protected repair apply"
        if apply
        else "Dynamic acclaimed knight requirement protected repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Review JSONL: {review_jsonl}",
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
        "Rows:",
    ]
    for row in rows:
        lines.append(
            f"- {row.get('segment_id')} | {row.get('source_key')} | "
            f"status={row.get('status')} | {row.get('blocked_reason_detail')}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def run(*, review_jsonl: Path, apply: bool) -> tuple[Counter, dict[str, int], tuple[Path, Path, Path], list[dict[str, Any]]]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    candidates = load_candidates(review_jsonl)
    apply_counts = {"applied": 0, "confirmation_promoted": 0, "output_written": 0, "files_touched": 0}
    with db.connect(settings) as conn:
        state_run_id = latest_state_run_id(conn)
        rows = [classify_row(output_root, row) for row in fetch_live_rows(conn, state_run_id, candidates)]
        counts = count_rows(rows)
        paths = write_reports(
            settings,
            review_jsonl=review_jsonl,
            rows=rows,
            counts=counts,
            apply=apply,
            apply_counts=apply_counts,
        )
        if len(rows) != EXPECTED_CANDIDATES:
            raise RuntimeError(f"Expected candidates={EXPECTED_CANDIDATES}, got {len(rows)}")
        if apply:
            assert_apply_gate(rows, counts)
            apply_counts = apply_ready(
                conn,
                output_root=output_root,
                rows=[row for row in rows if row["status"] == "ready"],
                now=db.utc_now(),
            )
            conn.commit()
            paths = write_reports(
                settings,
                review_jsonl=review_jsonl,
                rows=rows,
                counts=counts,
                apply=apply,
                apply_counts=apply_counts,
            )
    return counts, apply_counts, paths, rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        counts, apply_counts, paths, rows = run(review_jsonl=args.review_jsonl, apply=args.apply)
    except Exception as exc:
        print(f"[dynamic_acclaimed_knight_requirement_repair_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("[dynamic_acclaimed_knight_requirement_repair_apply] Completed")
    print(f"[dynamic_acclaimed_knight_requirement_repair_apply] Apply: {args.apply}")
    print(f"candidates={len(rows)}")
    print(f"ready={counts['ready']}")
    print(f"stale={counts['stale']}")
    print(f"blocked={counts['blocked']}")
    print(f"already_applied={counts['already_applied']}")
    print(f"applied={apply_counts.get('applied', 0)}")
    print(f"confirmation_promoted={apply_counts.get('confirmation_promoted', 0)}")
    print(f"output_written={apply_counts.get('output_written', 0)}")
    print(f"files_touched={apply_counts.get('files_touched', 0)}")
    print(f"report={paths[0]}")
    print(f"csv={paths[1]}")
    print(f"jsonl={paths[2]}")


if __name__ == "__main__":
    main()
