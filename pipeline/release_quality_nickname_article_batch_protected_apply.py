from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from release_quality_nickname_article_policy_review_readonly import (
    NICKNAMES_PATH,
    SELECT_ARTICLE_RE,
    short,
)


SOURCE = "release_quality_nickname_article_batch_protected_apply_v1"
DEFAULT_INPUT = Path("reports/20260707_143619_506459_release_quality_nickname_article_strict_policy_dry_run.jsonl")
SAFE_CONFIRMATION_SOURCE = "nickname_gender_article_reviewed_safe_production"
SAFE_CONFIRMATION_LABEL = "nickname_gender_article_reviewed_safe:strict_review_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def backup_root(batch_name: str) -> Path:
    path = db.project_path("memory/backups") / f"{batch_name}_{stamp()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for strict nickname article/gender batch.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--batch-name", default="release_quality_nickname_article_batch1")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 300000")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def select_batch(rows: list[dict[str, Any]], *, offset: int, limit: int, expected_count: int | None) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row.get("candidate_for_policy_apply_preview") is True]
    selected = candidates[offset : offset + limit]
    if expected_count is not None and len(selected) != expected_count:
        raise SystemExit(f"selected count guard failed: expected {expected_count}, got {len(selected)}")
    return selected


def fetch_live(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
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
        JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def latest_confirmation(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
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


def file_line(root: Path, relative_path: str, line_number: int | None) -> tuple[str | None, str | None]:
    if line_number is None:
        return None, "missing_output_line_number"
    path = root / Path(relative_path)
    if not path.exists():
        return None, "missing_output_file"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(line_number) - 1
    if index < 0 or index >= len(lines):
        return None, "output_line_out_of_range"
    return lines[index], None


def validate_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [int(row["segment_id"]) for row in rows]
    root = output_root()
    records: list[dict[str, Any]] = []
    with connect() as conn:
        live = fetch_live(conn, ids)
        for row in rows:
            segment_id = int(row["segment_id"])
            live_row = live.get(segment_id)
            confirmation = latest_confirmation(conn, segment_id)
            reasons: list[str] = []
            if live_row is None:
                reasons.append("missing_live_row")
                live_row = {}
            old_output = str(row.get("output_text") or "")
            target = str(row.get("confirmed_text") or "")
            current_raw = str(live_row.get("output_raw_line") or "")
            new_raw = replace_quoted_text(current_raw, target) if current_raw else ""
            added_tokens = list(row.get("added_tokens") or [])
            removed_tokens = list(row.get("removed_tokens") or [])
            disk_line, file_error = file_line(
                root,
                str(live_row.get("relative_path") or row.get("relative_path") or ""),
                live_row.get("output_line_number"),
            )
            if row.get("candidate_for_policy_apply_preview") is not True:
                reasons.append("not_policy_apply_preview_candidate")
            if row.get("relative_path") != NICKNAMES_PATH:
                reasons.append("not_nicknames_path")
            if live_row.get("relative_path") != row.get("relative_path"):
                reasons.append("live_relative_path_mismatch")
            if live_row.get("source_key") != row.get("source_key"):
                reasons.append("live_source_key_mismatch")
            if int(live_row.get("is_active") or 0) != 1:
                reasons.append("source_not_active")
            if str(live_row.get("output_text") or "") != old_output:
                reasons.append("live_output_text_mismatch")
            if removed_tokens:
                reasons.append("removed_tokens_not_allowed")
            if len(added_tokens) != 1 or not SELECT_ARTICLE_RE.fullmatch(str(added_tokens[0] if added_tokens else "")):
                reasons.append("added_token_not_single_article_select_cstring")
            if confirmation is None:
                reasons.append("missing_confirmation")
            else:
                if confirmation.get("confirmation_source") != SAFE_CONFIRMATION_SOURCE:
                    reasons.append("confirmation_source_not_safe_policy")
                if confirmation.get("confirmation_label") != SAFE_CONFIRMATION_LABEL:
                    reasons.append("confirmation_label_not_safe_policy")
                if str(confirmation.get("confirmed_text") or "") != target:
                    reasons.append("confirmation_target_mismatch")
            if file_error:
                reasons.append(file_error)
            elif disk_line != current_raw:
                reasons.append("disk_line_mismatch_output_segments")
            if target == old_output:
                reasons.append("no_output_change")
            if "\n" in target or "\r" in target:
                reasons.append("multiline_target_not_allowed")
            if not target.strip():
                reasons.append("empty_target")
            records.append(
                {
                    "source": SOURCE,
                    "record_type": "nickname_article_protected_apply_item",
                    "segment_id": segment_id,
                    "relative_path": live_row.get("relative_path") or row.get("relative_path"),
                    "source_key": live_row.get("source_key") or row.get("source_key"),
                    "source_line_number": live_row.get("source_line_number"),
                    "output_line_number": live_row.get("output_line_number"),
                    "old_output_text": old_output,
                    "target_text": target,
                    "confirmation_source": confirmation.get("confirmation_source") if confirmation else None,
                    "confirmation_label": confirmation.get("confirmation_label") if confirmation else None,
                    "current_raw_line": current_raw,
                    "new_raw_line": new_raw,
                    "disk_line": disk_line,
                    "added_tokens": added_tokens,
                    "removed_tokens": removed_tokens,
                    "target_hash": stable_hash(target),
                    "status": "ready" if not reasons else "blocked",
                    "block_reasons": reasons,
                    "apply_count": 0,
                    "ingest_count": 0,
                    "issue_closure_count": 0,
                    "segment_state_count": 0,
                    "production_full_count": 0,
                }
            )
    return records


def apply_records(records: list[dict[str, Any]], batch_name: str) -> tuple[int, int, Path]:
    ready = [record for record in records if record["status"] == "ready"]
    root = output_root()
    backup = backup_root(batch_name)
    timestamp = now_iso()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in ready:
        by_path[str(record["relative_path"])].append(record)

    applied = 0
    files_touched = 0
    with connect() as conn:
        for relative_path, path_records in sorted(by_path.items()):
            output_path = root / Path(relative_path)
            backup_path = backup / Path(relative_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            for record in sorted(path_records, key=lambda item: int(item["output_line_number"])):
                index = int(record["output_line_number"]) - 1
                if lines[index] != record["current_raw_line"]:
                    raise SystemExit(f"disk line changed during apply for segment {record['segment_id']}")
                lines[index] = str(record["new_raw_line"])
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
                        str(record["target_text"]),
                        str(record["new_raw_line"]),
                        str(record["target_hash"]),
                        timestamp,
                        int(record["segment_id"]),
                    ),
                )
                record["apply_count"] = 1
                applied += 1
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched += 1
        conn.commit()
    return applied, files_touched, backup


def post_validate(records: list[dict[str, Any]]) -> dict[str, Any]:
    root = output_root()
    file_ok = output_db_ok = confirmation_ok = 0
    details: list[dict[str, Any]] = []
    with connect() as conn:
        for record in records:
            if record["status"] != "ready":
                continue
            output_path = root / Path(str(record["relative_path"]))
            line = output_path.read_text(encoding="utf-8-sig").splitlines()[int(record["output_line_number"]) - 1]
            output = conn.execute(
                "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id=?",
                (int(record["segment_id"]),),
            ).fetchone()
            confirmation = latest_confirmation(conn, int(record["segment_id"]))
            f_ok = line == str(record["new_raw_line"])
            o_ok = bool(
                output
                and output["portuguese_text"] == str(record["target_text"])
                and output["output_raw_line"] == str(record["new_raw_line"])
                and output["portuguese_hash"] == str(record["target_hash"])
            )
            c_ok = bool(
                confirmation
                and confirmation["confirmation_source"] == SAFE_CONFIRMATION_SOURCE
                and confirmation["confirmation_label"] == SAFE_CONFIRMATION_LABEL
                and confirmation["confirmed_text"] == str(record["target_text"])
            )
            file_ok += int(f_ok)
            output_db_ok += int(o_ok)
            confirmation_ok += int(c_ok)
            details.append(
                {
                    "segment_id": int(record["segment_id"]),
                    "file_ok": f_ok,
                    "output_db_ok": o_ok,
                    "confirmation_ok": c_ok,
                }
            )
    return {
        "file_ok": file_ok,
        "output_db_ok": output_db_ok,
        "confirmation_ok": confirmation_ok,
        "details": details,
    }


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_quality_nickname_article_batch_protected_apply_{summary['mode']}"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# Release Quality Nickname Article Batch Protected Apply",
        "",
        f"- mode: `{summary['mode']}`",
        f"- selected: `{summary['selected_count']}`",
        f"- ready: `{summary['ready_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        f"- applied: `{summary['applied_count']}`",
        f"- files touched: `{summary['files_touched_count']}`",
        f"- backup: `{summary.get('backup_path') or ''}`",
        "",
        "## Block Reasons",
        "",
    ]
    if summary["block_reason_counts"]:
        for reason, count in summary["block_reason_counts"]:
            lines.append(f"- `{reason}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Applied/Ready Samples", ""])
    for record in [item for item in records if item["status"] == "ready"][:30]:
        lines.extend(
            [
                f"### {record['segment_id']} - {record['source_key']}",
                f"- before: {short(record['old_output_text'])}",
                f"- after: {short(record['target_text'])}",
                "",
            ]
        )
    summary["output_files"] = {
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    selected = select_batch(read_jsonl(args.input_jsonl), offset=args.offset, limit=args.limit, expected_count=args.expected_count)
    records = validate_records(selected)
    ready = [record for record in records if record["status"] == "ready"]
    blocked = [record for record in records if record["status"] != "ready"]
    if args.apply and blocked:
        raise SystemExit(f"blocked rows present; refusing apply: {Counter(reason for item in blocked for reason in item['block_reasons'])}")

    applied = 0
    files_touched = 0
    backup: Path | None = None
    post_validation: dict[str, Any] | None = None
    if args.apply:
        applied, files_touched, backup = apply_records(records, args.batch_name)
        post_validation = post_validate(records)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "apply" if args.apply else "dry_run",
        "input_jsonl": str(db.project_path(args.input_jsonl)),
        "offset": args.offset,
        "limit": args.limit,
        "selected_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "applied_count": applied,
        "files_touched_count": files_touched,
        "backup_path": str(backup) if backup else None,
        "post_validation": post_validation,
        "block_reason_counts": Counter(reason for item in blocked for reason in item["block_reasons"]).most_common(),
        "applied_segment_ids": [int(record["segment_id"]) for record in ready] if args.apply else [],
        "candidate_generation_count": 0,
        "ingest_count": 0,
        "issue_closure_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(applied),
        "single_operational_recommendation": (
            "Run segment-state and delta for this batch only." if args.apply else "Dry-run only; rerun with --apply if ready_count matches expectation."
        ),
    }
    md_path, jsonl_path, summary_path = write_reports(summary, records)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={summary['mode']}")
    print(f"selected_count={summary['selected_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"applied_count={summary['applied_count']}")
    print(f"files_touched_count={summary['files_touched_count']}")
    print(f"backup_path={summary['backup_path']}")
    if post_validation is not None:
        print(f"post_validation={json.dumps(post_validation, ensure_ascii=False, sort_keys=True)}")
    print("ingest_count=0")
    print("issue_closure_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print(f"source_changed={str(summary['source_changed']).lower()}")
    print(f"output_changed={str(summary['output_changed']).lower()}")


if __name__ == "__main__":
    main()
