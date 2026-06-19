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
from apply_safe_output_updates import protected_tokens, replace_quoted_text
from ptbr_orthographic_microfix_dry_run import (
    RULE_VERSION as DRY_RUN_RULE_VERSION,
    classify,
    fetch_candidates,
    report_paths,
)


RULE_VERSION = "ptbr_orthographic_microfix_apply_v1"
EXPECTED_READY = 18
EXPECTED_RETAINED = 12
EXPECTED_BLOCKED = 2
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "ptbr_orthographic_microfix_production"
CONFIRMATION_LABEL = "ptbr_orthographic_microfix:phase_b_ready18"
REVIEWER = "production_ptbr_orthographic_microfix_apply"

READY_ALLOWLIST: dict[int, str] = {
    28545: "minor_decisions.0003.desc.outro",
    106589: "host_dinner_events.3070.desc",
    109951: "steward_task.3101.desc",
    140249: "game_concept_councillor_task_desc",
    140540: "game_concept_dynast_interaction_desc",
    140762: "game_concept_fort_level_desc",
    141539: "game_concept_diplomatic_range_desc",
    142006: "game_concept_hybrid_culture_desc_DLC",
    146729: "COMBAT_WINDOW_PHASE_MANEUVER_TT",
    147317: "INNOVATION_NOT_IN_REGION",
    150240: "MV_EMBARKED_TT",
    154571: "action_should_pay_ransom_combined_group_description",
    235164: "friend_impressed_by_justice",
    235165: "friend_impressed_by_justice_corresponding",
    235450: "rival_age_old_rivalry",
    235472: "rival_better_understanding",
    235612: "nemesis_house_feud",
    286388: "reactive_advice_fabricate_claim_desc",
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def short(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


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


def file_raw_line(output_root: Path, row: dict[str, Any]) -> str:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}")
    return lines[line_index]


def validate_live_counts(classified: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter(row["category"] for row in classified)
    stale_count = sum(1 for row in classified if "stale_" in as_text(row.get("reasons")))
    counts["stale"] = stale_count
    if counts["ready_exact_microfix"] != EXPECTED_READY:
        raise RuntimeError(f"Expected ready={EXPECTED_READY}, got {counts['ready_exact_microfix']}.")
    if counts["candidate_multi_issue_requires_review"] != EXPECTED_RETAINED:
        raise RuntimeError(
            "Expected retained/review="
            f"{EXPECTED_RETAINED}, got {counts['candidate_multi_issue_requires_review']}."
        )
    if counts["blocked_structural_or_token_risk"] != EXPECTED_BLOCKED:
        raise RuntimeError(f"Expected blocked={EXPECTED_BLOCKED}, got {counts['blocked_structural_or_token_risk']}.")
    ready_ids = {int(row["segment_id"]) for row in classified if row["category"] == "ready_exact_microfix"}
    expected_ids = set(READY_ALLOWLIST)
    if ready_ids != expected_ids:
        raise RuntimeError(
            "Ready allowlist mismatch: "
            f"missing={sorted(expected_ids - ready_ids)} extra={sorted(ready_ids - expected_ids)}"
        )
    for row in classified:
        segment_id = int(row["segment_id"])
        if row["category"] == "ready_exact_microfix":
            if row["source_key"] != READY_ALLOWLIST[segment_id]:
                raise RuntimeError(f"Ready source_key mismatch for segment {segment_id}: {row['source_key']}")
            if not as_text(row["corrected_text"]).strip() or row["corrected_text"] == row["current_text"]:
                raise RuntimeError(f"Ready row has empty/no-op correction: {segment_id}")
            if protected_tokens(row["current_text"]) != protected_tokens(row["corrected_text"]):
                raise RuntimeError(f"Ready row changes protected tokens: {segment_id}")
    return counts


def load_live_rows(conn, ready_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    ids = [int(row["segment_id"]) for row in ready_rows]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.is_active,
            o.output_line_number,
            o.portuguese_text AS output_text
        FROM source_segments s
        JOIN output_segments o
          ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    live = {int(row["segment_id"]): dict(row) for row in rows}
    if set(live) != set(ids):
        raise RuntimeError(f"Missing live rows: {sorted(set(ids) - set(live))}")
    return live


def upsert_confirmation(conn, *, segment_id: int, corrected: str, now: str) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == corrected
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
            confirmation_level = excluded.confirmation_level,
            confirmed_text = excluded.confirmed_text,
            confirmation_source = excluded.confirmation_source,
            confirmation_label = excluded.confirmation_label,
            locked = excluded.locked,
            confidence_score = excluded.confidence_score,
            reviewer = excluded.reviewer,
            confirmed_at = COALESCE(segment_confirmations.confirmed_at, excluded.confirmed_at),
            updated_at = excluded.updated_at
        """,
        (segment_id, CONFIRMATION_LEVEL, corrected, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not bool(already)


def apply_ready_rows(conn, *, output_root: Path, ready_rows: list[dict[str, Any]], now: str) -> tuple[int, int, int]:
    live_rows = load_live_rows(conn, ready_rows)
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ready_rows:
        segment_id = int(row["segment_id"])
        live = live_rows[segment_id]
        if live["relative_path"] != row["relative_path"] or live["source_key"] != row["source_key"]:
            raise RuntimeError(f"Live metadata drift for segment {segment_id}")
        if as_text(live["output_text"]) != row["current_text"]:
            raise RuntimeError(f"Live DB output drift for segment {segment_id}")
        row["_output_line_number"] = live["output_line_number"]
        by_file[row["relative_path"]].append(row)

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, rows in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row in sorted(rows, key=lambda item: int(item["_output_line_number"])):
            line_index = int(row["_output_line_number"]) - 1
            raw_line = lines[line_index]
            first_quote = raw_line.find('"')
            last_quote = raw_line.rfind('"')
            if first_quote < 0 or last_quote <= first_quote:
                raise RuntimeError(f"Line without quoted value for segment {row['segment_id']}")
            file_text = raw_line[first_quote + 1 : last_quote].replace('\\"', '"')
            if file_text != row["current_text"]:
                raise RuntimeError(f"Live file output drift for segment {row['segment_id']}")
            new_line = replace_quoted_text(raw_line, row["corrected_text"])
            lines[line_index] = new_line
            segment_id = int(row["segment_id"])
            if upsert_confirmation(conn, segment_id=segment_id, corrected=row["corrected_text"], now=now):
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
                (row["corrected_text"], new_line, sha256_text(row["corrected_text"]), now, segment_id),
            )
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return confirmation_promoted, output_written, files_touched


def write_reports(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    counts: Counter,
    apply: bool,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings)
    stem = txt_path.stem.replace("_dry_run", "_apply" if apply else "_apply_dry_run")
    txt_path = txt_path.with_name(stem + ".txt")
    csv_path = csv_path.with_name(stem + ".csv")
    jsonl_path = jsonl_path.with_name(stem + ".jsonl")
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "output_line_number",
        "category",
        "replacements",
        "replacement_count",
        "reasons",
        "current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    ready = [row for row in rows if row["category"] == "ready_exact_microfix"]
    retained = [row for row in rows if row["category"] != "ready_exact_microfix"]
    lines = [
        "PT-BR orthographic microfix protected apply" if apply else "PT-BR orthographic microfix protected apply dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Dry-run rule version: {DRY_RUN_RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Mode: {'apply' if apply else 'dry_run'}",
        "",
        "Summary:",
        f"- candidates: {len(rows)}",
        f"- ready: {counts['ready_exact_microfix']}",
        f"- retained_review: {counts['candidate_multi_issue_requires_review']}",
        f"- blocked: {counts['blocked_structural_or_token_risk']}",
        f"- stale: {counts['stale']}",
        f"- applied: {counts['applied']}",
        f"- confirmation_promoted: {counts['confirmation_promoted']}",
        f"- output_written: {counts['output_written']}",
        f"- files_touched: {counts['files_touched']}",
        f"- TXT: {txt_path}",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        "",
        "Applied/ready allowlist:",
    ]
    for row in ready:
        lines.append(f"- {row['segment_id']} | {row['relative_path']}:{row['output_line_number']} | {row['source_key']}")
    lines.extend(["", "Retained or blocked, not applied:"])
    for row in retained:
        lines.append(f"- {row['segment_id']} | {row['category']} | {row['source_key']} | {row['reasons']}")
    lines.extend(["", "Samples:"])
    for row in ready[:20]:
        lines.extend(
            [
                f"- segment={row['segment_id']} | {row['source_key']}",
                f"  before: {short(row['current_text'])}",
                f"  after:  {short(row['corrected_text'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This script applies only the 18 allowlisted ready_exact_microfix rows.",
            "- It refuses partial apply if live totals drift.",
            "- It does not reindex sources or run production.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    with db.connect(settings) as conn:
        source_rows = fetch_candidates(conn)
        classified = [classify(row, output_root) for row in source_rows]
        counts = validate_live_counts(classified)
        ready_rows = [row for row in classified if row["category"] == "ready_exact_microfix"]
        if apply:
            now = datetime.now().isoformat(timespec="seconds")
            promoted, written, touched = apply_ready_rows(conn, output_root=output_root, ready_rows=ready_rows, now=now)
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

    txt_path, csv_path, jsonl_path = write_reports(settings, rows=classified, counts=counts, apply=apply)
    print("[ptbr_orthographic_microfix_apply] Done")
    print(f"[ptbr_orthographic_microfix_apply] Rule version: {RULE_VERSION}")
    print(f"[ptbr_orthographic_microfix_apply] Mode: {'apply' if apply else 'dry_run'}")
    print(f"[ptbr_orthographic_microfix_apply] Candidates: {len(classified)}")
    print(f"[ptbr_orthographic_microfix_apply] Ready: {counts['ready_exact_microfix']}")
    print(f"[ptbr_orthographic_microfix_apply] Retained/review: {counts['candidate_multi_issue_requires_review']}")
    print(f"[ptbr_orthographic_microfix_apply] Blocked: {counts['blocked_structural_or_token_risk']}")
    print(f"[ptbr_orthographic_microfix_apply] Stale: {counts['stale']}")
    print(f"[ptbr_orthographic_microfix_apply] Applied: {counts['applied']}")
    print(f"[ptbr_orthographic_microfix_apply] Confirmation promoted: {counts['confirmation_promoted']}")
    print(f"[ptbr_orthographic_microfix_apply] Output written: {counts['output_written']}")
    print(f"[ptbr_orthographic_microfix_apply] Files touched: {counts['files_touched']}")
    print(f"[ptbr_orthographic_microfix_apply] TXT: {txt_path}")
    print(f"[ptbr_orthographic_microfix_apply] CSV: {csv_path}")
    print(f"[ptbr_orthographic_microfix_apply] JSONL: {jsonl_path}")
    return {
        "apply": apply,
        "counts": dict(counts),
        "txt_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Protected apply for PT-BR orthographic microfix ready rows.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(apply=args.apply)
