from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_single_issue_residual_review_queue_v1"
AGENT_KEY = "micro_short_label_residual_repair"
ISSUE_FAMILY = "short_label_style_microagent"
ISSUE_KIND = "short_or_compact_label_reopened"
QUEUE_STRATEGY = "single_issue_short_label_residual_and_sentence_samples"

MOJIBAKE_A = chr(195)
MOJIBAKE_B = chr(194)
BAD_FRAGMENTS = (
    "#bol",
    "#bold no#!",
    " no#!",
    "sonido",
    "the beneficiary",
    "probabilidad",
    "reduccion",
    "reducción",
    "interludio",
    "iranio",
    "seran",
    "serán",
)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_single_issue_residual_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def latest_finished_id(conn, table: str) -> int:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table}
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No finished run found in {table}.")
    return int(row["id"])


def short(text: str | None, limit: int = 220) -> str:
    value = (text or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def classify_bucket(text: str) -> str:
    lower = text.lower()
    if MOJIBAKE_A in text or MOJIBAKE_B in text:
        return "mojibake_visible"
    if "#bol" in lower or "#bold no#!" in lower or " no#!" in lower:
        return "markup_or_no_literal"
    if any(fragment in lower for fragment in ("probabilidad", "reduccion", "reducción", "interludio", "iranio", "sonido")):
        return "spanish_lexical_residual"
    if "the beneficiary" in lower:
        return "english_visible"
    if '"' in text:
        return "dialogue_or_quote_surface"
    words = len(re.findall(r"\S+", text))
    if words > 10:
        return "sentence_like_short_label"
    return "residual_review_other"


def priority_score(bucket: str, text: str) -> float:
    base = {
        "markup_or_no_literal": 100.0,
        "spanish_lexical_residual": 95.0,
        "mojibake_visible": 90.0,
        "english_visible": 85.0,
        "dialogue_or_quote_surface": 60.0,
        "sentence_like_short_label": 50.0,
        "residual_review_other": 40.0,
    }.get(bucket, 10.0)
    return base + min(len(text), 200) / 1000.0


def fetch_candidates(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH counts AS (
            SELECT segment_id, COUNT(*) AS issue_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
            GROUP BY segment_id
        )
        SELECT
            item.id AS ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.issue_family,
            item.issue_kind,
            item.evidence_text,
            item.evidence_json,
            ss.english_text,
            ss.spanish_text,
            os.portuguese_text AS confirmed_text
        FROM ml_issue_ledger_items item
        JOIN counts c ON c.segment_id = item.segment_id
        LEFT JOIN source_segments ss ON ss.id = item.segment_id
        LEFT JOIN output_segments os ON os.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND c.issue_count = 1
          AND item.issue_family = ?
          AND item.issue_kind = ?
          AND item.status = 'open'
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.segment_id
        """,
        (ledger_run_id, ledger_run_id, ISSUE_FAMILY, ISSUE_KIND),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        text = str(item.get("evidence_text") or "")
        bucket = classify_bucket(text)
        if bucket in {"residual_review_other", "sentence_like_short_label"}:
            continue
        item["queue_bucket"] = bucket
        item["priority_score"] = priority_score(bucket, text)
        item["suggested_decision"] = "needs_repair"
        candidates.append(item)
    return candidates


def select_rows(candidates: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"]))

    selected: list[dict[str, Any]] = []
    for bucket in sorted(grouped, key=lambda key: (-len(grouped[key]), key)):
        for row in grouped[bucket][:per_bucket]:
            if len(selected) >= limit:
                return selected
            selected.append(row)
    if len(selected) < limit:
        seen = {int(row["ledger_item_id"]) for row in selected}
        remaining = [row for row in candidates if int(row["ledger_item_id"]) not in seen]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"], item["relative_path"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
    return selected


def write_reports(paths: tuple[Path, Path, Path, Path], *, run_id: int, ledger_run_id: int, rows: list[dict[str, Any]], candidates_count: int) -> None:
    txt_path, csv_path, jsonl_path, decisions_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    package_counts = Counter(row["relative_path"] for row in rows)

    lines = [
        "Short label single-issue residual review queue",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Summary:",
        f"- Candidates: {candidates_count:,}",
        f"- Selected: {len(rows):,}",
        "",
        "Buckets:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {key}: {value:,}" for key, value in package_counts.most_common(20)],
        "",
        "Samples:",
    ]
    for row in rows[:40]:
        lines.append(
            f"- {row['segment_id']} | {row['queue_bucket']} | {row['relative_path']}::{row['source_key']} | {short(row.get('evidence_text'))}"
        )
    lines.extend(["", "Safety note:", "- Queue only: no source/output writes, no confirmations, no production promotion."])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = {key: row.get(key) for key in fieldnames}
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                "queue_item_id": None,
                "ledger_item_id": int(row["ledger_item_id"]),
                "segment_id": int(row["segment_id"]),
                "decision": "",
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(*, ledger_run_id: int | None = None, limit: int = 120, per_bucket: int = 40) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_ledger_run_id = ledger_run_id or latest_finished_id(conn, "ml_issue_ledger_runs")
        candidates = fetch_candidates(conn, ledger_run_id=selected_ledger_run_id)
        selected = select_rows(candidates, limit=limit, per_bucket=per_bucket)
        bucket_counts = Counter(row["queue_bucket"] for row in selected)
        txt_path, csv_path, jsonl_path, decisions_path = paths
        run_id = conn.execute(
            """
            INSERT INTO ml_issue_review_queue_runs (
                rule_version,
                ledger_run_id,
                agent_key,
                issue_family,
                queue_strategy,
                limit_count,
                per_bucket,
                selected_count,
                open_count,
                reviewed_count,
                bucket_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_ledger_run_id,
                AGENT_KEY,
                ISSUE_FAMILY,
                QUEUE_STRATEGY,
                limit,
                per_bucket,
                len(selected),
                len(selected),
                json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_path),
                started_at,
                db.utc_now(),
                db.utc_now(),
            ),
        ).lastrowid
        now = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_review_queue_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                issue_kind,
                agent_key,
                queue_bucket,
                priority_score,
                review_status,
                suggested_decision,
                evidence_text,
                evidence_json,
                english_text,
                spanish_text,
                confirmed_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    selected_ledger_run_id,
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["issue_family"],
                    row["issue_kind"],
                    AGENT_KEY,
                    row["queue_bucket"],
                    float(row["priority_score"]),
                    row["suggested_decision"],
                    row.get("evidence_text") or "",
                    row.get("evidence_json") or "",
                    row.get("english_text") or "",
                    row.get("spanish_text") or "",
                    row.get("confirmed_text") or "",
                    now,
                )
                for row in selected
            ],
        )
        conn.commit()

    write_reports(paths, run_id=int(run_id), ledger_run_id=int(selected_ledger_run_id), rows=selected, candidates_count=len(candidates))
    print("[issue_short_label_single_issue_residual_review_queue] Queue generated")
    print(f"[issue_short_label_single_issue_residual_review_queue] Queue run id: {run_id}")
    print(f"[issue_short_label_single_issue_residual_review_queue] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_short_label_single_issue_residual_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_single_issue_residual_review_queue] Selected: {len(selected):,}")
    print(f"[issue_short_label_single_issue_residual_review_queue] Report: {paths[0]}")
    print(f"[issue_short_label_single_issue_residual_review_queue] Decisions template: {paths[3]}")
    return {
        "queue_run_id": int(run_id),
        "ledger_run_id": int(selected_ledger_run_id),
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(paths[0]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create residual/bad-literal review queue for single-issue short labels.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-bucket", type=int, default=40)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, limit=args.limit, per_bucket=args.per_bucket)
