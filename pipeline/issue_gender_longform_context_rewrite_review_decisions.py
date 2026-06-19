from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from local_quality_validator import validate_text


RULE_VERSION = "issue_gender_longform_context_rewrite_review_decisions_v1"
QUEUE_RULE_VERSION = "issue_gender_longform_context_rewrite_review_queue_v1"
REVIEWER = "learning_front_codex_context_review"
APPROVED_SEGMENTS = {76454, 159267, 160285, 160412}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_longform_context_rewrite_review_decisions_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_longform_context_rewrite_review_queue_runs
        WHERE rule_version = ?
          AND candidate_count = 4
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (QUEUE_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No 4-item gender longform context rewrite review queue found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_rewrite_review_decision_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            approved_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            token_delta_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            reviewer TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_rewrite_review_decision_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            decision_status TEXT NOT NULL,
            decision_action TEXT NOT NULL,
            block_reason TEXT,
            route_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            extraction_strategy TEXT NOT NULL,
            old_fragment TEXT NOT NULL,
            approved_fragment TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT NOT NULL,
            english_text TEXT,
            spanish_text TEXT,
            token_delta_status TEXT NOT NULL,
            token_delta_json TEXT,
            validator_issue_count INTEGER NOT NULL DEFAULT 0,
            validator_high_count INTEGER NOT NULL DEFAULT 0,
            validator_issues_json TEXT,
            reviewer_notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_longform_context_rewrite_review_decision_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_queue_items(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            queue.*,
            output.portuguese_text AS live_output_text,
            state.final_state AS live_final_state,
            COALESCE(state.needs_output_apply, 0) AS live_needs_output_apply,
            COALESCE(state.confirmed_matches_output, 0) AS live_confirmed_matches_output
        FROM ml_issue_gender_longform_context_rewrite_review_queue_items queue
        LEFT JOIN output_segments output
          ON output.segment_id = queue.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = queue.segment_id
         AND state.run_id = (
             SELECT MAX(id)
             FROM segment_state_runs
             WHERE total_segments > 1000
         )
        WHERE queue.run_id = ?
        ORDER BY queue.segment_id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def reviewed_payload(segment_id: int, row: dict[str, Any]) -> tuple[str, str, str, str]:
    current = as_text(row["current_text"])
    if segment_id == 76454:
        return (
            "queue_old_fragment",
            as_text(row["old_fragment"]),
            "Fica claro que est[examiner_to_bribe.Custom('ES_EA')] [examiner_to_bribe.GetWomanMan] "
            "est\u00e1 envolvid[examiner_to_bribe.Custom('ES_OA')] em arranjos #EMP lucrativos#!\u2026",
            "Approved context repair: adds missing ES_OA agreement and restores the complement for profitable arrangements.",
        )
    if segment_id == 159267:
        return (
            "queue_old_fragment",
            as_text(row["old_fragment"]),
            "quer ver [Select_CString( target.IsFemale, 'esta mulher morta', 'este homem morto' )]",
            "Approved context repair: moves the adjective inside Select_CString to avoid malformed word order.",
        )
    if segment_id == 160285:
        old = current.split("\n\n", 1)[0]
        return (
            "first_paragraph",
            old,
            '"O qu\u00ea... como... voc\u00eas claramente inventaram um absurdo #EMP completo#!. '
            "O que lhes deu na cabe\u00e7a para achar que poderiam tirar dinheiro disso, "
            "loc[ROOT.Char.Custom('ES_OA')]?\"",
            "Approved edited rewrite: removes Spanish quotes/mojibake, avoids duplicated adverb, and keeps emphasis token.",
        )
    if segment_id == 160412:
        start = current.find("\u00ab")
        if start < 0:
            raise RuntimeError("Could not find Spanish quote block in segment 160412.")
        old = current[start:]
        return (
            "from_first_spanish_quote_to_end",
            old,
            '"N\u00e3o era o que eu esperava como solu\u00e7\u00e3o", assente pensativamente '
            '[employer.GetFirstNameNoTooltip], "mas mal posso criticar o vosso esmero. '
            'Tem certeza de que [sacrifice.GetSheHe] ficar\u00e1 bem aqui?"\n\n'
            "Ergo uma sobrancelha.\n\n"
            '"Ah, claro, o que estou dizendo?", balbucia [employer.Custom(\'ES_ElLa\')] '
            '[employer.GetTitleAsNameNoTooltip|l], "Vou acabar me metendo no mesmo tipo de '
            "confus\u00e3o por falar demais. Aqui est\u00e1 o pagamento por nos cederem "
            "[Select_CString( sacrifice.IsFemale, 'uma mulher t\u00e3o h\u00e1bil', 'um homem t\u00e3o h\u00e1bil' )].\"",
            "Approved edited rewrite: removes Spanish quotes/mojibake, restores dynamic pronoun, and makes the final payment sentence natural.",
        )
    raise RuntimeError(f"Unexpected segment id for context rewrite review: {segment_id}")


def token_delta_status(old_text: str, new_text: str) -> tuple[str, dict[str, Any]]:
    old_tokens = protected_tokens(old_text)
    new_tokens = protected_tokens(new_text)
    added = +(new_tokens - old_tokens)
    removed = +(old_tokens - new_tokens)
    if not added and not removed:
        status = "same_protected_token_multiset"
    else:
        status = "reviewed_protected_token_delta"
    return status, {
        "added": dict(added),
        "removed": dict(removed),
        "old_count": sum(old_tokens.values()),
        "new_count": sum(new_tokens.values()),
    }


def classify(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    extraction_strategy, old_fragment, approved_fragment, notes = reviewed_payload(segment_id, row)
    current = as_text(row["current_text"])
    live_output = as_text(row.get("live_output_text"))
    block_reasons: list[str] = []

    if segment_id not in APPROVED_SEGMENTS:
        block_reasons.append("segment_not_in_review_allowlist")
    if live_output and live_output != current:
        block_reasons.append("live_output_differs_from_review_current")
    if old_fragment not in current:
        block_reasons.append("old_fragment_not_found")

    corrected_text = current.replace(old_fragment, approved_fragment, 1) if old_fragment in current else current
    validation = validate_text(corrected_text)
    issue_count = int(validation.get("issue_count") or 0)
    high_count = int(validation.get("high_count") or 0)
    blocking_issues = [
        issue
        for issue in validation.get("issues", [])
        if issue.get("severity") == "high"
        or issue.get("type") in {"spanish_punctuation", "question_mark_mojibake"}
    ]
    if blocking_issues:
        block_reasons.append("validator_blocking_issue")

    token_status, token_delta = token_delta_status(current, corrected_text)
    decision_status = "approved_context_rewrite" if not block_reasons else "blocked_context_rewrite"
    decision_action = "stage_gender_longform_context_rewrite_reviewed_apply" if not block_reasons else "preserve_for_manual_review"
    return {
        **row,
        "decision_status": decision_status,
        "decision_action": decision_action,
        "block_reason": ";".join(block_reasons),
        "extraction_strategy": extraction_strategy,
        "old_fragment": old_fragment,
        "approved_fragment": approved_fragment,
        "corrected_text": corrected_text,
        "token_delta_status": token_status,
        "token_delta_json": json.dumps(token_delta, ensure_ascii=False, sort_keys=True),
        "validator_issue_count": issue_count,
        "validator_high_count": high_count,
        "validator_issues_json": json.dumps(validation.get("issues", []), ensure_ascii=False, sort_keys=True),
        "reviewer_notes": notes,
    }


def short(value: str | None, limit: int = 240) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    queue_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "decision_status",
        "decision_action",
        "block_reason",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "route_key",
        "subpolicy_name",
        "extraction_strategy",
        "token_delta_status",
        "validator_issue_count",
        "validator_high_count",
        "old_fragment",
        "approved_fragment",
        "corrected_text",
        "english_text",
        "reviewer_notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["decision_status"] for row in rows)
    token_counts = Counter(row["token_delta_status"] for row in rows)
    lines = [
        "Issue gender longform context rewrite reviewed decisions",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Queue run id: {queue_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Approved: {status_counts['approved_context_rewrite']:,}",
        f"- Blocked: {status_counts['blocked_context_rewrite']:,}",
        "",
        "Token delta:",
        *[f"- {key}: {value:,}" for key, value in token_counts.most_common()],
        "",
        "Reviewed items:",
    ]
    for row in rows:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  status: {row['decision_status']} | action: {row['decision_action']}",
                f"  token_delta: {row['token_delta_status']} | validator_high={row['validator_high_count']}",
                f"  old: {short(row['old_fragment'])}",
                f"  approved: {short(row['approved_fragment'])}",
                f"  notes: {row['reviewer_notes']}",
            ]
        )
        if row["block_reason"]:
            lines.append(f"  block_reason: {row['block_reason']}")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Decision artifact only: no production run, no confirmations, no source/output writes.",
            "- Apply must be performed by the production front with exact checkpoint validation.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        raw_rows = fetch_queue_items(conn, selected_queue_run_id)
        if {int(row["segment_id"]) for row in raw_rows} != APPROVED_SEGMENTS:
            raise RuntimeError("Review queue segment set does not match the expected 4 context rewrites.")
        rows = [classify(row) for row in raw_rows]
        status_counts = Counter(row["decision_status"] for row in rows)
        token_counts = Counter(row["token_delta_status"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
        now = db.utc_now()
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_longform_context_rewrite_review_decision_runs (
                rule_version,
                queue_run_id,
                candidate_count,
                approved_count,
                blocked_count,
                status_counts_json,
                token_delta_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                reviewer,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_queue_run_id,
                len(rows),
                status_counts["approved_context_rewrite"],
                status_counts["blocked_context_rewrite"],
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(token_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                REVIEWER,
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_gender_longform_context_rewrite_review_decision_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    decision_status,
                    decision_action,
                    block_reason,
                    route_key,
                    subpolicy_name,
                    extraction_strategy,
                    old_fragment,
                    approved_fragment,
                    current_text,
                    corrected_text,
                    english_text,
                    spanish_text,
                    token_delta_status,
                    token_delta_json,
                    validator_issue_count,
                    validator_high_count,
                    validator_issues_json,
                    reviewer_notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_queue_run_id,
                    row["id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["decision_status"],
                    row["decision_action"],
                    row["block_reason"],
                    row["route_key"],
                    row["subpolicy_name"],
                    row["extraction_strategy"],
                    row["old_fragment"],
                    row["approved_fragment"],
                    row["current_text"],
                    row["corrected_text"],
                    row["english_text"],
                    row["spanish_text"],
                    row["token_delta_status"],
                    row["token_delta_json"],
                    row["validator_issue_count"],
                    row["validator_high_count"],
                    row["validator_issues_json"],
                    row["reviewer_notes"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            queue_run_id=selected_queue_run_id,
            rows=rows,
        )
        conn.commit()

    print(f"Gender longform context rewrite review decisions: {run_id}")
    print(f"Queue run id: {selected_queue_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Approved: {status_counts['approved_context_rewrite']}")
    print(f"Blocked: {status_counts['blocked_context_rewrite']}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "queue_run_id": selected_queue_run_id,
        "candidate_count": len(rows),
        "approved_count": status_counts["approved_context_rewrite"],
        "blocked_count": status_counts["blocked_context_rewrite"],
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record reviewed decisions for gender longform context rewrites.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(queue_run_id=args.queue_run_id)
