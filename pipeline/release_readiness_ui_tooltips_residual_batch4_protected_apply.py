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
from apply_safe_output_updates import protected_tokens, replace_quoted_text


SOURCE = "release_readiness_ui_tooltips_residual_batch4_protected_apply_v1"
INPUT_JSONL = Path("reports/20260702_164517_122874_release_readiness_ui_tooltips_residual_no_issue_diagnostic.jsonl")
CONFIRMATION_SOURCE = "codex_release_readiness_human_review"
CONFIRMATION_LABEL_CORRECTED = "ui_tooltips_residual_batch4_corrected"
CONFIRMATION_LABEL_APPROVED = "ui_tooltips_residual_batch4_approved_already_ok"
EXPECTED_REVIEWED = 13
EXPECTED_CORRECTED = 3


DECISIONS: dict[int, dict[str, str]] = {
    4184: {"human_decision": "approve_already_ok", "corrected_text": ""},
    35050: {"human_decision": "approve_already_ok", "corrected_text": ""},
    35493: {"human_decision": "approve_already_ok", "corrected_text": ""},
    38526: {"human_decision": "approve_already_ok", "corrected_text": ""},
    46892: {
        "human_decision": "corrected_text",
        "corrected_text": "Habilita eventos para recrutar [wanderers|lE] em troca de [gold_i][gold|lE], se você tiver cargos vagos para [officers|lE]",
    },
    49017: {"human_decision": "approve_already_ok", "corrected_text": ""},
    49487: {"human_decision": "approve_already_ok", "corrected_text": ""},
    49928: {"human_decision": "approve_already_ok", "corrected_text": ""},
    53322: {
        "human_decision": "corrected_text",
        "corrected_text": "Desbloqueia o [casus_belli|lE] #high $ep3_pillaging_foray$#!, usado para assediar [realms|lE] vizinhos",
    },
    58495: {
        "human_decision": "corrected_text",
        "corrected_text": "Unir $k_england$, $k_denmark$ e $k_norway$ em um [de_jure|lE] [realm|lE]",
    },
    62803: {"human_decision": "approve_already_ok", "corrected_text": ""},
    62843: {"human_decision": "approve_already_ok", "corrected_text": ""},
    74518: {"human_decision": "approve_already_ok", "corrected_text": ""},
}


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


def backup_root() -> Path:
    path = db.project_path("memory/backups") / f"release_readiness_ui_tooltips_residual_batch4_{stamp()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for UI/tooltips residual batch 4.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_live_rows(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_key,
          o.output_line_number,
          o.portuguese_text AS output_text,
          o.output_raw_line,
          c.confirmed_text,
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


def label_for(decision: str) -> str:
    if decision == "corrected_text":
        return CONFIRMATION_LABEL_CORRECTED
    return CONFIRMATION_LABEL_APPROVED


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    input_rows = {int(row["segment_id"]): row for row in read_jsonl(args.input_jsonl)}
    if set(DECISIONS) - set(input_rows):
        raise SystemExit(f"decision ids missing from input: {sorted(set(DECISIONS) - set(input_rows))}")
    if len(DECISIONS) != EXPECTED_REVIEWED:
        raise SystemExit("reviewed count guard failed")
    if sum(1 for decision in DECISIONS.values() if decision["human_decision"] == "corrected_text") != EXPECTED_CORRECTED:
        raise SystemExit("corrected count guard failed")

    with connect() as conn:
        live = fetch_live_rows(conn, sorted(DECISIONS))

    root = output_root()
    records: list[dict[str, Any]] = []
    for segment_id in sorted(DECISIONS):
        decision = DECISIONS[segment_id]
        source_row = input_rows[segment_id]
        live_row = live.get(segment_id) or {}
        reasons: list[str] = []
        relative_path = str(live_row.get("relative_path") or source_row.get("relative_path"))
        output_path = root / relative_path
        output_line_number = int(live_row.get("output_line_number") or 0)
        output_text = str(live_row.get("output_text") or "")
        target_text = decision["corrected_text"] if decision["human_decision"] == "corrected_text" else output_text
        current_raw_line = str(live_row.get("output_raw_line") or "")
        new_raw_line = replace_quoted_text(current_raw_line, target_text) if current_raw_line else ""
        disk_line = ""
        if not live_row:
            reasons.append("missing_live_row")
        if source_row.get("recommended_disposition") != "needs_human_confirmation":
            reasons.append("input_not_needs_human_confirmation")
        if source_row.get("confirmation_level") != "auto_confirmed":
            reasons.append("input_not_auto_confirmed")
        if int(source_row.get("issue_count") or 0) != 0 or int(source_row.get("high_issue_count") or 0) != 0:
            reasons.append("input_has_open_issue")
        if int(source_row.get("needs_output_apply") or 0) != 0:
            reasons.append("input_needs_output_apply")
        if int(source_row.get("confirmed_matches_output") or 0) != 1:
            reasons.append("input_confirmed_matches_output_not_1")
        if not output_path.exists():
            reasons.append("missing_output_file")
        elif output_line_number <= 0:
            reasons.append("missing_output_line_number")
        else:
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            index = output_line_number - 1
            if index < 0 or index >= len(lines):
                reasons.append("line_out_of_range")
            else:
                disk_line = lines[index]
                if disk_line != current_raw_line:
                    reasons.append("disk_line_mismatch_output_segments")
        if decision["human_decision"] == "corrected_text" and protected_tokens(output_text) != protected_tokens(target_text):
            reasons.append("token_integrity_mismatch")
        if "\n" in target_text or "\r" in target_text:
            reasons.append("unexpected_multiline_target")
        if decision["human_decision"] not in {"corrected_text", "approve_already_ok"}:
            reasons.append("unsupported_human_decision")
        label_after = label_for(decision["human_decision"])
        records.append(
            {
                "source": SOURCE,
                "record_type": "release_readiness_ui_tooltips_residual_batch4_decision",
                "segment_id": segment_id,
                "relative_path": relative_path,
                "source_key": live_row.get("source_key") or source_row.get("source_key"),
                "output_line_number": output_line_number,
                "human_decision": decision["human_decision"],
                "old_output_text": output_text,
                "old_confirmed_text": live_row.get("confirmed_text"),
                "target_text": target_text,
                "current_raw_line": current_raw_line,
                "new_raw_line": new_raw_line,
                "disk_line": disk_line,
                "token_integrity_ok": protected_tokens(output_text) == protected_tokens(target_text),
                "structure_integrity_ok": "\n" not in target_text and "\r" not in target_text,
                "requires_output_write": decision["human_decision"] == "corrected_text" and output_text != target_text,
                "requires_confirmation_update": (
                    (live_row.get("confirmed_text") or "") != target_text
                    or live_row.get("confirmation_source") != CONFIRMATION_SOURCE
                    or live_row.get("confirmation_label") != label_after
                    or int(live_row.get("locked") or 0) != 1
                ),
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "confirmation_source_after": CONFIRMATION_SOURCE,
                "confirmation_label_after": label_after,
                "target_hash": stable_hash(target_text),
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def apply_records(records: list[dict[str, Any]], backup: Path) -> tuple[int, int, int]:
    root = output_root()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["requires_output_write"]:
            by_path[str(record["relative_path"])].append(record)
    files_touched = 0
    output_applied = 0
    confirmation_updated = 0
    timestamp = now_iso()
    with connect() as conn:
        for relative_path, path_records in sorted(by_path.items()):
            output_path = root / relative_path
            backup_path = backup / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            for record in path_records:
                index = int(record["output_line_number"]) - 1
                if lines[index] != record["current_raw_line"]:
                    raise SystemExit(f"disk line changed during apply for segment {record['segment_id']}")
                lines[index] = str(record["new_raw_line"])
                conn.execute(
                    """
                    UPDATE output_segments
                    SET portuguese_text = ?, output_raw_line = ?, portuguese_hash = ?, last_indexed_at = ?
                    WHERE segment_id = ?
                    """,
                    (record["target_text"], record["new_raw_line"], record["target_hash"], timestamp, int(record["segment_id"])),
                )
                output_applied += 1
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched += 1
        for record in records:
            if not record["requires_confirmation_update"]:
                continue
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
                    record["confirmation_source_after"],
                    record["confirmation_label_after"],
                    timestamp,
                    timestamp,
                    int(record["segment_id"]),
                ),
            )
            confirmation_updated += 1
        conn.commit()
    return output_applied, files_touched, confirmation_updated


def post_validate(records: list[dict[str, Any]]) -> dict[str, Any]:
    root = output_root()
    file_ok = 0
    output_db_ok = 0
    confirmation_ok = 0
    details: list[dict[str, Any]] = []
    with connect() as conn:
        for record in records:
            segment_id = int(record["segment_id"])
            disk_matches = True
            if record["requires_output_write"]:
                lines = (root / record["relative_path"]).read_text(encoding="utf-8-sig").splitlines()
                disk_matches = lines[int(record["output_line_number"]) - 1] == record["new_raw_line"]
            output_row = conn.execute(
                "SELECT portuguese_text, portuguese_hash FROM output_segments WHERE segment_id=?",
                (segment_id,),
            ).fetchone()
            conf_row = conn.execute(
                "SELECT confirmed_text, confirmation_source, confirmation_label, locked FROM segment_confirmations WHERE segment_id=?",
                (segment_id,),
            ).fetchone()
            output_matches = bool(
                output_row
                and output_row["portuguese_text"] == record["target_text"]
                and output_row["portuguese_hash"] == record["target_hash"]
            )
            conf_matches = bool(
                conf_row
                and conf_row["confirmed_text"] == record["target_text"]
                and conf_row["confirmation_source"] == record["confirmation_source_after"]
                and conf_row["confirmation_label"] == record["confirmation_label_after"]
                and int(conf_row["locked"] or 0) == 1
            )
            file_ok += int(disk_matches)
            output_db_ok += int(output_matches)
            confirmation_ok += int(conf_matches)
            details.append(
                {
                    "segment_id": segment_id,
                    "disk_matches": disk_matches,
                    "output_db_matches": output_matches,
                    "confirmation_matches": conf_matches,
                }
            )
    return {"file_ok": file_ok, "output_db_ok": output_db_ok, "confirmation_ok": confirmation_ok, "details": details}


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_residual_batch4_protected_apply_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips residual batch4 protected apply",
                f"mode={summary['mode']}",
                f"reviewed_count={summary['reviewed_count']}",
                f"corrected_count={summary['corrected_count']}",
                f"approved_already_ok_count={summary['approved_already_ok_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"output_applied_count={summary['output_applied_count']}",
                f"confirmation_updated_count={summary['confirmation_updated_count']}",
                f"files_touched_count={summary['files_touched_count']}",
                f"backup_root={summary['backup_root']}",
                f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                f"apply_count={summary['output_applied_count']}",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    records = build_records(args)
    blocked = [record for record in records if record["status"] != "ready"]
    if blocked:
        mode = "blocked"
    output_applied = files_touched = confirmation_updated = 0
    backup = None
    if args.apply and not blocked:
        backup = backup_root()
        output_applied, files_touched, confirmation_updated = apply_records(records, backup)
    post_validation = (
        post_validate(records)
        if args.apply and not blocked
        else {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "details": []}
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "input_jsonl": str(args.input_jsonl),
        "reviewed_count": len(records),
        "corrected_count": sum(1 for record in records if record["human_decision"] == "corrected_text"),
        "approved_already_ok_count": sum(1 for record in records if record["human_decision"] == "approve_already_ok"),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "output_applied_count": output_applied,
        "confirmation_updated_count": confirmation_updated,
        "files_touched_count": files_touched,
        "backup_root": str(backup) if backup else None,
        "rollback_path": str(backup) if backup else "not_created_dry_run_or_blocked",
        "post_validation": post_validation,
        "candidate_generation_count": 0,
        "apply_count": output_applied,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(output_applied),
        "production_full_recommended_now": False,
    }
    txt_path, jsonl_path, summary_path = write_reports(summary, records)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={summary['mode']}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"corrected_count={summary['corrected_count']}")
    print(f"approved_already_ok_count={summary['approved_already_ok_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"output_applied_count={summary['output_applied_count']}")
    print(f"confirmation_updated_count={summary['confirmation_updated_count']}")
    print("candidate_generation_count=0")
    print(f"apply_count={summary['apply_count']}")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
