from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "title_landed_adjective_lexical_residue_repair_apply_v1"
DEFAULT_DRY_RUN_ID = 1
CONFIRMATION_LEVEL = "auto_confirmed"

PROFILES: dict[int, dict[str, Any]] = {
    1: {
        "expected_candidates": 21,
        "expected_ready": 21,
        "expected_blocked": 0,
        "expected_stale": 0,
        "expected_already_applied": 0,
        "decisions": {"ready_exact_lexical_map"},
        "confirmation_source": "title_landed_adjective_lexical_residue_repair_production",
        "confirmation_label": "title_landed_adjective_lexical_residue_exact_v1",
        "reviewer": "production_title_landed_adjective_lexical_residue_repair_apply",
    },
    3: {
        "expected_candidates": 14,
        "expected_ready": 14,
        "expected_blocked": 0,
        "expected_stale": 0,
        "expected_already_applied": 0,
        "decisions": {"ready_direction_spelling_only", "ready_direction_and_es_suffix_only"},
        "confirmation_source": "title_landed_adjective_lexical_residue_medium_repair_production",
        "confirmation_label": "title_landed_adjective_lexical_residue_direction_suffix_v1",
        "reviewer": "production_title_landed_adjective_lexical_residue_medium_repair_apply",
    },
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def canonical(value: str | None) -> str:
    return " ".join((value or "").split())


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def short(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def report_paths(settings: dict[str, Any], *, dry_run_id: int, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_title_landed_adjective_lexical_residue_repair_{mode}_dryrun_{dry_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


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


def quality_blockers(text: str | None) -> list[str]:
    validation = local_quality_validator.validate_text(text)
    blockers: list[str] = []
    for issue in validation.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "").lower()
        if severity in {"medium", "high", "error", "critical"}:
            blockers.append(str(issue.get("code") or "quality_issue"))
    return sorted(set(blockers))


def profile_for(dry_run_id: int) -> dict[str, Any]:
    profile = PROFILES.get(dry_run_id)
    if profile is None:
        raise RuntimeError(f"Unsupported dry-run id {dry_run_id}. Allowed ids: {sorted(PROFILES)}")
    return profile


def fetch_dry_run_scope(conn, dry_run_id: int, profile: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_title_landed_adjective_lexical_residue_repair_dry_runs
        WHERE id = ?
        LIMIT 1
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Dry-run id {dry_run_id} not found.")
    scope = set(json.loads(row["decision_scope_json"] or "[]"))
    expected_scope = set(profile["decisions"])
    if not expected_scope.issubset(scope):
        raise RuntimeError(f"Dry-run {dry_run_id} has unsupported decision scope: {scope!r}")
    return dict(row)


def fetch_dry_run_items(conn, dry_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            dry.id AS dry_item_id,
            dry.run_id AS dry_run_id,
            dry.diagnostic_run_id,
            dry.diagnostic_item_id,
            dry.segment_id,
            dry.relative_path,
            dry.source_key,
            dry.current_text,
            dry.proposed_text,
            dry.decision,
            dry.status AS dry_status,
            dry.block_reason AS dry_block_reason,
            output.output_line_number,
            output.portuguese_text AS live_output_text,
            source.is_active,
            source.relative_path AS live_relative_path,
            source.source_key AS live_source_key
        FROM ml_title_landed_adjective_lexical_residue_repair_dry_run_items dry
        JOIN source_segments source
          ON source.id = dry.segment_id
        JOIN output_segments output
          ON output.segment_id = dry.segment_id
        WHERE dry.run_id = ?
        ORDER BY dry.relative_path, output.output_line_number, dry.segment_id
        """,
        (dry_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def file_text_at(output_root: Path, row: dict[str, Any]) -> str:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"Line without quoted value for segment {row['segment_id']}")
    return raw_line[first_quote + 1 : last_quote].replace('\\"', '"')


def classify_live(conn, output_root: Path, row: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    current = as_text(row["current_text"])
    proposed = as_text(row["proposed_text"])
    confirmation = latest_confirmation(conn, segment_id)
    result = dict(row)
    result["status"] = "ready"
    result["block_reason"] = ""
    result["category"] = "ready"

    def block(reason: str, *, stale: bool = False) -> dict[str, Any]:
        result["status"] = "stale" if stale else "blocked"
        result["category"] = result["status"]
        result["block_reason"] = reason
        return result

    if row["dry_status"] != "ready":
        return block(f"dry_status_not_ready:{row['dry_status']}")
    if row["decision"] not in profile["decisions"]:
        return block(f"decision_not_allowed:{row['decision']}")
    if int(row.get("is_active") or 0) != 1:
        return block("source_not_active")
    if row["live_relative_path"] != row["relative_path"]:
        return block("relative_path_mismatch")
    if row["live_source_key"] != row["source_key"]:
        return block("source_key_mismatch")
    if not proposed or canonical(proposed) == canonical(current):
        return block("missing_or_noop_proposed_text")

    live_output = as_text(row["live_output_text"])
    if canonical(live_output) == canonical(proposed):
        if (
            confirmation
            and canonical(as_text(confirmation.get("confirmed_text"))) == canonical(proposed)
            and as_text(confirmation.get("confirmation_source")) == profile["confirmation_source"]
            and as_text(confirmation.get("confirmation_label")) == profile["confirmation_label"]
        ):
            result["status"] = "already_applied"
            result["category"] = "already_applied"
            return result
        return block("output_already_proposed_without_expected_confirmation", stale=True)
    if canonical(live_output) != canonical(current):
        return block("stale_output_text", stale=True)
    if confirmation is None:
        return block("missing_confirmation")
    if int(confirmation.get("locked") or 0) == 1:
        return block("locked_confirmation")
    if canonical(as_text(confirmation.get("confirmed_text"))) != canonical(current):
        return block("stale_confirmation_text", stale=True)
    try:
        file_text = file_text_at(output_root, row)
    except RuntimeError as exc:
        return block(str(exc))
    if canonical(file_text) != canonical(current):
        return block("stale_file_output_text", stale=True)
    if protected_tokens(current) != protected_tokens(proposed):
        return block("protected_token_signature_mismatch")
    quality = quality_blockers(proposed)
    if quality:
        return block("quality_block:" + ",".join(quality))
    return result


def validate_counts(rows: list[dict[str, Any]], profile: dict[str, Any]) -> Counter:
    counts: Counter = Counter(row["status"] for row in rows)
    stale_count = counts["stale"]
    if len(rows) != profile["expected_candidates"]:
        raise RuntimeError(f"Expected candidates={profile['expected_candidates']}, got {len(rows)}.")
    if counts["ready"] != profile["expected_ready"]:
        raise RuntimeError(f"Expected ready={profile['expected_ready']}, got {counts['ready']}.")
    if counts["blocked"] != profile["expected_blocked"]:
        raise RuntimeError(f"Expected blocked={profile['expected_blocked']}, got {counts['blocked']}.")
    if stale_count != profile["expected_stale"]:
        raise RuntimeError(f"Expected stale={profile['expected_stale']}, got {stale_count}.")
    if counts["already_applied"] != profile["expected_already_applied"]:
        raise RuntimeError(
            f"Expected already_applied={profile['expected_already_applied']}, got {counts['already_applied']}."
        )
    return counts


def upsert_confirmation(conn, *, segment_id: int, proposed: str, now: str, profile: dict[str, Any]) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        raise RuntimeError(f"Refusing locked confirmation for segment {segment_id}")
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == profile["confirmation_source"]
        and as_text(confirmation.get("confirmation_label")) == profile["confirmation_label"]
        and canonical(as_text(confirmation.get("confirmed_text"))) == canonical(proposed)
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
        (
            segment_id,
            CONFIRMATION_LEVEL,
            proposed,
            profile["confirmation_source"],
            profile["confirmation_label"],
            profile["reviewer"],
            now,
            now,
        ),
    )
    return not bool(already)


def apply_ready_rows(
    conn, *, output_root: Path, rows: list[dict[str, Any]], now: str, profile: dict[str, Any]
) -> tuple[int, int, int]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_file[as_text(row["relative_path"])].append(row)

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, file_rows in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row in sorted(file_rows, key=lambda item: int(item["output_line_number"])):
            segment_id = int(row["segment_id"])
            line_index = int(row["output_line_number"]) - 1
            raw_line = lines[line_index]
            file_text = file_text_at(output_root, row)
            if canonical(file_text) != canonical(as_text(row["current_text"])):
                raise RuntimeError(f"Live file output drift for segment {segment_id}")
            new_line = replace_quoted_text(raw_line, as_text(row["proposed_text"]))
            lines[line_index] = new_line
            if upsert_confirmation(
                conn, segment_id=segment_id, proposed=as_text(row["proposed_text"]), now=now, profile=profile
            ):
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
                (row["proposed_text"], new_line, sha256_text(as_text(row["proposed_text"])), now, segment_id),
            )
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return confirmation_promoted, output_written, files_touched


def write_reports(
    settings: dict[str, Any],
    *,
    dry_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter,
    apply: bool,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, dry_run_id=dry_run_id, apply=apply)
    fields = [
        "dry_run_id",
        "dry_item_id",
        "diagnostic_run_id",
        "diagnostic_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "output_line_number",
        "decision",
        "status",
        "block_reason",
        "current_text",
        "proposed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    ready = [row for row in rows if row["status"] == "ready"]
    blocked = [row for row in rows if row["status"] != "ready"]
    lines = [
        "Title landed adjective lexical residue repair protected apply"
        if apply
        else "Title landed adjective lexical residue repair protected apply dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Mode: {'apply' if apply else 'dry_run'}",
        f"Dry-run id: {dry_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(rows)}",
        f"- ready: {counts['ready']}",
        f"- stale: {counts['stale']}",
        f"- blocked: {counts['blocked']}",
        f"- already_applied: {counts['already_applied']}",
        f"- applied: {counts['applied']}",
        f"- confirmation_promoted: {counts['confirmation_promoted']}",
        f"- output_written: {counts['output_written']}",
        f"- files_touched: {counts['files_touched']}",
        f"- TXT: {txt_path}",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        "",
        "Ready/applied rows:",
    ]
    for row in ready:
        lines.extend(
            [
                f"- {row['segment_id']} | {row['relative_path']}:{row['output_line_number']} | {row['source_key']}",
                f"  before: {short(row['current_text'])}",
                f"  after:  {short(row['proposed_text'])}",
            ]
        )
    if blocked:
        lines.extend(["", "Blocked/stale/already applied rows:"])
        for row in blocked:
            lines.append(f"- {row['segment_id']} | {row['status']} | {row['block_reason']} | {row['source_key']}")
    lines.extend(
        [
            "",
            "Safety note:",
            f"- Applies only dry_run_id={dry_run_id} items with decisions={sorted(PROFILES[dry_run_id]['decisions'])}.",
            "- Does not apply candidate batches or unsupported dry-runs.",
            "- Does not reindex source files or run full production.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, dry_run_id: int = DEFAULT_DRY_RUN_ID, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    profile = profile_for(dry_run_id)
    with db.connect(settings) as conn:
        fetch_dry_run_scope(conn, dry_run_id, profile)
        dry_rows = fetch_dry_run_items(conn, dry_run_id)
        classified = [classify_live(conn, output_root, row, profile) for row in dry_rows]
        counts = validate_counts(classified, profile)
        if apply:
            now = datetime.now().isoformat(timespec="seconds")
            ready_rows = [row for row in classified if row["status"] == "ready"]
            promoted, written, touched = apply_ready_rows(
                conn, output_root=output_root, rows=ready_rows, now=now, profile=profile
            )
            counts["confirmation_promoted"] = promoted
            counts["output_written"] = written
            counts["applied"] = written
            counts["files_touched"] = touched
            conn.commit()
        else:
            counts["confirmation_promoted"] = 0
            counts["output_written"] = 0
            counts["applied"] = 0
            counts["files_touched"] = 0
            conn.rollback()

    txt_path, csv_path, jsonl_path = write_reports(settings, dry_run_id=dry_run_id, rows=classified, counts=counts, apply=apply)
    print("[title_landed_adjective_lexical_residue_repair_apply] Done")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Rule version: {RULE_VERSION}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Mode: {'apply' if apply else 'dry_run'}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Dry-run id: {dry_run_id}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Candidates: {len(classified)}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Ready: {counts['ready']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Stale: {counts['stale']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Blocked: {counts['blocked']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Already applied: {counts['already_applied']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Applied: {counts['applied']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Confirmation promoted: {counts['confirmation_promoted']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Output written: {counts['output_written']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] Files touched: {counts['files_touched']}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] TXT: {txt_path}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] CSV: {csv_path}")
    print(f"[title_landed_adjective_lexical_residue_repair_apply] JSONL: {jsonl_path}")
    return {
        "apply": apply,
        "dry_run_id": dry_run_id,
        "counts": dict(counts),
        "txt_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Protected apply for exact landed-title adjective lexical residue repairs.")
    parser.add_argument("--dry-run-id", type=int, default=DEFAULT_DRY_RUN_ID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(dry_run_id=args.dry_run_id, apply=args.apply)
