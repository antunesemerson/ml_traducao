from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_title_landed_adjective_assisted_review import AGENT_KEY, RULE_VERSION as ASSISTED_RULE_VERSION
from issue_title_landed_adjective_assisted_review import classify
from issue_title_policy_route_diagnostic import latest_ledger_run_id, route_lane
from issue_title_policy_route_review_queue import key_prefix, route_bucket, suffix_hint


RULE_VERSION = "issue_title_landed_adjective_opportunity_scan_v1"
ISSUE_FAMILY = "title_policy_microagent"
LANE = "landed_title_adjectives"


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_opportunity_scan"
    return base.with_suffix(".txt"), base.with_suffix(".csv")


def short(value: str | None, limit: int = 120) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def fetch_candidates(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            COALESCE(
                confirmation.confirmed_text,
                output.portuguese_text,
                source.old_text,
                ''
            ) AS confirmed_text
        FROM ml_issue_ledger_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = item.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.issue_family = ?
        ORDER BY item.relative_path, item.source_line_number, item.segment_id
        """,
        (ledger_run_id, ISSUE_FAMILY),
    ).fetchall()

    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if route_lane(item) != LANE:
            continue
        item["queue_bucket"] = route_bucket(item)
        item["key_prefix"] = key_prefix(str(item.get("source_key") or ""))
        item["suffix_hint"] = suffix_hint(str(item.get("evidence_text") or ""))
        output.append(item)
    return output


def write_report(
    *,
    txt_path: Path,
    csv_path: Path,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    classified: list[dict[str, Any]],
) -> None:
    decision_counts = Counter(row["decision"] for row in classified)
    reason_counts = Counter(row["reason"] for row in classified)
    bucket_counts = Counter(row["queue_bucket"] for row in classified)
    decision_bucket_counts = Counter(f"{row['decision']}|{row['queue_bucket']}" for row in classified)
    prefix_counts = Counter(row["key_prefix"] for row in classified)

    fieldnames = [
        "segment_id",
        "ledger_item_id",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "key_prefix",
        "suffix_hint",
        "decision",
        "reason",
        "evidence_text",
        "english_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in classified:
            writer.writerow(
                {
                    "segment_id": row["segment_id"],
                    "ledger_item_id": row["id"],
                    "source_key": row.get("source_key"),
                    "source_line_number": row.get("source_line_number"),
                    "queue_bucket": row["queue_bucket"],
                    "key_prefix": row["key_prefix"],
                    "suffix_hint": row["suffix_hint"],
                    "decision": row["decision"],
                    "reason": row["reason"],
                    "evidence_text": row.get("evidence_text"),
                    "english_text": row.get("english_text"),
                }
            )

    lines = [
        "Landed title adjective opportunity scan",
        f"Rule version: {RULE_VERSION}",
        f"Assisted rule version: {ASSISTED_RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent key: {AGENT_KEY}",
        f"Lane: {LANE}",
        f"Candidates: {len(rows):,}",
        "",
        "Decision projection:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Reason projection:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Bucket projection:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Decision by bucket:",
        *[f"- {key}: {value:,}" for key, value in decision_bucket_counts.most_common()],
        "",
        "Prefix projection:",
        *[f"- {key}: {value:,}" for key, value in prefix_counts.most_common()],
        "",
        "Top samples:",
    ]
    for row in classified[:120]:
        lines.append(
            f"- {row['decision']} | {row['reason']} | {row['queue_bucket']} | "
            f"segment={row['segment_id']} {row.get('source_key')} | {short(row.get('evidence_text'))}"
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This is a projection over pending title adjective ledger items, not a lifecycle bridge.",
            "- It estimates where route-specific microagents can reduce future manual review.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, csv_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        rows = fetch_candidates(conn, ledger_run_id=selected_ledger_run_id)

    classified: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        decision, corrected_text, reason = classify(row)
        item = dict(row)
        item["decision"] = decision
        item["corrected_text"] = corrected_text
        item["reason"] = reason
        counts[decision] += 1
        classified.append(item)

    write_report(
        txt_path=txt_path,
        csv_path=csv_path,
        ledger_run_id=selected_ledger_run_id,
        rows=rows,
        classified=classified,
    )

    print("[issue_title_landed_adjective_opportunity_scan] Scan generated")
    print(f"[issue_title_landed_adjective_opportunity_scan] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_opportunity_scan] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_title_landed_adjective_opportunity_scan] Candidates: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_title_landed_adjective_opportunity_scan] {key}: {value:,}")
    print(f"[issue_title_landed_adjective_opportunity_scan] Report: {txt_path}")
    print(f"[issue_title_landed_adjective_opportunity_scan] CSV: {csv_path}")
    return {
        "ledger_run_id": selected_ledger_run_id,
        "candidate_count": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan landed title adjective opportunities from the title-policy ledger lane.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id)
