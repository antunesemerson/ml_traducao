from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_title_landed_adjective_spanish_es_cross_tier_decisions_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
QUEUE_STRATEGY = "landed_title_adjective_spanish_es_suffix_repair"

CROSS_TIER_REPAIRS = {
    "b_brandenburg_adj": {
        "corrected_text": "brandemburg\u00eas",
        "support": "c_brandenburg_adj=brandemburg\u00eas",
    },
    "c_finland_adj": {
        "corrected_text": "finland\u00eas",
        "support": "k_finland_adj=finland\u00eas",
    },
    "d_finland_adj": {
        "corrected_text": "finland\u00eas",
        "support": "k_finland_adj=finland\u00eas",
    },
    "c_pressburg_adj": {
        "corrected_text": "presburgu\u00eas",
        "support": "d_pressburg_adj=presburgu\u00eas",
    },
}

EXPLICITLY_BLOCKED_CROSS_TIER = {
    "c_smaland_adj": "same root but English/current surface point to V\u00e4rend, not Sm\u00e5land",
    "d_halogaland_adj": "same root but English/current surface point to H\u00e1leygjer, not Halogaland",
}


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_cross_tier_decisions_queue_{queue_run_id}"
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
        """.format(placeholders=", ".join("?" for _ in CROSS_TIER_REPAIRS)),
        (queue_run_id, AGENT_KEY, *CROSS_TIER_REPAIRS.keys()),
    ).fetchall()
    return [dict(row) for row in rows]


def write_outputs(*, decisions_path: Path, report_path: Path, queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            policy = CROSS_TIER_REPAIRS[str(row["source_key"])]
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "decision": "needs_repair",
                        "corrected_text": policy["corrected_text"],
                        "notes": (
                            f"{RULE_VERSION}; cross_tier_support={policy['support']}; "
                            f"current={row.get('evidence_text')}; english={row.get('english_text')}"
                        ),
                        "reviewer": "codex_landed_title_spanish_es_cross_tier",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    lines = [
        "Spanish -es cross-tier title adjective decisions",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Rows: {len(rows):,}",
        f"Decisions path: {decisions_path}",
        "",
        "Rows:",
    ]
    for row in rows:
        policy = CROSS_TIER_REPAIRS[str(row["source_key"])]
        lines.append(
            f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
            f"{row.get('evidence_text')} -> {policy['corrected_text']} "
            f"({row.get('english_text')}; support={policy['support']})"
        )
    missing = sorted(set(CROSS_TIER_REPAIRS) - {str(row["source_key"]) for row in rows})
    if missing:
        lines.extend(["", "Not open or not found:", *[f"- {key}" for key in missing]])
    lines.extend(
        [
            "",
            "Blocked same-root cases:",
            *[f"- {key}: {reason}" for key, reason in sorted(EXPLICITLY_BLOCKED_CROSS_TIER.items())],
            "",
            "Safety note:",
            "- This records learning evidence only.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- Same-root reuse is not broad authority; semantic mismatches remain blocked.",
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
    write_outputs(decisions_path=decisions_path, report_path=report_path, queue_run_id=selected_queue_run_id, rows=rows)

    print("[issue_title_landed_adjective_spanish_es_cross_tier_decisions] Decisions generated")
    print(f"[issue_title_landed_adjective_spanish_es_cross_tier_decisions] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_cross_tier_decisions] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_cross_tier_decisions] Rows: {len(rows):,}")
    print(f"[issue_title_landed_adjective_spanish_es_cross_tier_decisions] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_spanish_es_cross_tier_decisions] Report: {report_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate cross-tier evidence decisions for selected Spanish -es landed title adjectives.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
