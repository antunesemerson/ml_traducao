from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text


SOURCE = "qa_post_release_visual_event_personality_hotfix_v1"
CONFIRMATION_SOURCE = "codex_qa_post_release_visual_hotfix_human_confirmed"
CONFIRMATION_LABEL = "qa_post_release_event_personality_hotfix"
PACKAGE_JSONL = Path("reports/20260704_184527_359955_qa_post_release_visual_event_personality_package_readonly_v2.jsonl")
EXPECTED_READY_IDS = {124725, 5419, 5494, 5427, 106691, 31205}

TOKEN_RE = re.compile(r"(\[[^\[\]]+\]|\$[^$]+\$|#[A-Za-z0-9_]+|#!)")
SELECT_RE = re.compile(r"\[Select_CString\(\s*([^,]+),\s*'[^']*',\s*'[^']*'\s*\)\]")


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
    return TOKEN_RE.findall(text or "")


def token_surface_signature(text: str) -> list[str]:
    normalized = SELECT_RE.sub(lambda match: f"[Select_CString({match.group(1).strip()},'<L1>','<L2>')]", text or "")
    return protected_tokens(normalized)


def load_package_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in db.project_path(PACKAGE_JSONL).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("decision") == "hotfix_ready" and record.get("hotfix_ready_guard_ok"):
            records.append(record)
    ids = {int(record["segment_id"]) for record in records}
    if ids != EXPECTED_READY_IDS:
        raise SystemExit(f"ready id guard failed: expected {sorted(EXPECTED_READY_IDS)}, got {sorted(ids)}")
    return sorted(records, key=lambda row: (str(row["relative_path"]), int(row["output_line_number"]), int(row["segment_id"])))


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
    validated: list[dict[str, Any]] = []
    for package in package_records:
        segment_id = int(package["segment_id"])
        row = rows.get(segment_id)
        reasons: list[str] = []
        if not row:
            reasons.append("missing_segment")
            row = {}
        current_text = str(row.get("output_text") or "")
        current_raw = str(row.get("output_raw_line") or "")
        target_text = str(package["proposed_text"])
        target_raw = replace_quoted_text(current_raw, target_text) if current_raw else ""
        if row.get("source_key") != package.get("source_key"):
            reasons.append("source_key_mismatch")
        if row.get("relative_path") != package.get("relative_path"):
            reasons.append("relative_path_mismatch")
        if current_text != str(package.get("output_text") or ""):
            reasons.append("current_output_text_changed_since_package")
        if target_text == current_text:
            reasons.append("no_output_change")
        if "\\u00" in target_text or "\\u00" in target_raw:
            reasons.append("target_contains_literal_unicode_escape")
        old_tokens = protected_tokens(current_text)
        new_tokens = protected_tokens(target_text)
        token_exact_ok = old_tokens == new_tokens
        token_surface_ok = bool(package.get("token_surface_guard_ok")) and token_surface_signature(current_text) == token_surface_signature(target_text)
        if not (token_exact_ok or token_surface_ok):
            reasons.append("token_integrity_failed")
        structure_ok = "\r" not in target_text and target_raw and current_raw.split(":", 1)[0] == target_raw.split(":", 1)[0]
        if not structure_ok:
            reasons.append("structure_integrity_failed")
        output_path = output_root() / str(row.get("relative_path", ""))
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
                "record_type": "qa_post_release_event_personality_hotfix_item",
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
                "current_raw_line": current_raw,
                "target_raw_line": target_raw,
                "disk_line": disk_line,
                "classification": package.get("classification"),
                "category": package.get("category"),
                "risk": package.get("risk"),
                "notes": package.get("notes"),
                "current_tokens": old_tokens,
                "target_tokens": new_tokens,
                "token_integrity_exact_ok": token_exact_ok,
                "token_surface_guard_ok": token_surface_ok,
                "token_integrity_ok": token_exact_ok or token_surface_ok,
                "structure_integrity_ok": bool(structure_ok),
                "canonical_l10n_ok": bool(target_text.strip()) and target_text != current_text,
                "utf8_real_ok": "\\u00" not in target_text and "\\u00" not in target_raw,
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
            }
        )
    return validated


def apply_records(records: list[dict[str, Any]]) -> tuple[int, int, str, list[str]]:
    ready = [record for record in records if record["status"] == "ready"]
    backup_root = db.project_path("memory/backups") / f"qa_post_release_event_personality_hotfix_{stamp()}"
    timestamp = now_iso()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in ready:
        by_path[str(record["relative_path"])].append(record)
    files_touched: list[str] = []
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
                    (record["target_text"], record["target_raw_line"], record["target_hash"], timestamp, int(record["segment_id"])),
                )
                conn.execute(
                    """
                    INSERT INTO segment_confirmations (
                      segment_id, confirmation_level, confirmed_text, confirmation_source,
                      confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
                    )
                    VALUES (?, 'human_confirmed', ?, ?, ?, 1, 1.0, 'codex', ?, ?)
                    ON CONFLICT(segment_id) DO UPDATE SET
                      confirmation_level = 'human_confirmed',
                      confirmed_text = excluded.confirmed_text,
                      confirmation_source = excluded.confirmation_source,
                      confirmation_label = excluded.confirmation_label,
                      locked = 1,
                      confidence_score = 1.0,
                      reviewer = 'codex',
                      updated_at = excluded.updated_at
                    """,
                    (int(record["segment_id"]), record["target_text"], CONFIRMATION_SOURCE, CONFIRMATION_LABEL, timestamp, timestamp),
                )
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched.append(relative_path)
        conn.commit()
    return len(ready), len(files_touched), str(backup_root), files_touched


def post_validate(records: list[dict[str, Any]]) -> dict[str, int]:
    totals = Counter()
    with connect() as conn:
        for record in records:
            if record["status"] != "ready":
                continue
            output_path = output_root() / str(record["relative_path"])
            line = output_path.read_text(encoding="utf-8-sig").splitlines()[int(record["output_line_number"]) - 1]
            output = conn.execute(
                "SELECT portuguese_text, output_raw_line FROM output_segments WHERE segment_id = ?",
                (int(record["segment_id"]),),
            ).fetchone()
            conf = conn.execute(
                "SELECT confirmed_text, confirmation_source, confirmation_label, locked FROM segment_confirmations WHERE segment_id = ?",
                (int(record["segment_id"]),),
            ).fetchone()
            totals["file_ok"] += int(line == record["target_raw_line"])
            totals["output_db_ok"] += int(bool(output and output["portuguese_text"] == record["target_text"] and output["output_raw_line"] == record["target_raw_line"]))
            totals["confirmation_ok"] += int(
                bool(
                    conf
                    and conf["confirmed_text"] == record["target_text"]
                    and conf["confirmation_source"] == CONFIRMATION_SOURCE
                    and conf["confirmation_label"] == CONFIRMATION_LABEL
                    and int(conf["locked"] or 0) == 1
                )
            )
            totals["token_integrity_ok"] += int(bool(record["token_integrity_ok"]))
            totals["structure_integrity_ok"] += int(bool(record["structure_integrity_ok"]))
            totals["utf8_real_ok"] += int("\\u00" not in line)
    return dict(totals)


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
            (
                len(ready),
                len(ready),
                len(ready),
                "QA post-release event/personality visual hotfix batch",
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        run_id = int(cur.lastrowid)
        for record in ready:
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
                    "human_confirmed_qa_post_release_visual_hotfix",
                    record["classification"],
                    1.0,
                    "ok_authorized_hotfix",
                    "safe",
                    1.0,
                    "high_confidence",
                    "correct",
                    record["target_text"],
                    "human-approved QA post-release visual hotfix",
                    "user_human_review_qa_post_release_visual_hotfix",
                    timestamp,
                    json.dumps(["qa_post_release_visual_hotfix", "human_confirmed", "protected_apply_post_validated"], ensure_ascii=True),
                    timestamp,
                    timestamp,
                    "qa-post-release-hotfix",
                    "qa_post_release_event_personality_hotfix",
                ),
            )
        conn.commit()
    return run_id


def issue_diagnostic(segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    with connect() as conn:
        issue_rows = [dict(row) for row in conn.execute(f"SELECT * FROM issues WHERE segment_id IN ({placeholders})", segment_ids)]
        ledger_run = conn.execute("SELECT MAX(id) AS id FROM ml_issue_ledger_runs").fetchone()["id"]
        ledger_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT segment_id, id, issue_family, issue_kind, issue_severity, status, proposed_action, validation_status
                FROM ml_issue_ledger_items
                WHERE run_id = ? AND segment_id IN ({placeholders})
                """,
                [ledger_run, *segment_ids],
            )
        ]
    return {"issues": issue_rows, "latest_ledger_run_id": ledger_run, "ledger_items": ledger_rows}


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
        f"applied_count: `{summary['applied_count']}`",
        f"learning_run_id: `{summary.get('learning_run_id')}`",
        f"backup_root: `{summary.get('backup_root')}`",
        "",
        "## Items",
    ]
    for record in records:
        lines.extend(
            [
                "",
                f"### {record['segment_id']} `{record['source_key']}`",
                f"- file: `{record['relative_path']}:{record['output_line_number']}`",
                f"- status: `{record['status']}`",
                f"- current: `{record['current_output_text']}`",
                f"- target: `{record['target_text']}`",
                f"- token_ok: `{record['token_integrity_ok']}`; structure_ok: `{record['structure_integrity_ok']}`; utf8_real_ok: `{record['utf8_real_ok']}`",
                "```diff",
                f"- {record['current_raw_line']}",
                f"+ {record['target_raw_line']}",
                "```",
            ]
        )
        if record["block_reasons"]:
            lines.append(f"- block_reasons: `{record['block_reasons']}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def run(apply: bool) -> None:
    records = validate_records(load_package_records())
    ready = [record for record in records if record["status"] == "ready"]
    blocked = [record for record in records if record["status"] != "ready"]
    mode = "dry_run"
    applied_count = 0
    files_touched_count = 0
    files_touched: list[str] = []
    backup_root = None
    learning_run_id = None
    post = {
        "file_ok": 0,
        "output_db_ok": 0,
        "confirmation_ok": 0,
        "token_integrity_ok": 0,
        "structure_integrity_ok": 0,
        "utf8_real_ok": 0,
    }
    if apply:
        if blocked:
            mode = "blocked"
        else:
            mode = "apply_learning"
            applied_count, files_touched_count, backup_root, files_touched = apply_records(records)
            post = post_validate(records)
            for key in ["file_ok", "output_db_ok", "confirmation_ok", "token_integrity_ok", "structure_integrity_ok", "utf8_real_ok"]:
                if int(post.get(key) or 0) != applied_count:
                    raise SystemExit(f"post-validation guard failed for {key}; refusing learning ingest")
            learning_run_id = ingest_learning(records)
    issues = issue_diagnostic([int(record["segment_id"]) for record in records])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "applied_ids": [int(record["segment_id"]) for record in ready] if apply and not blocked else [],
        "applied_count": applied_count,
        "apply_count": applied_count,
        "files_touched_count": files_touched_count,
        "files_touched": files_touched,
        "backup_root": backup_root,
        "rollback_path": backup_root or "not_created",
        "post_validation": post,
        "learning_ingest_count": len(ready) if learning_run_id else 0,
        "learning_run_id": learning_run_id,
        "issue_diagnostic": issues,
        "issue_closure_count": 0,
        "candidate_generation_count": 0,
        "discovery_amplo_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(applied_count),
    }
    md, jsonl, summary_path = write_report("qa_post_release_event_personality_hotfix_apply_learning", records, summary)
    print(json.dumps({"markdown": str(md), "jsonl": str(jsonl), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.apply)


if __name__ == "__main__":
    main()
