from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_context_microagent_diagnostic_v1"
RUN_STATUS = "read_only_context_diagnostic"
PRODUCTION_RELEASE_ALLOWED = 0


PAIR_CATEGORY: dict[tuple[str, str], tuple[str, str, str]] = {
    ("tus", "sus"): ("possessive_pronoun_context", "micro_select_cstring_possessive_context", "infer_ptbr_seu_sua_seus_suas_from_following_noun"),
    ("Tus", "Sus"): ("possessive_pronoun_context", "micro_select_cstring_possessive_context", "infer_ptbr_seu_sua_seus_suas_from_following_noun"),
    ("has", "ha"): ("auxiliary_perfect_context", "micro_select_cstring_auxiliary_context", "rewrite_auxiliary_plus_participle_to_ptbr_simple_past_or_present"),
    ("has", "han"): ("auxiliary_perfect_context", "micro_select_cstring_auxiliary_context", "rewrite_auxiliary_plus_participle_to_ptbr_simple_past_or_present"),
    ("habéis", "han"): ("auxiliary_perfect_context", "micro_select_cstring_auxiliary_context", "rewrite_auxiliary_plus_participle_to_ptbr_simple_past_or_present"),
    ("te", "le"): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "choose_lhe_se_omit_or_explicit_target_from_sentence_role"),
    ("te", "se"): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "choose_lhe_se_omit_or_explicit_target_from_sentence_role"),
    ("te", ""): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "decide_if_ptbr_pronoun_should_be_omitted"),
    (" te", ""): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "decide_if_ptbr_pronoun_should_be_omitted"),
    ("te ", " a "): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "repair_object_preposition_boundary"),
    ("me", "le"): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "choose_lhe_me_or_explicit_target_from_sentence_role"),
    ("le", "te"): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "choose_lhe_te_or_explicit_target_from_sentence_role"),
    ("os", "les"): ("object_pronoun_context", "micro_select_cstring_object_pronoun_context", "choose_lhes_or_omit_from_sentence_role"),
    ("diste", "dio"): ("preterite_verb_context", "micro_select_cstring_preterite_context", "rewrite_spanish_preterite_to_single_ptbr_past_verb_without_duplicate_context"),
    ("ganaste", "ganó"): ("preterite_verb_context", "micro_select_cstring_preterite_context", "rewrite_spanish_preterite_to_single_ptbr_past_verb_without_duplicate_context"),
    ("te negaste", "se negó"): ("reflexive_verb_context", "micro_select_cstring_reflexive_context", "rewrite_reflexive_phrase_to_ptbr_sentence_level"),
    ("te convertiste", "se convirtió"): ("reflexive_verb_context", "micro_select_cstring_reflexive_context", "rewrite_reflexive_phrase_to_ptbr_sentence_level"),
    ("te has", "se ha"): ("reflexive_auxiliary_context", "micro_select_cstring_reflexive_context", "rewrite_reflexive_auxiliary_phrase_to_ptbr_sentence_level"),
    ("sigues", "sigue"): ("continuative_verb_context", "micro_select_cstring_continuative_context", "rewrite_continues_still_as_ptbr_sentence_level"),
    ("hija mía", "hijo mío"): ("kinship_address_context", "micro_kinship_address_context", "avoid_meu_minha_when_gender_or_speaker_context_is_unsafe"),
    ("Hija mía", "Hijo mío"): ("kinship_address_context", "micro_kinship_address_context", "avoid_meu_minha_when_gender_or_speaker_context_is_unsafe"),
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def short(value: str | None, limit: int = 220) -> str:
    text = value or ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_context_microagent_diagnostic_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_queue_run_id INTEGER NOT NULL,
            rule_version TEXT NOT NULL,
            run_status TEXT NOT NULL,
            candidate_observations INTEGER NOT NULL DEFAULT 0,
            candidate_segments INTEGER NOT NULL DEFAULT 0,
            category_counts_json TEXT,
            microagent_counts_json TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_context_microagent_diagnostic_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_queue_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            select_index INTEGER NOT NULL,
            left_literal TEXT,
            right_literal TEXT,
            context_category TEXT NOT NULL,
            recommended_microagent TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            current_select_text TEXT,
            current_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_residual_literal_queue_runs
        WHERE production_release_allowed = 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("No Select_CString residual literal queue run found.")
    return int(row["id"] if hasattr(row, "keys") else row[0])


def classify_pair(left: str | None, right: str | None, relative_path: str, source_key: str) -> tuple[str, str, str, str]:
    pair = (left or "", right or "")
    if pair in PAIR_CATEGORY:
        category, microagent, action = PAIR_CATEGORY[pair]
    elif "nicknames" in relative_path:
        category = "nickname_select_cstring_context"
        microagent = "micro_nickname_select_cstring_context"
        action = "rewrite_nickname_description_dynamic_phrase_with_character_context"
    elif "interaction" in relative_path or "interaction" in source_key:
        category = "interaction_select_cstring_context"
        microagent = "micro_interaction_select_cstring_context"
        action = "rewrite_interaction_requirement_or_tooltip_phrase_with_actor_recipient_roles"
    else:
        category = "unmapped_select_cstring_context"
        microagent = "micro_select_cstring_context_router"
        action = "manual_cluster_or_new_context_rule_required"

    high_risk_categories = {
        "object_pronoun_context",
        "reflexive_verb_context",
        "reflexive_auxiliary_context",
        "kinship_address_context",
        "unmapped_select_cstring_context",
    }
    risk = "high" if category in high_risk_categories else "medium"
    return category, microagent, action, risk


def fetch_rows(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute(
        """
        SELECT
            run_id,
            segment_state_run_id,
            ledger_run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            select_index,
            left_literal,
            right_literal,
            current_select_text,
            current_text,
            english_text,
            spanish_text
        FROM ml_issue_select_cstring_residual_literal_queue_items
        WHERE run_id = ?
          AND literal_status = 'needs_context_microagent'
        ORDER BY relative_path, source_key, select_index, segment_id
        """,
        (queue_run_id,),
    ):
        item = dict(row)
        category, microagent, action, risk = classify_pair(
            item.get("left_literal"),
            item.get("right_literal"),
            item.get("relative_path", ""),
            item.get("source_key", ""),
        )
        item.update(
            {
                "context_category": category,
                "recommended_microagent": microagent,
                "recommended_action": action,
                "risk_level": risk,
            }
        )
        rows.append(item)
    return rows


def report_paths(queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    stamp = now_stamp()
    stem = f"{stamp}_issue_select_cstring_context_microagent_diagnostic_queue_{queue_run_id}"
    return reports_dir / f"{stem}.txt", reports_dir / f"{stem}.csv", reports_dir / f"{stem}.jsonl"


def write_reports(rows: list[dict[str, Any]], txt_path: Path, csv_path: Path, jsonl_path: Path, queue_run_id: int) -> None:
    category_counts = Counter(row["context_category"] for row in rows)
    microagent_counts = Counter(row["recommended_microagent"] for row in rows)
    pair_counts = Counter((row.get("left_literal") or "", row.get("right_literal") or "") for row in rows)

    lines = [
        "Issue Select_CString context microagent diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Source queue run id: {queue_run_id}",
        "",
        "Summary:",
        f"- Candidate observations: {len(rows):,}",
        f"- Candidate segments: {len({row['segment_id'] for row in rows}):,}",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Categories:",
    ]
    for category, count in category_counts.most_common():
        lines.append(f"- {category}: {count:,}")

    lines.extend(["", "Recommended microagents:"])
    for microagent, count in microagent_counts.most_common():
        lines.append(f"- {microagent}: {count:,}")

    lines.extend(["", "Top literal pairs:"])
    for (left, right), count in pair_counts.most_common(30):
        lines.append(f"- {left!r} -> {right!r}: {count:,}")

    lines.extend(["", "Samples:"])
    for row in rows[:30]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"category={row['context_category']} microagent={row['recommended_microagent']}"
        )
        lines.append(f"  select: {short(row.get('current_select_text'), 180)}")
        lines.append(f"  text: {short(row.get('current_text'), 260)}")

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "select_index",
        "left_literal",
        "right_literal",
        "context_category",
        "recommended_microagent",
        "recommended_action",
        "risk_level",
        "current_select_text",
        "current_text",
        "english_text",
        "spanish_text",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    conn = db.connect()
    conn.row_factory = db.sqlite3.Row
    try:
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        rows = fetch_rows(conn, selected_queue_run_id)
        txt_path, csv_path, jsonl_path = report_paths(selected_queue_run_id)
        write_reports(rows, txt_path, csv_path, jsonl_path, selected_queue_run_id)

        category_counts = Counter(row["context_category"] for row in rows)
        microagent_counts = Counter(row["recommended_microagent"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")

        cur = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_context_microagent_diagnostic_runs (
                source_queue_run_id,
                rule_version,
                run_status,
                candidate_observations,
                candidate_segments,
                category_counts_json,
                microagent_counts_json,
                production_release_allowed,
                report_path,
                csv_path,
                jsonl_path,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                selected_queue_run_id,
                RULE_VERSION,
                RUN_STATUS,
                len(rows),
                len({row["segment_id"] for row in rows}),
                json.dumps(dict(category_counts), ensure_ascii=False),
                json.dumps(dict(microagent_counts), ensure_ascii=False),
                PRODUCTION_RELEASE_ALLOWED,
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                now,
            ),
        )
        run_id = int(cur.lastrowid)

        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_context_microagent_diagnostic_items (
                    run_id,
                    source_queue_run_id,
                    segment_state_run_id,
                    ledger_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    select_index,
                    left_literal,
                    right_literal,
                    context_category,
                    recommended_microagent,
                    recommended_action,
                    risk_level,
                    current_select_text,
                    current_text,
                    english_text,
                    spanish_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_queue_run_id,
                    row["segment_state_run_id"],
                    row["ledger_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["select_index"],
                    row["left_literal"],
                    row["right_literal"],
                    row["context_category"],
                    row["recommended_microagent"],
                    row["recommended_action"],
                    row["risk_level"],
                    row["current_select_text"],
                    row["current_text"],
                    row["english_text"],
                    row["spanish_text"],
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    print("[issue_select_cstring_context_microagent_diagnostic] Diagnostic generated")
    print(f"[issue_select_cstring_context_microagent_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_context_microagent_diagnostic] Run id: {run_id}")
    print(f"[issue_select_cstring_context_microagent_diagnostic] Source queue run id: {selected_queue_run_id}")
    print(f"[issue_select_cstring_context_microagent_diagnostic] Candidate observations: {len(rows):,}")
    print(f"[issue_select_cstring_context_microagent_diagnostic] Candidate segments: {len({row['segment_id'] for row in rows}):,}")
    print("[issue_select_cstring_context_microagent_diagnostic] Apply allowed: 0")
    print(f"[issue_select_cstring_context_microagent_diagnostic] Report: {txt_path}")
    print(f"[issue_select_cstring_context_microagent_diagnostic] CSV: {csv_path}")
    print(f"[issue_select_cstring_context_microagent_diagnostic] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "source_queue_run_id": selected_queue_run_id,
        "candidate_observations": len(rows),
        "candidate_segments": len({row["segment_id"] for row in rows}),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose context microagents needed for Select_CString residual literal cases.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
