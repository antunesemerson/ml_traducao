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


RULE_VERSION = "issue_gender_select_cstring_literal_repair_dry_run_v1"
DIAGNOSTIC_RULE_VERSION = "issue_gender_dynamic_token_delegate_diagnostic_v1"
SOURCE_SUBPATTERN = "select_cstring_spanish_literal_repair"
REPAIR_STATUS_READY = "ready_select_cstring_literal_payload_repair"

SELECT_CSTRING_NAME_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
SINGLE_QUOTED_LITERAL_RE = re.compile(r"'[^']*'")

EXACT_LITERAL_REPAIRS: tuple[tuple[str, str, str], ...] = (
    ("'auténtica heroína'", "'autêntica heroína'", "autentica_heroina"),
    ("'auténtico héroe'", "'autêntico herói'", "autentico_heroe"),
    ("'Amiga mía'", "'Minha boa amiga'", "amiga_mia"),
    ("'Amigo mío'", "'Meu bom amigo'", "amigo_mio"),
    ("'Esta guerrera orgullosa'", "'Esta guerreira orgulhosa'", "guerrera_orgullosa"),
    ("'Este guerrero orgulloso'", "'Este guerreiro orgulhoso'", "guerrero_orgulloso"),
    ("'la guerrera solitaria'", "'a guerreira solitária'", "guerrera_solitaria"),
    ("'el guerrero solitario'", "'o guerreiro solitário'", "guerrero_solitario"),
    ("'otra gorrona'", "'outra aproveitadora'", "otra_gorrona"),
    ("'otro gorrón'", "'outro aproveitador'", "otro_gorron"),
    ("'a típica bębada'", "'a típica bêbada'", "bebada_mojibake"),
    ("'o típico bębado'", "'o típico bêbado'", "bebado_mojibake"),
    ("'de la buena'", "'da boa'", "de_la_buena"),
    ("'del buen'", "'do bom'", "del_buen"),
    ("'la misma'", "'a mesma'", "la_misma"),
    ("'el mismo'", "'o mesmo'", "el_mismo"),
    ("'serás'", "'será'", "seras"),
    ("'confiscarás'", "'confiscará'", "confiscaras"),
    ("'perderás'", "'perderá'", "perderas"),
    ("'tus'", "'suas'", "tus_terras"),
    ("'sus'", "'suas'", "sus_terras"),
    ("'te convertesses'", "'se convertesse'", "te_convertesses"),
    ("'teu'", "'seu'", "teu"),
    ("'La nińa'", "'A menina'", "la_nina_mojibake"),
    ("'El nińo'", "'O menino'", "el_nino_mojibake"),
    ("'La niña'", "'A menina'", "la_nina"),
    ("'El niño'", "'O menino'", "el_nino"),
    ("'estás'", "'está'", "estas"),
    ("'encontraste'", "'encontrou'", "encontraste"),
    ("'encontró'", "'encontrou'", "encontro"),
    ("'deberías'", "'deveria'", "deberias"),
    ("'debería'", "'deveria'", "deberia"),
    ("'fueras'", "'fosse'", "fueras"),
    ("'fuera'", "'fosse'", "fuera"),
    ("'te labraste'", "'construiu'", "te_labraste"),
    ("'se labró'", "'construiu'", "se_labro"),
    ("'fuiste'", "'foi'", "fuiste"),
    ("'fue'", "'foi'", "fue"),
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_select_cstring_literal_repair_dry_run_diag_{diagnostic_run_id}"
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_select_cstring_literal_repair_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_select_cstring_literal_repair_items (
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
            repair_keys TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            confirmation_id INTEGER,
            confirmation_locked INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_select_cstring_literal_repair_runs(id) ON DELETE CASCADE
        )
        """
    )


def mask_select_cstring_literals(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in SELECT_CSTRING_NAME_RE.finditer(text):
        depth = 0
        start = match.start()
        index = match.end() - 1
        end = None
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
            index += 1
        if end is None:
            continue
        parts.append(text[last_end:start])
        call = text[start:end]
        parts.append(SINGLE_QUOTED_LITERAL_RE.sub("'<lit>'", call))
        last_end = end
    parts.append(text[last_end:])
    return "".join(parts)


def propose_repair(text: str) -> tuple[str, list[str]]:
    corrected = text
    repair_keys: list[str] = []
    for old, new, key in EXACT_LITERAL_REPAIRS:
        if old in corrected:
            corrected = corrected.replace(old, new)
            repair_keys.append(key)
    return corrected, repair_keys


def select_payload_changes_only(current: str, corrected: str) -> bool:
    if current == corrected:
        return False
    return mask_select_cstring_literals(current) == mask_select_cstring_literals(corrected)


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("confirmed_text") or row.get("output_text") or ""
    corrected, repair_keys = propose_repair(current)
    reasons: list[str] = []

    if not current:
        reasons.append("missing_current_text")
    if not repair_keys:
        reasons.append("no_exact_literal_mapping")
    if corrected == current:
        reasons.append("no_text_delta")
    if repair_keys and not select_payload_changes_only(current, corrected):
        reasons.append("change_not_confined_to_select_cstring_literal_payload")
    if "?" in corrected and "?" not in current:
        reasons.append("lossy_question_marker_after_repair")

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
        "replacement_count": len(repair_keys),
        "repair_keys": ",".join(repair_keys),
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
        "repair_keys",
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
    repair_keys = Counter()
    for row in rows:
        for key in (row["repair_keys"] or "").split(","):
            if key:
                repair_keys[key] += 1

    lines = [
        "Issue gender Select_CString literal repair dry-run",
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
        "Top repair keys:",
        *[f"- {key}: {value:,}" for key, value in repair_keys.most_common(30)],
        "",
        "Ready samples:",
    ]
    for row in [item for item in rows if item["repair_status"] == REPAIR_STATUS_READY][:25]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  repairs: {row['repair_keys']}",
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
            "- This records partial literal-payload repair knowledge; segment closure needs a later coordinator/lifecycle gate.",
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
            INSERT INTO ml_issue_gender_select_cstring_literal_repair_runs (
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
                INSERT INTO ml_issue_gender_select_cstring_literal_repair_items (
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
                    repair_keys,
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
                    row["repair_keys"],
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

    print(f"Gender Select_CString literal repair dry-run: {run_id}")
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
    parser = argparse.ArgumentParser(description="Dry-run Select_CString literal payload micro-repair candidates.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id)
