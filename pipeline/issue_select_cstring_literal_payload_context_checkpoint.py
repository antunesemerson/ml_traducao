from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_literal_payload_context_checkpoint_v1"
TARGET_AGENT = "select_cstring_literal_payload_context_review"
CHECKPOINT_NAME = "select_cstring_literal_payload_context_checkpoint_v1"
CHECKPOINT_STATUS = "shadow_learning_only"
PRODUCTION_RELEASE_ALLOWED = 0


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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_literal_payload_context_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_literal_payload_context_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_partial_run_id INTEGER NOT NULL,
            source_partial_piece_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            queue_item_id INTEGER,
            ledger_item_id INTEGER,
            literal_subtype TEXT,
            target_agent TEXT NOT NULL,
            left_literal TEXT,
            right_literal TEXT,
            decision TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate INTEGER NOT NULL DEFAULT 0,
            proposed_left_literal TEXT,
            proposed_right_literal TEXT,
            block_reason TEXT,
            rationale TEXT,
            english_text TEXT,
            spanish_text TEXT,
            current_text TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_literal_payload_context_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_literal_payload_context_items_run
        ON ml_issue_select_cstring_literal_payload_context_checkpoint_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_literal_payload_context_items_segment
        ON ml_issue_select_cstring_literal_payload_context_checkpoint_items(segment_id)
        """
    )
    conn.commit()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_select_cstring_literal_payload_context_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, partial_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            piece.id AS source_partial_piece_id,
            piece.run_id AS source_partial_run_id,
            piece.segment_id,
            piece.relative_path,
            piece.source_key,
            piece.queue_item_id,
            piece.ledger_item_id,
            piece.literal_subtype,
            piece.suggested_microagent,
            piece.left_literal,
            piece.right_literal,
            src.english_text,
            src.spanish_text,
            out.portuguese_text AS current_text
        FROM ml_issue_select_cstring_partial_composition_checkpoint_pieces piece
        LEFT JOIN source_segments src ON src.id = piece.segment_id
        LEFT JOIN output_segments out ON out.segment_id = piece.segment_id
        WHERE piece.run_id = ?
          AND piece.suggested_microagent = ?
        ORDER BY piece.segment_id, piece.id
        """,
        (partial_run_id, TARGET_AGENT),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    left = row.get("left_literal") or ""
    right = row.get("right_literal") or ""
    decision = "blocked_context_composer_required"
    allowed = 0
    closure = 0
    proposed_left = ""
    proposed_right = ""
    block_reason = "unmapped_literal_payload_context"
    rationale = "No strict context rule was registered for this literal pair."

    if segment_id == 4146 and left == "recorrías" and right == "recorría":
        decision = "ready_partial_traversing_context_literal"
        allowed = 1
        proposed_left = "percorria"
        proposed_right = "percorria"
        block_reason = "segment_still_has_reflexive_duplicate_context"
        rationale = "The traversal literal can be neutralized to PT-BR 'percorria', but another Select_CString in the same sentence still needs a sentence-level rewrite."
    elif segment_id == 229121 and left == "vistes" and right == "viste":
        decision = "ready_partial_wear_context_literal"
        allowed = 1
        proposed_left = "usava"
        proposed_right = "usava"
        block_reason = "segment_still_has_pronoun_and_preterite_context"
        rationale = "The clothing verb can be neutralized to PT-BR 'usava', but the nickname sentence still has other dynamic literals."
    elif segment_id == 78599 and left == "teu" and right == "seu":
        decision = "blocked_semantic_possessive_requires_explicit_target"
        block_reason = "tributary_possessive_requires_target_context"
        rationale = "The possessive refers to the intimidator, not safely to the localized subject; a sentence composer should rewrite the relation explicitly."
    elif segment_id == 229909 and left == "habías" and right == "había":
        decision = "blocked_auxiliary_plus_following_verb_requires_sentence_rewrite"
        block_reason = "auxiliary_would_duplicate_or_break_following_ptbr_verb"
        rationale = "The current PT-BR tail already uses 'fez'; replacing only the auxiliary would produce an ungrammatical phrase."
    elif segment_id == 230381 and left == "fueras" and right == "fuera":
        decision = "blocked_duplicate_subjunctive_requires_sentence_rewrite"
        block_reason = "select_literal_duplicates_existing_ptbr_subjunctive"
        rationale = "The current text already has 'fosse' after the Select_CString; this needs phrase-level cleanup."

    return {
        "source_partial_run_id": int(row["source_partial_run_id"]),
        "source_partial_piece_id": int(row["source_partial_piece_id"]),
        "segment_id": segment_id,
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "queue_item_id": row.get("queue_item_id"),
        "ledger_item_id": row.get("ledger_item_id"),
        "literal_subtype": row.get("literal_subtype") or "",
        "target_agent": TARGET_AGENT,
        "left_literal": left,
        "right_literal": right,
        "decision": decision,
        "checkpoint_allowed": allowed,
        "segment_closure_candidate": closure,
        "proposed_left_literal": proposed_left,
        "proposed_right_literal": proposed_right,
        "block_reason": block_reason,
        "rationale": rationale,
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "current_text": row.get("current_text") or "",
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
        "Select_CString literal payload context checkpoint",
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
            "left={left_literal!r} right={right_literal!r} proposed=({proposed_left_literal!r}, {proposed_right_literal!r}) "
            "block={block_reason}".format(**item)
        )
        lines.append(f"  rationale: {item['rationale']}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fields = [
        "run_id",
        "source_partial_run_id",
        "source_partial_piece_id",
        "segment_id",
        "relative_path",
        "source_key",
        "literal_subtype",
        "target_agent",
        "left_literal",
        "right_literal",
        "decision",
        "checkpoint_allowed",
        "segment_closure_candidate",
        "proposed_left_literal",
        "proposed_right_literal",
        "block_reason",
        "rationale",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({"run_id": run_id, **{field: item.get(field) for field in fields if field != "run_id"}})
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
        source_rows = fetch_rows(conn, source_run_id)
        items = [classify(row) for row in source_rows]
        decision_counts = Counter(item["decision"] for item in items)
        block_counts = Counter(item["block_reason"] or "allowed" for item in items)
        allowed_count = sum(item["checkpoint_allowed"] for item in items)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_literal_payload_context_checkpoint_runs (
                created_at, rule_version, checkpoint_name, checkpoint_status,
                source_partial_run_id, candidate_count, checkpoint_allowed_count,
                blocked_count, production_release_allowed, decision_counts_json,
                block_counts_json, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                RULE_VERSION,
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS,
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
            INSERT INTO ml_issue_select_cstring_literal_payload_context_checkpoint_items (
                run_id, source_partial_run_id, source_partial_piece_id, segment_id,
                relative_path, source_key, queue_item_id, ledger_item_id,
                literal_subtype, target_agent, left_literal, right_literal,
                decision, checkpoint_allowed, segment_closure_candidate,
                proposed_left_literal, proposed_right_literal, block_reason,
                rationale, english_text, spanish_text, current_text,
                production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    item["source_partial_run_id"],
                    item["source_partial_piece_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["queue_item_id"],
                    item["ledger_item_id"],
                    item["literal_subtype"],
                    item["target_agent"],
                    item["left_literal"],
                    item["right_literal"],
                    item["decision"],
                    item["checkpoint_allowed"],
                    item["segment_closure_candidate"],
                    item["proposed_left_literal"],
                    item["proposed_right_literal"],
                    item["block_reason"],
                    item["rationale"],
                    item["english_text"],
                    item["spanish_text"],
                    item["current_text"],
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
    parser = argparse.ArgumentParser(description="Checkpoint Select_CString literal payload context review pieces.")
    parser.add_argument("--partial-run-id", type=int, default=None)
    args = parser.parse_args()
    print(json.dumps(build_checkpoint(args.partial_run_id), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
