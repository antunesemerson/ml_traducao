from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_title_policy_route_diagnostic import RULE_VERSION as DIAGNOSTIC_RULE_VERSION
from issue_title_policy_route_diagnostic import latest_ledger_run_id, route_lane
from issue_title_policy_route_review_queue import key_prefix, route_bucket, suffix_hint


RULE_VERSION = "issue_title_policy_route_standard_review_queue_v1"
ISSUE_FAMILY = "title_policy_microagent"
QUEUE_STRATEGY = "title_policy_route_standard_stratified_sample"

LANE_AGENT_KEYS = {
    "landed_title_adjectives": "micro_landed_title_adjective_policy",
    "culture_title_labels": "specialist_culture_title_labels",
    "credit_role_labels": "micro_credit_role_label_translation",
    "title_ui_logic_text": "micro_title_ui_logic",
}

DECISION_OPTIONS = [
    "safe",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
    "false_positive_reopen",
]


def report_paths(settings: dict[str, Any], lane: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_lane = lane.replace("/", "_").replace("\\", "_")
    base = reports_dir / f"{stamp}_issue_title_policy_route_standard_review_queue_{safe_lane}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            queue_strategy TEXT NOT NULL,
            limit_count INTEGER NOT NULL DEFAULT 0,
            per_bucket INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            issue_family TEXT,
            issue_kind TEXT,
            agent_key TEXT,
            queue_bucket TEXT,
            priority_score REAL NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            suggested_decision TEXT,
            evidence_text TEXT,
            evidence_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            confirmed_text TEXT,
            reviewer_decision TEXT,
            reviewer_notes TEXT,
            corrected_text TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL
        );
        """
    )


def agent_key_for_lane(lane: str) -> str:
    return LANE_AGENT_KEYS.get(lane, f"micro_title_policy_{lane}")


def fetch_candidates(
    conn,
    *,
    ledger_run_id: int,
    lane: str,
    agent_key: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
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
            ) AS confirmed_text,
            EXISTS (
                SELECT 1
                FROM ml_issue_review_queue_items queued
                WHERE queued.ledger_item_id = item.id
                  AND queued.agent_key = ?
            ) AS already_queued_for_agent,
            EXISTS (
                SELECT 1
                FROM ml_issue_review_decisions decision
                WHERE decision.ledger_item_id = item.id
                  AND decision.agent_key = ?
                  AND decision.valid = 1
                  AND decision.validation_status = 'accepted'
            ) AS already_decided_for_agent
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
        (agent_key, agent_key, ledger_run_id, ISSUE_FAMILY),
    ).fetchall()

    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if route_lane(item) != lane:
            continue
        if not include_existing and (item["already_queued_for_agent"] or item["already_decided_for_agent"]):
            continue
        item["title_route_lane"] = lane
        item["title_route_bucket"] = route_bucket(item)
        item["key_prefix"] = key_prefix(str(item.get("source_key") or ""))
        item["suffix_hint"] = suffix_hint(str(item.get("evidence_text") or ""))
        output.append(item)
    return output


def priority_score(row: dict[str, Any]) -> float:
    prefix = row.get("key_prefix") or ""
    bucket = row.get("title_route_bucket") or ""
    score = 1000.0
    if prefix in {"c_", "d_", "k_", "e_", "b_"}:
        score += 100.0
    if "other_suffix" in bucket:
        score += 40.0
    if "multiword" in bucket:
        score += 30.0
    if row.get("suffix_hint") in {"suffix_ense", "suffix_ano", "suffix_iano"}:
        score += 15.0
    score += (int(row.get("segment_id") or 0) % 97) / 1000
    return round(score, 4)


def select_rows(rows: list[dict[str, Any]], *, limit: int, per_prefix: int) -> list[dict[str, Any]]:
    by_prefix: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_prefix[str(row["key_prefix"])].append(row)

    prefix_names = sorted(by_prefix, key=lambda key: (-len(by_prefix[key]), key))
    queues: dict[str, deque[dict[str, Any]]] = {}
    for prefix in prefix_names:
        ordered = sorted(
            by_prefix[prefix],
            key=lambda row: (
                row["title_route_bucket"],
                str(row.get("relative_path") or ""),
                int(row.get("source_line_number") or 0),
                int(row.get("segment_id") or 0),
            ),
        )
        queues[prefix] = deque(ordered)

    selected: list[dict[str, Any]] = []
    prefix_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    while any(queues.values()) and (not limit or len(selected) < limit):
        for prefix in prefix_names:
            queue = queues[prefix]
            if not queue:
                continue
            if per_prefix and prefix_counts[prefix] >= per_prefix:
                queue.clear()
                continue
            row = queue.popleft()
            bucket = str(row["title_route_bucket"])
            if per_prefix and bucket_counts[bucket] >= max(5, per_prefix // 3):
                continue
            row["queue_bucket"] = bucket
            row["priority_score"] = priority_score(row)
            row["suggested_decision"] = "classify_title_policy_route_lane"
            selected.append(row)
            prefix_counts[prefix] += 1
            bucket_counts[bucket] += 1
            if limit and len(selected) >= limit:
                break
    return selected


def insert_queue_run(
    conn,
    *,
    ledger_run_id: int,
    lane: str,
    agent_key: str,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
    per_prefix: int,
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
            ledger_run_id,
            agent_key,
            ISSUE_FAMILY,
            f"{QUEUE_STRATEGY}:{lane}",
            limit,
            per_prefix,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
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


def insert_queue_items(conn, *, queue_run_id: int, ledger_run_id: int, agent_key: str, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        evidence = parse_json(row.get("evidence_json"), {})
        evidence.update(
            {
                "diagnostic_rule_version": DIAGNOSTIC_RULE_VERSION,
                "queue_rule_version": RULE_VERSION,
                "title_route_lane": row["title_route_lane"],
                "title_route_bucket": row["title_route_bucket"],
                "key_prefix": row["key_prefix"],
                "suffix_hint": row["suffix_hint"],
            }
        )
        cur = conn.execute(
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
            (
                queue_run_id,
                ledger_run_id,
                int(row["id"]),
                int(row["segment_id"]),
                row.get("relative_path"),
                row.get("source_key"),
                row.get("source_line_number"),
                ISSUE_FAMILY,
                row.get("issue_kind") or "",
                agent_key,
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                row.get("evidence_text"),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            ),
        )
        row["queue_item_id"] = int(cur.lastrowid)


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    ledger_run_id: int,
    lane: str,
    agent_key: str,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    prefix_counts = Counter(row["key_prefix"] for row in selected)
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    candidate_bucket_counts = Counter(row["title_route_bucket"] for row in candidates)

    fieldnames = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "ledger_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "lane",
        "queue_bucket",
        "key_prefix",
        "suffix_hint",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "queue_item_id": row["queue_item_id"],
                    "ledger_item_id": row["id"],
                    "ledger_run_id": ledger_run_id,
                    "segment_id": row["segment_id"],
                    "relative_path": row.get("relative_path"),
                    "source_key": row.get("source_key"),
                    "source_line_number": row.get("source_line_number"),
                    "lane": lane,
                    "queue_bucket": row["queue_bucket"],
                    "key_prefix": row["key_prefix"],
                    "suffix_hint": row["suffix_hint"],
                    "priority_score": row["priority_score"],
                    "suggested_decision": row["suggested_decision"],
                    "evidence_text": row.get("evidence_text"),
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["queue_item_id"],
                        "ledger_run_id": ledger_run_id,
                        "ledger_item_id": int(row["id"]),
                        "segment_id": int(row["segment_id"]),
                        "relative_path": row.get("relative_path"),
                        "source_key": row.get("source_key"),
                        "source_line_number": row.get("source_line_number"),
                        "agent_key": agent_key,
                        "issue_family": ISSUE_FAMILY,
                        "lane": lane,
                        "queue_bucket": row["queue_bucket"],
                        "key_prefix": row["key_prefix"],
                        "suffix_hint": row["suffix_hint"],
                        "priority_score": row["priority_score"],
                        "suggested_decision": row["suggested_decision"],
                        "texts": {
                            "english_text": row.get("english_text"),
                            "spanish_text": row.get("spanish_text"),
                            "confirmed_text": row.get("confirmed_text"),
                            "evidence_text": row.get("evidence_text"),
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["queue_item_id"],
                        "ledger_item_id": row["id"],
                        "segment_id": row["segment_id"],
                        "lane": lane,
                        "queue_bucket": row["queue_bucket"],
                        "decision": "",
                        "decision_options": DECISION_OPTIONS,
                        "corrected_text": "",
                        "notes": "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    lines = [
        "Title policy route standard review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent key: {agent_key}",
        f"Lane: {lane}",
        "",
        "Selection:",
        f"- Candidates available after dedupe: {len(candidates):,}",
        f"- Selected: {len(selected):,}",
        "",
        "Candidate buckets:",
        *[f"- {key}: {value:,}" for key, value in candidate_bucket_counts.most_common()],
        "",
        "Selected prefixes:",
        *[f"- {key}: {value:,}" for key, value in prefix_counts.most_common()],
        "",
        "Selected buckets:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Decision options accepted by issue_review_ingest:",
        *[f"- {label}" for label in DECISION_OPTIONS],
        "",
        "Review guidance:",
        "- This queue is learning evidence only; it does not authorize output writes.",
        "- For landed title adjectives, mark safe only when the current adjective/demonym is acceptable PT-BR for the title context.",
        "- Use needs_domain_context for place names that require gazetteer/context instead of a broad suffix rule.",
        "- Use needs_new_microagent when the sample reveals a recurring subpattern not owned by this lane.",
        "",
        "Samples:",
    ]
    for row in selected[:30]:
        lines.append(
            f"- item={row['queue_item_id']} ledger={row['id']} segment={row['segment_id']} "
            f"{row.get('relative_path')}::{row.get('source_key')} "
            f"bucket={row['queue_bucket']} text={short(row.get('evidence_text'))}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    ledger_run_id: int | None = None,
    lane: str = "landed_title_adjectives",
    limit: int = 240,
    per_prefix: int = 60,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings, lane)
    agent_key = agent_key_for_lane(lane)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        candidates = fetch_candidates(
            conn,
            ledger_run_id=selected_ledger_run_id,
            lane=lane,
            agent_key=agent_key,
            include_existing=include_existing,
        )
        selected = select_rows(candidates, limit=limit, per_prefix=per_prefix)
        queue_run_id = insert_queue_run(
            conn,
            ledger_run_id=selected_ledger_run_id,
            lane=lane,
            agent_key=agent_key,
            selected=selected,
            paths=paths,
            limit=limit,
            per_prefix=per_prefix,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            ledger_run_id=selected_ledger_run_id,
            agent_key=agent_key,
            rows=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        ledger_run_id=selected_ledger_run_id,
        lane=lane,
        agent_key=agent_key,
        candidates=candidates,
        selected=selected,
    )

    print("[issue_title_policy_route_standard_review_queue] Queue generated")
    print(f"[issue_title_policy_route_standard_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_title_policy_route_standard_review_queue] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_title_policy_route_standard_review_queue] Agent key: {agent_key}")
    print(f"[issue_title_policy_route_standard_review_queue] Lane: {lane}")
    print(f"[issue_title_policy_route_standard_review_queue] Candidates after dedupe: {len(candidates):,}")
    print(f"[issue_title_policy_route_standard_review_queue] Selected: {len(selected):,}")
    print(f"[issue_title_policy_route_standard_review_queue] Report: {paths[0]}")
    print(f"[issue_title_policy_route_standard_review_queue] Decisions template: {paths[3]}")
    return {
        "queue_run_id": queue_run_id,
        "ledger_run_id": selected_ledger_run_id,
        "agent_key": agent_key,
        "lane": lane,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "report_path": str(paths[0]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a standard review queue for title-policy route lanes.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--lane", default="landed_title_adjectives")
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--per-prefix", type=int, default=60)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        ledger_run_id=args.ledger_run_id,
        lane=args.lane,
        limit=args.limit,
        per_prefix=args.per_prefix,
        include_existing=args.include_existing,
    )
