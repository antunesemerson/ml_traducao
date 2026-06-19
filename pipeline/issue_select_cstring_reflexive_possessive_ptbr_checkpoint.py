from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_reflexive_possessive_ptbr_checkpoint_v1"
PRODUCTION_RELEASE_ALLOWED = 0
TARGET_AGENTS = {
    "select_cstring_local_player_reflexive_phrase_rewrite",
    "select_cstring_local_player_possessive_pronoun_rewrite",
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_audit_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    matches = sorted(reports_dir.glob("*_issue_dynamic_select_cstring_literal_subtype_audit.csv"))
    if not matches:
        raise SystemExit("No dynamic Select_CString literal subtype audit CSV found.")
    return matches[-1]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            source_audit_csv TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            microagent_ready_count INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            target_agent_counts_json TEXT NOT NULL,
            decision_counts_json TEXT NOT NULL,
            block_counts_json TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            queue_item_id INTEGER,
            ledger_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            condition_family TEXT,
            condition TEXT,
            left_literal TEXT,
            right_literal TEXT,
            literal_subtype TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            decision TEXT NOT NULL,
            microagent_ready INTEGER NOT NULL DEFAULT 0,
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
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_reflexive_possessive_items_run
        ON ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_reflexive_possessive_items_segment
        ON ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_items(segment_id)
        """
    )
    conn.commit()


def load_audit_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [row for row in rows if row.get("suggested_microagent") in TARGET_AGENTS]


def fetch_context(conn, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS current_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        segment_ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def int_or_none(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(float(str(value).strip()))


def classify(row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    source_key = row.get("source_key", "")
    left = (row.get("left_literal") or "").strip()
    right = (row.get("right_literal") or "").strip()
    subtype = row.get("literal_subtype") or ""
    target_agent = row.get("suggested_microagent") or ""
    current_text = context.get("current_text") or ""

    decision = "blocked_unknown_pattern"
    microagent_ready = 0
    segment_closure_candidate = 0
    proposed_left = ""
    proposed_right = ""
    block_reason = "unknown_pattern"
    rationale = "No strict PT-BR mapping is registered for this literal pair."

    if subtype == "reflexive_phrase":
        if left == "te apaixonaste" and right == "se apaixonou":
            decision = "ready_exact_reflexive_phrase"
            microagent_ready = 1
            segment_closure_candidate = 1
            proposed_left = "se apaixonou"
            proposed_right = "se apaixonou"
            block_reason = ""
            rationale = "Both branches become the same natural PT-BR reflexive phrase."
        elif left == "te convertesses" and right == "se convertesse":
            decision = "ready_partial_reflexive_phrase"
            microagent_ready = 1
            proposed_left = "se convertesse"
            proposed_right = "se convertesse"
            block_reason = "segment_has_additional_dynamic_possessive_or_verb_issues"
            rationale = "The reflexive phrase is reusable, but the segment still needs other dynamic rewrites."
        elif left == "te presentaste" and right == "se presentó":
            decision = "ready_partial_reflexive_phrase"
            microagent_ready = 1
            proposed_left = "se apresentou"
            proposed_right = "se apresentou"
            block_reason = "segment_has_additional_preterite_or_auxiliary_issues"
            rationale = "The reflexive phrase is reusable, but the segment has other Spanish dynamic literals."
        elif left == "te maravillaste" and right == "se maravilló":
            decision = "blocked_sentence_composer_required"
            block_reason = "trailing_ptbr_reflexive_duplicate"
            proposed_left = "se maravilhou"
            proposed_right = "se maravilhou"
            rationale = "The current text already has a trailing PT-BR reflexive phrase, so a literal-only rewrite would duplicate it."
        elif left == "te conviertes" and right == "se convierte":
            decision = "blocked_sentence_composer_required"
            block_reason = "existing_ptbr_predicate_after_select"
            proposed_left = "se torna"
            proposed_right = "se torna"
            rationale = "The current text already has the PT-BR predicate after the Select_CString token."
        elif left == "te labraste" and right == "se labró":
            decision = "blocked_semantic_context_required"
            block_reason = "idiomatic_carve_out_domain_requires_context_rewrite"
            rationale = "The Spanish idiom needs a full contextual rewrite, not a generic reflexive replacement."
    elif subtype == "possessive_pronoun_plural":
        if source_key == "thievery_helper_opinion":
            decision = "ready_partial_possessive_pronoun"
            microagent_ready = 1
            proposed_left = "suas"
            proposed_right = "suas"
            block_reason = "segment_has_additional_dynamic_verb_issues"
            rationale = "The following noun is feminine plural, but the segment has other unresolved dynamic Spanish literals."
        elif source_key == "replace_ceremonial_regent_faction_war_victory_desc":
            decision = "ready_partial_possessive_pronoun"
            microagent_ready = 1
            proposed_left = "suas"
            proposed_right = "suas"
            block_reason = "segment_has_additional_future_or_agent_issues"
            rationale = "The following noun is feminine plural, but the segment still needs future-tense and agent rewrites."
        elif source_key == "nick_culture_khagan_desc":
            decision = "blocked_gender_context_required"
            block_reason = "collective_noun_gender_unknown"
            rationale = "The next localization can require 'seus' or 'suas'; this needs a gender/context composer."

    if decision == "blocked_sentence_composer_required" and "Select_CString" not in current_text:
        block_reason = "current_text_context_missing_select_cstring"

    return {
        "queue_item_id": int_or_none(row.get("queue_item_id")),
        "ledger_item_id": int_or_none(row.get("ledger_item_id")),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": source_key,
        "condition_family": row.get("condition_family") or "",
        "condition": row.get("condition") or "",
        "left_literal": left,
        "right_literal": right,
        "literal_subtype": subtype,
        "target_agent": target_agent,
        "decision": decision,
        "microagent_ready": microagent_ready,
        "segment_closure_candidate": segment_closure_candidate,
        "proposed_left_literal": proposed_left,
        "proposed_right_literal": proposed_right,
        "block_reason": block_reason,
        "rationale": rationale,
        "english_text": context.get("english_text") or "",
        "spanish_text": context.get("spanish_text") or "",
        "current_text": current_text,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_select_cstring_reflexive_possessive_ptbr_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    run_id: int,
    audit_csv: Path,
    items: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
) -> None:
    target_counts = Counter(item["target_agent"] for item in items)
    decision_counts = Counter(item["decision"] for item in items)
    block_counts = Counter(item["block_reason"] or "allowed" for item in items)
    subtype_counts = Counter(item["literal_subtype"] for item in items)
    ready_count = sum(1 for item in items if item["microagent_ready"])
    closure_count = sum(1 for item in items if item["segment_closure_candidate"])
    blocked_count = len(items) - ready_count

    lines = [
        "Select_CString reflexive/possessive PT-BR checkpoint",
        f"Run: {run_id}",
        f"Rule version: {RULE_VERSION}",
        f"Source audit CSV: {audit_csv}",
        "",
        "Summary",
        f"- candidates: {len(items)}",
        f"- microagent_ready: {ready_count}",
        f"- segment_closure_candidates: {closure_count}",
        f"- blocked_for_now: {blocked_count}",
        f"- production_release_allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "By subtype",
        *[f"- {key}: {value}" for key, value in sorted(subtype_counts.items())],
        "",
        "By target agent",
        *[f"- {key}: {value}" for key, value in sorted(target_counts.items())],
        "",
        "By decision",
        *[f"- {key}: {value}" for key, value in sorted(decision_counts.items())],
        "",
        "By block reason",
        *[f"- {key}: {value}" for key, value in sorted(block_counts.items())],
        "",
        "Items",
    ]
    for item in items:
        lines.append(
            "- segment_id={segment_id} key={source_key} subtype={literal_subtype} "
            "decision={decision} ready={microagent_ready} closure={segment_closure_candidate} "
            "left={left_literal!r} right={right_literal!r} proposed=({proposed_left_literal!r}, {proposed_right_literal!r}) "
            "block={block_reason}".format(**item)
        )
        lines.append(f"  rationale: {item['rationale']}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "condition_family",
        "condition",
        "left_literal",
        "right_literal",
        "literal_subtype",
        "target_agent",
        "decision",
        "microagent_ready",
        "segment_closure_candidate",
        "proposed_left_literal",
        "proposed_right_literal",
        "block_reason",
        "rationale",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow({"run_id": run_id, **{field: item.get(field) for field in fieldnames if field != "run_id"}})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps({"run_id": run_id, **item}, ensure_ascii=False, sort_keys=True) + "\n")


def build_checkpoint(audit_csv: Path | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    source_audit_csv = audit_csv or latest_audit_csv(settings)
    source_audit_csv = source_audit_csv if source_audit_csv.is_absolute() else db.PROJECT_ROOT / source_audit_csv
    rows = load_audit_rows(source_audit_csv)
    created_at = datetime.now().isoformat(timespec="seconds")
    report_path, csv_path, jsonl_path = report_paths(settings)

    with db.connect(settings) as conn:
        ensure_tables(conn)
        context_by_segment = fetch_context(conn, sorted({int(row["segment_id"]) for row in rows}))
        items = [classify(row, context_by_segment.get(int(row["segment_id"]), {})) for row in rows]
        target_counts = Counter(item["target_agent"] for item in items)
        decision_counts = Counter(item["decision"] for item in items)
        block_counts = Counter(item["block_reason"] or "allowed" for item in items)
        ready_count = sum(1 for item in items if item["microagent_ready"])
        closure_count = sum(1 for item in items if item["segment_closure_candidate"])
        blocked_count = len(items) - ready_count

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_runs (
                created_at, rule_version, source_audit_csv, candidate_count,
                microagent_ready_count, segment_closure_candidate_count, blocked_count,
                target_agent_counts_json, decision_counts_json, block_counts_json,
                production_release_allowed, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                created_at,
                RULE_VERSION,
                str(source_audit_csv),
                len(items),
                ready_count,
                closure_count,
                blocked_count,
                json.dumps(dict(target_counts), ensure_ascii=False, sort_keys=True),
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
            INSERT INTO ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_items (
                run_id, queue_item_id, ledger_item_id, segment_id, relative_path,
                source_key, condition_family, condition, left_literal, right_literal,
                literal_subtype, target_agent, decision, microagent_ready,
                segment_closure_candidate, proposed_left_literal, proposed_right_literal,
                block_reason, rationale, english_text, spanish_text, current_text,
                production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    item["queue_item_id"],
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["condition_family"],
                    item["condition"],
                    item["left_literal"],
                    item["right_literal"],
                    item["literal_subtype"],
                    item["target_agent"],
                    item["decision"],
                    item["microagent_ready"],
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
        audit_csv=source_audit_csv,
        items=items,
        report_path=report_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
    )
    return {
        "run_id": run_id,
        "candidate_count": len(items),
        "microagent_ready_count": ready_count,
        "segment_closure_candidate_count": closure_count,
        "blocked_count": blocked_count,
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decision_counts": dict(decision_counts),
        "block_counts": dict(block_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-csv", type=Path, help="Dynamic Select_CString literal subtype audit CSV.")
    args = parser.parse_args()
    result = build_checkpoint(args.audit_csv)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
