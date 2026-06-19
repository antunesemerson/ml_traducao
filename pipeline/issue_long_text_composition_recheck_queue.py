from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_long_text_composition_recheck_queue_v1"
QUEUE_NAME = "long_text_composition_recheck_queue_v1"
QUEUE_STRATEGY = "long_text_composition_recheck"
AGENT_KEY = "composition_coordinator_v1"
ISSUE_FAMILY = "long_text_composition_recheck"
ISSUE_KIND = "whole_segment_composition_recheck"
QUEUE_BUCKET = "candidate_composition_recheck"


def latest_impact_run_id(conn, impact_run_id: int | None) -> int:
    if impact_run_id is not None:
        return impact_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_partial_composition_impact_runs
        WHERE finished_at IS NOT NULL
          AND candidate_recheck_segment_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No partial composition impact run with recheck candidates found.")
    return int(row["id"])


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return payload if isinstance(payload, list) else [payload]


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_composition_recheck_queue_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            queue_name TEXT NOT NULL,
            queue_strategy TEXT NOT NULL,
            impact_run_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            review_queue_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            released_component_count INTEGER NOT NULL DEFAULT 0,
            component_source_counts_json TEXT,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_composition_recheck_queue_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            impact_run_id INTEGER NOT NULL,
            impact_item_id INTEGER NOT NULL,
            review_queue_run_id INTEGER NOT NULL,
            review_queue_item_id INTEGER NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            source_decision_id INTEGER NOT NULL,
            source_queue_item_id INTEGER,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            released_component_count INTEGER NOT NULL DEFAULT 0,
            released_component_sources_json TEXT,
            queue_bucket TEXT NOT NULL,
            suggested_decision TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            evidence_json TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_composition_recheck_queue_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], impact_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_composition_recheck_queue_impact_run_{impact_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def fetch_impact_run(conn, impact_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_partial_composition_impact_runs
        WHERE id = ?
        """,
        (impact_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial composition impact run not found: {impact_run_id}")
    return dict(row)


def fetch_candidates(conn, impact_run_id: int, *, limit: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            impact.id AS impact_item_id,
            impact.run_id AS impact_run_id,
            impact.decision_run_id,
            impact.decision_id,
            impact.segment_id,
            impact.relative_path,
            impact.source_key,
            impact.source_line_number,
            impact.released_component_count,
            impact.released_component_sources_json,
            impact.notes,
            decision.queue_item_id AS source_queue_item_id,
            decision.ledger_run_id,
            decision.ledger_item_id,
            source_queue.english_text,
            source_queue.spanish_text,
            source_queue.confirmed_text
        FROM ml_issue_long_text_partial_composition_impact_items impact
        JOIN ml_issue_review_decisions decision ON decision.id = impact.decision_id
        LEFT JOIN ml_issue_review_queue_items source_queue ON source_queue.id = decision.queue_item_id
        WHERE impact.run_id = ?
          AND impact.coverage_state = 'candidate_composition_recheck'
          AND impact.blocker_count = 0
          AND impact.released_component_count > 0
        ORDER BY impact.released_component_count DESC, impact.segment_id
        """
        + (" LIMIT ?" if limit is not None else ""),
        (impact_run_id, limit) if limit is not None else (impact_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def component_source_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        for component in parse_json_list(row.get("released_component_sources_json")):
            if isinstance(component, dict):
                counts[str(component.get("source") or "unknown")] += 1
            else:
                counts["unknown"] += 1
    return counts


def priority_score(row: dict[str, Any]) -> float:
    components = parse_json_list(row.get("released_component_sources_json"))
    source_bonus = len({str(item.get("source") or "") for item in components if isinstance(item, dict)}) * 35
    return round(1000 + int(row["released_component_count"] or 0) * 120 + source_bonus, 4)


def build_evidence(row: dict[str, Any]) -> dict[str, Any]:
    components = parse_json_list(row.get("released_component_sources_json"))
    return {
        "source": "ml_issue_long_text_partial_composition_impact",
        "impact_run_id": int(row["impact_run_id"]),
        "impact_item_id": int(row["impact_item_id"]),
        "source_decision_run_id": int(row["decision_run_id"]),
        "source_decision_id": int(row["decision_id"]),
        "recheck_goal": "Validate whether released partial components jointly close the whole segment.",
        "recheck_scope": "whole_segment_shadow_review",
        "released_component_count": int(row["released_component_count"] or 0),
        "released_components": components,
        "known_blocker_count": 0,
        "required_checks": [
            "semantic_equivalence_against_english_reference",
            "pt_br_fluency_in_full_segment",
            "no_residual_spanish_visible_text",
            "ck3_token_placeholder_integrity",
            "no_gender_or_scope_regression",
            "no_manual_exception_violation",
        ],
        "original_notes": row.get("notes") or "",
    }


def build_queue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue_rows: list[dict[str, Any]] = []
    for row in rows:
        evidence = build_evidence(row)
        score = priority_score(row)
        queue_rows.append(
            {
                **row,
                "issue_family": ISSUE_FAMILY,
                "issue_kind": ISSUE_KIND,
                "agent_key": AGENT_KEY,
                "queue_bucket": QUEUE_BUCKET,
                "priority_score": score,
                "suggested_decision": "composition_ready",
                "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                "evidence_text": (
                    "Whole-segment composition recheck: "
                    f"{int(row['released_component_count'] or 0)} lifecycle components, no known blockers. "
                    "Confirm whether the full segment is semantically correct, fluent PT-BR, token-safe, and free of residual Spanish. "
                    f"Notes: {short(row.get('notes'))}"
                ),
            }
        )
    return queue_rows


def insert_review_queue_run(
    conn,
    *,
    impact_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    ledger_run_id = int(selected[0]["ledger_run_id"]) if selected else 0
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    cursor = conn.execute(
        """
        INSERT INTO ml_issue_review_queue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            issue_family,
            queue_strategy,
            limit_count,
            per_bucket,
            selected_count,
            open_count,
            reviewed_count,
            bucket_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            ledger_run_id,
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            len(selected),
            len(selected),
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_recheck_run(
    conn,
    *,
    impact_run: dict[str, Any],
    review_queue_run_id: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    component_counts = component_source_counts(selected)
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    cursor = conn.execute(
        """
        INSERT INTO ml_issue_long_text_composition_recheck_queue_runs (
            rule_version,
            queue_name,
            queue_strategy,
            impact_run_id,
            decision_run_id,
            review_queue_run_id,
            candidate_count,
            selected_count,
            open_count,
            reviewed_count,
            released_component_count,
            component_source_counts_json,
            bucket_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            QUEUE_NAME,
            QUEUE_STRATEGY,
            int(impact_run["id"]),
            int(impact_run["decision_run_id"]),
            review_queue_run_id,
            int(impact_run["candidate_recheck_segment_count"] or 0),
            len(selected),
            len(selected),
            sum(int(row["released_component_count"] or 0) for row in selected),
            json.dumps(dict(component_counts), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(
    conn,
    *,
    recheck_run_id: int,
    review_queue_run_id: int,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    now = db.utc_now()
    inserted: list[dict[str, Any]] = []
    for row in rows:
        review_cursor = conn.execute(
            """
            INSERT INTO ml_issue_review_queue_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                issue_kind,
                agent_key,
                queue_bucket,
                priority_score,
                review_status,
                suggested_decision,
                evidence_text,
                evidence_json,
                english_text,
                spanish_text,
                confirmed_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_queue_run_id,
                int(row["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["issue_family"],
                row["issue_kind"],
                row["agent_key"],
                row["queue_bucket"],
                float(row["priority_score"]),
                row["suggested_decision"],
                row["evidence_text"],
                row["evidence_json"],
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            ),
        )
        review_queue_item_id = int(review_cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO ml_issue_long_text_composition_recheck_queue_items (
                run_id,
                impact_run_id,
                impact_item_id,
                review_queue_run_id,
                review_queue_item_id,
                source_decision_run_id,
                source_decision_id,
                source_queue_item_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                released_component_count,
                released_component_sources_json,
                queue_bucket,
                suggested_decision,
                priority_score,
                evidence_json,
                notes,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recheck_run_id,
                int(row["impact_run_id"]),
                int(row["impact_item_id"]),
                review_queue_run_id,
                review_queue_item_id,
                int(row["decision_run_id"]),
                int(row["decision_id"]),
                row.get("source_queue_item_id"),
                int(row["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                int(row["released_component_count"] or 0),
                row.get("released_component_sources_json") or "[]",
                row["queue_bucket"],
                row["suggested_decision"],
                float(row["priority_score"]),
                row["evidence_json"],
                row.get("notes") or "",
                now,
            ),
        )
        inserted.append({**row, "review_queue_item_id": review_queue_item_id})
    return inserted


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    recheck_run_id: int,
    review_queue_run_id: int,
    impact_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fieldnames = [
        "recheck_run_id",
        "review_queue_run_id",
        "review_queue_item_id",
        "impact_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "released_component_count",
        "queue_bucket",
        "suggested_decision",
        "priority_score",
        "released_component_sources_json",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "recheck_run_id": recheck_run_id,
                    "review_queue_run_id": review_queue_run_id,
                }
            )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "recheck_run_id": recheck_run_id,
                "review_queue_run_id": review_queue_run_id,
                "review_queue_item_id": row["review_queue_item_id"],
                "impact_run_id": int(impact_run["id"]),
                "impact_item_id": row["impact_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row.get("source_line_number"),
                "suggested_decision": row["suggested_decision"],
                "evidence": json.loads(row["evidence_json"]),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    decision_options = [
        "composition_ready",
        "needs_repair",
        "needs_domain_context",
        "needs_new_microagent",
        "manual_exception",
        "false_positive_reopen",
    ]
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": review_queue_run_id,
                "queue_item_id": row["review_queue_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "decision": "",
                "decision_options": decision_options,
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    component_counts = component_source_counts(rows)
    lines = [
        "Issue long-text composition recheck queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue name: {QUEUE_NAME}",
        f"Recheck run id: {recheck_run_id}",
        f"Review queue run id: {review_queue_run_id}",
        f"Impact run id: {impact_run['id']}",
        f"Decision run id: {impact_run['decision_run_id']}",
        "",
        "Summary:",
        f"- Impact candidate recheck segments: {int(impact_run['candidate_recheck_segment_count'] or 0):,}",
        f"- Selected for recheck: {len(rows):,}",
        f"- Released components represented: {sum(int(row['released_component_count'] or 0) for row in rows):,}",
        f"- Component sources: {json.dumps(dict(component_counts), ensure_ascii=False, sort_keys=True)}",
        "",
        "Review guidance:",
        "- Validate the whole segment, not only the listed partial component.",
        "- Use `composition_ready` only when the final current text is semantically correct, fluent PT-BR, token-safe, and has no visible Spanish residue.",
        "- Use `needs_repair` when a concrete correction is still required and fill corrected_text if possible.",
        "- Use `needs_new_microagent` when the remaining problem is reusable and not covered by existing components.",
        "- This queue is learning-front only: no source/output read, no output write, no confirmation promotion.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Rows:",
    ]
    for row in rows:
        lines.append(
            (
                f"- queue_item={row['review_queue_item_id']} | components={row['released_component_count']} | "
                f"{row['relative_path']}::{row['source_key']}"
            )
        )
        lines.append(f"  notes={short(row.get('notes'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, impact_run_id: int | None = None, limit: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    paths: tuple[Path, Path, Path, Path] | None = None
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_impact_run_id = latest_impact_run_id(conn, impact_run_id)
        impact_run = fetch_impact_run(conn, selected_impact_run_id)
        raw_rows = fetch_candidates(conn, selected_impact_run_id, limit=limit)
        if not raw_rows:
            raise RuntimeError(f"Impact run {selected_impact_run_id} has no composition recheck candidates.")
        rows = build_queue_rows(raw_rows)
        paths = report_paths(settings, selected_impact_run_id)
        review_queue_run_id = insert_review_queue_run(conn, impact_run=impact_run, selected=rows, paths=paths)
        recheck_run_id = insert_recheck_run(
            conn,
            impact_run=impact_run,
            review_queue_run_id=review_queue_run_id,
            selected=rows,
            paths=paths,
        )
        inserted_rows = insert_items(
            conn,
            recheck_run_id=recheck_run_id,
            review_queue_run_id=review_queue_run_id,
            rows=rows,
        )
        write_outputs(
            paths=paths,
            recheck_run_id=recheck_run_id,
            review_queue_run_id=review_queue_run_id,
            impact_run=impact_run,
            rows=inserted_rows,
        )
        conn.commit()

    assert paths is not None
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_long_text_composition_recheck_queue] Queue generated")
    print(f"[issue_long_text_composition_recheck_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_composition_recheck_queue] Recheck run id: {recheck_run_id}")
    print(f"[issue_long_text_composition_recheck_queue] Review queue run id: {review_queue_run_id}")
    print(f"[issue_long_text_composition_recheck_queue] Impact run id: {selected_impact_run_id}")
    print(f"[issue_long_text_composition_recheck_queue] Selected: {len(inserted_rows):,}")
    print(f"[issue_long_text_composition_recheck_queue] Report: {txt_path}")
    print(f"[issue_long_text_composition_recheck_queue] Decisions template: {decisions_template_path}")
    return {
        "recheck_run_id": recheck_run_id,
        "review_queue_run_id": review_queue_run_id,
        "impact_run_id": selected_impact_run_id,
        "selected_count": len(inserted_rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a whole-segment long-text composition recheck queue.")
    parser.add_argument("--impact-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(impact_run_id=args.impact_run_id, limit=args.limit)
