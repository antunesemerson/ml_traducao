from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text


SEGMENT_ID = 149518
SOURCE_KEY = "INVENTORY_TAB_CLAIMS_HIS_HER_MY"
TARGET_TEXT = "Reivindicações"
SOURCE = "qa_visual_claims_button_hotfix_v1"
CONFIRMATION_SOURCE = "codex_qa_visual_hotfix_human_confirmed"
CONFIRMATION_LABEL = "qa_visual_claims_button_hotfix_post596"
EXPECTED_CURRENT = "[Character.GetHerHisMy|U] $INVENTORY_TAB_CLAIMS$"


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


def fetch_row() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
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
            WHERE s.id = ?
            """,
            (SEGMENT_ID,),
        ).fetchone()
    if not row:
        raise SystemExit(f"missing segment {SEGMENT_ID}")
    return dict(row)


def validate(row: dict[str, Any]) -> dict[str, Any]:
    current_text = str(row["output_text"] or "")
    current_raw = str(row["output_raw_line"] or "")
    target_raw = replace_quoted_text(current_raw, TARGET_TEXT)
    path = output_root() / str(row["relative_path"])
    reasons: list[str] = []
    disk_line = ""
    if not path.exists():
        reasons.append("missing_output_file")
    else:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        idx = int(row["output_line_number"]) - 1
        disk_line = lines[idx] if 0 <= idx < len(lines) else ""
        if disk_line != current_raw:
            reasons.append("disk_line_mismatch_output_segments")
    if int(row["segment_id"]) != SEGMENT_ID:
        reasons.append("segment_id_mismatch")
    if row["source_key"] != SOURCE_KEY:
        reasons.append("source_key_mismatch")
    if current_text != EXPECTED_CURRENT:
        reasons.append("current_text_not_expected_claims_compound")
    if str(row["confirmed_text"] or "") != current_text:
        reasons.append("confirmed_text_not_current_output")
    if TARGET_TEXT == current_text:
        reasons.append("no_output_change")
    if "\n" in TARGET_TEXT or "\r" in TARGET_TEXT or '"' in TARGET_TEXT:
        reasons.append("structure_integrity_mismatch")
    old_tokens = protected_tokens(current_text)
    new_tokens = protected_tokens(TARGET_TEXT)
    token_ok = old_tokens == ["[Character.GetHerHisMy|U]", "$INVENTORY_TAB_CLAIMS$"] and new_tokens == []
    if not token_ok:
        reasons.append("token_integrity_failed")
    return {
        "record_type": "qa_visual_claims_button_hotfix",
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_line_number": row["source_line_number"],
        "output_line_number": row["output_line_number"],
        "source_key": row["source_key"],
        "english_text": row["english_text"],
        "spanish_text": row["spanish_text"],
        "old_text": row["old_text"],
        "current_output_text": current_text,
        "current_confirmed_text": row["confirmed_text"],
        "confirmation_level": row["confirmation_level"],
        "locked": row["locked"],
        "target_text": TARGET_TEXT,
        "target_hash": stable_hash(TARGET_TEXT),
        "hotfix_kind": "explicit_ui_compact_possessive_removal_allowlist",
        "evidence": "Inventory claims tab renders Meu Reivindicações / Meu Reivindicações0; compact UI label should be Reivindicações.",
        "current_tokens": old_tokens,
        "target_tokens": new_tokens,
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": "\n" not in TARGET_TEXT and "\r" not in TARGET_TEXT and '"' not in TARGET_TEXT,
        "canonical_l10n_ok": TARGET_TEXT != current_text,
        "current_raw_line": current_raw,
        "target_raw_line": target_raw,
        "disk_line": disk_line,
        "status": "ready" if not reasons else "blocked",
        "classification": "hotfix_ready" if not reasons else "blocked",
        "block_reasons": reasons,
    }


def apply_record(record: dict[str, Any]) -> tuple[int, str]:
    backup_root = db.project_path("memory/backups") / f"qa_visual_claims_button_hotfix_{stamp()}"
    timestamp = now_iso()
    output_path = output_root() / str(record["relative_path"])
    backup_path = backup_root / str(record["relative_path"])
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output_path, backup_path)
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    idx = int(record["output_line_number"]) - 1
    if lines[idx] != record["current_raw_line"]:
        raise SystemExit("disk line changed before apply")
    lines[idx] = str(record["target_raw_line"])
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    with connect() as conn:
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
    return 1, str(backup_root)


def post_validate(record: dict[str, Any]) -> dict[str, Any]:
    line = (output_root() / str(record["relative_path"])).read_text(encoding="utf-8-sig").splitlines()[
        int(record["output_line_number"]) - 1
    ]
    with connect() as conn:
        output = conn.execute(
            "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id = ?",
            (int(record["segment_id"]),),
        ).fetchone()
        conf = conn.execute(
            "SELECT confirmed_text, confirmation_source, confirmation_label, locked FROM segment_confirmations WHERE segment_id = ?",
            (int(record["segment_id"]),),
        ).fetchone()
    return {
        "file_ok": int(line == record["target_raw_line"]),
        "output_db_ok": int(bool(output and output["portuguese_text"] == record["target_text"] and output["output_raw_line"] == record["target_raw_line"])),
        "confirmation_ok": int(
            bool(
                conf
                and conf["confirmed_text"] == record["target_text"]
                and conf["confirmation_source"] == CONFIRMATION_SOURCE
                and conf["confirmation_label"] == CONFIRMATION_LABEL
                and int(conf["locked"] or 0) == 1
            )
        ),
        "token_integrity_ok": int(bool(record["token_integrity_ok"])),
        "structure_integrity_ok": int(bool(record["structure_integrity_ok"])),
    }


def ingest_learning(record: dict[str, Any]) -> int:
    timestamp = now_iso()
    reasons = [
        "qa_visual_hotfix",
        "human_confirmed",
        "explicit_ui_compact_possessive_removal_allowlist",
        "protected_apply_post_validated",
        "no_source_write",
    ]
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO local_learning_runs (
              mode, limit_count, auto_confidence_threshold, candidate_count,
              high_confidence_count, pending_human_count, status, notes,
              started_at, finished_at, updated_at
            )
            VALUES ('hotfix', 1, 1.0, 1, 1, 0, 'completed', ?, ?, ?, ?)
            """,
            ("qa visual claims button hotfix post596", timestamp, timestamp, timestamp),
        )
        run_id = int(cur.lastrowid)
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
                "human-approved QA visual claims button hotfix",
                "user_human_review_qa_visual_hotfix",
                timestamp,
                json.dumps(reasons, ensure_ascii=True),
                timestamp,
                timestamp,
                "qa-visual-hotfix",
                "qa_visual_claims_button_hotfix_post596",
            ),
        )
        conn.commit()
    return run_id


def write_report(slug: str, records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_{slug}"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records) + "\n", encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"# {slug}",
        "",
        f"mode: `{summary['mode']}`",
        f"ready_count: `{summary.get('ready_count', 0)}`",
        f"blocked_count: `{summary.get('blocked_count', 0)}`",
        f"applied_count: `{summary.get('applied_count', 0)}`",
        "",
    ]
    for record in records:
        lines.append(f"## {record['source_key']} ({record['segment_id']})")
        lines.append(f"- file: `{record['relative_path']}:{record['output_line_number']}`")
        lines.append(f"- current: `{record['current_output_text']}`")
        lines.append(f"- target: `{record['target_text']}`")
        lines.append(f"- status: `{record['status']}`")
        lines.append(f"- token policy: `{record['hotfix_kind']}`")
        lines.extend(["```diff", f"- {record['current_raw_line']}", f"+ {record['target_raw_line']}", "```"])
        if record.get("block_reasons"):
            lines.append(f"- block_reasons: `{record['block_reasons']}`")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def run(apply: bool) -> None:
    record = validate(fetch_row())
    blocked = record["status"] != "ready"
    applied = 0
    backup_root = None
    learning_run_id = None
    post = {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "token_integrity_ok": 0, "structure_integrity_ok": 0}
    mode = "dry_run"
    if apply:
        if blocked:
            mode = "blocked"
        else:
            mode = "apply_learning"
            applied, backup_root = apply_record(record)
            post = post_validate(record)
            if not all(post[key] == 1 for key in ["file_ok", "output_db_ok", "confirmation_ok", "token_integrity_ok", "structure_integrity_ok"]):
                raise SystemExit("post-validation failed; refusing learning ingest")
            learning_run_id = ingest_learning(record)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": 1,
        "ready_count": int(not blocked),
        "blocked_count": int(blocked),
        "block_reason_counts": dict(Counter(record.get("block_reasons", [])).most_common()),
        "applied_count": applied,
        "apply_count": applied,
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
    md, jsonl, summary_path = write_report("qa_visual_claims_button_hotfix_apply_learning", [record], summary)
    print(json.dumps({"markdown": str(md), "jsonl": str(jsonl), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.apply)


if __name__ == "__main__":
    main()
