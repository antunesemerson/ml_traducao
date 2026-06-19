from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_title_landed_adjective_spanish_es_direct_semantic_decisions_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
QUEUE_STRATEGY = "landed_title_adjective_spanish_es_suffix_repair"

DIRECT_REPAIRS = {
    "c_albania_adj": "alban\u00eas",
    "d_albania_adj": "alban\u00eas",
    "d_calabria_adj": "calabr\u00eas",
    "e_carthage_adj": "cartagin\u00eas",
    "k_carthage_adj": "cartagin\u00eas",
    "k_carthago_nova_adj": "cartagin\u00eas",
    "k_finland_adj": "finland\u00eas",
}


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_direct_semantic_decisions_queue_{queue_run_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE agent_key = ?
          AND queue_strategy = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, QUEUE_STRATEGY),
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
          AND review_status != 'reviewed'
          AND source_key IN ({placeholders})
        ORDER BY source_key, id
        """.format(placeholders=", ".join("?" for _ in DIRECT_REPAIRS)),
        (queue_run_id, AGENT_KEY, *DIRECT_REPAIRS.keys()),
    ).fetchall()
    return [dict(row) for row in rows]


def write_outputs(*, decisions_path: Path, report_path: Path, queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            corrected_text = DIRECT_REPAIRS[str(row["source_key"])]
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "decision": "needs_repair",
                        "corrected_text": corrected_text,
                        "notes": (
                            f"{RULE_VERSION}; direct_ptbr_gentilic_exception; "
                            f"current={row.get('evidence_text')}; english={row.get('english_text')}"
                        ),
                        "reviewer": "codex_landed_title_spanish_es_direct_semantic",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    lines = [
        "Spanish -es direct semantic gentilic decisions",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Rows: {len(rows):,}",
        f"Decisions path: {decisions_path}",
        "",
        "Rows:",
    ]
    for row in rows:
        corrected_text = DIRECT_REPAIRS[str(row["source_key"])]
        lines.append(
            f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
            f"{row.get('evidence_text')} -> {corrected_text} ({row.get('english_text')})"
        )
    missing = sorted(set(DIRECT_REPAIRS) - {str(row["source_key"]) for row in rows})
    if missing:
        lines.extend(["", "Not open or not found:", *[f"- {key}" for key in missing]])
    lines.extend(
        [
            "",
            "Safety note:",
            "- This records learning evidence only.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- Ambiguous cases such as Apulian, short Finnish forms, and multiword Reggian remain blocked.",
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

    print("[issue_title_landed_adjective_spanish_es_direct_semantic_decisions] Decisions generated")
    print(f"[issue_title_landed_adjective_spanish_es_direct_semantic_decisions] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_direct_semantic_decisions] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_direct_semantic_decisions] Rows: {len(rows):,}")
    print(f"[issue_title_landed_adjective_spanish_es_direct_semantic_decisions] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_spanish_es_direct_semantic_decisions] Report: {report_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate direct PT-BR semantic decisions for selected Spanish -es landed title adjectives.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
