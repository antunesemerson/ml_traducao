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


SOURCE = "qa_visual_hotfix_batch_post596_v1"
CONFIRMATION_SOURCE = "codex_qa_visual_hotfix_human_confirmed"
CONFIRMATION_LABEL = "qa_visual_hotfix_post596"

HOTFIX_TARGETS: dict[int, dict[str, Any]] = {
    229649: {
        "source_key": "nick_the_missionary",
        "target_text": "[Select_CString( CHARACTER.IsFemale, 'a Mission\\u00e1ria', 'o Mission\\u00e1rio' )]",
        "kind": "authorized_ES_helper_to_Select_CString_gender_delta",
        "evidence": "Inventory/name QA visual: rendered o/a Mission\\u00e1rio/o",
    },
    229132: {
        "source_key": "nick_the_bald",
        "target_text": "[Select_CString( CHARACTER.IsFemale, 'a Calva', 'o Calvo' )]",
        "kind": "authorized_ES_helper_to_Select_CString_gender_delta",
        "evidence": "Visual QA: masculine ok, feminine would render article/gender mismatch",
    },
    267273: {
        "source_key": "k_east_francia",
        "target_text": "Fr\\u00e2ncia Oriental",
        "kind": "plain_text_title_accent_hotfix",
        "evidence": "Visual QA: Francia Oriental vs Fr\\u00e2ncia Ocidental",
    },
    158307: {
        "source_key": "trinket",
        "target_text": "Adorno",
        "kind": "plain_text_inventory_slot_category_hotfix",
        "evidence": "Inventory artifact slot rendered as Bugiganga; category should be Adorno",
    },
    158314: {
        "source_key": "artifact_slot_miscellaneous",
        "target_text": "Adorno",
        "kind": "plain_text_inventory_slot_category_hotfix",
        "evidence": "Inventory artifact slot rendered as Bugiganga; category should be Adorno",
    },
    158334: {
        "source_key": "artifact_trinket",
        "target_text": "Adorno",
        "kind": "plain_text_inventory_slot_category_hotfix",
        "evidence": "Inventory artifact trinket rendered as Badulaque/Bugiganga family; category should be Adorno",
    },
}

INVENTORY_REVIEW_KEYS = [
    "trinket",
    "artifact_slot_miscellaneous",
    "artifact_trinket",
    "miscellaneous",
    "miscellaneous_when_not_court",
    "trinket_1",
    "trinket_2",
    "trinket_3",
    "trinket_4",
    "artifact_miscellaneous",
]


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


def fetch_rows_for_ids(segment_ids: list[int]) -> dict[int, dict[str, Any]]:
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


def fetch_inventory_review_rows() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = []
        for key in INVENTORY_REVIEW_KEYS:
            row = conn.execute(
                """
                SELECT
                  s.id AS segment_id,
                  s.relative_path,
                  s.source_line_number,
                  s.source_key,
                  s.english_text,
                  s.spanish_text,
                  o.output_line_number,
                  o.portuguese_text AS output_text,
                  o.output_raw_line,
                  c.confirmed_text,
                  c.confirmation_level,
                  c.locked
                FROM source_segments s
                JOIN output_segments o ON o.segment_id = s.id
                LEFT JOIN segment_confirmations c ON c.segment_id = s.id
                WHERE s.relative_path = 'inventory/inventory_l_spanish.yml'
                  AND s.source_key = ?
                """,
                (key,),
            ).fetchone()
            if row:
                rows.append(dict(row))
    return rows


def validate_record(row: dict[str, Any], target: str, kind: str) -> dict[str, Any]:
    current_text = str(row["output_text"] or "")
    current_raw = str(row["output_raw_line"] or "")
    target_raw = replace_quoted_text(current_raw, target)
    path = output_root() / str(row["relative_path"])
    reasons: list[str] = []
    if not path.exists():
        reasons.append("missing_output_file")
        disk_line = ""
    else:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        idx = int(row["output_line_number"]) - 1
        disk_line = lines[idx] if 0 <= idx < len(lines) else ""
        if disk_line != current_raw:
            reasons.append("disk_line_mismatch_output_segments")
    if str(row["confirmed_text"] or "") != current_text:
        reasons.append("confirmed_text_not_current_output")
    if "\n" in target or "\r" in target:
        reasons.append("structure_integrity_mismatch")
    if target == current_text:
        reasons.append("no_output_change")
    old_tokens = protected_tokens(current_text)
    new_tokens = protected_tokens(target)
    token_ok = False
    if kind == "authorized_ES_helper_to_Select_CString_gender_delta":
        token_ok = bool(old_tokens and len(new_tokens) == 1 and new_tokens[0].startswith("[Select_CString( CHARACTER.IsFemale,"))
    elif kind.startswith("plain_text"):
        token_ok = old_tokens == new_tokens == []
    if not token_ok:
        reasons.append("token_integrity_failed")
    return {
        "current_tokens": old_tokens,
        "target_tokens": new_tokens,
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": "\n" not in target and "\r" not in target,
        "canonical_l10n_ok": target != current_text,
        "current_raw_line": current_raw,
        "target_raw_line": target_raw,
        "disk_line": disk_line,
        "status": "ready" if not reasons else "blocked",
        "block_reasons": reasons,
    }


def build_hotfix_records(segment_ids: list[int]) -> list[dict[str, Any]]:
    rows = fetch_rows_for_ids(segment_ids)
    records: list[dict[str, Any]] = []
    for segment_id in sorted(segment_ids):
        spec = HOTFIX_TARGETS[segment_id]
        row = rows.get(segment_id)
        if not row:
            records.append(
                {
                    "record_type": "qa_visual_hotfix_apply_item",
                    "segment_id": segment_id,
                    "source_key": spec["source_key"],
                    "target_text": spec["target_text"],
                    "status": "blocked",
                    "block_reasons": ["missing_segment"],
                }
            )
            continue
        target = str(spec["target_text"])
        validation = validate_record(row, target, str(spec["kind"]))
        if row["source_key"] != spec["source_key"]:
            validation["status"] = "blocked"
            validation["block_reasons"].append("source_key_mismatch")
        records.append(
            {
                "record_type": "qa_visual_hotfix_apply_item",
                "source": SOURCE,
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "output_line_number": row["output_line_number"],
                "source_key": row["source_key"],
                "english_text": row["english_text"],
                "spanish_text": row["spanish_text"],
                "old_text": row["old_text"],
                "current_output_text": row["output_text"],
                "current_confirmed_text": row["confirmed_text"],
                "confirmation_level": row["confirmation_level"],
                "locked": row["locked"],
                "target_text": target,
                "target_hash": stable_hash(target),
                "hotfix_kind": spec["kind"],
                "evidence": spec["evidence"],
                "classification": "hotfix_ready" if validation["status"] == "ready" else "blocked",
                **validation,
            }
        )
    return records


def build_inventory_package() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in fetch_inventory_review_rows():
        segment_id = int(row["segment_id"])
        spec = HOTFIX_TARGETS.get(segment_id)
        if spec:
            target = str(spec["target_text"])
            validation = validate_record(row, target, str(spec["kind"]))
            classification = "hotfix_ready" if validation["status"] == "ready" else "blocked"
        else:
            target = None
            validation = {
                "current_tokens": protected_tokens(str(row["output_text"] or "")),
                "target_tokens": [],
                "token_integrity_ok": True,
                "structure_integrity_ok": True,
                "canonical_l10n_ok": False,
                "current_raw_line": row["output_raw_line"],
                "target_raw_line": None,
                "status": "hold",
                "block_reasons": [],
            }
            classification = "hold"
        records.append(
            {
                "record_type": "inventory_trinket_visual_hotfix_review",
                "segment_id": segment_id,
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "output_line_number": row["output_line_number"],
                "source_key": row["source_key"],
                "english_text": row["english_text"],
                "spanish_text": row["spanish_text"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
                "confirmation_level": row["confirmation_level"],
                "locked": row["locked"],
                "classification": classification,
                "proposal": target,
                "classification_reason": (
                    "categoria visivel de slot/equipamento de artefato; Adorno e mais adequado que Bugiganga/Badulaque"
                    if spec
                    else "chave pass-through ou derivada via $trinket$; nao alterar diretamente"
                ),
                **validation,
            }
        )
    return records


def apply_records(records: list[dict[str, Any]]) -> tuple[int, int, str]:
    ready = [r for r in records if r.get("status") == "ready"]
    backup_root = db.project_path("memory/backups") / f"qa_visual_hotfix_batch_{stamp()}"
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in ready:
        by_path[str(record["relative_path"])].append(record)
    files_touched = 0
    timestamp = now_iso()
    with connect() as conn:
        for relative_path, path_records in sorted(by_path.items()):
            output_path = output_root() / relative_path
            backup_path = backup_root / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            for record in path_records:
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
    return len(ready), files_touched, str(backup_root)


def post_validate(records: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [r for r in records if r.get("status") == "ready"]
    details = []
    file_ok = output_db_ok = confirmation_ok = token_ok = structure_ok = 0
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
            f_ok = line == record["target_raw_line"]
            o_ok = bool(output and output["portuguese_text"] == record["target_text"] and output["output_raw_line"] == record["target_raw_line"])
            c_ok = bool(
                conf
                and conf["confirmed_text"] == record["target_text"]
                and conf["confirmation_source"] == CONFIRMATION_SOURCE
                and conf["confirmation_label"] == CONFIRMATION_LABEL
                and int(conf["locked"] or 0) == 1
            )
            t_ok = bool(record["token_integrity_ok"])
            s_ok = bool(record["structure_integrity_ok"])
            file_ok += int(f_ok)
            output_db_ok += int(o_ok)
            confirmation_ok += int(c_ok)
            token_ok += int(t_ok)
            structure_ok += int(s_ok)
            details.append(
                {
                    "segment_id": int(record["segment_id"]),
                    "file_ok": f_ok,
                    "output_db_ok": o_ok,
                    "confirmation_ok": c_ok,
                    "token_integrity_ok": t_ok,
                    "structure_integrity_ok": s_ok,
                }
            )
    return {
        "file_ok": file_ok,
        "output_db_ok": output_db_ok,
        "confirmation_ok": confirmation_ok,
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": structure_ok,
        "details": details,
    }


def ingest_learning(records: list[dict[str, Any]]) -> int:
    ready = [r for r in records if r.get("status") == "ready"]
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
            (len(ready), len(ready), len(ready), "qa visual hotfix batch post596", timestamp, timestamp, timestamp),
        )
        run_id = int(cur.lastrowid)
        for record in ready:
            reasons = [
                "qa_visual_hotfix",
                "human_confirmed",
                str(record["hotfix_kind"]),
                "protected_apply_post_validated",
                "no_source_write",
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
                    "human-approved QA visual hotfix",
                    "user_human_review_qa_visual_hotfix",
                    timestamp,
                    json.dumps(reasons, ensure_ascii=True),
                    timestamp,
                    timestamp,
                    "qa-visual-hotfix",
                    "qa_visual_hotfix_post596",
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
        f"record_count: `{summary['record_count']}`",
        f"ready_count: `{summary.get('ready_count', 0)}`",
        f"blocked_count: `{summary.get('blocked_count', 0)}`",
        "",
    ]
    for record in records:
        lines.append(f"## {record.get('source_key')} ({record.get('segment_id')})")
        lines.append(f"- classification/status: `{record.get('classification', record.get('status'))}`")
        lines.append(f"- file: `{record.get('relative_path')}:{record.get('output_line_number')}`")
        lines.append(f"- current: `{record.get('current_output_text', record.get('output_text'))}`")
        lines.append(f"- proposal: `{record.get('target_text', record.get('proposal'))}`")
        if record.get("target_raw_line"):
            lines.extend(["- diff preview:", "```diff", f"- {record.get('current_raw_line')}", f"+ {record.get('target_raw_line')}", "```"])
        if record.get("block_reasons"):
            lines.append(f"- block_reasons: `{record.get('block_reasons')}`")
        lines.append("")
    lines.extend(
        [
            "## Guards",
            "- apply=0 unless this is the protected apply report",
            "- discovery_amplo=0",
            "- reindex=0",
            "- producao_full=0",
            "- source_changed=false",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, jsonl_path, summary_path


def inventory_package() -> None:
    records = build_inventory_package()
    counts = Counter(r["classification"] for r in records)
    hotfix_ready = [r for r in records if r["classification"] == "hotfix_ready"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_inventory_trinket_visual_hotfix_package",
        "record_count": len(records),
        "classification_counts": dict(counts),
        "hotfix_ready_count": len(hotfix_ready),
        "hotfix_ready_ids": [int(r["segment_id"]) for r in hotfix_ready],
        "token_integrity_ok_count": sum(1 for r in hotfix_ready if r.get("token_integrity_ok")),
        "structure_integrity_ok_count": sum(1 for r in hotfix_ready if r.get("structure_integrity_ok")),
        "canonical_l10n_ok_count": sum(1 for r in hotfix_ready if r.get("canonical_l10n_ok")),
        "guards": {
            "apply_count": 0,
            "ingest_count": 0,
            "issue_closure_count": 0,
            "lifecycle_count": 0,
            "materializer_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
            "source_changed": False,
            "output_changed": False,
        },
        "recommendation": "include hotfix_ready inventory ids in protected QA visual hotfix apply; keep pass-through keys unchanged",
    }
    md, jsonl, summary_path = write_report("inventory_trinket_visual_hotfix_package_readonly", records, summary)
    print(json.dumps({"markdown": str(md), "jsonl": str(jsonl), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


def apply_batch() -> None:
    segment_ids = sorted(HOTFIX_TARGETS)
    records = build_hotfix_records(segment_ids)
    blocked = [r for r in records if r.get("status") != "ready"]
    if blocked:
        mode = "blocked"
        applied = files_touched = 0
        backup_root = None
        post = {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "token_integrity_ok": 0, "structure_integrity_ok": 0, "details": []}
        learning_run_id = None
    else:
        mode = "apply_learning"
        applied, files_touched, backup_root = apply_records(records)
        post = post_validate(records)
        expected = len(records)
        if not all(post.get(k) == expected for k in ["file_ok", "output_db_ok", "confirmation_ok", "token_integrity_ok", "structure_integrity_ok"]):
            raise SystemExit("post-validation guard failed; refusing learning ingest")
        learning_run_id = ingest_learning(records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len([r for r in records if r.get("status") == "ready"]),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(Counter(reason for r in blocked for reason in r.get("block_reasons", [])).most_common()),
        "applied_count": applied,
        "apply_count": applied,
        "files_touched_count": files_touched,
        "backup_root": backup_root,
        "rollback_path": backup_root or "not_created_blocked",
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
    md, jsonl, summary_path = write_report("qa_visual_hotfix_batch_apply_learning", records, summary)
    print(json.dumps({"markdown": str(md), "jsonl": str(jsonl), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["inventory-package", "apply-batch"])
    args = parser.parse_args()
    if args.mode == "inventory-package":
        inventory_package()
    elif args.mode == "apply-batch":
        apply_batch()


if __name__ == "__main__":
    main()
