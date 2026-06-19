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
from issue_title_landed_adjective_assisted_review import classify
from issue_title_landed_adjective_opportunity_scan import fetch_candidates
from issue_title_policy_route_diagnostic import latest_ledger_run_id


RULE_VERSION = "issue_title_landed_adjective_lexical_residue_diagnostic_v1"
ISSUE_FAMILY = "title_policy_microagent"
AGENT_KEY = "micro_landed_title_lexical_residue_repair"

EXACT_LEXICAL_MAP = {
    "armenio": "armênio",
    "bretón": "bretão",
    "frisón": "frísio",
    "gascón": "gascão",
    "griego": "grego",
    "ruso": "russo",
    "sajón": "saxão",
}

REVIEW_LEXICAL_MAP = {
    "berrichón": "berrichão",
    "brabanzón": "brabantino",
    "grisón": "grisão",
    "krasón": "krassoniano",
    "lapón": "lapão",
    "vascón": "basco",
}

SPANISH_ES_SUFFIX_RE = re.compile(r"\b([A-Za-zÀ-ÿ]+)és\b", re.IGNORECASE)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_lexical_residue_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_title_landed_adjective_lexical_residue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_title_landed_adjective_lexical_residue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            current_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            old_text TEXT,
            output_text TEXT,
            confirmed_text TEXT,
            projected_reason TEXT,
            decision TEXT NOT NULL,
            proposed_text TEXT,
            block_reason TEXT,
            confidence_hint TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, ledger_item_id)
        )
        """
    )


def normalize_space(value: str) -> str:
    return " ".join((value or "").split())


def replace_final_es(text: str) -> str:
    return SPANISH_ES_SUFFIX_RE.sub(lambda match: f"{match.group(1)}ês", text)


def propose(text: str) -> tuple[str, str, str, str]:
    current = normalize_space(text)
    lower = current.casefold()
    if not current:
        return "blocked_empty_text", "", "empty_text", "blocked"

    if lower in EXACT_LEXICAL_MAP:
        return "ready_exact_lexical_map", EXACT_LEXICAL_MAP[lower], "", "high"

    if lower in REVIEW_LEXICAL_MAP:
        return "candidate_exact_lexical_requires_review", REVIEW_LEXICAL_MAP[lower], "ambiguous_historical_gentilic", "review"

    proposed = current
    changed: list[str] = []
    for source, target in EXACT_LEXICAL_MAP.items():
        next_value = re.sub(rf"\b{re.escape(source)}\b", target, proposed, flags=re.IGNORECASE)
        if next_value != proposed:
            changed.append(f"lexical:{source}->{target}")
            proposed = next_value
    for source, target in REVIEW_LEXICAL_MAP.items():
        next_value = re.sub(rf"\b{re.escape(source)}\b", target, proposed, flags=re.IGNORECASE)
        if next_value != proposed:
            changed.append(f"review_lexical:{source}->{target}")
            proposed = next_value

    if "occidental" in proposed.casefold():
        proposed = re.sub(r"\boccidental\b", "ocidental", proposed, flags=re.IGNORECASE)
        changed.append("direction:occidental->ocidental")

    es_fixed = replace_final_es(proposed)
    if es_fixed != proposed:
        changed.append("suffix:és->ês")
        proposed = es_fixed

    if not changed or proposed == current:
        return "candidate_unmapped_title_residue_requires_review", "", "no_safe_mapping", "review"

    if any(item.startswith("review_lexical:") for item in changed):
        return "candidate_compound_lexical_requires_review", proposed, ";".join(changed), "review"

    if changed == ["direction:occidental->ocidental"]:
        return "ready_direction_spelling_only", proposed, "", "medium"

    if all(item in {"direction:occidental->ocidental", "suffix:és->ês"} for item in changed):
        return "ready_direction_and_es_suffix_only", proposed, "", "medium"

    if any(item.startswith("lexical:") for item in changed):
        return "candidate_compound_lexical_requires_review", proposed, ";".join(changed), "review"

    return "candidate_mapped_requires_review", proposed, ";".join(changed), "review"


def fetch_output_context(conn, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.spanish_text,
            s.old_text,
            output.portuguese_text AS output_text,
            confirmation.confirmed_text
        FROM source_segments s
        LEFT JOIN output_segments output ON output.segment_id = s.id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = s.id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE s.id IN ({placeholders})
        """,
        segment_ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def insert_run(
    conn,
    *,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
    paths: tuple[Path, Path, Path],
) -> int:
    now = db.utc_now()
    ready_count = sum(value for key, value in counts.items() if key.startswith("ready_"))
    review_count = sum(value for key, value in counts.items() if key.startswith("candidate_"))
    blocked_count = sum(value for key, value in counts.items() if key.startswith("blocked_"))
    txt_path, csv_path, jsonl_path = paths
    cur = conn.execute(
        """
        INSERT INTO ml_issue_title_landed_adjective_lexical_residue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            candidate_count,
            ready_count,
            review_count,
            blocked_count,
            decision_counts_json,
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
            ledger_run_id,
            AGENT_KEY,
            len(rows),
            ready_count,
            review_count,
            blocked_count,
            json.dumps(dict(counts.most_common()), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            now,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def insert_items(conn, *, run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        conn.execute(
            """
            INSERT INTO ml_issue_title_landed_adjective_lexical_residue_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                current_text,
                english_text,
                spanish_text,
                old_text,
                output_text,
                confirmed_text,
                projected_reason,
                decision,
                proposed_text,
                block_reason,
                confidence_hint,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ledger_run_id,
                int(row["id"]),
                int(row["segment_id"]),
                row.get("relative_path"),
                row.get("source_key"),
                row.get("source_line_number"),
                row.get("evidence_text"),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("old_text"),
                row.get("output_text"),
                row.get("confirmed_text"),
                row.get("projected_reason"),
                row["decision"],
                row.get("proposed_text") or "",
                row.get("block_reason") or "",
                row.get("confidence_hint") or "",
                now,
            ),
        )


def write_reports(*, paths: tuple[Path, Path, Path], run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    txt_path, csv_path, jsonl_path = paths
    counts = Counter(row["decision"] for row in rows)
    reason_counts = Counter(row["projected_reason"] for row in rows)

    fields = [
        "run_id",
        "ledger_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "current_text",
        "proposed_text",
        "english_text",
        "projected_reason",
        "decision",
        "confidence_hint",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["run_id"] = run_id
            payload["ledger_run_id"] = ledger_run_id
            payload["ledger_item_id"] = row["id"]
            payload["current_text"] = row.get("evidence_text") or ""
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "run_id": run_id,
                "ledger_run_id": ledger_run_id,
                "ledger_item_id": int(row["id"]),
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "current_text": row.get("evidence_text"),
                "proposed_text": row.get("proposed_text") or "",
                "english_text": row.get("english_text"),
                "decision": row["decision"],
                "projected_reason": row["projected_reason"],
                "confidence_hint": row.get("confidence_hint") or "",
                "block_reason": row.get("block_reason") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Title landed adjective lexical residue diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent key: {AGENT_KEY}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Projected reasons:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Ready samples:",
    ]
    for row in [item for item in rows if item["decision"].startswith("ready_")][:80]:
        lines.append(
            f"- segment={row['segment_id']} {row.get('source_key')} | "
            f"{row.get('evidence_text')} -> {row.get('proposed_text')} | {row.get('english_text')}"
        )
    lines.extend(["", "Review samples:"])
    for row in [item for item in rows if item["decision"].startswith("candidate_")][:80]:
        lines.append(
            f"- {row['decision']} | segment={row['segment_id']} {row.get('source_key')} | "
            f"{row.get('evidence_text')} -> {row.get('proposed_text') or '-'} | "
            f"{row.get('block_reason') or '-'} | {row.get('english_text')}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is read-only learning evidence.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- Ready means suitable for a future protected dry-run, not approved for direct application.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        candidates = []
        for row in fetch_candidates(conn, ledger_run_id=selected_ledger_run_id):
            decision, _corrected, reason = classify(row)
            if decision != "needs_repair":
                continue
            item = dict(row)
            item["projected_reason"] = reason
            candidates.append(item)
        context = fetch_output_context(conn, [int(row["segment_id"]) for row in candidates])

        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for row in candidates:
            item = dict(row)
            item.update(context.get(int(item["segment_id"]), {}))
            decision, proposed_text, block_reason, confidence_hint = propose(str(item.get("evidence_text") or ""))
            item["decision"] = decision
            item["proposed_text"] = proposed_text
            item["block_reason"] = block_reason
            item["confidence_hint"] = confidence_hint
            counts[decision] += 1
            classified.append(item)

        run_id = insert_run(
            conn,
            ledger_run_id=selected_ledger_run_id,
            rows=classified,
            counts=counts,
            paths=paths,
        )
        insert_items(conn, run_id=run_id, ledger_run_id=selected_ledger_run_id, rows=classified)
        conn.commit()

    write_reports(paths=paths, run_id=run_id, ledger_run_id=selected_ledger_run_id, rows=classified)

    print("[issue_title_landed_adjective_lexical_residue_diagnostic] Diagnostic generated")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] Run id: {run_id}")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] Candidates: {len(classified):,}")
    for key, value in counts.most_common():
        print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] {key}: {value:,}")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] Report: {paths[0]}")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] CSV: {paths[1]}")
    print(f"[issue_title_landed_adjective_lexical_residue_diagnostic] JSONL: {paths[2]}")
    return {
        "run_id": run_id,
        "ledger_run_id": selected_ledger_run_id,
        "candidate_count": len(classified),
        "counts": dict(counts),
        "report_path": str(paths[0]),
        "csv_path": str(paths[1]),
        "jsonl_path": str(paths[2]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose lexical Spanish residue in landed title adjective title-policy items.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id)
