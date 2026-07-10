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


SOURCE = "nick_the_german_visual_bug_hotfix_v1"
SEGMENT_KEY = "nick_the_german"
RELATIVE_PATH = "nicknames_l_spanish.yml"
EXPECTED_POST_MORTEM_KEY = "nick_the_german_post_mortem"
EXPECTED_POST_MORTEM_VALUE = "$nick_the_german$"
CURRENT_TEXT = "o/a Alem\u00e3o/\u00e3[CHARACTER.Custom('ES_AnAna')]"
TARGET_TEXT = "[Select_CString( CHARACTER.IsFemale, 'a Alem\u00e3', 'o Alem\u00e3o' )]"
CONFIRMATION_SOURCE = "codex_hotfix_visual_bug_human_confirmed"
CONFIRMATION_LABEL = "nick_the_german_select_cstring_gender_hotfix"


def now_stamp() -> str:
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


def line_value(raw_line: str) -> str:
    prefix, _, rest = raw_line.partition(":")
    if not _:
        return ""
    rest = rest.strip()
    if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
        return rest[1:-1]
    return rest


def select_tokens(text: str) -> list[str]:
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
        elif text[i] == "$":
            j = text.find("$", i + 1)
            if j != -1:
                tokens.append(text[i : j + 1])
                i = j + 1
                continue
        i += 1
    return tokens


def fetch_context() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              s.id AS segment_id,
              s.relative_path,
              s.source_line_number,
              s.source_key,
              s.spanish_text,
              s.english_text,
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
            WHERE s.relative_path = ? AND s.source_key = ?
            """,
            (RELATIVE_PATH, SEGMENT_KEY),
        ).fetchone()
        if not row:
            raise SystemExit("missing nick_the_german source/output segment")
        latest_state_run = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs").fetchone()["id"]
        state = conn.execute(
            "SELECT * FROM segment_state_items WHERE run_id = ? AND segment_id = ?",
            (latest_state_run, int(row["segment_id"])),
        ).fetchone()
        issue_rows = []
        try:
            issue_rows = [dict(r) for r in conn.execute("SELECT * FROM issues WHERE segment_id = ?", (int(row["segment_id"]),)).fetchall()]
        except sqlite3.OperationalError:
            issue_rows = []
    return {
        "segment": dict(row),
        "latest_segment_state_run_id": int(latest_state_run or 0),
        "segment_state": dict(state) if state else None,
        "issues": issue_rows,
    }


def build_record() -> dict[str, Any]:
    ctx = fetch_context()
    seg = ctx["segment"]
    output_path = output_root() / str(seg["relative_path"])
    disk_lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    output_line = int(seg["output_line_number"])
    disk_line = disk_lines[output_line - 1]
    target_raw_line = replace_quoted_text(str(seg["output_raw_line"]), TARGET_TEXT)
    post_line = next((line for line in disk_lines if line.strip().startswith(EXPECTED_POST_MORTEM_KEY + ":")), "")
    reasons: list[str] = []
    if int(seg["segment_id"]) != 229182:
        reasons.append("unexpected_segment_id")
    if seg["relative_path"] != RELATIVE_PATH:
        reasons.append("unexpected_relative_path")
    if seg["source_key"] != SEGMENT_KEY:
        reasons.append("unexpected_source_key")
    if str(seg["output_text"]) != CURRENT_TEXT:
        reasons.append("unexpected_current_output_text")
    if str(seg["confirmed_text"] or "") != CURRENT_TEXT:
        reasons.append("unexpected_current_confirmed_text")
    if disk_line != str(seg["output_raw_line"]):
        reasons.append("disk_line_mismatch_output_segments")
    if line_value(post_line) != EXPECTED_POST_MORTEM_VALUE:
        reasons.append("post_mortem_reference_not_preserved")
    if not TARGET_TEXT.startswith("[Select_CString( CHARACTER.IsFemale, "):
        reasons.append("target_not_expected_select_cstring")
    if "\n" in TARGET_TEXT or "\r" in TARGET_TEXT:
        reasons.append("structure_integrity_mismatch")
    old_tokens = select_tokens(CURRENT_TEXT)
    new_tokens = select_tokens(TARGET_TEXT)
    token_delta_authorized = (
        old_tokens == ["[CHARACTER.Custom('ES_AnAna')]"]
        and new_tokens == ["[Select_CString( CHARACTER.IsFemale, 'a Alemã', 'o Alemão' )]"]
    )
    if not token_delta_authorized:
        reasons.append("token_delta_not_authorized")
    if len(ctx["issues"]) > 0:
        # Existing issue closure is handled separately; this does not block the apply.
        pass
    return {
        "source": SOURCE,
        "record_type": "hotfix_candidate",
        "evidence": "Screenshot: Rei Ludwig II 'o/a Alemão/ãan' de Francia Oriental",
        "segment_id": int(seg["segment_id"]),
        "relative_path": seg["relative_path"],
        "source_key": seg["source_key"],
        "source_line_number": seg["source_line_number"],
        "output_line_number": output_line,
        "english_text": seg["english_text"],
        "spanish_text": seg["spanish_text"],
        "old_text": seg["old_text"],
        "current_output_text": seg["output_text"],
        "target_text": TARGET_TEXT,
        "current_confirmed_text": seg["confirmed_text"],
        "current_raw_line": seg["output_raw_line"],
        "target_raw_line": target_raw_line,
        "post_mortem_line": post_line,
        "post_mortem_preserved": line_value(post_line) == EXPECTED_POST_MORTEM_VALUE,
        "old_tokens": old_tokens,
        "new_tokens": new_tokens,
        "token_delta_authorized": token_delta_authorized,
        "token_integrity_ok": token_delta_authorized,
        "structure_integrity_ok": "\n" not in TARGET_TEXT and "\r" not in TARGET_TEXT,
        "status": "ready" if not reasons else "blocked",
        "block_reasons": reasons,
        "issue_count_existing": len(ctx["issues"]),
        "issues_existing": ctx["issues"],
        "latest_segment_state_run_id": ctx["latest_segment_state_run_id"],
        "latest_final_state": (ctx["segment_state"] or {}).get("final_state"),
        "latest_needs_output_apply": (ctx["segment_state"] or {}).get("needs_output_apply"),
        "target_hash": stable_hash(TARGET_TEXT),
    }


def apply_record(record: dict[str, Any]) -> dict[str, Any]:
    if record["status"] != "ready":
        raise SystemExit(f"cannot apply blocked record: {record['block_reasons']}")
    backup_root = db.project_path("memory/backups") / f"nick_the_german_hotfix_{now_stamp()}"
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
    timestamp = now_iso()
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
        cur = conn.execute(
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
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO segment_confirmations (
                  segment_id, confirmation_level, confirmed_text,
                  confirmation_source, confirmation_label, locked,
                  confidence_score, candidate_id, feedback_id, reviewer,
                  confirmed_at, updated_at
                )
                VALUES (?, 'human_confirmed', ?, ?, ?, 1, 1.0, NULL, NULL, 'codex', ?, ?)
                """,
                (
                    int(record["segment_id"]),
                    record["target_text"],
                    CONFIRMATION_SOURCE,
                    CONFIRMATION_LABEL,
                    timestamp,
                    timestamp,
                ),
            )
        conn.commit()
    return {"backup_root": str(backup_root), "rollback_path": str(backup_root)}


def post_validate(record: dict[str, Any]) -> dict[str, Any]:
    output_path = output_root() / str(record["relative_path"])
    line = output_path.read_text(encoding="utf-8-sig").splitlines()[int(record["output_line_number"]) - 1]
    with connect() as conn:
        output = conn.execute(
            "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id = ?",
            (int(record["segment_id"]),),
        ).fetchone()
        conf = conn.execute(
            "SELECT confirmed_text, confirmation_source, confirmation_label, locked FROM segment_confirmations WHERE segment_id = ?",
            (int(record["segment_id"]),),
        ).fetchone()
    file_ok = line == record["target_raw_line"]
    output_db_ok = bool(output and output["portuguese_text"] == record["target_text"] and output["output_raw_line"] == record["target_raw_line"])
    confirmation_ok = bool(
        conf
        and conf["confirmed_text"] == record["target_text"]
        and conf["confirmation_source"] == CONFIRMATION_SOURCE
        and conf["confirmation_label"] == CONFIRMATION_LABEL
        and int(conf["locked"] or 0) == 1
    )
    return {
        "file_ok": int(file_ok),
        "output_db_ok": int(output_db_ok),
        "confirmation_ok": int(confirmation_ok),
        "token_integrity_ok": int(record["token_integrity_ok"]),
        "structure_integrity_ok": int(record["structure_integrity_ok"]),
        "details": [
            {
                "segment_id": int(record["segment_id"]),
                "file_ok": file_ok,
                "output_db_ok": output_db_ok,
                "confirmation_ok": confirmation_ok,
                "token_integrity_ok": bool(record["token_integrity_ok"]),
                "structure_integrity_ok": bool(record["structure_integrity_ok"]),
            }
        ],
    }


def ingest_learning(record: dict[str, Any]) -> int:
    timestamp = now_iso()
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
            ("nick_the_german visual bug hotfix human-confirmed", timestamp, timestamp, timestamp),
        )
        run_id = int(cur.lastrowid)
        reasons = [
            "visual_bug_in_game",
            "human_confirmed_hotfix",
            "token_delta_authorized_select_cstring_gender",
            "post_mortem_reference_preserved",
            "protected_apply_post_validated",
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
                "human_confirmed_nick_the_german_visual_hotfix",
                "nickname_select_cstring_gender_hotfix",
                1.0,
                "ok_authorized_token_delta",
                "safe",
                1.0,
                "high_confidence",
                "correct",
                record["target_text"],
                "human-approved visual hotfix for nick_the_german",
                "user_human_review_visual_bug_hotfix",
                timestamp,
                json.dumps(reasons, ensure_ascii=True),
                timestamp,
                timestamp,
                "visual-bug-hotfix",
                "nick_the_german_hotfix",
            ),
        )
        conn.commit()
    return run_id


def write_reports(mode: str, record: dict[str, Any], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{now_stamp()}_nick_the_german_hotfix_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "nick_the_german visual bug hotfix",
                f"mode={mode}",
                f"segment_id={record['segment_id']}",
                f"status={record['status']}",
                f"ready_count={summary['ready_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"applied_count={summary['applied_count']}",
                f"learning_run_id={summary.get('learning_run_id')}",
                f"issue_closure_count={summary['issue_closure_count']}",
                f"backup_root={summary.get('backup_root')}",
                f"rollback_path={summary.get('rollback_path')}",
                f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "discovery_amplo_count=0",
                f"apply_count={summary['apply_count']}",
                f"learning_ingest_count={summary['learning_ingest_count']}",
                "reindex_count=0",
                "production_full_count=0",
                "source_changed=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ingest-learning", action="store_true")
    args = parser.parse_args()
    record = build_record()
    blocked = record["status"] != "ready"
    mode = "blocked" if blocked else "dry_run"
    applied = 0
    learning_run_id = None
    backup = {"backup_root": None, "rollback_path": "not_created_dry_run_or_blocked"}
    post = {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "token_integrity_ok": 0, "structure_integrity_ok": 0, "details": []}
    if args.apply and not blocked:
        mode = "apply"
        backup = apply_record(record)
        applied = 1
        post = post_validate(record)
    if args.ingest_learning and not blocked:
        if not args.apply:
            post = post_validate(record)
        if not all(post.get(k) == 1 for k in ["file_ok", "output_db_ok", "confirmation_ok", "token_integrity_ok", "structure_integrity_ok"]):
            raise SystemExit("post-validation guard failed; refusing learning ingest")
        learning_run_id = ingest_learning(record)
        mode = "apply_learning" if args.apply else "learning"
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "ready_count": 0 if blocked else 1,
        "blocked_count": 1 if blocked else 0,
        "block_reason_counts": dict(Counter(record["block_reasons"]).most_common()),
        "applied_count": applied,
        "apply_count": applied,
        "learning_ingest_count": 1 if learning_run_id else 0,
        "learning_run_id": learning_run_id,
        "issue_closure_count": 0,
        "issue_existing_count": record["issue_count_existing"],
        "post_validation": post,
        "backup_root": backup["backup_root"],
        "rollback_path": backup["rollback_path"],
        "post_mortem_preserved": record["post_mortem_preserved"],
        "token_delta_authorized": record["token_delta_authorized"],
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
    txt, jsonl_path, summary_path = write_reports(mode, record, summary)
    print(json.dumps({"txt": str(txt), "jsonl": str(jsonl_path), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
