from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "in_game_feedback_vassal_stances_claim_cb_microfix_apply_v1"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "in_game_feedback_vassal_stances_claim_cb_microfix_production"
CONFIRMATION_LABEL = "in_game_feedback_claim_cb_microfix"
REVIEWER = "production_in_game_feedback_microfix_apply"
LOCKED_OVERRIDE_SEGMENT_ID = 142275
LOCKED_OVERRIDE_CONFIRMATION_SOURCE = "in_game_feedback_manual_locked_override"
LOCKED_OVERRIDE_CONFIRMATION_LABEL = "vassal_stances_compact_ui_label"
LOCKED_OVERRIDE_REVIEWER = "production_in_game_feedback_locked_override_apply"

TARGETS: dict[int, dict[str, str]] = {
    142275: {
        "relative_path": "game_concepts_l_spanish.yml",
        "source_key": "game_concept_vassal_stances",
        "english_text": "Vassal Stances",
        "spanish_text": "Posturas vasallas",
        "current_text": "Posturas do Vassalo",
        "corrected_text": "Posturas",
    },
    287381: {
        "relative_path": "wars_l_spanish.yml",
        "source_key": "CLAIM_CB_NAME",
        "current_text": "[CLAIMANT.LocalPlayerString( 'Tus', 'Los' )] [claims|lE][CLAIMANT.LocalPlayerString( '', 'Loc_ES_de_GetShortUIName' )]",
        "corrected_text": "[CLAIMANT.LocalPlayerString( 'Suas', 'As' )] [claims|lE][CLAIMANT.LocalPlayerString( '', 'Loc_ES_de_GetShortUIName' )]",
    },
    287397: {
        "relative_path": "wars_l_spanish.yml",
        "source_key": "claim_cb",
        "current_text": "Claim",
        "corrected_text": "Reivindicação",
    },
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


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


def fetch_live_rows(conn) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGETS)
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
            o.portuguese_text AS output_text
        FROM source_segments s
        JOIN output_segments o
          ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        tuple(TARGETS),
    ).fetchall()
    live = {int(row["segment_id"]): dict(row) for row in rows}
    if set(live) != set(TARGETS):
        raise RuntimeError(f"Missing target rows: {sorted(set(TARGETS) - set(live))}")
    return live


def read_file_text(output_root: Path, row: dict[str, Any]) -> tuple[Path, list[str], int, str, str]:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"Line has no quoted localization value for segment {row['segment_id']}")
    return output_path, lines, line_index, raw_line, raw_line[first_quote + 1 : last_quote].replace('\\"', '"')


def evaluate(conn, output_root: Path) -> list[dict[str, Any]]:
    live_rows = fetch_live_rows(conn)
    results: list[dict[str, Any]] = []
    for segment_id, target in TARGETS.items():
        row = live_rows[segment_id]
        confirmation = latest_confirmation(conn, segment_id)
        current_text = target["current_text"]
        corrected_text = target["corrected_text"]
        reasons: list[str] = []
        status = "ready"

        if int(row["is_active"] or 0) != 1:
            reasons.append("source_not_active")
        if as_text(row["relative_path"]) != target["relative_path"]:
            reasons.append("relative_path_mismatch")
        if as_text(row["source_key"]) != target["source_key"]:
            reasons.append("source_key_mismatch")
        if confirmation is None:
            reasons.append("missing_confirmation")
        elif int(confirmation.get("locked") or 0) == 1:
            if segment_id == LOCKED_OVERRIDE_SEGMENT_ID:
                if as_text(row.get("english_text")) != target.get("english_text", ""):
                    reasons.append("locked_override_english_text_mismatch")
                if as_text(row.get("spanish_text")) != target.get("spanish_text", ""):
                    reasons.append("locked_override_spanish_text_mismatch")
            else:
                reasons.append("locked_confirmation")

        output_text = as_text(row["output_text"])
        confirmed_text = as_text(confirmation.get("confirmed_text")) if confirmation else ""
        locked_override_allowed = (
            segment_id == LOCKED_OVERRIDE_SEGMENT_ID
            and confirmation is not None
            and int(confirmation.get("locked") or 0) == 1
        )
        if output_text == corrected_text and confirmed_text == corrected_text:
            status = "already_applied"
        elif locked_override_allowed and output_text == corrected_text and confirmed_text == current_text:
            status = "ready_locked_override"
        else:
            if output_text != current_text:
                reasons.append("db_output_text_unexpected")
            if confirmed_text != current_text:
                reasons.append("confirmation_text_unexpected")

        if structural_tokens(current_text) != structural_tokens(corrected_text):
            reasons.append("structural_tokens_changed")

        try:
            output_path, lines, line_index, raw_line, file_text = read_file_text(output_root, row)
        except RuntimeError as exc:
            output_path = None
            lines = []
            line_index = -1
            raw_line = ""
            file_text = ""
            reasons.append(str(exc))
        else:
            if status == "already_applied" or (locked_override_allowed and file_text == corrected_text):
                if file_text != corrected_text:
                    reasons.append("file_output_text_unexpected")
            elif file_text != current_text:
                reasons.append("file_output_text_unexpected")

        if reasons:
            status = "blocked"
        elif (
            status != "already_applied"
            and segment_id == LOCKED_OVERRIDE_SEGMENT_ID
            and confirmation
            and int(confirmation.get("locked") or 0) == 1
        ):
            status = "ready_locked_override"
        results.append(
            {
                "segment_id": segment_id,
                "relative_path": target["relative_path"],
                "source_key": target["source_key"],
                "current_text": current_text,
                "corrected_text": corrected_text,
                "status": status,
                "reasons": reasons,
                "_row": row,
                "_output_path": output_path,
                "_lines": lines,
                "_line_index": line_index,
                "_raw_line": raw_line,
            }
        )
    return results


def upsert_confirmation(conn, *, item: dict[str, Any], now: str) -> bool:
    segment_id = int(item["segment_id"])
    confirmation = latest_confirmation(conn, segment_id)
    locked_override = segment_id == LOCKED_OVERRIDE_SEGMENT_ID
    if confirmation and int(confirmation.get("locked") or 0) == 1 and not locked_override:
        raise RuntimeError(f"Refusing locked confirmation for segment {segment_id}")
    source = LOCKED_OVERRIDE_CONFIRMATION_SOURCE if locked_override else CONFIRMATION_SOURCE
    label = LOCKED_OVERRIDE_CONFIRMATION_LABEL if locked_override else CONFIRMATION_LABEL
    reviewer = LOCKED_OVERRIDE_REVIEWER if locked_override else REVIEWER
    locked = 1 if locked_override else 0
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == source
        and as_text(confirmation.get("confirmation_label")) == label
        and as_text(confirmation.get("confirmed_text")) == item["corrected_text"]
        and int(confirmation.get("locked") or 0) == locked
    )
    if locked_override:
        result = conn.execute(
            """
            UPDATE segment_confirmations
            SET confirmation_level = ?,
                confirmed_text = ?,
                confirmation_source = ?,
                confirmation_label = ?,
                locked = 1,
                confidence_score = 1.0,
                reviewer = ?,
                confirmed_at = COALESCE(confirmed_at, ?),
                updated_at = ?
            WHERE segment_id = ?
              AND locked = 1
            """,
            (
                CONFIRMATION_LEVEL,
                item["corrected_text"],
                source,
                label,
                reviewer,
                now,
                now,
                segment_id,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Locked override confirmation update failed for segment {segment_id}")
        return not bool(already)
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
        (
            segment_id,
            CONFIRMATION_LEVEL,
            item["corrected_text"],
            source,
            label,
            reviewer,
            now,
            now,
        ),
    )
    return not bool(already)


def write_report(settings: dict[str, Any], *, apply: bool, results: list[dict[str, Any]], summary: Counter) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    mode = "apply" if apply else "dry_run"
    path = reports_dir / f"{now_stamp()}_in_game_feedback_vassal_stances_claim_cb_microfix_{mode}.txt"
    lines = [
        "In-game feedback vassal stances + claim CB microfix",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        "",
        "Counts:",
        f"- candidates: {len(results)}",
        f"- ready: {summary['ready']}",
        f"- ready_locked_override: {summary['ready_locked_override']}",
        f"- stale: {summary['stale']}",
        f"- blocked: {summary['blocked']}",
        f"- already_applied: {summary['already_applied']}",
        f"- applied: {summary['applied']}",
        f"- confirmation_promoted: {summary['confirmation_promoted']}",
        f"- output_written: {summary['output_written']}",
        f"- files_touched: {summary['files_touched']}",
        "",
        "Items:",
    ]
    for item in results:
        lines.append(
            f"- {item['segment_id']} | {item['relative_path']}::{item['source_key']} | "
            f"status={item['status']} | blocks={', '.join(item['reasons']) if item['reasons'] else 'none'}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(*, apply: bool) -> tuple[Counter, Path]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    conn = db.connect(settings)
    applied = confirmation_promoted = output_written = files_touched = 0
    try:
        results = evaluate(conn, output_root)
        counts = Counter(item["status"] for item in results)
        counts["stale"] = counts["stale"]
        if counts["blocked"]:
            conn.rollback()
            if apply:
                raise RuntimeError("Apply aborted because blocked rows are present")
        elif apply:
            ready = [item for item in results if item["status"] in {"ready", "ready_locked_override"}]
            by_file: dict[Path, list[dict[str, Any]]] = defaultdict(list)
            for item in ready:
                by_file[item["_output_path"]].append(item)
            now = datetime.now().isoformat(timespec="seconds")
            for output_path, items in sorted(by_file.items(), key=lambda pair: str(pair[0])):
                lines = items[0]["_lines"]
                for item in sorted(items, key=lambda row: int(row["_line_index"])):
                    line_index = int(item["_line_index"])
                    lines[line_index] = replace_quoted_text(item["_raw_line"], item["corrected_text"])
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
                            item["corrected_text"],
                            lines[line_index],
                            sha256_text(item["corrected_text"]),
                            now,
                            int(item["segment_id"]),
                        ),
                    )
                    output_written += 1
                output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
                files_touched += 1
            for item in ready:
                if upsert_confirmation(conn, item=item, now=now):
                    confirmation_promoted += 1
            conn.commit()
            applied = len(ready)
            results = evaluate(conn, output_root)
            counts = Counter(item["status"] for item in results)
        else:
            conn.rollback()
        counts["applied"] = applied
        counts["confirmation_promoted"] = confirmation_promoted
        counts["output_written"] = output_written
        counts["files_touched"] = files_touched
        report = write_report(settings, apply=apply, results=results, summary=counts)
        return counts, report
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        counts, report = run(apply=args.apply)
    except Exception as exc:
        print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] ERROR: {exc}")
        raise SystemExit(1)
    print("[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Completed")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Apply: {args.apply}")
    print("[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Candidates: 3")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Ready: {counts['ready']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Ready locked override: {counts['ready_locked_override']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Stale: {counts['stale']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Blocked: {counts['blocked']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Already applied: {counts['already_applied']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Applied: {counts['applied']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Confirmation promoted: {counts['confirmation_promoted']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Output written: {counts['output_written']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Files touched: {counts['files_touched']}")
    print(f"[in_game_feedback_vassal_stances_claim_cb_microfix_apply] Report: {report}")


if __name__ == "__main__":
    main()
