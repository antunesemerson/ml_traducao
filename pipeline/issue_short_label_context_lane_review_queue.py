from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_context_lane_review_queue_v1"
QUEUE_STRATEGY = "short_label_context_lane_stratified_review"
ISSUE_FAMILY = "short_label_style_microagent"
DEFAULT_ROUTE_LANE = "custom_localization_fragment_context"
DECISION_OPTIONS = [
    "safe_fragment",
    "needs_domain_context",
    "needs_repair",
    "needs_new_microagent",
    "manual_exception",
]


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label pure no-token shadow blocker diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def priority_score(row: dict[str, Any]) -> float:
    text_length = int(row.get("text_length") or len(str(row.get("evidence_text") or "")))
    word_count = int(row.get("word_count") or 0)
    source_key = str(row.get("source_key") or "")
    score = 10.0
    if 12 <= text_length <= 90:
        score += 3.0
    if 3 <= word_count <= 12:
        score += 2.0
    if any(part in source_key for part in ("Descriptor", "Exchange", "Greeting", "War", "Conflict", "Toast")):
        score += 1.5
    if any(mark in str(row.get("evidence_text") or "") for mark in ("[", "]", "$", "#", "Select_", "Custom(")):
        score -= 3.0
    return score


def fetch_candidates(
    conn,
    *,
    diagnostic_run_id: int,
    route_lane: str,
    include_existing: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_runs
        WHERE id = ?
        """,
        (diagnostic_run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"Diagnostic run not found: {diagnostic_run_id}")

    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = item.suggested_agent_key
                AND queued.queue_bucket = 'short_label_context:' || item.route_lane
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.segment_id = item.segment_id
                AND decision.agent_key = item.suggested_agent_key
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
          )
        """
    )
    rows = conn.execute(
        f"""
        SELECT
            item.*
        FROM ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_items item
        WHERE item.run_id = ?
          AND item.route_lane = ?
          AND item.shadow_status != 'shadow_ready'
          {existing_filter}
        ORDER BY item.package, item.relative_path, item.source_line_number, item.source_key
        """,
        (diagnostic_run_id, route_lane),
    ).fetchall()
    return dict(run), [dict(row) for row in rows]


def stratified_select(rows: list[dict[str, Any]], *, limit: int, per_package: int, per_file: int) -> list[dict[str, Any]]:
    by_file: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(rows, key=lambda item: (-priority_score(item), item["relative_path"], item["source_line_number"] or 0)):
        row["priority_score"] = priority_score(row)
        by_file[str(row.get("relative_path") or "unknown")].append(row)

    selected: list[dict[str, Any]] = []
    seen_segments: set[int] = set()
    package_counts: Counter[str] = Counter()
    for relative_path, file_rows in sorted(by_file.items(), key=lambda item: (-len(item[1]), item[0])):
        picked = 0
        while file_rows and picked < per_file and len(selected) < limit:
            row = file_rows.popleft()
            segment_id = int(row["segment_id"])
            if segment_id in seen_segments:
                continue
            package = str(row.get("package") or "unknown")
            if package_counts[package] >= per_package:
                continue
            selected.append(row)
            seen_segments.add(segment_id)
            package_counts[package] += 1
            picked += 1

    file_cycle = deque(sorted(by_file, key=lambda key: (-len(by_file[key]), key)))
    while file_cycle and len(selected) < limit:
        relative_path = file_cycle.popleft()
        bucket = by_file[relative_path]
        while bucket:
            row = bucket.popleft()
            segment_id = int(row["segment_id"])
            if segment_id in seen_segments:
                continue
            package = str(row.get("package") or "unknown")
            if package_counts[package] >= per_package:
                break
            selected.append(row)
            seen_segments.add(segment_id)
            package_counts[package] += 1
            break
        if bucket:
            file_cycle.append(relative_path)
    return selected


def insert_generic_queue(
    conn,
    *,
    diagnostic_run: dict[str, Any],
    route_lane: str,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    now = db.utc_now()
    agent_key = selected[0]["suggested_agent_key"] if selected else f"{route_lane}_review"
    bucket = f"short_label_context:{route_lane}"
    bucket_counts = Counter(bucket for _ in selected)
    cursor = conn.execute(
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
            diagnostic_run.get("ledger_run_id"),
            agent_key,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            limit,
            limit,
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
    queue_run_id = int(cursor.lastrowid)
    inserted: list[dict[str, Any]] = []
    for row in selected:
        evidence_json = {
            "source": RULE_VERSION,
            "diagnostic_run_id": row["run_id"],
            "shadow_run_id": row["shadow_run_id"],
            "shadow_item_id": row["shadow_item_id"],
            "route_lane": route_lane,
            "recommendation": row["recommendation"],
            "decision_options": DECISION_OPTIONS,
        }
        item_cursor = conn.execute(
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, '', '', '', ?)
            """,
            (
                queue_run_id,
                diagnostic_run.get("ledger_run_id"),
                row["ledger_item_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row.get("issue_family") or ISSUE_FAMILY,
                row.get("issue_kind") or "",
                row["suggested_agent_key"],
                bucket,
                row["priority_score"],
                "audit_context_fragment",
                row.get("evidence_text") or "",
                json.dumps(evidence_json, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        inserted.append(
            {
                **row,
                "queue_run_id": queue_run_id,
                "queue_item_id": int(item_cursor.lastrowid),
                "queue_bucket": bucket,
                "suggested_decision": "audit_context_fragment",
            }
        )
    return queue_run_id, inserted


def write_files(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    diagnostic_run: dict[str, Any],
    route_lane: str,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fields = [
        "queue_run_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "route_lane",
        "suggested_agent_key",
        "queue_bucket",
        "priority_score",
        "text_length",
        "word_count",
        "evidence_text",
        "recommendation",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            writer.writerow({key: row.get(key) for key in fields})

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(json.dumps({key: row.get(key) for key in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row["queue_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "decision": "pending",
                "corrected_text": "",
                "notes": "",
                "reviewer": "",
                "allowed_decisions": DECISION_OPTIONS,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    selected_packages = Counter(str(row.get("package") or "unknown") for row in selected)
    lines = [
        "Short Label Context Lane Review Queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Diagnostic run id: {diagnostic_run.get('id')}",
        f"Shadow run id: {diagnostic_run.get('shadow_run_id')}",
        f"Ledger run id: {diagnostic_run.get('ledger_run_id')}",
        f"Route lane: {route_lane}",
        f"Candidates after filters: {len(candidates):,}",
        f"Selected: {len(selected):,}",
        "",
        "Review contract:",
        "- safe_fragment: the fragment is acceptable PT-BR and can be trusted only inside this lane after enough precision evidence.",
        "- needs_domain_context: the fragment may be correct, but cannot be safely judged without its host localization.",
        "- needs_repair: visible wording, style, residual language or punctuation needs correction.",
        "- needs_new_microagent: the case is systematic but belongs to a different future neuron.",
        "- manual_exception: keep as explicit exception if the game context justifies it.",
        "",
        "Selected by package:",
    ]
    for package, count in selected_packages.most_common():
        lines.append(f"- {package}: {count:,}")
    lines.extend(
        [
            "",
            "Files:",
            f"- csv: {csv_path}",
            f"- jsonl: {jsonl_path}",
            f"- decisions_template: {decisions_template_path}",
            "",
            "Safety note:",
            "- This queue does not write source/output and does not create confirmations.",
            "- It is a learning queue for route-level evidence.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    diagnostic_run_id: int | None = None,
    route_lane: str = DEFAULT_ROUTE_LANE,
    limit: int = 120,
    per_package: int = 40,
    per_file: int = 8,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        diagnostic_run, candidates = fetch_candidates(
            conn,
            diagnostic_run_id=selected_diagnostic_run_id,
            route_lane=route_lane,
            include_existing=include_existing,
        )
        selected = stratified_select(candidates, limit=limit, per_package=per_package, per_file=per_file)
        paths = report_paths(settings)
        queue_run_id, inserted = insert_generic_queue(
            conn,
            diagnostic_run=diagnostic_run,
            route_lane=route_lane,
            selected=selected,
            paths=paths,
            limit=limit,
        )
        conn.commit()

    write_files(
        paths=paths,
        queue_run_id=queue_run_id,
        diagnostic_run=diagnostic_run,
        route_lane=route_lane,
        candidates=candidates,
        selected=inserted,
    )

    print("[issue_short_label_context_lane_review_queue] Queue generated")
    print(f"[issue_short_label_context_lane_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_context_lane_review_queue] Diagnostic run id: {diagnostic_run.get('id')}")
    print(f"[issue_short_label_context_lane_review_queue] Route lane: {route_lane}")
    print(f"[issue_short_label_context_lane_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_context_lane_review_queue] Selected: {len(inserted):,}")
    print(f"[issue_short_label_context_lane_review_queue] Report: {paths[0]}")
    print(f"[issue_short_label_context_lane_review_queue] Decisions: {paths[3]}")
    return {
        "queue_run_id": queue_run_id,
        "diagnostic_run_id": diagnostic_run.get("id"),
        "route_lane": route_lane,
        "candidate_count": len(candidates),
        "selected_count": len(inserted),
        "report_path": str(paths[0]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a review queue for a routed short-label context lane.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--route-lane", default=DEFAULT_ROUTE_LANE)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-package", type=int, default=40)
    parser.add_argument("--per-file", type=int, default=8)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        route_lane=args.route_lane,
        limit=args.limit,
        per_package=args.per_package,
        per_file=args.per_file,
        include_existing=args.include_existing,
    )
