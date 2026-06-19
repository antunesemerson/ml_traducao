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
from apply_segment_state_updates import short


RULE_VERSION = "religion_divine_realm_confirmation_repair_v1"
FIX_LABELS = {"minor_fix", "major_fix", "semantic_error", "residual_spanish"}
STRUCTURAL_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[A-Za-z0-9_]+\$")


def structural_tokens(text: str | None) -> list[str]:
    return STRUCTURAL_TOKEN_RE.findall(text or "")


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_religion_divine_realm_confirmation_repair"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            sc.id AS confirmation_id,
            sc.confirmed_text,
            sc.confirmation_label,
            sc.confirmation_source,
            sc.locked,
            ll.id AS learning_candidate_id,
            ll.human_label,
            ll.corrected_text,
            ll.reason,
            ll.reviewer,
            ll.reviewed_at
        FROM source_segments s
        JOIN segment_confirmations sc
          ON sc.id = (
              SELECT sc2.id
              FROM segment_confirmations sc2
              WHERE sc2.segment_id = s.id
              ORDER BY sc2.updated_at DESC, sc2.id DESC
              LIMIT 1
          )
        JOIN local_learning_candidates ll
          ON ll.id = (
              SELECT ll2.id
              FROM local_learning_candidates ll2
              WHERE ll2.segment_id = s.id
                AND ll2.local_status = 'reviewed_human'
                AND ll2.human_label IN ('minor_fix', 'major_fix', 'semantic_error', 'residual_spanish')
                AND ll2.corrected_text IS NOT NULL
                AND trim(ll2.corrected_text) <> ''
              ORDER BY COALESCE(ll2.reviewed_at, ll2.updated_at, ll2.created_at) DESC, ll2.id DESC
              LIMIT 1
          )
        WHERE s.is_active = 1
          AND s.relative_path LIKE 'religion/%'
          AND s.source_key LIKE '%divine_realm%'
        ORDER BY s.relative_path, s.source_key
        """
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("confirmed_text") or ""
    corrected = (row.get("corrected_text") or "").strip()
    reasons: list[str] = []
    if int(row.get("locked") or 0) != 1:
        reasons.append("confirmation_not_locked")
    if row.get("human_label") not in FIX_LABELS:
        reasons.append("not_fix_label")
    if not corrected:
        reasons.append("missing_corrected_text")
    if "?" not in current:
        reasons.append("current_has_no_question_mojibake")
    if "?" in corrected:
        reasons.append("corrected_still_has_question_mojibake")
    if structural_tokens(current) != structural_tokens(corrected):
        reasons.append("structural_tokens_changed")
    if current == corrected:
        reasons.append("already_matches")

    status = "ready_repair" if not reasons else "blocked"
    return {
        **row,
        "repair_status": status,
        "repair_reasons": reasons,
        "current_tokens_json": json.dumps(structural_tokens(current), ensure_ascii=False),
        "corrected_tokens_json": json.dumps(structural_tokens(corrected), ensure_ascii=False),
    }


def apply_repairs(conn, rows: list[dict[str, Any]], *, timestamp: str) -> int:
    applied = 0
    for row in rows:
        if row["repair_status"] != "ready_repair":
            continue
        conn.execute(
            """
            UPDATE segment_confirmations
            SET
                confirmed_text = ?,
                confirmation_source = 'local_learning_repair',
                reviewer = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                row["corrected_text"],
                row.get("reviewer") or "codex_divine_realm_confirmation_repair",
                timestamp,
                row["confirmation_id"],
            ),
        )
        applied += 1
    return applied


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    rows: list[dict[str, Any]],
    apply: bool,
    applied: int,
    started_at: datetime,
) -> None:
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "confirmation_id",
        "confirmation_label",
        "confirmation_source",
        "locked",
        "learning_candidate_id",
        "human_label",
        "repair_status",
        "confirmed_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "repair_reasons": row["repair_reasons"],
                "current_tokens": json.loads(row["current_tokens_json"]),
                "corrected_tokens": json.loads(row["corrected_tokens_json"]),
                "reason": row.get("reason") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["repair_status"] for row in rows)
    lines = [
        "Religion divine-realm confirmation repair",
        f"Rule version: {RULE_VERSION}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Apply: {apply}",
        "",
        "Summary:",
        f"- Candidates inspected: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        f"- Applied repairs: {applied:,}",
        "",
        "Ready/block sample:",
    ]
    for row in rows[:20]:
        lines.extend(
            [
                f"- {row['repair_status']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  reasons={', '.join(row['repair_reasons']) or 'none'}",
                f"  current:   {short(row.get('confirmed_text'))}",
                f"  corrected: {short(row.get('corrected_text'))}",
            ]
        )
    if not rows:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This only repairs segment_confirmations from existing human corrected_text.",
            "- It does not write output files, change source files, or promote an ML model.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    timestamp = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = [classify(row) for row in fetch_rows(conn)]
        applied = apply_repairs(conn, rows, timestamp=timestamp) if apply else 0
        if apply:
            conn.commit()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        rows=rows,
        apply=apply,
        applied=applied,
        started_at=started_at,
    )
    status_counts = Counter(row["repair_status"] for row in rows)
    print("[religion_divine_realm_confirmation_repair] Audit generated")
    print(f"[religion_divine_realm_confirmation_repair] Apply: {apply}")
    print(f"[religion_divine_realm_confirmation_repair] Candidates inspected: {len(rows):,}")
    for key, value in status_counts.most_common():
        print(f"[religion_divine_realm_confirmation_repair] {key}: {value:,}")
    print(f"[religion_divine_realm_confirmation_repair] Applied repairs: {applied:,}")
    print(f"[religion_divine_realm_confirmation_repair] Report: {txt_path}")
    print(f"[religion_divine_realm_confirmation_repair] CSV: {csv_path}")
    print(f"[religion_divine_realm_confirmation_repair] JSONL: {jsonl_path}")
    return {"rows": len(rows), "applied": applied, "report_path": str(txt_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair divine_realm confirmations from existing human corrected_text.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(apply=args.apply)
