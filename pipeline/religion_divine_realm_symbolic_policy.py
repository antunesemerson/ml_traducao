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


RULE_VERSION = "religion_divine_realm_symbolic_policy_v1"
ALIAS_RE = re.compile(r"^\$[A-Za-z0-9_]+\$$")
POSITIVE_LABELS = {"correct", "contextual_exception"}
FIX_LABELS = {
    "major_fix",
    "minor_fix",
    "rejected",
    "rejected_suggestion",
    "residual_spanish",
    "semantic_error",
    "structure_error",
    "token_mismatch",
}


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_religion_divine_realm_symbolic_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS religion_divine_realm_symbolic_policy_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            alias_ready_count INTEGER NOT NULL DEFAULT 0,
            direct_ready_count INTEGER NOT NULL DEFAULT 0,
            dynamic_phrase_ready_count INTEGER NOT NULL DEFAULT 0,
            locked_human_correction_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS religion_divine_realm_symbolic_policy_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            text_family TEXT NOT NULL,
            human_label TEXT,
            local_status TEXT,
            confirmation_label TEXT,
            confirmation_source TEXT,
            locked INTEGER NOT NULL DEFAULT 0,
            policy_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            is_alias_key INTEGER NOT NULL DEFAULT 0,
            is_dab_qhuas INTEGER NOT NULL DEFAULT 0,
            learning_candidate_id INTEGER,
            issue_signals_json TEXT,
            effective_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_religion_divine_realm_symbolic_policy_items_run
        ON religion_divine_realm_symbolic_policy_items(run_id, policy_status)
        """
    )


def insert_run(
    conn,
    *,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    status_counts = Counter(row["policy_status"] for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO religion_divine_realm_symbolic_policy_runs (
            rule_version,
            total_rows,
            shadow_ready_count,
            blocked_count,
            alias_ready_count,
            direct_ready_count,
            dynamic_phrase_ready_count,
            locked_human_correction_count,
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
            len(rows),
            sum(1 for row in rows if row["policy_status"].startswith("shadow_ready")),
            sum(1 for row in rows if not row["policy_status"].startswith("shadow_ready")),
            status_counts["shadow_ready_alias_reference"],
            status_counts["shadow_ready_reviewed_direct_text"],
            status_counts["shadow_ready_reviewed_dynamic_phrase"],
            status_counts["shadow_ready_locked_human_correction"],
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in rows:
        conn.execute(
            """
            INSERT INTO religion_divine_realm_symbolic_policy_items (
                run_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                text_family,
                human_label,
                local_status,
                confirmation_label,
                confirmation_source,
                locked,
                policy_status,
                policy_action,
                is_alias_key,
                is_dab_qhuas,
                learning_candidate_id,
                issue_signals_json,
                effective_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["text_family"],
                row.get("human_label"),
                row.get("local_status"),
                row.get("confirmation_label"),
                row.get("confirmation_source"),
                int(row.get("locked") or 0),
                row["policy_status"],
                row["policy_action"],
                row["is_alias_key"],
                row["is_dab_qhuas"],
                row.get("learning_candidate_id"),
                json.dumps(row["issue_signals"], ensure_ascii=False, sort_keys=True),
                row["effective_text"],
                now,
            ),
        )
    return run_id


def latest_learning_cte() -> str:
    return """
        latest_learning AS (
            SELECT c.*
            FROM local_learning_candidates c
            WHERE c.id = (
                SELECT c2.id
                FROM local_learning_candidates c2
                WHERE c2.segment_id = c.segment_id
                  AND c2.local_status = 'reviewed_human'
                ORDER BY
                  COALESCE(c2.reviewed_at, c2.updated_at, c2.created_at) DESC,
                  c2.id DESC
                LIMIT 1
            )
        )
    """


def fetch_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        WITH {latest_learning_cte()}
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text,
            sc.confirmed_text,
            sc.confirmation_label,
            sc.confirmation_source,
            sc.locked,
            ll.id AS learning_candidate_id,
            ll.human_label,
            ll.local_status,
            ll.corrected_text,
            ll.reason,
            ll.reviewer,
            ll.reviewed_at
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc
          ON sc.id = (
              SELECT sc2.id
              FROM segment_confirmations sc2
              WHERE sc2.segment_id = s.id
              ORDER BY sc2.updated_at DESC, sc2.id DESC
              LIMIT 1
          )
        LEFT JOIN latest_learning ll ON ll.segment_id = s.id
        WHERE s.is_active = 1
          AND s.relative_path LIKE 'religion/%'
          AND s.source_key LIKE '%divine_realm%'
        ORDER BY s.relative_path, s.source_key, s.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def text_value(row: dict[str, Any]) -> str:
    return (
        row.get("corrected_text")
        or row.get("confirmed_text")
        or row.get("output_text")
        or row.get("old_text")
        or row.get("spanish_text")
        or ""
    ).strip()


def text_family(text: str) -> str:
    if ALIAS_RE.match(text):
        return "alias_reference"
    if not text:
        return "empty"
    if "$" in text:
        return "dynamic_variable_phrase"
    if "[" in text or "]" in text:
        return "dynamic_or_token_text"
    if len(text) <= 24:
        return "short_direct_text"
    return "long_direct_text"


def has_encoding_signal(text: str) -> bool:
    # Keep this deliberately conservative: '?' is common only after broken accent repair in this scope.
    return "?" in text or "Ã" in text or "�" in text


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = text_value(row)
    family = text_family(text)
    label = row.get("human_label") or ""
    local_status = row.get("local_status") or ""
    reviewed = local_status == "reviewed_human" and bool(label)
    issue_signals: list[str] = []
    if has_encoding_signal(text):
        issue_signals.append("encoding_signal")
    if label in FIX_LABELS:
        issue_signals.append(f"latest_label_{label}")
    if not reviewed:
        issue_signals.append("missing_latest_human_review")

    locked = int(row.get("locked") or 0) == 1
    confirmed_text = (row.get("confirmed_text") or "").strip()
    corrected_text = (row.get("corrected_text") or "").strip()
    corrected_confirmed = bool(corrected_text) and corrected_text == confirmed_text

    if family == "alias_reference" and reviewed and label in POSITIVE_LABELS and not issue_signals:
        status = "shadow_ready_alias_reference"
        action = "would_accept_alias_reference_boundary"
    elif family in {"short_direct_text", "dynamic_or_token_text"} and reviewed and label in POSITIVE_LABELS and not issue_signals:
        status = "shadow_ready_reviewed_direct_text"
        action = "would_accept_reviewed_direct_boundary"
    elif family == "dynamic_variable_phrase" and reviewed and label in POSITIVE_LABELS and not issue_signals:
        status = "shadow_ready_reviewed_dynamic_phrase"
        action = "would_accept_reviewed_dynamic_phrase_boundary"
    elif label in FIX_LABELS and locked and corrected_confirmed and not has_encoding_signal(text):
        status = "shadow_ready_locked_human_correction"
        action = "would_accept_locked_human_correction"
    elif family == "alias_reference" and reviewed and label in POSITIVE_LABELS and issue_signals == ["encoding_signal"]:
        status = "blocked_by_text_integrity"
        action = "keep_manual_review"
    elif label in FIX_LABELS:
        status = "blocked_by_fix_evidence"
        action = "keep_manual_review"
    elif not reviewed:
        status = "needs_human_evidence"
        action = "queue_review"
    else:
        status = "contextual_boundary_only"
        action = "keep_shadow_only"

    return {
        **row,
        "effective_text": text,
        "text_family": family,
        "policy_status": status,
        "policy_action": action,
        "issue_signals": issue_signals,
        "is_alias_key": 1 if row["source_key"].endswith("_2") or row["source_key"].endswith("_3") else 0,
        "is_dab_qhuas": 1 if row["source_key"].startswith("dab_qhuas_") else 0,
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "text_family",
        "human_label",
        "local_status",
        "confirmation_label",
        "confirmation_source",
        "locked",
        "policy_status",
        "policy_action",
        "is_alias_key",
        "is_dab_qhuas",
        "learning_candidate_id",
        "effective_text",
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
                "issue_signals": row["issue_signals"],
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("output_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "reason": row.get("reason") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["policy_status"] for row in rows)
    family_counts = Counter(row["text_family"] for row in rows)
    label_counts = Counter(row.get("human_label") or "no_latest_label" for row in rows)
    ready = [row for row in rows if row["policy_status"].startswith("shadow_ready")]
    blocked = [row for row in rows if not row["policy_status"].startswith("shadow_ready")]
    lines = [
        "Religion divine-realm symbolic policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows):,}",
        f"- Shadow-ready rows: {len(ready):,}",
        f"- Blocked/review rows: {len(blocked):,}",
        "",
        "Policy statuses:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Text families:",
        *[f"- {key}: {value:,}" for key, value in family_counts.most_common()],
        "",
        "Latest learning labels:",
        *[f"- {key}: {value:,}" for key, value in label_counts.most_common()],
        "",
        "Ready sample:",
    ]
    for row in ready[:20]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  family={row['text_family']}; label={row.get('human_label')}; text={short(row['effective_text'])}",
            ]
        )
    if not ready:
        lines.append("- none")
    lines.extend(["", "Blocked/review sample:"])
    for row in blocked[:20]:
        lines.extend(
            [
                f"- {row['policy_status']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  family={row['text_family']}; label={row.get('human_label')}; signals={', '.join(row['issue_signals']) or 'none'}; text={short(row['effective_text'])}",
            ]
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a learning/dry-run policy only.",
            "- It does not change confirmations, promote models, approve production apply, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        rows = [classify(row) for row in fetch_rows(conn)]
        run_id = insert_run(
            conn,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
        )
        conn.commit()
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        rows=rows,
        started_at=started_at,
    )

    status_counts = Counter(row["policy_status"] for row in rows)
    print("[religion_divine_realm_symbolic_policy] Dry-run generated")
    print(f"[religion_divine_realm_symbolic_policy] Policy run id: {run_id}")
    print(f"[religion_divine_realm_symbolic_policy] Rows inspected: {len(rows):,}")
    for key, value in status_counts.most_common():
        print(f"[religion_divine_realm_symbolic_policy] {key}: {value:,}")
    print("[religion_divine_realm_symbolic_policy] Apply allowed: 0")
    print(f"[religion_divine_realm_symbolic_policy] Report: {txt_path}")
    print(f"[religion_divine_realm_symbolic_policy] CSV: {csv_path}")
    print(f"[religion_divine_realm_symbolic_policy] JSONL: {jsonl_path}")
    return {
        "rows": len(rows),
        "run_id": run_id,
        "statuses": dict(status_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run symbolic policy for religion divine_realm rows.")
    parser.parse_args()
    main()
