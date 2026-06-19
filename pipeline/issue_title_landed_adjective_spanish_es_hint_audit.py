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


RULE_VERSION = "issue_title_landed_adjective_spanish_es_hint_audit_v1"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"

KNOWN_DIRECT_ENGLISH_HINTS = (
    "aragonese",
    "chinese",
    "danish",
    "english",
    "french",
    "irish",
    "japanese",
    "leonese",
    "portuguese",
    "scottish",
    "siamese",
    "welsh",
)

LIKELY_NOT_SUFFIX_ONLY_ENGLISH = (
    "apulian",
    "finnish",
    "albanian",
    "catalan",
    "carthaginian",
    "calabrian",
)


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_hint_audit_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv")


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def short(value: str | None, limit: int = 120) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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
        ORDER BY queue_bucket, source_key, id
        """,
        (queue_run_id,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        evidence = parse_json(item.get("evidence_json"), {})
        item["repair_hint"] = str(evidence.get("repair_hint") or "")
        output.append(item)
    return output


def classify_hint(row: dict[str, Any]) -> tuple[str, str]:
    current = str(row.get("evidence_text") or "")
    hint = str(row.get("repair_hint") or "")
    english = str(row.get("english_text") or "").casefold()
    source_key = str(row.get("source_key") or "")

    if not hint:
        return "blocked", "missing_repair_hint"
    if " " in current.strip():
        return "blocked", "multiword_surface_requires_manual_repair"
    if any(term in english for term in LIKELY_NOT_SUFFIX_ONLY_ENGLISH):
        return "blocked", "english_semantics_suggests_not_suffix_only"
    if any(term in english for term in KNOWN_DIRECT_ENGLISH_HINTS):
        return "plausible_suffix_only", "known_direct_ptbr_suffix_family"
    if re.search(r"(?:burger|berger|lander|er)$", english):
        return "needs_review", "english_demonym_family_uncertain_but_suffix_possible"
    if source_key.startswith(("k_", "e_")):
        return "needs_review", "high_tier_title_requires_context"
    return "needs_review", "obscure_place_gentilic_requires_human_confirmation"


def write_report(*, txt_path: Path, csv_path: Path, queue_run_id: int, rows: list[dict[str, Any]], classified: list[dict[str, Any]]) -> None:
    class_counts = Counter(row["hint_class"] for row in classified)
    reason_counts = Counter(row["hint_reason"] for row in classified)
    bucket_counts = Counter(f"{row['hint_class']}|{row.get('queue_bucket')}" for row in classified)

    fieldnames = [
        "queue_item_id",
        "segment_id",
        "source_key",
        "queue_bucket",
        "hint_class",
        "hint_reason",
        "current_text",
        "repair_hint",
        "english_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in classified:
            writer.writerow(
                {
                    "queue_item_id": row["id"],
                    "segment_id": row["segment_id"],
                    "source_key": row.get("source_key"),
                    "queue_bucket": row.get("queue_bucket"),
                    "hint_class": row["hint_class"],
                    "hint_reason": row["hint_reason"],
                    "current_text": row.get("evidence_text"),
                    "repair_hint": row.get("repair_hint"),
                    "english_text": row.get("english_text"),
                }
            )

    lines = [
        "Spanish -es landed title adjective repair hint audit",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Rows: {len(rows):,}",
        "",
        "Hint classes:",
        *[f"- {key}: {value:,}" for key, value in class_counts.most_common()],
        "",
        "Reasons:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Class by bucket:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Samples:",
    ]
    for row in classified[:100]:
        lines.append(
            f"- {row['hint_class']} | {row['hint_reason']} | item={row['id']} "
            f"{row.get('source_key')} current={short(row.get('evidence_text'))} "
            f"hint={short(row.get('repair_hint'))} english={short(row.get('english_text'))}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This audit does not ingest decisions and does not write output.",
            "- plausible_suffix_only is still not production authority; it means the hint is worth human/checkpoint validation.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)

    classified: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        hint_class, hint_reason = classify_hint(row)
        item = dict(row)
        item["hint_class"] = hint_class
        item["hint_reason"] = hint_reason
        classified.append(item)
        counts[hint_class] += 1

    txt_path, csv_path = report_paths(settings, selected_queue_run_id)
    write_report(
        txt_path=txt_path,
        csv_path=csv_path,
        queue_run_id=selected_queue_run_id,
        rows=rows,
        classified=classified,
    )

    print("[issue_title_landed_adjective_spanish_es_hint_audit] Audit generated")
    print(f"[issue_title_landed_adjective_spanish_es_hint_audit] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_hint_audit] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_hint_audit] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_title_landed_adjective_spanish_es_hint_audit] {key}: {value:,}")
    print(f"[issue_title_landed_adjective_spanish_es_hint_audit] Report: {txt_path}")
    print(f"[issue_title_landed_adjective_spanish_es_hint_audit] CSV: {csv_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit repair hints for Spanish -es landed title adjective queue.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
