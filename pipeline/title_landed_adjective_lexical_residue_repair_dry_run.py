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
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "title_landed_adjective_lexical_residue_repair_dry_run_v1"
AGENT_KEY = "micro_landed_title_lexical_residue_repair"
READY_DECISIONS_DEFAULT = {"ready_exact_lexical_map"}
READY_DECISIONS_WITH_MEDIUM = {
    "ready_exact_lexical_map",
    "ready_direction_spelling_only",
    "ready_direction_and_es_suffix_only",
}


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_title_landed_adjective_lexical_residue_repair_dry_run_diag_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_title_landed_adjective_lexical_residue_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No lexical residue diagnostic run found.")
    return int(row["id"])


def latest_state_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_title_landed_adjective_lexical_residue_repair_dry_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER,
            agent_key TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            already_aligned_count INTEGER NOT NULL DEFAULT 0,
            decision_scope_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_title_landed_adjective_lexical_residue_repair_dry_run_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            current_text TEXT,
            proposed_text TEXT,
            decision TEXT NOT NULL,
            status TEXT NOT NULL,
            block_reason TEXT,
            final_state TEXT,
            review_state TEXT,
            apply_state TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, diagnostic_item_id)
        )
        """
    )


def canonical(value: str | None) -> str:
    return " ".join((value or "").split())


def quality_blockers(text: str | None) -> list[str]:
    validation = local_quality_validator.validate_text(text)
    blockers: list[str] = []
    for issue in validation.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "").lower()
        if severity in {"medium", "high", "error", "critical"}:
            blockers.append(str(issue.get("code") or "quality_issue"))
    return sorted(set(blockers))


def fetch_candidates(conn, *, diagnostic_run_id: int, ready_decisions: set[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ready_decisions)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_title_landed_adjective_lexical_residue_items
        WHERE run_id = ?
          AND decision IN ({placeholders})
        ORDER BY relative_path, source_line_number, segment_id
        """,
        (diagnostic_run_id, *sorted(ready_decisions)),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_live(conn, *, segment_id: int, state_run_id: int | None) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            source.id AS segment_id,
            source.relative_path,
            source.source_key,
            source.is_active,
            output.portuguese_text AS output_text,
            confirmation.confirmed_text,
            confirmation.locked,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            state.final_state,
            state.review_state,
            state.apply_state,
            state.needs_output_apply
        FROM source_segments source
        LEFT JOIN output_segments output ON output.segment_id = source.id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = source.id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        LEFT JOIN segment_state_items state
          ON state.segment_id = source.id
         AND state.run_id = ?
        WHERE source.id = ?
        LIMIT 1
        """,
        (state_run_id, segment_id),
    ).fetchone()
    return dict(row) if row else None


def evaluate(row: dict[str, Any], live: dict[str, Any] | None) -> tuple[str, str]:
    if live is None:
        return "blocked", "missing_live_segment"
    if int(live.get("is_active") or 0) != 1:
        return "blocked", "source_not_active"
    if live.get("relative_path") != row.get("relative_path"):
        return "blocked", "relative_path_mismatch"
    if live.get("source_key") != row.get("source_key"):
        return "blocked", "source_key_mismatch"

    current = canonical(row.get("current_text"))
    proposed = canonical(row.get("proposed_text"))
    output = canonical(live.get("output_text"))
    confirmed = canonical(live.get("confirmed_text"))

    if not proposed:
        return "blocked", "missing_proposed_text"
    if output == proposed and confirmed == proposed:
        return "already_aligned", ""
    if output != current:
        return "blocked", "stale_output_text"
    if confirmed != current:
        return "blocked", "stale_confirmation_text"
    if int(live.get("locked") or 0) == 1:
        return "blocked", "locked_confirmation"
    if protected_tokens(current) != protected_tokens(proposed):
        return "blocked", "protected_token_signature_mismatch"
    quality = quality_blockers(proposed)
    if quality:
        return "blocked", "quality_block:" + ",".join(quality)
    return "ready", ""


def insert_run(
    conn,
    *,
    diagnostic_run_id: int,
    state_run_id: int | None,
    rows: list[dict[str, Any]],
    paths: tuple[Path, Path, Path],
    ready_decisions: set[str],
) -> int:
    now = db.utc_now()
    counts = Counter(row["status"] for row in rows)
    txt_path, csv_path, jsonl_path = paths
    cur = conn.execute(
        """
        INSERT INTO ml_title_landed_adjective_lexical_residue_repair_dry_runs (
            rule_version,
            diagnostic_run_id,
            segment_state_run_id,
            agent_key,
            candidate_count,
            ready_count,
            blocked_count,
            already_aligned_count,
            decision_scope_json,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            diagnostic_run_id,
            state_run_id,
            AGENT_KEY,
            len(rows),
            counts["ready"],
            counts["blocked"],
            counts["already_aligned"],
            json.dumps(sorted(ready_decisions), ensure_ascii=False),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            now,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def insert_items(conn, *, run_id: int, diagnostic_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        conn.execute(
            """
            INSERT INTO ml_title_landed_adjective_lexical_residue_repair_dry_run_items (
                run_id,
                diagnostic_run_id,
                diagnostic_item_id,
                segment_id,
                relative_path,
                source_key,
                current_text,
                proposed_text,
                decision,
                status,
                block_reason,
                final_state,
                review_state,
                apply_state,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                diagnostic_run_id,
                int(row["id"]),
                int(row["segment_id"]),
                row.get("relative_path"),
                row.get("source_key"),
                row.get("current_text"),
                row.get("proposed_text"),
                row.get("decision"),
                row.get("status"),
                row.get("block_reason") or "",
                row.get("final_state") or "",
                row.get("review_state") or "",
                row.get("apply_state") or "",
                now,
            ),
        )


def write_reports(*, paths: tuple[Path, Path, Path], run_id: int, diagnostic_run_id: int, rows: list[dict[str, Any]]) -> None:
    txt_path, csv_path, jsonl_path = paths
    counts = Counter(row["status"] for row in rows)
    block_counts = Counter(row["block_reason"] for row in rows if row.get("block_reason"))
    fields = [
        "run_id",
        "diagnostic_run_id",
        "diagnostic_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "current_text",
        "proposed_text",
        "decision",
        "status",
        "block_reason",
        "final_state",
        "review_state",
        "apply_state",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["run_id"] = run_id
            payload["diagnostic_run_id"] = diagnostic_run_id
            payload["diagnostic_item_id"] = row["id"]
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "run_id": run_id,
                "diagnostic_run_id": diagnostic_run_id,
                "diagnostic_item_id": int(row["id"]),
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "current_text": row.get("current_text"),
                "proposed_text": row.get("proposed_text"),
                "decision": row.get("decision"),
                "status": row.get("status"),
                "block_reason": row.get("block_reason") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Title landed adjective lexical residue repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready: {counts['ready']:,}",
        f"- Already aligned: {counts['already_aligned']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common()],
        "",
        "Ready rows:",
    ]
    for row in [item for item in rows if item["status"] == "ready"][:80]:
        lines.append(
            f"- segment={row['segment_id']} {row.get('source_key')} | "
            f"{row.get('current_text')} -> {row.get('proposed_text')} | {row.get('decision')}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Dry-run only.",
            "- This does not write source/output, confirmations, lifecycle policies, or production artifacts.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None, include_medium: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        state_run_id = latest_state_run_id(conn)
        ready_decisions = READY_DECISIONS_WITH_MEDIUM if include_medium else READY_DECISIONS_DEFAULT
        candidates = fetch_candidates(conn, diagnostic_run_id=selected_diagnostic_run_id, ready_decisions=ready_decisions)
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            live = fetch_live(conn, segment_id=int(candidate["segment_id"]), state_run_id=state_run_id)
            status, block_reason = evaluate(candidate, live)
            item = dict(candidate)
            item["current_text"] = candidate.get("current_text") or candidate.get("current_text") or candidate.get("current_text", "")
            item["status"] = status
            item["block_reason"] = block_reason
            if live:
                item["final_state"] = live.get("final_state")
                item["review_state"] = live.get("review_state")
                item["apply_state"] = live.get("apply_state")
            rows.append(item)
        paths = report_paths(settings, selected_diagnostic_run_id)
        run_id = insert_run(
            conn,
            diagnostic_run_id=selected_diagnostic_run_id,
            state_run_id=state_run_id,
            rows=rows,
            paths=paths,
            ready_decisions=ready_decisions,
        )
        insert_items(conn, run_id=run_id, diagnostic_run_id=selected_diagnostic_run_id, rows=rows)
        conn.commit()

    write_reports(paths=paths, run_id=run_id, diagnostic_run_id=selected_diagnostic_run_id, rows=rows)
    counts = Counter(row["status"] for row in rows)
    print("[title_landed_adjective_lexical_residue_repair_dry_run] Dry-run generated")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Rule version: {RULE_VERSION}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Run id: {run_id}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Candidates: {len(rows):,}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Ready: {counts['ready']:,}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Already aligned: {counts['already_aligned']:,}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Blocked: {counts['blocked']:,}")
    print(f"[title_landed_adjective_lexical_residue_repair_dry_run] Report: {paths[0]}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "candidate_count": len(rows),
        "ready": counts["ready"],
        "already_aligned": counts["already_aligned"],
        "blocked": counts["blocked"],
        "report_path": str(paths[0]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run high-confidence lexical residue title adjective repairs.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--include-medium", action="store_true")
    args = parser.parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id, include_medium=args.include_medium)
