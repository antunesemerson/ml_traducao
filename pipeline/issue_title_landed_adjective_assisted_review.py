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


RULE_VERSION = "issue_title_landed_adjective_assisted_review_v1"
AGENT_KEY = "micro_landed_title_adjective_policy"

FINAL_SPANISH_ES_RE = re.compile(r"(?:\u00e9s|\u00e9s\b)$", re.IGNORECASE)
FINAL_I_RE = re.compile(r"\u00ed$", re.IGNORECASE)
FINAL_ON_RE = re.compile(r"\u00f3n$", re.IGNORECASE)
SPANISH_ORTHOGRAPHY_RE = re.compile(
    r"\b(?:griego|ruso|armenio|fris\u00f3n|saj\u00f3n|suebo|occidental)\b",
    re.IGNORECASE,
)
DIRECTION_RE = re.compile(r"\b(?:oriental|occidental|ocidental|norte\u00f1o|meridional|septentrional)\b", re.IGNORECASE)


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_assisted_review_queue_{queue_run_id}"
    return (
        base.with_name(base.name + "_decisions").with_suffix(".jsonl"),
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
    )


def short(value: str | None, limit: int = 140) -> str:
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
          AND queue_strategy LIKE 'title_policy_route_standard_stratified_sample:landed_title_adjectives%'
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No landed title adjective review queue found.")
    return int(row["id"])


def fetch_queue(conn, *, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    return dict(row)


def fetch_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM ml_issue_review_queue_items
            WHERE run_id = ?
            ORDER BY queue_bucket, source_key, id
            """,
            (queue_run_id,),
        )
    ]


def canonical_text(row: dict[str, Any]) -> str:
    return str(row.get("evidence_text") or row.get("confirmed_text") or "").strip()


def classify(row: dict[str, Any]) -> tuple[str, str, str]:
    text = canonical_text(row)
    lower = text.casefold()
    bucket = str(row.get("queue_bucket") or "")
    source_key = str(row.get("source_key") or "")
    english = str(row.get("english_text") or "")

    if not text:
        return "needs_domain_context", "", "empty_landed_title_adjective"
    if any(marker in text for marker in ("[", "]", "$", "{", "}")):
        return "needs_new_microagent", "", "dynamic_marker_in_title_adjective"
    if lower.startswith("de "):
        return "needs_new_microagent", "", "prepositional_title_adjective_requires_dedicated_policy"
    if FINAL_SPANISH_ES_RE.search(lower):
        return "needs_repair", "", "spanish_final_es_gentilic_needs_pt_br_suffix_review"
    if FINAL_ON_RE.search(lower):
        return "needs_repair", "", "spanish_final_on_gentilic_needs_pt_br_suffix_review"
    if "occidental" in lower:
        return "needs_repair", "", "spanish_double_c_occidental_requires_repair_but_not_full_text_proposal"
    if SPANISH_ORTHOGRAPHY_RE.search(lower):
        return "needs_repair", "", "spanish_or_unaccented_romance_gentilic_requires_pt_br_review"
    if FINAL_I_RE.search(lower):
        return "needs_domain_context", "", "final_i_gentilic_requires_place_name_gazetteer"
    if "multiword" in bucket:
        if DIRECTION_RE.search(lower) or any(word in english.casefold() for word in ("east", "west", "north", "south")):
            return "needs_new_microagent", "", "directional_landed_title_adjective_requires_subpolicy"
        return "needs_domain_context", "", "multiword_landed_title_adjective_requires_context"
    if bucket.endswith("other_suffix"):
        return "needs_domain_context", "", "other_suffix_landed_gentilic_requires_gazetteer"
    if bucket.endswith("suffix_ano") or bucket.endswith("suffix_iano"):
        return "needs_domain_context", "", "ano_iano_gentilic_possible_safe_but_requires_gazetteer"
    if bucket.endswith("suffix_ense") or bucket.endswith("suffix_ino") or bucket.endswith("suffix_ita"):
        return "needs_domain_context", "", "common_gentilic_suffix_possible_safe_but_requires_gazetteer"
    if source_key.isupper():
        return "needs_domain_context", "", "uppercase_special_title_key_requires_context"
    return "needs_domain_context", "", "conservative_landed_title_adjective_fallback"


def write_outputs(
    *,
    decisions_path: Path,
    report_path: Path,
    csv_path: Path,
    queue: dict[str, Any],
    rows: list[dict[str, Any]],
    classified: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in classified:
            payload = {
                "queue_run_id": row["run_id"],
                "queue_item_id": row["id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "decision": row["decision"],
                "corrected_text": row["corrected_text"],
                "notes": f"{RULE_VERSION}; {row['reason']}; bucket={row.get('queue_bucket')}; source_key={row.get('source_key')}",
                "reviewer": "codex_landed_title_adjective_assisted",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    fieldnames = [
        "queue_item_id",
        "segment_id",
        "source_key",
        "queue_bucket",
        "decision",
        "reason",
        "corrected_text",
        "evidence_text",
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
                    "decision": row["decision"],
                    "reason": row["reason"],
                    "corrected_text": row["corrected_text"],
                    "evidence_text": row.get("evidence_text"),
                    "english_text": row.get("english_text"),
                }
            )

    decision_bucket_counts = Counter(f"{row['decision']}|{row.get('queue_bucket')}" for row in classified)
    reason_counts = Counter(row["reason"] for row in classified)
    lines = [
        "Landed title adjective assisted review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue['id']}",
        f"Queue agent: {queue['agent_key']}",
        f"Rows: {len(rows):,}",
        f"Decisions file: {decisions_path}",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Reason counts:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Decision by bucket:",
        *[f"- {key}: {value:,}" for key, value in decision_bucket_counts.most_common()],
        "",
        "Representative samples:",
    ]
    for row in classified[:80]:
        lines.append(
            f"- {row['decision']} | {row.get('queue_bucket')} | item={row['id']} "
            f"segment={row['segment_id']} {row.get('source_key')} | {row['reason']} | "
            f"{short(row.get('evidence_text'))}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This file is assisted learning evidence only.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- Safe decisions are intentionally absent in v1; this pass separates repair/context/subpolicy needs before any closure bridge.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue = fetch_queue(conn, queue_run_id=selected_queue_run_id)
        rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)

    decisions_path, report_path, csv_path = report_paths(settings, int(queue["id"]))
    counts: Counter[str] = Counter()
    classified: list[dict[str, Any]] = []
    for row in rows:
        decision, corrected_text, reason = classify(row)
        item = dict(row)
        item["decision"] = decision
        item["corrected_text"] = corrected_text
        item["reason"] = reason
        counts[decision] += 1
        classified.append(item)

    write_outputs(
        decisions_path=decisions_path,
        report_path=report_path,
        csv_path=csv_path,
        queue=queue,
        rows=rows,
        classified=classified,
        counts=counts,
    )

    print("[issue_title_landed_adjective_assisted_review] Draft generated")
    print(f"[issue_title_landed_adjective_assisted_review] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_assisted_review] Queue run id: {queue['id']}")
    print(f"[issue_title_landed_adjective_assisted_review] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_title_landed_adjective_assisted_review] {key}: {value:,}")
    print(f"[issue_title_landed_adjective_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_assisted_review] Report: {report_path}")
    return {
        "queue_run_id": int(queue["id"]),
        "rows": len(rows),
        "counts": dict(counts),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a conservative assisted review for landed title adjective queues.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
