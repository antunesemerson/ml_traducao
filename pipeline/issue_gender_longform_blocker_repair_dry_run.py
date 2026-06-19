from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_gender_longform_blocker_repair_dry_run_v1"
SOURCE_CHECKPOINT_RULE_VERSION = "issue_gender_longform_false_reopen_checkpoint_v1"
READY_STATUSES = {
    "ready_exact_lexical_microfix",
    "ready_kinship_select_cstring_repair",
    "ready_context_false_reopen_noop",
}


PROPOSALS: dict[int, dict[str, str]] = {
    39407: {
        "status": "ready_exact_lexical_microfix",
        "route": "demonstrative_article_gender_boundary_context",
        "subpolicy": "longform_exact_spanish_lexical_microfix",
        "old": "#EMP Podría#!",
        "new": "#EMP Poderia#!",
        "note": "Spanish conditional residue inside emphasized text.",
    },
    76454: {
        "status": "candidate_context_rewrite_requires_review",
        "route": "demonstrative_article_gender_boundary_context",
        "subpolicy": "longform_demonstrative_article_context_rewrite",
        "old": "Fica claro que est[examiner_to_bribe.Custom('ES_EA')] [examiner_to_bribe.GetWomanMan] está envolvid #EMP lucrativo#!…",
        "new": "Fica claro que est[examiner_to_bribe.Custom('ES_EA')] [examiner_to_bribe.GetWomanMan] está envolvid[examiner_to_bribe.Custom('ES_OA')] em arranjos #EMP lucrativos#!…",
        "note": "Needs context rewrite because the current text is missing the gender suffix and object complement.",
    },
    159028: {
        "status": "ready_kinship_select_cstring_repair",
        "route": "kinship_gender_suffix_boundary_repair",
        "subpolicy": "longform_kinship_select_cstring_boundary_repair",
        "old": "meu avô[ROOT.Char.Custom('ES_OA')]",
        "new": "[Select_CString( ROOT.Char.IsFemale, 'minha avó', 'meu avô' )]",
        "note": "Portuguese avô/avó cannot be formed by plain ES_OA suffix.",
    },
    159267: {
        "status": "candidate_context_rewrite_requires_review",
        "route": "laamp_contract_longform_gender_context",
        "subpolicy": "laamp_target_dead_select_cstring_rewrite",
        "old": "quer ver mort[target.Custom('ES_OA')] a [Select_CString( target.IsFemale, 'esta mulher', 'este homem' )]",
        "new": "quer ver [Select_CString( target.IsFemale, 'esta mulher morta', 'este homem morto' )]",
        "note": "Corrects word order and removes the malformed fixed preposition before the dynamic noun phrase.",
    },
    159931: {
        "status": "ready_exact_lexical_microfix",
        "route": "laamp_contract_longform_gender_context",
        "subpolicy": "longform_exact_spanish_lexical_microfix",
        "old": "#EMP excesivamente#!",
        "new": "#EMP excessivamente#!",
        "note": "Spanish adverb residue in otherwise coherent longform text.",
    },
    160173: {
        "status": "ready_context_false_reopen_noop",
        "route": "laamp_contract_longform_gender_context",
        "subpolicy": "laamp_context_false_reopen_noop_after_review",
        "old": "",
        "new": "",
        "note": "Manual inspection found no visible residual or malformed gender expression in the preserved blocker.",
    },
    160285: {
        "status": "candidate_sentence_rewrite_requires_review",
        "route": "visible_residual_longform_repair",
        "subpolicy": "longform_quote_punctuation_and_sentence_rewrite",
        "old": "«O quê... como... vocês claramente inventaram #EMP muito#! claramente um galimatias absoluto. O que possuiu vocês para pensar que poderiam tirar algum dinheiro disso, loc[ROOT.Char.Custom('ES_OA')]?».",
        "new": "\"O quê... como... vocês claramente inventaram um galimatias absoluto. O que lhes deu na cabeça para achar que poderiam tirar dinheiro disso, loc[ROOT.Char.Custom('ES_OA')]?\"",
        "note": "Spanish quotes plus awkward duplicated adverb; needs sentence-level review before apply.",
    },
    160412: {
        "status": "candidate_sentence_rewrite_requires_review",
        "route": "visible_residual_longform_repair",
        "subpolicy": "longform_quote_punctuation_and_pronoun_rewrite",
        "old": "«Não era o que eu esperaria como solução», assente pensativamente [employer.GetFirstNameNoTooltip], «mas mal posso criticar o vosso esmero. Tem certeza de que ele ficará bem aqui?».",
        "new": "\"Não era o que eu esperava como solução\", assente pensativamente [employer.GetFirstNameNoTooltip], \"mas mal posso criticar o vosso esmero. Tem certeza de que [sacrifice.GetSheHe] ficará bem aqui?\"",
        "note": "Spanish quotes plus fixed masculine pronoun in a dynamic sacrifice context.",
    },
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_longform_blocker_repair_dry_run_checkpoint_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_longform_false_reopen_checkpoint_runs
        WHERE rule_version = ?
          AND checkpoint_blocked_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_CHECKPOINT_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No longform false-reopen checkpoint with preserved blockers found.")
    return int(row["id"])


def fetch_blockers(conn, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS checkpoint_item_id,
            item.checkpoint_run_id,
            item.route_run_id,
            item.diagnostic_item_id,
            item.diagnostic_run_id,
            item.segment_state_run_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.route_key,
            item.block_reason AS source_block_reason,
            item.final_state,
            item.confirmed_matches_output,
            item.needs_output_apply,
            item.validation_issue_codes,
            item.current_text,
            c.id AS confirmation_id,
            c.locked AS confirmation_locked
        FROM ml_issue_gender_longform_false_reopen_checkpoint_items item
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = item.segment_id
              ORDER BY c2.updated_at DESC, c2.confirmed_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE item.checkpoint_run_id = ?
          AND item.checkpoint_allowed = 0
        ORDER BY item.route_key, item.segment_id
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_blocker_repair_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            subpolicy_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_blocker_repair_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            route_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            repair_status TEXT NOT NULL,
            route_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            block_reason TEXT,
            old_fragment TEXT,
            new_fragment TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            token_delta_status TEXT NOT NULL,
            confirmation_id INTEGER,
            confirmation_locked INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_longform_blocker_repair_runs(id) ON DELETE CASCADE
        )
        """
    )


def apply_fragment(text: str, old: str, new: str) -> tuple[str, str]:
    if not old and not new:
        return text, "noop"
    if old not in text:
        return text, "old_fragment_not_found"
    corrected = text.replace(old, new, 1)
    if structural_tokens(text) == structural_tokens(corrected):
        return corrected, "same_structural_tokens"
    return corrected, "structural_tokens_changed"


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    proposal = PROPOSALS.get(int(row["segment_id"]))
    reasons: list[str] = []
    if proposal is None:
        proposal = {
            "status": "blocked_no_proposal",
            "route": row.get("route_key") or "",
            "subpolicy": "unmapped_longform_blocker",
            "old": "",
            "new": "",
            "note": "No governed proposal for this blocker.",
        }
        reasons.append("no_governed_proposal")

    current = row.get("current_text") or ""
    corrected, token_delta_status = apply_fragment(current, proposal["old"], proposal["new"])
    status = proposal["status"]

    if status in READY_STATUSES:
        if int(row.get("confirmation_locked") or 0):
            reasons.append("confirmation_locked")
        if not str(row.get("final_state") or "").startswith("reopen_auto_confirmed"):
            reasons.append("not_auto_confirmed_reopen")
        if not int(row.get("confirmed_matches_output") or 0):
            reasons.append("confirmation_output_mismatch")
        if int(row.get("needs_output_apply") or 0):
            reasons.append("needs_output_apply")
        if status != "ready_context_false_reopen_noop" and token_delta_status == "old_fragment_not_found":
            reasons.append("old_fragment_not_found")
    elif status.startswith("candidate_"):
        reasons.append("requires_human_or_context_review")
    else:
        reasons.append(status)

    final_status = status if not reasons or status.startswith("candidate_") else "blocked"
    return {
        "checkpoint_run_id": int(row["checkpoint_run_id"]),
        "checkpoint_item_id": int(row["checkpoint_item_id"]),
        "route_run_id": int(row["route_run_id"]),
        "diagnostic_item_id": int(row["diagnostic_item_id"]),
        "diagnostic_run_id": int(row["diagnostic_run_id"]),
        "segment_state_run_id": row.get("segment_state_run_id"),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "source_line_number": int(row.get("source_line_number") or 0),
        "repair_status": final_status,
        "route_key": proposal["route"],
        "subpolicy_name": proposal["subpolicy"],
        "block_reason": ";".join(reasons),
        "old_fragment": proposal["old"],
        "new_fragment": proposal["new"],
        "current_text": current,
        "corrected_text": corrected if corrected != current else "",
        "token_delta_status": token_delta_status,
        "confirmation_id": row.get("confirmation_id"),
        "confirmation_locked": int(row.get("confirmation_locked") or 0),
        "note": proposal["note"],
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    checkpoint_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "repair_status",
        "route_key",
        "subpolicy_name",
        "block_reason",
        "token_delta_status",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "old_fragment",
        "new_fragment",
        "current_text",
        "corrected_text",
        "note",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["repair_status"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in rows)
    lines = [
        "Issue gender longform blocker repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source checkpoint run id: {checkpoint_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready: {sum(status_counts[status] for status in READY_STATUSES):,}",
        f"- Review required: {sum(value for key, value in status_counts.items() if key.startswith('candidate_')):,}",
        f"- Blocked: {status_counts['blocked'] + status_counts['blocked_no_proposal']:,}",
        "",
        "By status:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "By subpolicy:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Ready samples:",
    ]
    ready = [row for row in rows if row["repair_status"] in READY_STATUSES]
    if ready:
        for row in ready:
            lines.extend(
                [
                    f"- {row['repair_status']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                    f"  old: {row['old_fragment'] or '<noop>'}",
                    f"  new: {row['new_fragment'] or '<noop>'}",
                    f"  text: {short(row['corrected_text'] or row['current_text'], 240)}",
                ]
            )
    else:
        lines.append("- none")
    review = [row for row in rows if row["repair_status"].startswith("candidate_") or row["repair_status"] == "blocked"]
    lines.extend(["", "Review/blocker samples:"])
    if review:
        for row in review:
            lines.extend(
                [
                    f"- {row['repair_status']} | {row['block_reason']} | "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                    f"  proposal: {short(row['new_fragment'] or '<none>', 220)}",
                    f"  note: {row['note']}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Dry-run only: no production run, no confirmations, no source/output writes.",
            "- Ready means suitable for a later protected apply/lifecycle prompt, not automatically applied here.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        source_rows = fetch_blockers(conn, selected_checkpoint_run_id)
        rows = [evaluate(row) for row in source_rows]
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_checkpoint_run_id)
        status_counts = Counter(row["repair_status"] for row in rows)
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows)
        ready_count = sum(status_counts[status] for status in READY_STATUSES)
        review_count = sum(value for key, value in status_counts.items() if key.startswith("candidate_"))
        blocked_count = status_counts["blocked"] + status_counts["blocked_no_proposal"]
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_longform_blocker_repair_runs (
                rule_version,
                checkpoint_run_id,
                candidate_count,
                ready_count,
                review_count,
                blocked_count,
                status_counts_json,
                subpolicy_counts_json,
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
                selected_checkpoint_run_id,
                len(rows),
                ready_count,
                review_count,
                blocked_count,
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_gender_longform_blocker_repair_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    route_run_id,
                    diagnostic_item_id,
                    diagnostic_run_id,
                    segment_state_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    repair_status,
                    route_key,
                    subpolicy_name,
                    block_reason,
                    old_fragment,
                    new_fragment,
                    current_text,
                    corrected_text,
                    token_delta_status,
                    confirmation_id,
                    confirmation_locked,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["checkpoint_run_id"],
                    row["checkpoint_item_id"],
                    row["route_run_id"],
                    row["diagnostic_item_id"],
                    row["diagnostic_run_id"],
                    row["segment_state_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["repair_status"],
                    row["route_key"],
                    row["subpolicy_name"],
                    row["block_reason"],
                    row["old_fragment"],
                    row["new_fragment"],
                    row["current_text"],
                    row["corrected_text"],
                    row["token_delta_status"],
                    row["confirmation_id"],
                    row["confirmation_locked"],
                    row["note"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            checkpoint_run_id=selected_checkpoint_run_id,
            rows=rows,
        )
        conn.commit()

    print(f"Gender longform blocker repair dry-run: {run_id}")
    print(f"Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Ready: {ready_count}")
    print(f"Review required: {review_count}")
    print(f"Blocked: {blocked_count}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "candidate_count": len(rows),
        "ready_count": ready_count,
        "review_count": review_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run repair routes for preserved longform gender blockers.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
