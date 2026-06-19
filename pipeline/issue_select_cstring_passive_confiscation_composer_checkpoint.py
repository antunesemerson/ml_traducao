from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator


RULE_VERSION = "issue_select_cstring_passive_confiscation_composer_checkpoint_v1"
COMPOSER_NAME = "select_cstring_passive_confiscation_sentence_composer_v1"
COMPOSER_STATUS = "shadow_learning_only"
PRODUCTION_RELEASE_ALLOWED = 0
TARGET_SEGMENT_ID = 71709

CORRECTED_TEXT = (
    "[defender.GetShortUIName|U] será encarcerad[defender.Custom('ES_OA')] por "
    "[Select_CString( attacker.IsLocalPlayer, 'você', attacker.GetShortUIName )] "
    "e todas as suas terras serão confiscadas."
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_partial_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_partial_composition_checkpoint_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise SystemExit("No Select_CString partial composition checkpoint run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_passive_confiscation_composer_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            composer_name TEXT NOT NULL,
            composer_status TEXT NOT NULL,
            source_partial_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT NOT NULL,
            block_counts_json TEXT NOT NULL,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_partial_run_id INTEGER NOT NULL,
            source_partial_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            decision TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate INTEGER NOT NULL DEFAULT 0,
            current_text TEXT,
            corrected_text TEXT,
            block_reason TEXT,
            rationale TEXT,
            validation_issues_json TEXT NOT NULL,
            english_text TEXT,
            spanish_text TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_passive_confiscation_composer_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_passive_confiscation_items_run
        ON ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_passive_confiscation_items_segment
        ON ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items(segment_id)
        """
    )
    conn.commit()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_select_cstring_passive_confiscation_composer_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_candidate(conn, partial_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            item.id AS source_partial_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.composition_status,
            item.ready_sources_json,
            item.block_reasons_json,
            src.english_text,
            src.spanish_text,
            out.portuguese_text AS current_text
        FROM ml_issue_select_cstring_partial_composition_checkpoint_items item
        JOIN source_segments src ON src.id = item.segment_id
        LEFT JOIN output_segments out ON out.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.segment_id = ?
        LIMIT 1
        """,
        (partial_run_id, TARGET_SEGMENT_ID),
    ).fetchone()
    return dict(row) if row else None


def blocking_validation_issues(text: str) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    blocking_codes = {
        "spanish_residue",
        "spanish_residue_in_literal",
        "spanish_punctuation",
        "mojibake_or_unexpected_script",
        "utf8_mojibake_sequence",
        "replacement_question_mark_mojibake",
        "token_breakage",
        "placeholder_breakage",
    }
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in blocking_codes
    ]


def classify(row: dict[str, Any] | None, partial_run_id: int) -> dict[str, Any]:
    if row is None:
        return {
            "source_partial_run_id": partial_run_id,
            "source_partial_item_id": None,
            "segment_id": TARGET_SEGMENT_ID,
            "relative_path": "",
            "source_key": "",
            "decision": "blocked_missing_partial_composition_candidate",
            "checkpoint_allowed": 0,
            "segment_closure_candidate": 0,
            "current_text": "",
            "corrected_text": "",
            "block_reason": "partial_candidate_not_found",
            "rationale": "The target segment was not found in the selected partial composition run.",
            "validation_issues": [],
            "english_text": "",
            "spanish_text": "",
            "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        }

    block_reasons = json.loads(row.get("block_reasons_json") or "{}")
    ready_sources = json.loads(row.get("ready_sources_json") or "{}")
    validation_issues = blocking_validation_issues(CORRECTED_TEXT)
    decision = "blocked_passive_confiscation_context_not_matched"
    allowed = 0
    closure = 0
    block_reason = "required_partial_sources_or_block_missing"
    rationale = "The segment does not match the exact guarded passive-confiscation context."

    has_required_block = block_reasons.get("ptbr_third_person_loses_attacker_subject") == 1
    has_required_ready = (
        ready_sources.get("future_tense_checkpoint") == 1
        and ready_sources.get("possessive_context_checkpoint") == 1
    )
    if has_required_block and has_required_ready and not validation_issues:
        decision = "ready_passive_confiscation_sentence_composer"
        allowed = 1
        closure = 1
        block_reason = ""
        rationale = "The sentence-level composer preserves the attacker through the 'por [Select_CString(...)]' phrase and rewrites the confiscation clause as passive PT-BR."
    elif validation_issues:
        decision = "blocked_validation_issue"
        block_reason = "blocking_validation_issue"
        rationale = "The proposed passive sentence still has blocking local validation issues."

    return {
        "source_partial_run_id": partial_run_id,
        "source_partial_item_id": row["source_partial_item_id"],
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "decision": decision,
        "checkpoint_allowed": allowed,
        "segment_closure_candidate": closure,
        "current_text": row.get("current_text") or "",
        "corrected_text": CORRECTED_TEXT,
        "block_reason": block_reason,
        "rationale": rationale,
        "validation_issues": validation_issues,
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def write_reports(
    *,
    run_id: int,
    partial_run_id: int,
    items: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
) -> None:
    decision_counts = Counter(item["decision"] for item in items)
    block_counts = Counter(item["block_reason"] or "allowed" for item in items)
    allowed_count = sum(item["checkpoint_allowed"] for item in items)
    lines = [
        "Select_CString passive confiscation composer checkpoint",
        f"Run: {run_id}",
        f"Rule version: {RULE_VERSION}",
        f"Source partial run: {partial_run_id}",
        "",
        "Summary",
        f"- candidates: {len(items)}",
        f"- checkpoint_allowed: {allowed_count}",
        f"- blocked: {len(items) - allowed_count}",
        f"- production_release_allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Decisions",
        *[f"- {key}: {value}" for key, value in sorted(decision_counts.items())],
        "",
        "Block reasons",
        *[f"- {key}: {value}" for key, value in sorted(block_counts.items())],
        "",
        "Items",
    ]
    for item in items:
        lines.append(
            "- segment_id={segment_id} key={source_key} decision={decision} allowed={checkpoint_allowed} "
            "closure={segment_closure_candidate} block={block_reason}".format(**item)
        )
        lines.append(f"  rationale: {item['rationale']}")
        lines.append(f"  corrected_text: {item['corrected_text']}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fields = [
        "run_id",
        "source_partial_run_id",
        "source_partial_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "decision",
        "checkpoint_allowed",
        "segment_closure_candidate",
        "block_reason",
        "rationale",
        "current_text",
        "corrected_text",
        "validation_issues_json",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "run_id": run_id,
                    "source_partial_run_id": item["source_partial_run_id"],
                    "source_partial_item_id": item["source_partial_item_id"],
                    "segment_id": item["segment_id"],
                    "relative_path": item["relative_path"],
                    "source_key": item["source_key"],
                    "decision": item["decision"],
                    "checkpoint_allowed": item["checkpoint_allowed"],
                    "segment_closure_candidate": item["segment_closure_candidate"],
                    "block_reason": item["block_reason"],
                    "rationale": item["rationale"],
                    "current_text": item["current_text"],
                    "corrected_text": item["corrected_text"],
                    "validation_issues_json": json.dumps(item["validation_issues"], ensure_ascii=False, sort_keys=True),
                    "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                }
            )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps({"run_id": run_id, **item}, ensure_ascii=False, sort_keys=True) + "\n")


def build_checkpoint(partial_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    created_at = datetime.now().isoformat(timespec="seconds")
    report_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        ensure_tables(conn)
        source_run_id = partial_run_id or latest_partial_run_id(conn)
        items = [classify(fetch_candidate(conn, source_run_id), source_run_id)]
        decision_counts = Counter(item["decision"] for item in items)
        block_counts = Counter(item["block_reason"] or "allowed" for item in items)
        allowed_count = sum(item["checkpoint_allowed"] for item in items)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_passive_confiscation_composer_checkpoint_runs (
                created_at, rule_version, composer_name, composer_status,
                source_partial_run_id, candidate_count, checkpoint_allowed_count,
                blocked_count, production_release_allowed, decision_counts_json,
                block_counts_json, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                RULE_VERSION,
                COMPOSER_NAME,
                COMPOSER_STATUS,
                source_run_id,
                len(items),
                allowed_count,
                len(items) - allowed_count,
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(report_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items (
                run_id, source_partial_run_id, source_partial_item_id, segment_id,
                relative_path, source_key, decision, checkpoint_allowed,
                segment_closure_candidate, current_text, corrected_text, block_reason,
                rationale, validation_issues_json, english_text, spanish_text,
                production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    item["source_partial_run_id"],
                    item["source_partial_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["decision"],
                    item["checkpoint_allowed"],
                    item["segment_closure_candidate"],
                    item["current_text"],
                    item["corrected_text"],
                    item["block_reason"],
                    item["rationale"],
                    json.dumps(item["validation_issues"], ensure_ascii=False, sort_keys=True),
                    item["english_text"],
                    item["spanish_text"],
                    created_at,
                )
                for item in items
            ],
        )
        conn.commit()

    write_reports(
        run_id=run_id,
        partial_run_id=source_run_id,
        items=items,
        report_path=report_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
    )
    return {
        "run_id": run_id,
        "source_partial_run_id": source_run_id,
        "candidate_count": len(items),
        "checkpoint_allowed_count": allowed_count,
        "blocked_count": len(items) - allowed_count,
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decision_counts": dict(decision_counts),
        "block_counts": dict(block_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Checkpoint Select_CString passive confiscation sentence composer.")
    parser.add_argument("--partial-run-id", type=int, default=None)
    args = parser.parse_args()
    print(json.dumps(build_checkpoint(args.partial_run_id), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
