from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_text_decision_rebase_v1"


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_diagnostic_runs
        WHERE finished_at IS NOT NULL
          AND total_items > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed text diagnostic run found.")
    return int(row["id"])


def latest_source_diagnostic_run_id(conn, *, before_run_id: int) -> int:
    row = conn.execute(
        """
        SELECT diagnostic_run_id
        FROM auto_confirmation_reopen_text_review_decisions
        WHERE diagnostic_run_id IS NOT NULL
          AND diagnostic_run_id <> ?
        GROUP BY diagnostic_run_id
        ORDER BY MAX(updated_at) DESC, diagnostic_run_id DESC
        LIMIT 1
        """,
        (before_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No source diagnostic run with text review decisions found.")
    return int(row["diagnostic_run_id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_decision_rebase"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_source_decisions(conn, *, source_diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_text_review_decisions
        WHERE diagnostic_run_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (source_diagnostic_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_target_items(conn, *, target_diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text,
            existing.id AS existing_decision_id
        FROM auto_confirmation_reopen_text_diagnostic_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        LEFT JOIN auto_confirmation_reopen_text_review_decisions existing
          ON existing.id = (
              SELECT d.id
              FROM auto_confirmation_reopen_text_review_decisions d
              WHERE d.diagnostic_run_id = item.run_id
                AND d.diagnostic_item_id = item.id
              ORDER BY d.updated_at DESC, d.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (target_diagnostic_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def index_source_decisions(rows: list[dict[str, Any]]) -> tuple[dict[tuple[Any, ...], dict[str, Any]], dict[tuple[Any, ...], dict[str, Any]]]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_segment: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        text_hash = row.get("current_confirmed_text_hash")
        if not text_hash:
            continue
        key = (
            row["relative_path"],
            row["source_key"],
            row["agent_key"],
            row["text_subfamily"],
            text_hash,
        )
        segment_key = (
            row["segment_id"],
            row["agent_key"],
            row["text_subfamily"],
            text_hash,
        )
        by_key.setdefault(key, row)
        by_segment.setdefault(segment_key, row)
    return by_key, by_segment


def classify_match(item: dict[str, Any], by_key: dict[tuple[Any, ...], dict[str, Any]], by_segment: dict[tuple[Any, ...], dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    text_hash = sha256_text(item.get("confirmed_text"))
    if not text_hash:
        return "missing_confirmed_hash", None
    agent_key = item.get("suggested_agent_key")
    text_subfamily = item.get("text_subfamily")
    key = (item["relative_path"], item["source_key"], agent_key, text_subfamily, text_hash)
    if key in by_key:
        return "matched_by_path_key_hash", by_key[key]
    segment_key = (item["segment_id"], agent_key, text_subfamily, text_hash)
    if segment_key in by_segment:
        return "matched_by_segment_hash", by_segment[segment_key]
    return "no_safe_match", None


def suggested_review_label(item: dict[str, Any], source_decision: dict[str, Any]) -> str:
    evidence = source_decision.get("evidence_label") or ""
    if evidence == "positive_evidence":
        return "rebased_positive_evidence"
    if evidence == "negative_boundary":
        return "rebased_negative_boundary"
    if evidence == "needs_more_context":
        return "rebased_needs_more_context"
    if evidence == "manual_exception_only":
        return "rebased_manual_exception"
    return "rebased_review_evidence"


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    rows: list[dict[str, Any]],
    source_diagnostic_run_id: int,
    target_diagnostic_run_id: int,
    apply: bool,
    started_at: datetime,
) -> None:
    fieldnames = [
        "target_diagnostic_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "agent_key",
        "text_subfamily",
        "match_status",
        "source_decision_id",
        "source_decision",
        "source_evidence_label",
        "queue_item_id",
        "decision_id",
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
                "confirmed_preview": short(row.get("confirmed_text")),
                "notes": row.get("notes") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["match_status"] for row in rows)
    applied = [row for row in rows if row.get("decision_id")]
    lines = [
        "Auto-confirmation text decision rebase",
        f"Rule version: {RULE_VERSION}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Source diagnostic run id: {source_diagnostic_run_id}",
        f"Target diagnostic run id: {target_diagnostic_run_id}",
        f"Apply: {apply}",
        "",
        "Summary:",
        f"- Target rows inspected: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in by_status.most_common()],
        f"- Decisions inserted: {len(applied):,}",
        "",
        "Rebased sample:",
    ]
    for row in applied[:20]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  decision={row['source_decision']}; evidence={row['source_evidence_label']}; match={row['match_status']}",
            ]
        )
    if not applied:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This only reuses prior human evidence when the confirmed text hash is unchanged.",
            "- It does not alter confirmations, promote models, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def insert_rebased_rows(
    conn,
    *,
    rows: list[dict[str, Any]],
    source_diagnostic_run_id: int,
    target_diagnostic_run_id: int,
    txt_path: Path,
    jsonl_path: Path,
    started_at: datetime,
) -> None:
    selected = [row for row in rows if row.get("source_decision")]
    if not selected:
        return

    now = datetime.now().isoformat(timespec="seconds")
    high_count = sum(1 for row in selected if row.get("risk_level") == "high")
    medium_count = sum(1 for row in selected if row.get("risk_level") == "medium")
    queue_cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_review_queue_runs (
            rule_version,
            diagnostic_run_id,
            agent_key,
            text_subfamily,
            total_candidates,
            selected_count,
            high_risk_count,
            medium_risk_count,
            skipped_previously_queued_count,
            report_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            target_diagnostic_run_id,
            "rebased_text_evidence",
            "multiple",
            len(rows),
            len(selected),
            high_count,
            medium_count,
            0,
            str(txt_path),
            str(jsonl_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    queue_run_id = int(queue_cursor.lastrowid)
    decision_cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_review_decision_runs (
            rule_version,
            queue_run_id,
            agent_key,
            decisions_path,
            total_rows,
            accepted_count,
            positive_count,
            negative_count,
            needs_more_context_count,
            manual_exception_count,
            skipped_count,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            queue_run_id,
            "rebased_text_evidence",
            str(jsonl_path),
            len(rows),
            len(selected),
            sum(1 for row in selected if row["source_decision"]["evidence_label"] == "positive_evidence"),
            sum(1 for row in selected if row["source_decision"]["evidence_label"] == "negative_boundary"),
            sum(1 for row in selected if row["source_decision"]["evidence_label"] == "needs_more_context"),
            sum(1 for row in selected if row["source_decision"]["evidence_label"] == "manual_exception_only"),
            len(rows) - len(selected),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    decision_run_id = int(decision_cursor.lastrowid)

    for rank, row in enumerate(selected, start=1):
        source_decision = row["source_decision"]
        item_cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_review_queue_items (
                run_id,
                diagnostic_run_id,
                diagnostic_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                confirmation_label,
                text_subfamily,
                suggested_agent_key,
                risk_level,
                select_cstring_count,
                spanish_literal_hint_count,
                issue_count,
                model_safe_probability,
                review_priority,
                queue_rank,
                suggested_review_label,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_run_id,
                target_diagnostic_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row.get("confirmation_label"),
                row["text_subfamily"],
                row["suggested_agent_key"],
                row["risk_level"],
                int(row.get("select_cstring_count") or 0),
                int(row.get("spanish_literal_hint_count") or 0),
                int(row.get("issue_count") or 0),
                row.get("model_safe_probability"),
                float(row.get("review_priority") or 0),
                rank,
                suggested_review_label(row, source_decision),
                now,
            ),
        )
        queue_item_id = int(item_cursor.lastrowid)
        reasons = {
            "rule_version": RULE_VERSION,
            "source_diagnostic_run_id": source_diagnostic_run_id,
            "source_decision_id": source_decision["id"],
            "target_diagnostic_run_id": target_diagnostic_run_id,
            "match_status": row["match_status"],
        }
        notes = (
            f"Rebased from text review decision {source_decision['id']} "
            f"because confirmed_text_hash is unchanged. "
            f"{source_decision.get('notes') or ''}"
        ).strip()
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_review_decisions (
                run_id,
                queue_run_id,
                queue_item_id,
                diagnostic_run_id,
                diagnostic_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                agent_key,
                text_subfamily,
                risk_level,
                decision,
                evidence_label,
                corrected_text,
                notes,
                reviewer,
                current_confirmed_text_hash,
                corrected_text_hash,
                reasons_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_run_id,
                queue_run_id,
                queue_item_id,
                target_diagnostic_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["suggested_agent_key"],
                row["text_subfamily"],
                row["risk_level"],
                source_decision["decision"],
                source_decision["evidence_label"],
                source_decision.get("corrected_text"),
                notes,
                "codex_text_decision_rebase",
                sha256_text(row.get("confirmed_text")),
                source_decision.get("corrected_text_hash"),
                json.dumps(reasons, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        row["queue_item_id"] = queue_item_id
        row["decision_id"] = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def main(
    *,
    source_diagnostic_run_id: int | None = None,
    target_diagnostic_run_id: int | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        target_run_id = target_diagnostic_run_id or latest_diagnostic_run_id(conn)
        source_run_id = source_diagnostic_run_id or latest_source_diagnostic_run_id(conn, before_run_id=target_run_id)
        source_decisions = fetch_source_decisions(conn, source_diagnostic_run_id=source_run_id)
        by_key, by_segment = index_source_decisions(source_decisions)
        rows = fetch_target_items(conn, target_diagnostic_run_id=target_run_id)
        for row in rows:
            row["target_diagnostic_item_id"] = row["id"]
            row["agent_key"] = row.get("suggested_agent_key")
            row["source_decision_id"] = None
            row["source_decision_label"] = None
            row["source_evidence_label"] = None
            if row.get("existing_decision_id"):
                row["match_status"] = "already_has_target_decision"
                row["source_decision"] = None
                continue
            status, source_decision = classify_match(row, by_key, by_segment)
            row["match_status"] = status
            row["source_decision"] = source_decision
            row["source_decision_id"] = source_decision["id"] if source_decision else None
            row["source_decision_label"] = source_decision["decision"] if source_decision else None
            row["source_evidence_label"] = source_decision["evidence_label"] if source_decision else None
        txt_path, csv_path, jsonl_path = report_paths(settings)
        if apply:
            insert_rebased_rows(
                conn,
                rows=rows,
                source_diagnostic_run_id=source_run_id,
                target_diagnostic_run_id=target_run_id,
                txt_path=txt_path,
                jsonl_path=jsonl_path,
                started_at=started_at,
            )
            conn.commit()
        for row in rows:
            source_decision = row.get("source_decision")
            row["source_decision_id"] = source_decision["id"] if source_decision else None
            row["source_decision"] = source_decision["decision"] if source_decision else None
            row["source_evidence_label"] = source_decision["evidence_label"] if source_decision else None
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            rows=rows,
            source_diagnostic_run_id=source_run_id,
            target_diagnostic_run_id=target_run_id,
            apply=apply,
            started_at=started_at,
        )

    status_counts = Counter(row["match_status"] for row in rows)
    inserted = sum(1 for row in rows if row.get("decision_id"))
    print("[auto_confirmation_reopen_text_decision_rebase] Rebase evaluated")
    print(f"[auto_confirmation_reopen_text_decision_rebase] Source diagnostic run id: {source_run_id}")
    print(f"[auto_confirmation_reopen_text_decision_rebase] Target diagnostic run id: {target_run_id}")
    print(f"[auto_confirmation_reopen_text_decision_rebase] Rows inspected: {len(rows):,}")
    for key, value in status_counts.most_common():
        print(f"[auto_confirmation_reopen_text_decision_rebase] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_decision_rebase] Decisions inserted: {inserted:,}")
    print(f"[auto_confirmation_reopen_text_decision_rebase] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_decision_rebase] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_decision_rebase] JSONL: {jsonl_path}")
    return {
        "source_diagnostic_run_id": source_run_id,
        "target_diagnostic_run_id": target_run_id,
        "rows": len(rows),
        "inserted": inserted,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebase text review decisions to a newer diagnostic run when confirmed text hashes are unchanged."
    )
    parser.add_argument("--source-diagnostic-run-id", type=int, default=None)
    parser.add_argument("--target-diagnostic-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(
        source_diagnostic_run_id=args.source_diagnostic_run_id,
        target_diagnostic_run_id=args.target_diagnostic_run_id,
        apply=args.apply,
    )
