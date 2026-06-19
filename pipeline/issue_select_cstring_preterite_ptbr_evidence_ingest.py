from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_preterite_ptbr_evidence_ingest_v1"
DEFAULT_DECISIONS_GLOB = "*_issue_select_cstring_preterite_ptbr_assisted_review.jsonl"
TARGET_AGENT = "select_cstring_local_player_preterite_verb_rewrite"
ALLOWED_DECISIONS = {
    "approve_ptbr_neutral_literal",
    "edit_ptbr_neutral_literal",
    "needs_context",
    "manual_exception",
    "false_positive",
}


def latest_decisions(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(DEFAULT_DECISIONS_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No Select_CString preterite assisted review decisions found.")
    return candidates[0]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"Line {line_number} is not a JSON object.")
        rows.append(payload)
    return rows


def as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def require_safe_learning_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("Decision file is empty.")
    for index, row in enumerate(rows, start=1):
        decision = str(row.get("decision") or "").strip()
        if decision not in ALLOWED_DECISIONS:
            raise ValueError(f"Row {index} has unsupported decision: {decision!r}")
        if int(row.get("learning_only") or 0) != 1:
            raise ValueError(f"Row {index} is not marked learning_only=1.")
        if int(row.get("apply_allowed") or 0) != 0:
            raise ValueError(f"Row {index} has apply_allowed != 0.")
        if int(row.get("production_release_allowed") or 0) != 0:
            raise ValueError(f"Row {index} has production_release_allowed != 0.")
        if str(row.get("target_agent") or TARGET_AGENT) != TARGET_AGENT:
            raise ValueError(f"Row {index} targets a different agent.")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_preterite_ptbr_decision_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rule_version TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            source_decisions_path TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            positive_count INTEGER NOT NULL,
            boundary_count INTEGER NOT NULL,
            learning_only INTEGER NOT NULL DEFAULT 1,
            apply_allowed INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_preterite_ptbr_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            queue_item_id INTEGER,
            ledger_item_id INTEGER,
            segment_id INTEGER,
            target_agent TEXT NOT NULL,
            left_literal TEXT NOT NULL,
            right_literal TEXT,
            sample_reason TEXT,
            decision TEXT NOT NULL,
            approved_ptbr_neutral_literal TEXT,
            confidence_label TEXT NOT NULL,
            review_reason TEXT,
            reviewer TEXT,
            learning_only INTEGER NOT NULL DEFAULT 1,
            apply_allowed INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_preterite_ptbr_decision_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_preterite_ptbr_decisions_run
        ON ml_issue_select_cstring_preterite_ptbr_decisions(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_preterite_ptbr_decisions_agent
        ON ml_issue_select_cstring_preterite_ptbr_decisions(target_agent, decision)
        """
    )


def output_report(settings: dict[str, Any], lines: list[str]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_path = reports_dir / f"{stamp}_issue_select_cstring_preterite_ptbr_evidence_ingest.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def ingest(decisions_path: Path) -> dict[str, Any]:
    settings = db.load_settings()
    rows = load_jsonl(decisions_path)
    require_safe_learning_rows(rows)
    decision_counts = Counter(str(row.get("decision") or "") for row in rows)
    confidence_counts = Counter(str(row.get("confidence_label") or "") for row in rows)
    now = datetime.now().isoformat(timespec="seconds")

    with db.connect(settings) as conn:
        ensure_tables(conn)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_preterite_ptbr_decision_runs (
                started_at, finished_at, rule_version, target_agent, source_decisions_path,
                row_count, positive_count, boundary_count, learning_only, apply_allowed,
                production_release_allowed, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?)
            """,
            (
                now,
                now,
                RULE_VERSION,
                TARGET_AGENT,
                str(decisions_path),
                len(rows),
                confidence_counts["positive"],
                confidence_counts["boundary"],
                "Learning-only PT-BR neutral literal evidence; no output or production release.",
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_preterite_ptbr_decisions (
                run_id, queue_item_id, ledger_item_id, segment_id, target_agent,
                left_literal, right_literal, sample_reason, decision,
                approved_ptbr_neutral_literal, confidence_label, review_reason, reviewer,
                learning_only, apply_allowed, production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?)
            """,
            [
                (
                    run_id,
                    as_int(row.get("queue_item_id")),
                    as_int(row.get("ledger_item_id")),
                    as_int(row.get("segment_id")),
                    str(row.get("target_agent") or TARGET_AGENT),
                    str(row.get("left_literal") or ""),
                    str(row.get("right_literal") or ""),
                    str(row.get("sample_reason") or ""),
                    str(row.get("decision") or ""),
                    str(row.get("approved_ptbr_neutral_literal") or ""),
                    str(row.get("confidence_label") or ""),
                    str(row.get("review_reason") or ""),
                    str(row.get("reviewer") or ""),
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()

    report_lines = [
        "Select_CString preterite PT-BR evidence ingest",
        f"Rule version: {RULE_VERSION}",
        f"Decision run id: {run_id}",
        f"Source decisions: {decisions_path}",
        "",
        "Summary:",
        f"- Rows ingested: {len(rows):,}",
        f"- Positive evidence: {confidence_counts['positive']:,}",
        f"- Boundary/context evidence: {confidence_counts['boundary']:,}",
        "- Learning only: 1",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Decisions:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
    ]
    report_path = output_report(settings, report_lines)
    print("[issue_select_cstring_preterite_ptbr_evidence_ingest] Ingested learning evidence")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_ingest] Run id: {run_id}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_ingest] Rows: {len(rows):,}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_ingest] Report: {report_path}")
    return {"run_id": run_id, "rows": len(rows), "report_path": str(report_path)}


def main(*, decisions_jsonl: str | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    decisions_path = Path(decisions_jsonl) if decisions_jsonl else latest_decisions(settings)
    return ingest(decisions_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest learning-only Select_CString preterite PT-BR evidence.")
    parser.add_argument("--decisions-jsonl", default=None)
    args = parser.parse_args()
    main(decisions_jsonl=args.decisions_jsonl)
