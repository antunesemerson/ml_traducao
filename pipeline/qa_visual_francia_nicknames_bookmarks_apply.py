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


SOURCE = "qa_visual_francia_nicknames_bookmarks_apply_v1"
CONFIRMATION_SOURCE = "codex_qa_visual_hotfix_human_confirmed"
CONFIRMATION_LABEL = "qa_visual_francia_nicknames_bookmarks_post600"
PACKAGE_JSONL = Path("reports/20260704_165209_719857_qa_visual_francia_nicknames_bookmarks_package_readonly_v2.jsonl")
READY_CLASSES = {"hotfix_ready", "style_ready"}
EXPECTED_READY_IDS = {267273, 267274, 229124, 229206, 229515, 229649, 8430, 8432, 8449, 7906}


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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 300000")
    return conn


def protected_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "[":
            depth = 1
            j = i + 1
            while j < len(text) and depth:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                j += 1
            if depth == 0:
                tokens.append(text[i:j])
                i = j
                continue
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j != -1:
                tokens.append(text[i : j + 1])
                i = j + 1
                continue
        i += 1
    return tokens


def canonical_unicode_escapes(text: str) -> str:
    def replace_match(match: Any) -> str:
        return chr(int(match.group(1), 16))

    import re

    return re.sub(r"\\u([0-9a-fA-F]{4})", replace_match, text)


def load_package_records() -> list[dict[str, Any]]:
    records = []
    for line in db.project_path(PACKAGE_JSONL).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("classification") in READY_CLASSES:
            records.append(record)
    ids = {int(record["segment_id"]) for record in records}
    if ids != EXPECTED_READY_IDS:
        raise SystemExit(f"ready id guard failed: expected {sorted(EXPECTED_READY_IDS)}, got {sorted(ids)}")
    return sorted(records, key=lambda row: (str(row["relative_path"]), int(row["line"]), int(row["segment_id"])))


def fetch_current_rows(segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              s.id AS segment_id,
              s.relative_path,
              s.source_line_number,
              s.source_key,
              s.english_text,
              s.spanish_text,
              s.old_text,
              o.output_line_number,
              o.portuguese_text AS output_text,
              o.output_raw_line,
              c.confirmed_text,
              c.confirmation_level,
              c.confirmation_source,
              c.confirmation_label,
              c.locked
            FROM source_segments s
            JOIN output_segments o ON o.segment_id = s.id
            LEFT JOIN segment_confirmations c ON c.segment_id = s.id
            WHERE s.id IN ({placeholders})
            """,
            segment_ids,
        ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def validate_records(package_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = fetch_current_rows([int(row["segment_id"]) for row in package_records])
    output_dir = output_root()
    validated = []
    for package in package_records:
        segment_id = int(package["segment_id"])
        row = rows.get(segment_id)
        reasons: list[str] = []
        if not row:
            reasons.append("missing_segment")
            row = {}
        target_text = str(package["proposed_text"])
        current_text = str(row.get("output_text") or "")
        current_raw = str(row.get("output_raw_line") or "")
        target_raw = replace_quoted_text(current_raw, target_text) if current_raw else ""
        if row.get("source_key") != package.get("source_key"):
            reasons.append("source_key_mismatch")
        if row.get("relative_path") != package.get("relative_path"):
            reasons.append("relative_path_mismatch")
        if current_text != str(package.get("current_output_text") or ""):
            reasons.append("current_output_text_changed_since_package")
        if target_text != str(package.get("proposed_text") or ""):
            reasons.append("target_text_mismatch")
        if target_text == current_text:
            reasons.append("no_output_change")
        if "\\u00" in target_text or "\\u00" in target_raw:
            reasons.append("target_contains_literal_unicode_escape")
        old_tokens = protected_tokens(current_text)
        new_tokens = protected_tokens(target_text)
        token_policy = str(package.get("token_policy") or "")
        token_ok = old_tokens == new_tokens
        if token_policy == "approved_plain_to_gender_select_cstring":
            token_ok = old_tokens == [] and len(new_tokens) == 1 and new_tokens[0].startswith("[Select_CString( CHARACTER.IsFemale,")
        elif token_policy == "unicode_escape_cleanup_inside_select_cstring_token":
            token_ok = [canonical_unicode_escapes(token) for token in old_tokens] == [canonical_unicode_escapes(token) for token in new_tokens]
        elif token_policy == "unicode_escape_cleanup_no_token_change":
            token_ok = old_tokens == new_tokens == []
        if not token_ok:
            reasons.append("token_integrity_failed")
        if "\r" in target_text:
            reasons.append("structure_integrity_failed")
        output_path = output_dir / str(row.get("relative_path", ""))
        disk_line = ""
        if not output_path.exists():
            reasons.append("missing_output_file")
        else:
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            idx = int(row["output_line_number"]) - 1
            disk_line = lines[idx] if 0 <= idx < len(lines) else ""
            if disk_line != current_raw:
                reasons.append("disk_line_mismatch_output_segments")
        validated.append(
            {
                "record_type": "qa_visual_francia_nicknames_bookmarks_apply_item",
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_line_number": row.get("source_line_number"),
                "output_line_number": row.get("output_line_number"),
                "source_key": row.get("source_key"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "old_text": row.get("old_text"),
                "current_output_text": current_text,
                "current_confirmed_text": row.get("confirmed_text"),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_label": row.get("confirmation_label"),
                "locked": row.get("locked"),
                "target_text": target_text,
                "target_hash": stable_hash(target_text),
                "target_raw_line": target_raw,
                "current_raw_line": current_raw,
                "disk_line": disk_line,
                "package_classification": package.get("classification"),
                "hotfix_kind": token_policy or package.get("classification"),
                "classification_reason": package.get("classification_reason"),
                "current_tokens": old_tokens,
                "target_tokens": new_tokens,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": "\r" not in target_text,
                "canonical_l10n_ok": bool(target_text.strip()) and "\\u00" not in target_text,
                "utf8_real_ok": "\\u00" not in target_text and "\\u00" not in target_raw,
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
            }
        )
    return validated


def apply_records(records: list[dict[str, Any]]) -> tuple[int, int, str]:
    ready = [record for record in records if record["status"] == "ready"]
    backup_root = db.project_path("memory/backups") / f"qa_visual_francia_nicknames_bookmarks_{stamp()}"
    timestamp = now_iso()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in ready:
        by_path[str(record["relative_path"])].append(record)
    files_touched = 0
    with connect() as conn:
        for relative_path, path_records in sorted(by_path.items()):
            output_path = output_root() / relative_path
            backup_path = backup_root / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            for record in sorted(path_records, key=lambda row: int(row["output_line_number"])):
                idx = int(record["output_line_number"]) - 1
                if lines[idx] != record["current_raw_line"]:
                    raise SystemExit(f"disk line changed before apply for {record['segment_id']}")
                lines[idx] = str(record["target_raw_line"])
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
                        record["target_text"],
                        record["target_raw_line"],
                        record["target_hash"],
                        timestamp,
                        int(record["segment_id"]),
                    ),
                )
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched += 1
        for record in ready:
            conn.execute(
                """
                UPDATE segment_confirmations
                SET confirmation_level = 'human_confirmed',
                    confirmed_text = ?,
                    confirmation_source = ?,
                    confirmation_label = ?,
                    locked = 1,
                    confidence_score = 1.0,
                    reviewer = 'codex',
                    updated_at = ?,
                    confirmed_at = COALESCE(confirmed_at, ?)
                WHERE segment_id = ?
                """,
                (
                    record["target_text"],
                    CONFIRMATION_SOURCE,
                    CONFIRMATION_LABEL,
                    timestamp,
                    timestamp,
                    int(record["segment_id"]),
                ),
            )
        conn.commit()
    return len(ready), files_touched, str(backup_root)


def post_validate(records: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [record for record in records if record["status"] == "ready"]
    details = []
    counts = Counter()
    with connect() as conn:
        for record in ready:
            line = (output_root() / str(record["relative_path"])).read_text(encoding="utf-8-sig").splitlines()[
                int(record["output_line_number"]) - 1
            ]
            output = conn.execute(
                "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id = ?",
                (int(record["segment_id"]),),
            ).fetchone()
            conf = conn.execute(
                "SELECT confirmed_text, confirmation_source, confirmation_label, locked FROM segment_confirmations WHERE segment_id = ?",
                (int(record["segment_id"]),),
            ).fetchone()
            result = {
                "segment_id": int(record["segment_id"]),
                "file_ok": line == record["target_raw_line"],
                "output_db_ok": bool(output and output["portuguese_text"] == record["target_text"] and output["output_raw_line"] == record["target_raw_line"]),
                "confirmation_ok": bool(
                    conf
                    and conf["confirmed_text"] == record["target_text"]
                    and conf["confirmation_source"] == CONFIRMATION_SOURCE
                    and conf["confirmation_label"] == CONFIRMATION_LABEL
                    and int(conf["locked"] or 0) == 1
                ),
                "token_integrity_ok": bool(record["token_integrity_ok"]),
                "structure_integrity_ok": bool(record["structure_integrity_ok"]),
                "utf8_real_ok": "\\u00" not in line and "\\u00" not in str(record["target_text"]),
            }
            for key, value in result.items():
                if key != "segment_id":
                    counts[key] += int(bool(value))
            details.append(result)
    return {**dict(counts), "details": details}


def ingest_learning(records: list[dict[str, Any]]) -> int:
    ready = [record for record in records if record["status"] == "ready"]
    timestamp = now_iso()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO local_learning_runs (
              mode, limit_count, auto_confidence_threshold, candidate_count,
              high_confidence_count, pending_human_count, status, notes,
              started_at, finished_at, updated_at
            )
            VALUES ('hotfix', ?, 1.0, ?, ?, 0, 'completed', ?, ?, ?, ?)
            """,
            (len(ready), len(ready), len(ready), "qa visual francia nicknames bookmarks post600", timestamp, timestamp, timestamp),
        )
        run_id = int(cur.lastrowid)
        for record in ready:
            reasons = [
                "qa_visual_hotfix",
                "human_confirmed",
                str(record["hotfix_kind"]),
                "protected_apply_post_validated",
                "no_source_write",
                "utf8_real",
            ]
            conn.execute(
                """
                INSERT INTO local_learning_candidates (
                  run_id, feedback_id, suggestion_id, segment_id, relative_path,
                  source_key, source_line_number, english_text, spanish_text, old_text,
                  current_output_text, suggested_text, suggested_hash, source_language,
                  origin, match_type, match_score, token_status, suggestion_status,
                  local_confidence_score, local_status, human_label, corrected_text,
                  reason, reviewer, reviewed_at, reasons_json, created_at, updated_at,
                  queue_source, focus_group
                )
                VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(record["segment_id"]),
                    record["relative_path"],
                    record["source_key"],
                    record["source_line_number"],
                    record["english_text"],
                    record["spanish_text"],
                    record["old_text"],
                    record["current_output_text"],
                    record["target_text"],
                    record["target_hash"],
                    "human_corrected",
                    "human_confirmed_qa_visual_hotfix",
                    record["hotfix_kind"],
                    1.0,
                    "ok_authorized_hotfix",
                    "safe",
                    1.0,
                    "high_confidence",
                    "correct",
                    record["target_text"],
                    "human-approved QA visual Francia/nickname/bookmark hotfix",
                    "user_human_review_qa_visual_hotfix",
                    timestamp,
                    json.dumps(reasons, ensure_ascii=True),
                    timestamp,
                    timestamp,
                    "qa-visual-hotfix",
                    "qa_visual_francia_nicknames_bookmarks_post600",
                ),
            )
        conn.commit()
    return run_id


def write_report(slug: str, records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_{slug}"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n", encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"# {slug}",
        "",
        f"mode: `{summary['mode']}`",
        f"ready_count: `{summary['ready_count']}`",
        f"blocked_count: `{summary['blocked_count']}`",
        f"applied_count: `{summary.get('applied_count', 0)}`",
        "",
    ]
    for record in records:
        lines.append(f"## {record['source_key']} ({record['segment_id']})")
        lines.append(f"- file: `{record['relative_path']}:{record['output_line_number']}`")
        lines.append(f"- current: `{record['current_output_text']}`")
        lines.append(f"- target: `{record['target_text']}`")
        lines.append(f"- status: `{record['status']}`")
        lines.append(f"- token_integrity_ok: `{record['token_integrity_ok']}`")
        lines.append(f"- utf8_real_ok: `{record['utf8_real_ok']}`")
        lines.extend(["```diff", f"- {record['current_raw_line']}", f"+ {record['target_raw_line']}", "```"])
        if record["block_reasons"]:
            lines.append(f"- block_reasons: `{record['block_reasons']}`")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def run(apply: bool) -> None:
    records = validate_records(load_package_records())
    blocked = [record for record in records if record["status"] != "ready"]
    mode = "dry_run"
    applied = 0
    files_touched = 0
    backup_root = None
    learning_run_id = None
    post = {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "token_integrity_ok": 0, "structure_integrity_ok": 0, "utf8_real_ok": 0, "details": []}
    if apply:
        if blocked:
            mode = "blocked"
        else:
            mode = "apply_learning"
            applied, files_touched, backup_root = apply_records(records)
            post = post_validate(records)
            expected = len(records)
            for key in ["file_ok", "output_db_ok", "confirmation_ok", "token_integrity_ok", "structure_integrity_ok", "utf8_real_ok"]:
                if int(post.get(key) or 0) != expected:
                    raise SystemExit(f"post-validation guard failed for {key}; refusing learning ingest")
            learning_run_id = ingest_learning(records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len([record for record in records if record["status"] == "ready"]),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "applied_ids": [int(record["segment_id"]) for record in records if record["status"] == "ready"],
        "applied_count": applied,
        "apply_count": applied,
        "files_touched_count": files_touched,
        "files_touched": sorted({str(record["relative_path"]) for record in records if record["status"] == "ready"}),
        "backup_root": backup_root,
        "rollback_path": backup_root or "not_created",
        "post_validation": post,
        "learning_ingest_count": 1 if learning_run_id else 0,
        "learning_run_id": learning_run_id,
        "issue_closure_count": 0,
        "candidate_generation_count": 0,
        "discovery_amplo_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(applied),
    }
    md, jsonl, summary_path = write_report("qa_visual_francia_nicknames_bookmarks_apply_learning", records, summary)
    print(json.dumps({"markdown": str(md), "jsonl": str(jsonl), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.apply)


if __name__ == "__main__":
    main()
