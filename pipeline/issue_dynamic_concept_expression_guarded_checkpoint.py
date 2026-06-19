from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_dynamic_concept_expression_guarded_checkpoint_v1"
CHECKPOINT_NAME = "dynamic_concept_expression_guarded_checkpoint_v1"
CHECKPOINT_ACTION = "checked_dynamic_concept_expression_guarded"
AGENT_KEY = "micro_dynamic_ck3_expression"
ISSUE_FAMILY = "dynamic_ck3_expression_microagent"
ISSUE_KIND = "concept_expression"

CONCEPT_CALL_RE = re.compile(r"Concept\s*\(", re.IGNORECASE)
SIMPLE_CONCEPT_RE = re.compile(
    r"Concept\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)",
    re.IGNORECASE,
)
BAD_VISIBLE_TERMS = {
    "acantonado",
    "acantonados",
    "aquella",
    "asaltar",
    "atrae",
    "atraen",
    "alianza",
    "caballero",
    "caballeros",
    "caballera",
    "caballeras",
    "cautiva",
    "cautivas",
    "cautivo",
    "cautivos",
    "compasivo",
    "conversión",
    "congénito",
    "congénitos",
    "vasallaje",
    "derecho",
    "derechos",
    "encarcelas",
    "encarcela",
    "encarcelar",
    "eficiencia",
    "enfoque",
    "escaramuzadores",
    "gano",
    "ganadora",
    "heredera",
    "hibridas",
    "impulsando",
    "impulsar",
    "independiente",
    "independientes",
    "involucrada",
    "involucradas",
    "involucrado",
    "involucrados",
    "involucrarte",
    "jugable",
    "muy",
    "mueres",
    "nombramiento",
    "nombramientos",
    "nómada",
    "nómadas",
    "rivalidad",
    "poseer",
    "tierra",
    "tierras",
    "tribal",
    "tribales",
    "torturar\u00e1s",
    "viajas",
    "viajarás",
    "vassalage",
    "vassals",
    "vassal",
    "county",
    "barony",
    "duchy",
    "kingdom",
    "empire",
    "opinion",
    "increase",
    "decrease",
    "lowered",
    "force",
}
BAD_VISIBLE_PHRASES = {
    "compartiendo poder",
    "comparto poder",
    "enfoque educativo",
    "familiares cercanos",
    "oferta de vasallaje",
    "opini\u00f3n del condado",
    "resultado do seu nerge",
}
BAD_TEXT_MARKERS = ("¿", "¡", "«", "»", "Â", "Ã", "�")


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def normalize_words(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def evidence_matches_current(*, evidence_text: str | None, current_text: str | None) -> bool:
    evidence = evidence_text or ""
    current = current_text or ""
    if not evidence or not current:
        return False
    if evidence == current:
        return True
    if evidence.endswith("..."):
        preview = evidence[:-3]
        return bool(preview) and current.startswith(preview)
    return False


def visible_term_block(value: str) -> str:
    lowered = normalize_words(value)
    if any(marker in value for marker in BAD_TEXT_MARKERS):
        return "visible_text_encoding_or_spanish_punctuation"
    phrase_hits = [phrase for phrase in sorted(BAD_VISIBLE_PHRASES) if phrase in lowered]
    if phrase_hits:
        return "visible_foreign_phrase:" + ",".join(phrase_hits[:4])
    hits = []
    for term in sorted(BAD_VISIBLE_TERMS):
        if re.search(rf"(?<![A-Za-zÀ-ÿ]){re.escape(term)}(?![A-Za-zÀ-ÿ])", lowered, re.IGNORECASE):
            hits.append(term)
    if hits:
        return "visible_foreign_term:" + ",".join(hits[:6])
    if re.search(r"\b\w+(ción|ciones|dad|dades)\b", lowered, re.IGNORECASE):
        return "visible_suspected_spanish_suffix"
    return ""


def classify_concept_payload(text: str) -> tuple[str, list[str]]:
    raw_count = len(CONCEPT_CALL_RE.findall(text))
    if raw_count <= 0:
        return "no_concept_call", []
    matches = list(SIMPLE_CONCEPT_RE.finditer(text))
    if len(matches) != raw_count:
        return "complex_or_nested_concept_payload", []

    displays = [match.group(2).strip() for match in matches]
    if any(not display for display in displays):
        return "empty_concept_display_text", displays
    for display in displays:
        block = visible_term_block(display)
        if block:
            return block, displays
        if any(marker in display for marker in ("[", "]", "Select_CString", "Get", "Custom", "$")):
            return "dynamic_marker_inside_concept_display", displays
    return "", displays


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_concept_expression_guarded_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_ledger_run_id(conn, *, ledger_run_id: int | None) -> int:
    if ledger_run_id is not None:
        return ledger_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished issue ledger run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_concept_expression_guarded_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            display_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_concept_expression_guarded_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL,
            block_reason TEXT,
            display_texts_json TEXT,
            current_confirmed_text_hash TEXT,
            evidence_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_dynamic_concept_expression_guarded_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_rows(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.needs_reopen,
            state.is_closed,
            state.locked AS state_locked,
            confirmation.confirmed_text AS current_confirmed_text,
            confirmation.locked AS confirmation_locked
        FROM ml_issue_ledger_items item
        LEFT JOIN segment_state_items state ON state.id = item.state_item_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = item.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.agent_key = ?
          AND item.issue_family = ?
          AND item.issue_kind = ?
          AND item.status = 'open'
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.id
        """,
        (ledger_run_id, AGENT_KEY, ISSUE_FAMILY, ISSUE_KIND),
    ).fetchall()
    return [dict(row) for row in rows]


def row_block_reason(row: dict[str, Any]) -> tuple[str, list[str]]:
    text = row.get("current_confirmed_text") or ""
    if not text:
        return "missing_current_confirmation", []
    if int(row.get("confirmation_locked") or 0) or int(row.get("state_locked") or 0):
        return "confirmation_locked", []
    if int(row.get("confirmed_matches_output") or 0) != 1:
        return "confirmation_not_aligned_with_output", []
    if int(row.get("needs_output_apply") or 0) != 0:
        return "needs_output_apply", []
    if int(row.get("is_closed") or 0) != 0:
        return "segment_already_closed", []
    if not evidence_matches_current(evidence_text=row.get("evidence_text"), current_text=text):
        return "evidence_text_mismatch", []
    text_block = visible_term_block(text)
    if text_block:
        return text_block, []
    concept_block, displays = classify_concept_payload(text)
    if concept_block:
        return concept_block, displays
    return "", displays


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    checkpoint_status: str,
) -> None:
    fields = [
        "checkpoint_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "subpolicy_name",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "display_texts_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fields}
            payload["confirmed_preview"] = short(row.get("current_confirmed_text"))
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    allowed_rows = [row for row in rows if row["checkpoint_allowed"]]
    blocked_rows = [row for row in rows if not row["checkpoint_allowed"]]
    blocker_counts = Counter(row["block_reason"] or "checkpoint_allowed" for row in rows)
    display_counts = Counter()
    for row in allowed_rows:
        for display in json.loads(row.get("display_texts_json") or "[]"):
            display_counts[display] += 1

    lines = [
        "Dynamic Concept Expression Guarded Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        "Production release allowed: 0",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {len(allowed_rows):,}",
        f"- Blocked: {len(blocked_rows):,}",
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Top allowed display texts:",
        *[f"- {key}: {value:,}" for key, value in display_counts.most_common(40)],
        "",
        "Allowed samples:",
    ]
    for row in allowed_rows[:25]:
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}::{row['source_key']} | "
            f"{short(row.get('current_confirmed_text'))}"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked_rows[:25]:
        lines.append(
            f"- {row['block_reason']} | segment={row['segment_id']} | "
            f"{row['relative_path']}::{row['source_key']} | {short(row.get('current_confirmed_text'))}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations, no production promotion.",
            "- It covers only simple Concept() dynamic structure with safe visible PT-BR display text.",
            "- Complex/nested Concept payloads and visible foreign terms remain open for specific neurons.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = latest_ledger_run_id(conn, ledger_run_id=ledger_run_id)
        rows = fetch_rows(conn, ledger_run_id=selected_ledger_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            block_reason, displays = row_block_reason(row)
            row["subpolicy_name"] = "dynamic_concept_simple_visible_ptbr"
            row["checkpoint_action"] = CHECKPOINT_ACTION
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            row["display_texts_json"] = json.dumps(displays, ensure_ascii=False, sort_keys=True)
            row["current_confirmed_text_hash"] = stable_hash(row.get("current_confirmed_text"))
            row["evidence_text_hash"] = stable_hash(row.get("evidence_text"))

        allowed_count = sum(1 for row in rows if row["checkpoint_allowed"])
        blocked_count = len(rows) - allowed_count
        checkpoint_status = "ready_for_partial_coverage" if allowed_count else "blocked_by_checkpoint_guard"
        blocker_counts = Counter(row["block_reason"] or "checkpoint_allowed" for row in rows)
        display_counts = Counter()
        for row in rows:
            if row["checkpoint_allowed"]:
                for display in json.loads(row["display_texts_json"]):
                    display_counts[display] += 1

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_dynamic_concept_expression_guarded_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                agent_key,
                issue_family,
                issue_kind,
                ledger_run_id,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                blocker_counts_json,
                display_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                AGENT_KEY,
                ISSUE_FAMILY,
                ISSUE_KIND,
                selected_ledger_run_id,
                len(rows),
                allowed_count,
                blocked_count,
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(display_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_dynamic_concept_expression_guarded_checkpoint_items (
                    checkpoint_run_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    issue_family,
                    issue_kind,
                    subpolicy_name,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    display_texts_json,
                    current_confirmed_text_hash,
                    evidence_text_hash,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_ledger_run_id,
                    row["id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row["display_texts_json"],
                    row["current_confirmed_text_hash"],
                    row["evidence_text_hash"],
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cursor.lastrowid)
            row["ledger_item_id"] = int(row["id"])
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        ledger_run_id=selected_ledger_run_id,
        rows=rows,
        checkpoint_status=checkpoint_status,
    )
    print("[issue_dynamic_concept_expression_guarded_checkpoint] Checkpoint generated")
    print(f"[issue_dynamic_concept_expression_guarded_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_dynamic_concept_expression_guarded_checkpoint] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_dynamic_concept_expression_guarded_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_dynamic_concept_expression_guarded_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_dynamic_concept_expression_guarded_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_dynamic_concept_expression_guarded_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "ledger_run_id": selected_ledger_run_id,
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint safe simple Concept() dynamic CK3 expressions for partial coverage.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id)
    "ley",
    "leyes",
    "marcial",
    "marciales",
    "meterse",
    "metido",
    "metidos",
