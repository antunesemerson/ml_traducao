from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_custom_localization_lexical_domain_checkpoint_v1"
CHECKPOINT_NAME = "custom_localization_flower_lexical_repair_checkpoint_v1"
CHECKPOINT_ACTION = "stage_custom_localization_flower_lexical_repair"
AGENT_KEY = "micro_ptbr_flower_lexicon"
PRODUCTION_RELEASE_ALLOWED = 0


def latest_diagnostic_run_id(conn, diagnostic_run_id: int | None) -> int:
    if diagnostic_run_id is not None:
        return diagnostic_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_custom_localization_lexical_domain_microdiagnostic_runs
        WHERE diagnostic_status = 'shadow_diagnostic'
          AND total_candidates > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No custom localization lexical/domain diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_custom_localization_lexical_domain_checkpoint_diagnostic_run_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_lexical_domain_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_lexical_domain_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            ledger_item_id INTEGER,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            english_text TEXT,
            spanish_text TEXT,
            confirmed_text TEXT,
            proposed_text TEXT,
            category TEXT NOT NULL,
            confidence TEXT NOT NULL,
            recommended_decision TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_custom_localization_lexical_domain_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_custom_loc_lexical_domain_checkpoint_items_run
        ON ml_issue_custom_localization_lexical_domain_checkpoint_items(checkpoint_run_id, checkpoint_allowed);

        CREATE INDEX IF NOT EXISTS idx_custom_loc_lexical_domain_checkpoint_items_segment
        ON ml_issue_custom_localization_lexical_domain_checkpoint_items(segment_id);
        """
    )
    existing_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(ml_issue_custom_localization_lexical_domain_checkpoint_items)")
    }
    if "ledger_run_id" not in existing_columns:
        conn.execute("ALTER TABLE ml_issue_custom_localization_lexical_domain_checkpoint_items ADD COLUMN ledger_run_id INTEGER")
    if "ledger_item_id" not in existing_columns:
        conn.execute("ALTER TABLE ml_issue_custom_localization_lexical_domain_checkpoint_items ADD COLUMN ledger_item_id INTEGER")


def fetch_diagnostic_run(conn, diagnostic_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_custom_localization_lexical_domain_microdiagnostic_runs
        WHERE id = ?
        """,
        (diagnostic_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Diagnostic run not found: {diagnostic_run_id}")
    return dict(row)


def fetch_items(conn, diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_custom_localization_lexical_domain_microdiagnostic_items
        WHERE run_id = ?
        ORDER BY category, source_key
        """,
        (diagnostic_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished issue ledger run found.")
    return int(row["id"])


def ledger_items_for_segment(conn, *, ledger_run_id: int, segment_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, issue_family, issue_kind
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
        ORDER BY
            CASE issue_family
                WHEN 'semantic_review_router' THEN 0
                WHEN 'short_label_style_microagent' THEN 1
                ELSE 2
            END,
            id
        """,
        (ledger_run_id, segment_id),
    ).fetchall()
    return [dict(row) for row in rows]


def block_reason(item: dict[str, Any]) -> str:
    if item.get("microagent_key") != AGENT_KEY:
        return "not_flower_lexicon_microagent"
    if item.get("category") != "ptbr_flower_lexical_repair":
        return "not_flower_lexical_repair"
    if item.get("confidence") != "high":
        return "confidence_not_high"
    if item.get("recommended_decision") != "needs_repair":
        return "decision_not_repair"
    if not str(item.get("proposed_text") or "").strip():
        return "missing_proposed_text"
    if str(item.get("confirmed_text") or "").strip() == str(item.get("proposed_text") or "").strip():
        return "already_matches_proposed_text"
    return ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    diagnostic_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "segment_id",
        "ledger_run_id",
        "ledger_item_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "english_text",
        "spanish_text",
        "confirmed_text",
        "proposed_text",
        "category",
        "confidence",
        "recommended_decision",
        "microagent_key",
        "checkpoint_action",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    allowed_samples = [
        (
            f"- segment={row['segment_id']} {row['source_key']} "
            f"| {row.get('confirmed_text')!r} -> {row.get('proposed_text')!r}"
        )
        for row in rows
        if int(row["checkpoint_allowed"])
    ][:40]
    blocked_samples = [
        (
            f"- {row['block_reason']} | segment={row['segment_id']} {row['source_key']} "
            f"| category={row['category']} confidence={row['confidence']}"
        )
        for row in rows
        if not int(row["checkpoint_allowed"])
    ][:40]
    lines = [
        "Custom Localization Lexical/Domain Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint action: {CHECKPOINT_ACTION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Diagnostic run id: {diagnostic_run['id']}",
        f"Source decision run id: {diagnostic_run['source_decision_run_id']}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates evaluated: {len(rows):,}",
        f"- Checkpoint allowed: {counts['allowed']:,}",
        f"- Checkpoint blocked: {counts['blocked']:,}",
        "",
        "Block reasons:",
        *[f"- {key.removeprefix('block_')}: {value:,}" for key, value in counts.items() if key.startswith("block_")],
        "",
        "Allowed samples:",
        *allowed_samples,
        "",
        "Blocked samples:",
        *blocked_samples,
        "",
        "Safety note:",
        "- This checkpoint stages learning evidence only.",
        "- It does not write source/output or production lifecycle authority.",
        "- Production application would require a separate production-flow change and validation.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_run_id = latest_diagnostic_run_id(conn, diagnostic_run_id)
        diagnostic_run = fetch_diagnostic_run(conn, selected_run_id)
        items = fetch_items(conn, selected_run_id)
        ledger_run_id = latest_ledger_run_id(conn)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        counts: Counter[str] = Counter()
        prepared: list[dict[str, Any]] = []
        for item in items:
            reason = block_reason(item)
            allowed = 0 if reason else 1
            ledger_items = ledger_items_for_segment(conn, ledger_run_id=ledger_run_id, segment_id=int(item["segment_id"]))
            if not ledger_items:
                ledger_items = [{"id": None, "issue_family": "unknown", "issue_kind": "unknown"}]
            if not allowed:
                ledger_items = ledger_items[:1]
            for ledger_item in ledger_items:
                if allowed:
                    counts["allowed"] += 1
                else:
                    counts["blocked"] += 1
                    counts[f"block_{reason}"] += 1
                prepared.append(
                    {
                        **item,
                        "diagnostic_item_id": item["id"],
                        "ledger_run_id": ledger_run_id,
                        "ledger_item_id": ledger_item["id"],
                        "checkpoint_action": CHECKPOINT_ACTION,
                        "checkpoint_allowed": allowed,
                        "block_reason": reason,
                        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                    }
                )

        cur = conn.execute(
            """
            INSERT INTO ml_issue_custom_localization_lexical_domain_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                diagnostic_run_id,
                source_decision_run_id,
                agent_key,
                candidate_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                updated_at
            )
            VALUES (?, ?, 'shadow_checkpoint', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                selected_run_id,
                int(diagnostic_run["source_decision_run_id"]),
                AGENT_KEY,
                len(prepared),
                counts["allowed"],
                counts["blocked"],
                PRODUCTION_RELEASE_ALLOWED,
                json.dumps({k.removeprefix("block_"): v for k, v in counts.items() if k.startswith("block_")}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                started_at,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        now = db.utc_now()
        for row in prepared:
            conn.execute(
                """
                INSERT INTO ml_issue_custom_localization_lexical_domain_checkpoint_items (
                    checkpoint_run_id,
                    diagnostic_run_id,
                    diagnostic_item_id,
                    source_decision_run_id,
                    ledger_run_id,
                    segment_id,
                    ledger_item_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    english_text,
                    spanish_text,
                    confirmed_text,
                    proposed_text,
                    category,
                    confidence,
                    recommended_decision,
                    microagent_key,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    production_release_allowed,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_run_id,
                    int(row["diagnostic_item_id"]),
                    int(diagnostic_run["source_decision_run_id"]),
                    int(row["ledger_run_id"]),
                    int(row["segment_id"]),
                    row.get("ledger_item_id"),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row.get("english_text"),
                    row.get("spanish_text"),
                    row.get("confirmed_text"),
                    row.get("proposed_text"),
                    row["category"],
                    row["confidence"],
                    row["recommended_decision"],
                    row["microagent_key"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    PRODUCTION_RELEASE_ALLOWED,
                    now,
                ),
            )

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            diagnostic_run=diagnostic_run,
            rows=prepared,
            counts=counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_custom_localization_lexical_domain_checkpoint_runs
            SET finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (finished_at, finished_at, checkpoint_run_id),
        )
        conn.commit()

    print("[issue_custom_localization_lexical_domain_checkpoint] Checkpoint generated")
    print(f"[issue_custom_localization_lexical_domain_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_custom_localization_lexical_domain_checkpoint] Diagnostic run id: {selected_run_id}")
    print(f"[issue_custom_localization_lexical_domain_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_custom_localization_lexical_domain_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_custom_localization_lexical_domain_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "diagnostic_run_id": selected_run_id,
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint high-confidence custom-localization lexical repairs.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    args = parser.parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id)
