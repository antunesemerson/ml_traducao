from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_autofix_unknown_cluster_diagnostic import action_hint, authority_hint, surface_cluster
from issue_review_queue import percent, short


RULE_VERSION = "issue_autofix_unknown_cluster_review_queue_v1"
AGENT_KEY = "micro_autofix_unknown_router"
ISSUE_FAMILY = "autofix_unknown_microagent"
DEFAULT_CLUSTERS = (
    "ui_warning_or_blocker_text",
    "list_value_or_counter_text",
    "confirmation_or_question_text",
    "ui_tooltip_or_markup_text",
)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_cluster_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


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
    if not row:
        raise RuntimeError("No finished ml_issue_ledger_runs found. Run issue-ledger first.")
    return int(row["id"])


def parse_evidence(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("evidence_json") or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def priority(row: dict[str, Any], cluster: str) -> float:
    evidence = parse_evidence(row)
    token_count = int(evidence.get("token_count") or 0)
    word_count = int(evidence.get("word_count") or 0)
    text_length = int(evidence.get("text_length") or 0)
    base = {
        "ui_warning_or_blocker_text": 1200,
        "list_value_or_counter_text": 1050,
        "confirmation_or_question_text": 980,
        "ui_tooltip_or_markup_text": 780,
    }.get(cluster, 400)
    base += min(token_count, 12) * 16
    base += min(word_count, 80) * 1.5
    base += min(text_length, 600) / 30
    base += int(row.get("segment_id") or 0) % 17 / 100
    return round(base, 4)


def suggested_decision(cluster: str) -> str:
    return {
        "ui_warning_or_blocker_text": "classify_warning_tooltip_or_blocker",
        "list_value_or_counter_text": "classify_list_or_counter_surface",
        "confirmation_or_question_text": "classify_confirmation_prompt_surface",
        "ui_tooltip_or_markup_text": "classify_ui_markup_surface",
    }.get(cluster, "classify_autofix_unknown_surface")


def has_existing_segment_review(conn, *, segment_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM ml_issue_review_decisions decision
        WHERE decision.segment_id = ?
          AND decision.agent_key = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        LIMIT 1
        """,
        (segment_id, AGENT_KEY),
    ).fetchone()
    return row is not None


def fetch_candidates(
    conn,
    ledger_run_id: int,
    clusters: set[str],
    include_existing: bool,
    exclude_existing_segments: bool,
) -> list[dict[str, Any]]:
    existing_sql = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.ledger_item_id = item.id
                AND queued.agent_key = ?
          )
        """
    )
    params: list[Any] = [ledger_run_id, ISSUE_FAMILY]
    if not include_existing:
        params.append(AGENT_KEY)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_ledger_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.issue_family = ?
          AND item.status = 'open'
          {existing_sql}
        ORDER BY item.segment_id
        """,
        tuple(params),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        cluster = surface_cluster(item)
        if cluster not in clusters:
            continue
        if exclude_existing_segments and has_existing_segment_review(conn, segment_id=int(item["segment_id"])):
            continue
        item["queue_bucket"] = cluster
        item["priority_score"] = priority(item, cluster)
        item["suggested_decision"] = suggested_decision(cluster)
        item["action_hint"] = action_hint(cluster)
        item["authority_hint"] = authority_hint(cluster)
        result.append(item)
    return result


def select_rows(candidates: list[dict[str, Any]], limit: int, per_cluster: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"]))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    cluster_order = [cluster for cluster in DEFAULT_CLUSTERS if grouped.get(cluster)]
    cluster_order.extend(sorted(cluster for cluster in grouped if cluster not in set(cluster_order)))

    for idx in range(per_cluster):
        for cluster in cluster_order:
            rows = grouped.get(cluster, [])
            if idx >= len(rows):
                continue
            if len(selected) >= limit:
                return selected
            row = rows[idx]
            selected.append(row)
            selected_ids.add(int(row["id"]))

    if len(selected) < limit:
        remaining = [row for row in candidates if int(row["id"]) not in selected_ids]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
    return selected


def insert_queue_run(
    conn,
    ledger_run_id: int,
    selected: list[dict[str, Any]],
    limit: int,
    per_cluster: int,
    clusters: list[str],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
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
            ledger_run_id,
            AGENT_KEY,
            ISSUE_FAMILY,
            "autofix_unknown_surface_cluster_review",
            limit,
            per_cluster,
            len(selected),
            len(selected),
            json.dumps(
                {
                    "clusters": clusters,
                    "selected": dict(bucket_counts.most_common()),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    ).lastrowid
    return int(run_id)


def insert_queue_items(conn, run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    payload = []
    for row in rows:
        payload.append(
            (
                run_id,
                ledger_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["issue_family"],
                row["issue_kind"],
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                "pending",
                row["suggested_decision"],
                row.get("evidence_text"),
                row.get("evidence_json"),
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
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    candidates_count: int,
    limit: int,
    per_cluster: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    package_counts = Counter((row.get("relative_path") or "unknown").split("/", 1)[0] for row in rows)
    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "action_hint",
        "authority_hint",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "ledger_item_id": row["id"],
                    **{name: row.get(name) for name in fieldnames if name not in {"queue_run_id", "ledger_item_id"}},
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_run_id": ledger_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": AGENT_KEY,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "action_hint": row["action_hint"],
                "authority_hint": row["authority_hint"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "evidence": parse_evidence(row),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    decision_options = [
        "safe_surface",
        "false_positive_reopen",
        "needs_repair",
        "needs_domain_context",
        "needs_new_microagent",
        "manual_exception",
    ]
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "cluster": row["queue_bucket"],
                "decision": "pending",
                "decision_options": decision_options,
                "corrected_text": "",
                "notes": "",
                "review_hint": "Classify this autofix_unknown surface. Use safe_surface only when current confirmed/output text is natural PT-BR, semantically aligned, and structurally token-safe. Prefer needs_domain_context for prose or ambiguous gameplay semantics.",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown cluster review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent: {AGENT_KEY}",
        "",
        "Coverage:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(rows):,} ({percent(len(rows), candidates_count):.2f}%)",
        f"- Limit: {limit:,}",
        f"- Per cluster: {per_cluster:,}",
        "",
        "Clusters:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Packages:",
        *[f"- {package}: {count:,}" for package, count in package_counts.most_common(20)],
        "",
        "Review guidance:",
        "- This queue does not authorize output writes.",
        "- Treat rows as surface-pattern evidence for splitting `autofix_unknown_microagent`.",
        "- Use `safe_surface` only for clearly safe UI/list/confirmation surfaces.",
        "- Use `needs_new_microagent` when the cluster is real but should be handled by a narrower reusable specialist.",
        "- Use `needs_domain_context` for prose, event meaning, building descriptions or ambiguous gameplay text.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in rows[:30]:
        lines.append(
            f"- ledger={row['id']} segment={row['segment_id']} | {row['queue_bucket']} | "
            f"{row['relative_path']}::{row['source_key']}"
        )
        lines.append(f"  text: {short(row.get('evidence_text'), 220)}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create autofix_unknown surface cluster review queue.")
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-cluster", type=int, default=30)
    parser.add_argument("--clusters", nargs="*", default=list(DEFAULT_CLUSTERS))
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument("--exclude-existing-segments", action="store_true")
    args = parser.parse_args()

    settings = db.load_settings()
    paths = report_paths(settings)
    clusters = set(args.clusters)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ledger_run_id = args.ledger_run_id or latest_ledger_run_id(conn)
        candidates = fetch_candidates(
            conn,
            ledger_run_id,
            clusters,
            args.include_existing,
            args.exclude_existing_segments,
        )
        selected = select_rows(candidates, args.limit, args.per_cluster)
        queue_run_id = insert_queue_run(
            conn,
            ledger_run_id,
            selected,
            args.limit,
            args.per_cluster,
            list(args.clusters),
            paths,
        )
        insert_queue_items(conn, queue_run_id, ledger_run_id, selected)
        conn.commit()

    write_outputs(paths, queue_run_id, ledger_run_id, selected, len(candidates), args.limit, args.per_cluster)
    print("[issue_autofix_unknown_cluster_review_queue] Queue generated")
    print(f"[issue_autofix_unknown_cluster_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_autofix_unknown_cluster_review_queue] Ledger run id: {ledger_run_id}")
    print(f"[issue_autofix_unknown_cluster_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_autofix_unknown_cluster_review_queue] Selected: {len(selected):,}")
    print(f"[issue_autofix_unknown_cluster_review_queue] Report: {paths[0]}")
    print(f"[issue_autofix_unknown_cluster_review_queue] Decisions template: {paths[3]}")


if __name__ == "__main__":
    main()
