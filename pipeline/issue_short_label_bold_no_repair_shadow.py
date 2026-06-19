from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens
from issue_review_assisted_draft import english_hits, has_actual_mojibake, spanish_hits
from local_quality_validator import validate_text


RULE_VERSION = "issue_short_label_bold_no_repair_shadow_v1"
POLICY_NAME = "short_label_bold_no_ptbr_repair_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_short_label_bold_no_repair"
SOURCE_SUBLANE = "short_label_bold_no_repair"


def latest_sublane_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_repair_sublane_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label sublane diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], sublane_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_bold_no_repair_shadow_sublane_run_{sublane_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_bold_no_repair_shadow_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            source_sublane_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_shadow_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_bold_no_repair_shadow_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_sublane_run_id INTEGER NOT NULL,
            sublane_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            checkpoint_candidate INTEGER NOT NULL DEFAULT 0,
            shadow_status TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            validation_summary TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_bold_no_repair_shadow_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_bold_no_shadow_items_run
        ON ml_issue_short_label_bold_no_repair_shadow_items(run_id, shadow_status);

        CREATE INDEX IF NOT EXISTS idx_short_label_bold_no_shadow_items_ledger
        ON ml_issue_short_label_bold_no_repair_shadow_items(ledger_run_id, ledger_item_id);
        """
    )


def fetch_rows(conn, *, sublane_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_repair_sublane_diagnostic_items
        WHERE run_id = ?
          AND sublane = ?
        ORDER BY relative_path, source_line_number, source_key, id
        """,
        (sublane_run_id, SOURCE_SUBLANE),
    ).fetchall()
    return [dict(row) for row in rows]


def repair_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag")
        word = match.group("word")
        if word.isupper():
            replacement = "NÃO"
        elif word[:1].isupper():
            replacement = "Não"
        else:
            replacement = "não"
        return f"#{tag} {replacement}#!"

    return re.sub(r"#(?P<tag>bold|BOLD)\s+(?P<word>No|no|NO)#!", repl, text)


def normalize_for_negative_guard(text: str) -> str:
    lowered = text.lower()
    replacements = {
        "ã": "a",
        "á": "a",
        "à": "a",
        "â": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    return lowered


def has_duplicate_negative_after_repair(text: str) -> bool:
    normalized = normalize_for_negative_guard(text)
    return bool(
        re.search(
            r"#(?:bold)\s+nao#!\s+(?:voce\s+|eu\s+|ele\s+|ela\s+|eles\s+|elas\s+)?nao\b",
            normalized,
        )
    )


def validator_summary(text: str) -> str:
    result = validate_text(text)
    issues = result.get("issues") or []
    if not issues:
        return ""
    return ",".join(
        f"{issue.get('code') or 'quality_issue'}:{issue.get('severity') or 'unknown'}"
        for issue in issues[:5]
    )


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("evidence_text") or ""
    corrected = repair_text(current)
    block_reasons: list[str] = []
    if not current.strip():
        block_reasons.append("missing_current_text")
    if current == corrected:
        block_reasons.append("no_bold_no_delta")
    if has_duplicate_negative_after_repair(corrected):
        block_reasons.append("duplicate_negative_after_bold_no_repair")
    if has_actual_mojibake(corrected):
        block_reasons.append("mojibake_after_repair")
    if structural_tokens(current) != structural_tokens(corrected):
        block_reasons.append("structural_tokens_changed")

    remaining_spanish = [hit for hit in spanish_hits(corrected) if "#bold\\s+no#!" not in hit]
    remaining_english = english_hits(corrected)
    if remaining_spanish:
        block_reasons.append("spanish_residual_after_repair:" + ",".join(remaining_spanish[:4]))
    if remaining_english:
        block_reasons.append("english_residual_after_repair:" + ",".join(remaining_english[:4]))

    validation = validator_summary(corrected)
    if validation:
        block_reasons.append("local_validator:" + validation)

    checkpoint_candidate = 0 if block_reasons else 1
    return {
        "sublane_item_id": row["id"],
        "ledger_run_id": row["ledger_run_id"],
        "ledger_item_id": row["ledger_item_id"],
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "agent_key": AGENT_KEY,
        "checkpoint_candidate": checkpoint_candidate,
        "shadow_status": "ready_shadow" if checkpoint_candidate else "blocked",
        "block_reason": ";".join(block_reasons),
        "token_status": "same_structural_tokens" if structural_tokens(current) == structural_tokens(corrected) else "structural_tokens_changed",
        "current_text": current,
        "corrected_text": corrected if corrected != current else "",
        "validation_summary": validation,
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    sublane_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "shadow_status",
        "checkpoint_candidate",
        "block_reason",
        "token_status",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short Label Bold No Repair Shadow",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source sublane run id: {sublane_run_id}",
        f"Policy: {POLICY_NAME}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready shadow: {counts['ready_shadow']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Blockers:",
    ]
    for key, value in counts.most_common():
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Samples:"])
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- {row['shadow_status']} | segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']} | block={row['block_reason'] or 'none'}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220) if row['corrected_text'] else '<none>'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a shadow repair only.",
            "- It does not write source/output, create confirmations, or promote lifecycle policy.",
            "- ready_shadow means the repair preserves structural token shape and has no local residual blockers.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, sublane_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_sublane_run_id = sublane_run_id or latest_sublane_run_id(conn)
        source_rows = fetch_rows(conn, sublane_run_id=selected_sublane_run_id)
        classified = [classify(row) for row in source_rows]
        counts: Counter[str] = Counter(row["shadow_status"] for row in classified)
        for row in classified:
            if row["block_reason"]:
                for reason in row["block_reason"].split(";"):
                    counts[f"block:{reason}"] += 1

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_sublane_run_id)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_bold_no_repair_shadow_runs (
                rule_version,
                policy_name,
                policy_status,
                source_sublane_run_id,
                candidate_count,
                ready_shadow_count,
                blocked_count,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                selected_sublane_run_id,
                len(classified),
                counts["ready_shadow"],
                counts["blocked"],
                json.dumps(dict(counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_bold_no_repair_shadow_items (
                run_id,
                source_sublane_run_id,
                sublane_item_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                agent_key,
                checkpoint_candidate,
                shadow_status,
                block_reason,
                token_status,
                current_text,
                corrected_text,
                validation_summary,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    selected_sublane_run_id,
                    row["sublane_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["agent_key"],
                    row["checkpoint_candidate"],
                    row["shadow_status"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["validation_summary"],
                    now,
                )
                for row in classified
            ],
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        sublane_run_id=selected_sublane_run_id,
        rows=classified,
        counts=counts,
    )

    print("[issue_short_label_bold_no_repair_shadow] Shadow generated")
    print(f"[issue_short_label_bold_no_repair_shadow] Run id: {run_id}")
    print(f"[issue_short_label_bold_no_repair_shadow] Source sublane run id: {selected_sublane_run_id}")
    print(f"[issue_short_label_bold_no_repair_shadow] Candidates: {len(classified):,}")
    print(f"[issue_short_label_bold_no_repair_shadow] Ready shadow: {counts['ready_shadow']:,}")
    print(f"[issue_short_label_bold_no_repair_shadow] Blocked: {counts['blocked']:,}")
    print(f"[issue_short_label_bold_no_repair_shadow] Report: {txt_path}")
    print(f"[issue_short_label_bold_no_repair_shadow] CSV: {csv_path}")
    print(f"[issue_short_label_bold_no_repair_shadow] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "source_sublane_run_id": selected_sublane_run_id,
        "candidate_count": len(classified),
        "ready_shadow_count": counts["ready_shadow"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a shadow repair proposal for #bold No#! short-label residuals.")
    parser.add_argument("--sublane-run-id", type=int, default=None)
    args = parser.parse_args()
    main(sublane_run_id=args.sublane_run_id)
