from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_semantic_recheck_queue_v1"
AGENT_KEY = "composition_coordinator_short_label_semantic"
ISSUE_FAMILY = "segment_composition"
QUEUE_STRATEGY = "short_label_semantic_whole_segment_recheck"
DEFAULT_BUCKET = "dual_covered_ready_recheck"
DEFAULT_LIMIT = 160
DEFAULT_PER_BUCKET = 40

DECISION_OPTIONS = [
    "composition_ready",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
]


def latest_diagnostic_run_id(conn, diagnostic_run_id: int | None) -> int:
    if diagnostic_run_id is not None:
        return diagnostic_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_semantic_composition_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label + semantic diagnostic run found.")
    return int(row["id"])


def fetch_diagnostic_run(conn, diagnostic_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_semantic_composition_diagnostic_runs
        WHERE id = ?
        """,
        (diagnostic_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Diagnostic run not found: {diagnostic_run_id}")
    return dict(row)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_semantic_recheck_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def short(value: str | None, limit: int = 240) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def queue_bucket(row: dict[str, Any]) -> str:
    surface = str(row.get("surface_bucket") or "surface_unknown")
    has_token = int(row.get("has_ck3_token") or 0) == 1
    path = str(row.get("path_group") or "path_unknown")
    if surface in {"single_word", "short_phrase_2_3"} and not has_token:
        return "recheck_short_no_token"
    if surface in {"single_word", "short_phrase_2_3"}:
        return "recheck_short_tokenized"
    if surface == "compact_phrase_4_8" and not has_token:
        return "recheck_compact_no_token"
    if surface == "compact_phrase_4_8":
        return "recheck_compact_tokenized"
    if path in {"event_localization", "dlc"}:
        return "recheck_long_event_or_dlc"
    return "recheck_long_or_special"


def priority_score(row: dict[str, Any]) -> float:
    bucket = queue_bucket(row)
    score = {
        "recheck_short_no_token": 1200,
        "recheck_compact_no_token": 1120,
        "recheck_short_tokenized": 980,
        "recheck_compact_tokenized": 900,
        "recheck_long_event_or_dlc": 700,
        "recheck_long_or_special": 620,
    }.get(bucket, 500)
    score += min(int(row.get("covered_issue_count") or 0), 8) * 40
    score -= min(int(row.get("open_issue_count") or 0), 8) * 120
    score -= min(int(row.get("word_count") or 0), 80) * 1.5
    score += int(row.get("segment_id") or 0) % 23 / 100
    return round(score, 4)


def fetch_anchor_ledger_items(conn, *, ledger_run_id: int, segment_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND status = 'open'
        ORDER BY
          segment_id,
          CASE issue_family
              WHEN 'semantic_review_router' THEN 0
              WHEN 'short_label_style_microagent' THEN 1
              ELSE 2
          END,
          issue_family,
          id
        """,
        (ledger_run_id, *sorted(segment_ids)),
    ).fetchall()
    anchors: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        segment_id = int(payload["segment_id"])
        anchors.setdefault(segment_id, payload)
    return anchors


def fetch_candidates(
    conn,
    *,
    diagnostic_run: dict[str, Any],
    bucket: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              JOIN ml_issue_review_queue_runs qrun ON qrun.id = queued.run_id
              WHERE queued.segment_id = item.segment_id
                AND qrun.agent_key = ?
                AND qrun.queue_strategy = ?
          )
        """
    )
    params: list[Any] = [int(diagnostic_run["id"]), bucket]
    if not include_existing:
        params.extend([AGENT_KEY, QUEUE_STRATEGY])
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_short_label_semantic_composition_diagnostic_items item
        WHERE item.run_id = ?
          AND item.composition_bucket = ?
          {existing_filter}
        ORDER BY item.path_group, item.surface_bucket, item.relative_path, item.source_line_number
        """,
        tuple(params),
    ).fetchall()
    candidates = [dict(row) for row in rows]
    anchors = fetch_anchor_ledger_items(
        conn,
        ledger_run_id=int(diagnostic_run["ledger_run_id"]),
        segment_ids={int(row["segment_id"]) for row in candidates},
    )
    enriched: list[dict[str, Any]] = []
    for row in candidates:
        anchor = anchors.get(int(row["segment_id"]))
        if anchor is None:
            continue
        row["ledger_item_id"] = int(anchor["id"])
        row["issue_family"] = anchor.get("issue_family") or ISSUE_FAMILY
        row["issue_kind"] = anchor.get("issue_kind") or "whole_segment_recheck"
        row["evidence_text"] = (
            row.get("confirmed_text")
            or row.get("output_text")
            or row.get("old_text")
            or row.get("spanish_text")
            or ""
        )
        row["queue_bucket"] = queue_bucket(row)
        row["priority_score"] = priority_score(row)
        row["suggested_decision"] = "classify_short_label_semantic_composition"
        row["agent_key"] = AGENT_KEY
        enriched.append(row)
    return enriched


def select_stratified(rows: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(lambda: defaultdict(deque))
    for row in rows:
        grouped[row["queue_bucket"]][row["path_group"]].append(row)
    for path_groups in grouped.values():
        for path_group, bucket_rows in list(path_groups.items()):
            path_groups[path_group] = deque(
                sorted(
                    bucket_rows,
                    key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"]),
                )
            )

    selected: list[dict[str, Any]] = []
    selected_segments: set[int] = set()
    bucket_order = [
        "recheck_short_no_token",
        "recheck_compact_no_token",
        "recheck_short_tokenized",
        "recheck_compact_tokenized",
        "recheck_long_event_or_dlc",
        "recheck_long_or_special",
    ]
    for bucket in bucket_order:
        path_groups = grouped.get(bucket) or {}
        packages = deque(sorted(path_groups, key=lambda key: (-len(path_groups[key]), key)))
        picked = 0
        while packages and picked < per_bucket and len(selected) < limit:
            package = packages.popleft()
            rows_for_path = path_groups[package]
            while rows_for_path:
                row = rows_for_path.popleft()
                segment_id = int(row["segment_id"])
                if segment_id in selected_segments:
                    continue
                selected.append(row)
                selected_segments.add(segment_id)
                picked += 1
                break
            if rows_for_path:
                packages.append(package)

    if len(selected) < limit:
        remaining = [row for row in rows if int(row["segment_id"]) not in selected_segments]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"], item["path_group"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
            selected_segments.add(int(row["segment_id"]))
    return selected


def insert_queue_run(
    conn,
    *,
    diagnostic_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
    per_bucket: int,
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    cur = conn.execute(
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
            int(diagnostic_run["ledger_run_id"]),
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
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def insert_queue_items(conn, *, queue_run_id: int, diagnostic_run: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    payload = []
    for row in rows:
        evidence = {
            "_short_label_semantic_composition": {
                "diagnostic_run_id": int(diagnostic_run["id"]),
                "partial_coverage_run_id": int(diagnostic_run["partial_coverage_run_id"]),
                "composition_bucket": row["composition_bucket"],
                "opportunity_tier": row["opportunity_tier"],
                "coverage_state": row["coverage_state"],
                "review_state": row["review_state"],
                "total_issue_count": int(row["total_issue_count"] or 0),
                "covered_issue_count": int(row["covered_issue_count"] or 0),
                "open_issue_count": int(row["open_issue_count"] or 0),
                "surface_bucket": row["surface_bucket"],
                "path_group": row["path_group"],
                "has_ck3_token": bool(row["has_ck3_token"]),
            }
        }
        payload.append(
            (
                queue_run_id,
                int(diagnostic_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["issue_family"],
                row["issue_kind"],
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                "pending",
                row["suggested_decision"],
                row.get("evidence_text"),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            )
        )
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    diagnostic_run: dict[str, Any],
    queue_run_id: int,
    candidates_count: int,
    selected: list[dict[str, Any]],
    limit: int,
    per_bucket: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fieldnames = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "queue_bucket",
        "composition_bucket",
        "opportunity_tier",
        "coverage_state",
        "review_state",
        "total_issue_count",
        "covered_issue_count",
        "open_issue_count",
        "surface_bucket",
        "word_count",
        "has_ck3_token",
        "priority_score",
        "suggested_decision",
    ]
    rows = [
        dict(row, queue_run_id=queue_run_id, queue_item_id=row.get("queue_item_id"))
        for row in selected
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload.update(
                {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                }
            )
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row.get("queue_item_id"),
                        "ledger_item_id": row.get("ledger_item_id"),
                        "segment_id": row.get("segment_id"),
                        "decision": "pending",
                        "corrected_text": "",
                        "notes": "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    path_counts = Counter(row["path_group"] for row in selected)
    surface_counts = Counter(row["surface_bucket"] for row in selected)
    lines = [
        "Short-label + semantic-review whole-segment recheck queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Diagnostic run id: {diagnostic_run['id']}",
        f"Partial coverage run id: {diagnostic_run['partial_coverage_run_id']}",
        f"Ledger run id: {diagnostic_run['ledger_run_id']}",
        f"Segment-state run id: {diagnostic_run['segment_state_run_id']}",
        "Production release allowed: 0",
        "",
        "Selection:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(selected):,}",
        f"- Limit: {limit:,}",
        f"- Per bucket: {per_bucket:,}",
        "",
        "Decision options:",
        *[f"- {option}" for option in DECISION_OPTIONS],
        "",
        "Queue buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Selected path groups:",
        *[f"- {path}: {count:,}" for path, count in path_counts.most_common(20)],
        "",
        "Selected surfaces:",
        *[f"- {surface}: {count:,}" for surface, count in surface_counts.most_common()],
        "",
        "Review guidance:",
        "- Treat each row as a whole-segment composition check.",
        "- Use `composition_ready` only when the final current text is semantically correct, fluent PT-BR, token-safe, and has no visible Spanish residue.",
        "- Use `needs_repair` when a concrete corrected_text is required before closure.",
        "- Use `needs_new_microagent` when the failure is reusable and should become a new micro-neuron/subpolicy.",
        "- Do not apply output from this queue; it is learning-front evidence only.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in selected[:30]:
        lines.append(
            f"- segment {row['segment_id']} | {row['queue_bucket']} | "
            f"{row['relative_path']}::{row['source_key']} | "
            f"issues={row['covered_issue_count']}/{row['total_issue_count']} | "
            f"surface={row['surface_bucket']}"
        )
        lines.append(f"  confirmed: {short(row.get('confirmed_text'))}")
        lines.append(f"  english: {short(row.get('english_text'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def backfill_queue_item_ids(conn, *, queue_run_id: int, selected: list[dict[str, Any]]) -> None:
    rows = conn.execute(
        """
        SELECT id, ledger_item_id
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
        """,
        (queue_run_id,),
    ).fetchall()
    by_ledger = {int(row["ledger_item_id"]): int(row["id"]) for row in rows}
    for row in selected:
        row["queue_item_id"] = by_ledger.get(int(row["ledger_item_id"]))


def main(
    *,
    diagnostic_run_id: int | None = None,
    bucket: str = DEFAULT_BUCKET,
    limit: int = DEFAULT_LIMIT,
    per_bucket: int = DEFAULT_PER_BUCKET,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    print("[issue_short_label_semantic_recheck_queue] Starting queue")
    print(f"[issue_short_label_semantic_recheck_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_short_label_semantic_recheck_queue] Bucket: {bucket}")
    print(f"[issue_short_label_semantic_recheck_queue] Limit: {limit}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = latest_diagnostic_run_id(conn, diagnostic_run_id)
        diagnostic_run = fetch_diagnostic_run(conn, selected_diagnostic_run_id)
        candidates = fetch_candidates(
            conn,
            diagnostic_run=diagnostic_run,
            bucket=bucket,
            include_existing=include_existing,
        )
        selected = select_stratified(candidates, limit=limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            diagnostic_run=diagnostic_run,
            selected=selected,
            paths=paths,
            limit=limit,
            per_bucket=per_bucket,
        )
        insert_queue_items(conn, queue_run_id=queue_run_id, diagnostic_run=diagnostic_run, rows=selected)
        backfill_queue_item_ids(conn, queue_run_id=queue_run_id, selected=selected)
        conn.commit()
    write_outputs(
        paths=paths,
        diagnostic_run=diagnostic_run,
        queue_run_id=queue_run_id,
        candidates_count=len(candidates),
        selected=selected,
        limit=limit,
        per_bucket=per_bucket,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print(f"[issue_short_label_semantic_recheck_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_semantic_recheck_queue] Diagnostic run id: {diagnostic_run['id']}")
    print(f"[issue_short_label_semantic_recheck_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_semantic_recheck_queue] Selected: {len(selected):,}")
    print(f"[issue_short_label_semantic_recheck_queue] Report: {txt_path}")
    print(f"[issue_short_label_semantic_recheck_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "diagnostic_run_id": int(diagnostic_run["id"]),
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a whole-segment recheck queue from short-label + semantic composition diagnostics.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        bucket=args.bucket,
        limit=args.limit,
        per_bucket=args.per_bucket,
        include_existing=args.include_existing,
    )
