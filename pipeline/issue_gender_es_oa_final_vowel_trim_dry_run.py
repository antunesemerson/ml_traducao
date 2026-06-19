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


RULE_VERSION = "issue_gender_es_oa_final_vowel_trim_dry_run_v1"
DIAGNOSTIC_RULE_VERSION = "issue_gender_dynamic_token_delegate_diagnostic_v1"
SOURCE_SUBPATTERN = "stem_ending_o_or_a_before_es_oa_repair"
REPAIR_STATUS_READY = "ready_partial_repair"

ES_OA_SURFACE_RE = re.compile(
    r"(?P<surface>[A-Za-zÀ-ÿ]{3,})(?P<space>\s*)"
    r"(?P<token>\[[^\]]*\.Custom\(\s*['\"]ES_OA['\"]\s*\)[^\]]*\])",
    re.IGNORECASE,
)
INVARIANT_SURFACES = {
    "ciclope",
    "diplomata",
    "eremita",
    "livre",
    "jovem",
    "nobre",
    "pobre",
    "prudente",
    "inteligente",
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_es_oa_final_vowel_trim_dry_run_diag_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_dynamic_delegate_diagnostic_runs
        WHERE rule_version = ?
          AND candidate_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (DIAGNOSTIC_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No gender dynamic delegate diagnostic run found.")
    return int(row["id"])


def fetch_candidates(conn, diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            d.id AS diagnostic_item_id,
            d.run_id AS diagnostic_run_id,
            d.segment_id,
            d.relative_path,
            d.source_key,
            d.source_line_number,
            d.queue_run_id,
            d.ledger_run_id,
            d.ledger_item_id,
            d.subpattern,
            d.next_action,
            d.evidence_count,
            c.id AS confirmation_id,
            c.confirmed_text,
            c.locked AS confirmation_locked,
            o.portuguese_text AS output_text
        FROM ml_issue_gender_dynamic_delegate_diagnostic_items d
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = d.segment_id
              ORDER BY c2.updated_at DESC, c2.confirmed_at DESC, c2.id DESC
              LIMIT 1
          )
        LEFT JOIN output_segments o
          ON o.segment_id = d.segment_id
        WHERE d.run_id = ?
          AND d.subpattern = ?
        ORDER BY d.relative_path, d.source_line_number, d.source_key, d.segment_id
        """,
        (diagnostic_run_id, SOURCE_SUBPATTERN),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_es_oa_final_vowel_trim_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_es_oa_final_vowel_trim_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            queue_run_id INTEGER NOT NULL DEFAULT 0,
            ledger_run_id INTEGER NOT NULL DEFAULT 0,
            ledger_item_id INTEGER NOT NULL DEFAULT 0,
            repair_status TEXT NOT NULL,
            block_reason TEXT,
            replacement_count INTEGER NOT NULL DEFAULT 0,
            replaced_surfaces TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            confirmation_id INTEGER,
            confirmation_locked INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_es_oa_final_vowel_trim_runs(id) ON DELETE CASCADE
        )
        """
    )


def trim_surface(surface: str) -> str | None:
    low = surface.casefold()
    if low in INVARIANT_SURFACES:
        return None
    if low.endswith(("ario", "aria")) and not low.endswith(("ário", "ária")):
        return None
    if low[-1:] not in {"a", "o"}:
        return None
    return surface[:-1]


def propose_repair(text: str) -> tuple[str, list[str], list[str]]:
    surfaces: list[str] = []
    blockers: list[str] = []

    def replace(match: re.Match[str]) -> str:
        surface = match.group("surface")
        trimmed = trim_surface(surface)
        if trimmed is None:
            low = surface.casefold()
            if low in INVARIANT_SURFACES:
                blockers.append(f"invariant_surface:{surface}")
            elif low.endswith(("ario", "aria")) and not low.endswith(("ário", "ária")):
                blockers.append(f"unaccented_ario_surface:{surface}")
            return match.group(0)
        surfaces.append(surface)
        return f"{trimmed}{match.group('space')}{match.group('token')}"

    corrected = ES_OA_SURFACE_RE.sub(replace, text)
    return corrected, surfaces, blockers


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("confirmed_text") or row.get("output_text") or ""
    reasons: list[str] = []
    if not current:
        reasons.append("missing_current_text")
        corrected = ""
        surfaces: list[str] = []
        blockers: list[str] = []
    else:
        corrected, surfaces, blockers = propose_repair(current)
        reasons.extend(blockers)

    if not surfaces:
        reasons.append("no_safe_final_vowel_surface")
    if corrected == current:
        reasons.append("no_text_delta")
    if structural_tokens(current) != structural_tokens(corrected):
        reasons.append("structural_tokens_changed")

    status = REPAIR_STATUS_READY if not reasons else "blocked"
    return {
        "diagnostic_item_id": int(row["diagnostic_item_id"]),
        "diagnostic_run_id": int(row["diagnostic_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "source_line_number": int(row.get("source_line_number") or 0),
        "queue_run_id": int(row.get("queue_run_id") or 0),
        "ledger_run_id": int(row.get("ledger_run_id") or 0),
        "ledger_item_id": int(row.get("ledger_item_id") or 0),
        "repair_status": status,
        "block_reason": ";".join(reasons),
        "replacement_count": len(surfaces),
        "replaced_surfaces": ",".join(surfaces),
        "current_text": current,
        "corrected_text": corrected if corrected != current else "",
        "confirmation_id": row.get("confirmation_id"),
        "confirmation_locked": int(row.get("confirmation_locked") or 0),
        "evidence_count": int(row.get("evidence_count") or 1),
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    diagnostic_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "repair_status",
        "block_reason",
        "replacement_count",
        "replaced_surfaces",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "current_text",
        "corrected_text",
        "evidence_count",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["repair_status"] for row in rows)
    blockers = Counter(row["block_reason"] or "none" for row in rows)
    surfaces = Counter()
    for row in rows:
        for surface in (row["replaced_surfaces"] or "").split(","):
            if surface:
                surfaces[surface] += 1

    lines = [
        "Issue gender ES_OA final-vowel trim dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready partial repair: {counts[REPAIR_STATUS_READY]:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "By blocker:",
        *[f"- {key}: {value:,}" for key, value in blockers.most_common()],
        "",
        "Top replaced surfaces:",
        *[f"- {key}: {value:,}" for key, value in surfaces.most_common(20)],
        "",
        "Ready samples:",
    ]
    for row in [item for item in rows if item["repair_status"] == REPAIR_STATUS_READY][:30]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
            ]
        )
    blocked = [item for item in rows if item["repair_status"] != REPAIR_STATUS_READY]
    lines.extend(["", "Blocked samples:"])
    if blocked:
        for row in blocked[:30]:
            lines.append(
                f"- {row['block_reason']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Dry-run only: no confirmations, no output/source writes, no production run.",
            "- This is a partial micro-repair. Segment closure must be decided by a later coordinator/lifecycle gate.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        source_rows = fetch_candidates(conn, selected_diagnostic_run_id)
        rows = [evaluate(row) for row in source_rows]
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_diagnostic_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        counts = Counter(row["repair_status"] for row in rows)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_es_oa_final_vowel_trim_runs (
                rule_version,
                diagnostic_run_id,
                candidate_count,
                ready_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_diagnostic_run_id,
                len(rows),
                int(counts[REPAIR_STATUS_READY]),
                int(counts["blocked"]),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_gender_es_oa_final_vowel_trim_items (
                    run_id,
                    diagnostic_item_id,
                    diagnostic_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_run_id,
                    ledger_run_id,
                    ledger_item_id,
                    repair_status,
                    block_reason,
                    replacement_count,
                    replaced_surfaces,
                    current_text,
                    corrected_text,
                    confirmation_id,
                    confirmation_locked,
                    evidence_count,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["diagnostic_item_id"],
                    row["diagnostic_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_run_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["repair_status"],
                    row["block_reason"],
                    row["replacement_count"],
                    row["replaced_surfaces"],
                    row["current_text"],
                    row["corrected_text"],
                    row["confirmation_id"],
                    row["confirmation_locked"],
                    row["evidence_count"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            diagnostic_run_id=selected_diagnostic_run_id,
            rows=rows,
        )
        conn.commit()

    print(f"Gender ES_OA final-vowel trim dry-run: {run_id}")
    print(f"Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Ready: {counts[REPAIR_STATUS_READY]}")
    print(f"Blocked: {counts['blocked']}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "candidate_count": len(rows),
        "ready_count": int(counts[REPAIR_STATUS_READY]),
        "blocked_count": int(counts["blocked"]),
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run ES_OA final-vowel trim micro-repair candidates.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id)
