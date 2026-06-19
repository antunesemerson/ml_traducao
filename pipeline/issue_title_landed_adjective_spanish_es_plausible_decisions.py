from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_title_landed_adjective_spanish_es_plausible_decisions_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_plausible_decisions_queue_{queue_run_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE agent_key = ?
          AND queue_strategy = 'landed_title_adjective_spanish_es_suffix_repair'
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No Spanish -es suffix repair queue found.")
    return int(row["id"])


def fetch_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
          AND agent_key = ?
        ORDER BY id
        """,
        (queue_run_id, AGENT_KEY),
    ).fetchall()
    output: list[dict[str, Any]] = []
    known_english = {
        "Aragonese",
        "Danish",
        "English",
        "French",
        "Irish",
        "Japanese",
        "New English",
        "Portuguese",
        "Scottish",
        "Siamese",
        "Welsh",
    }
    for row in rows:
        item = dict(row)
        evidence = parse_json(item.get("evidence_json"), {})
        hint = str(evidence.get("repair_hint") or "")
        if not hint:
            continue
        if str(item.get("english_text") or "") not in known_english:
            continue
        if " " in str(item.get("evidence_text") or "").strip():
            continue
        item["repair_hint"] = hint
        output.append(item)
    return output


def write_outputs(*, decisions_path: Path, report_path: Path, queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "decision": "needs_repair",
                        "corrected_text": row["repair_hint"],
                        "notes": (
                            f"{RULE_VERSION}; plausible_suffix_only_known_ptbr_gentilic; "
                            f"current={row.get('evidence_text')}; english={row.get('english_text')}"
                        ),
                        "reviewer": "codex_landed_title_spanish_es_plausible",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    lines = [
        "Spanish -es plausible suffix repair decisions",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Rows: {len(rows):,}",
        f"Decisions path: {decisions_path}",
        "",
        "Rows:",
    ]
    for row in rows:
        lines.append(
            f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
            f"{row.get('evidence_text')} -> {row['repair_hint']} ({row.get('english_text')})"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This records learning evidence only.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- The decision is limited to common PT-BR gentilic forms with direct English support.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)

    decisions_path, report_path = report_paths(settings, selected_queue_run_id)
    write_outputs(
        decisions_path=decisions_path,
        report_path=report_path,
        queue_run_id=selected_queue_run_id,
        rows=rows,
    )
    print("[issue_title_landed_adjective_spanish_es_plausible_decisions] Decisions generated")
    print(f"[issue_title_landed_adjective_spanish_es_plausible_decisions] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_plausible_decisions] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_plausible_decisions] Rows: {len(rows):,}")
    print(f"[issue_title_landed_adjective_spanish_es_plausible_decisions] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_spanish_es_plausible_decisions] Report: {report_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate conservative decisions for known Spanish -es gentilic repairs.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
