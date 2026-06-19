from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_title_landed_adjective_assisted_review import classify
from issue_title_landed_adjective_opportunity_scan import fetch_candidates
from issue_title_policy_route_diagnostic import latest_ledger_run_id
from issue_title_policy_route_standard_review_queue import ensure_tables


RULE_VERSION = "issue_title_landed_adjective_spanish_es_repair_queue_v1"
ISSUE_FAMILY = "title_policy_microagent"
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
QUEUE_STRATEGY = "landed_title_adjective_spanish_es_suffix_repair"
REPAIR_REASON = "spanish_final_es_gentilic_needs_pt_br_suffix_review"

DECISION_OPTIONS = [
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
    "safe",
]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_repair_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def repair_hint(text: str) -> str:
    stripped = text.strip()
    if stripped.endswith("és"):
        return stripped[:-2] + "ês"
    if stripped.endswith("És"):
        return stripped[:-2] + "Ês"
    return ""


def already_seen(conn, *, ledger_item_id: int) -> bool:
    queued = conn.execute(
        """
        SELECT 1
        FROM ml_issue_review_queue_items
        WHERE ledger_item_id = ?
          AND agent_key = ?
        LIMIT 1
        """,
        (ledger_item_id, AGENT_KEY),
    ).fetchone()
    if queued is not None:
        return True
    decided = conn.execute(
        """
        SELECT 1
        FROM ml_issue_review_decisions
        WHERE ledger_item_id = ?
          AND agent_key = ?
          AND valid = 1
          AND validation_status = 'accepted'
        LIMIT 1
        """,
        (ledger_item_id, AGENT_KEY),
    ).fetchone()
    return decided is not None


def focus_candidates(conn, *, ledger_run_id: int, include_existing: bool) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in fetch_candidates(conn, ledger_run_id=ledger_run_id):
        decision, _corrected_text, reason = classify(row)
        if decision != "needs_repair" or reason != REPAIR_REASON:
            continue
        if not include_existing and already_seen(conn, ledger_item_id=int(row["id"])):
            continue
        item = dict(row)
        item["decision_projection"] = decision
        item["repair_reason"] = reason
        item["repair_hint"] = repair_hint(str(item.get("evidence_text") or ""))
        item["focus_bucket"] = f"{REPAIR_REASON}:{item['queue_bucket']}"
        output.append(item)
    return output


def priority_score(row: dict[str, Any]) -> float:
    score = 1000.0
    bucket = str(row.get("queue_bucket") or "")
    prefix = str(row.get("key_prefix") or "")
    if prefix == "c_":
        score += 60
    elif prefix == "d_":
        score += 40
    elif prefix in {"k_", "e_", "b_"}:
        score += 20
    if bucket.endswith("other_suffix"):
        score += 25
    if "multiword" in bucket:
        score += 10
    score += (int(row.get("segment_id") or 0) % 97) / 1000
    return round(score, 4)


def select_rows(rows: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    if limit <= 0:
        limit = len(rows)
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_bucket[str(row["queue_bucket"])].append(row)

    bucket_names = sorted(by_bucket, key=lambda key: (-len(by_bucket[key]), key))
    queues: dict[str, deque[dict[str, Any]]] = {}
    for bucket in bucket_names:
        ordered = sorted(
            by_bucket[bucket],
            key=lambda row: (
                str(row.get("key_prefix") or ""),
                str(row.get("source_key") or ""),
                int(row.get("segment_id") or 0),
            ),
        )
        queues[bucket] = deque(ordered)

    selected: list[dict[str, Any]] = []
    bucket_counts: Counter[str] = Counter()
    while any(queues.values()) and len(selected) < limit:
        for bucket in bucket_names:
            if len(selected) >= limit:
                break
            queue = queues[bucket]
            if not queue:
                continue
            if per_bucket and bucket_counts[bucket] >= per_bucket:
                queue.clear()
                continue
            row = queue.popleft()
            row["priority_score"] = priority_score(row)
            row["queue_bucket"] = row["focus_bucket"]
            row["suggested_decision"] = "review_landed_title_spanish_es_suffix_repair"
            selected.append(row)
            bucket_counts[bucket] += 1
    return selected


def insert_queue_run(
    conn,
    *,
    ledger_run_id: int,
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
            ledger_run_id,
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            limit,
            per_bucket,
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


def insert_queue_items(conn, *, queue_run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        evidence = parse_json(row.get("evidence_json"), {})
        evidence.update(
            {
                "queue_rule_version": RULE_VERSION,
                "repair_reason": REPAIR_REASON,
                "repair_hint": row.get("repair_hint") or "",
                "source_agent": "micro_landed_title_adjective_policy",
                "source_lane": "landed_title_adjectives",
                "source_bucket": row.get("title_route_bucket") or row.get("queue_bucket"),
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
                AGENT_KEY,
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
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    candidate_buckets = Counter(row["queue_bucket"] for row in candidates)
    selected_buckets = Counter(row["queue_bucket"] for row in selected)
    prefix_counts = Counter(row.get("key_prefix") or "" for row in selected)

    fieldnames = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "key_prefix",
        "suffix_hint",
        "priority_score",
        "evidence_text",
        "english_text",
        "repair_hint",
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
                    "segment_id": row["segment_id"],
                    "relative_path": row.get("relative_path"),
                    "source_key": row.get("source_key"),
                    "source_line_number": row.get("source_line_number"),
                    "queue_bucket": row["queue_bucket"],
                    "key_prefix": row.get("key_prefix"),
                    "suffix_hint": row.get("suffix_hint"),
                    "priority_score": row["priority_score"],
                    "evidence_text": row.get("evidence_text"),
                    "english_text": row.get("english_text"),
                    "repair_hint": row.get("repair_hint"),
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
                        "agent_key": AGENT_KEY,
                        "issue_family": ISSUE_FAMILY,
                        "queue_bucket": row["queue_bucket"],
                        "priority_score": row["priority_score"],
                        "suggested_decision": row["suggested_decision"],
                        "repair_hint": row.get("repair_hint"),
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
                        "source_key": row.get("source_key"),
                        "current_text": row.get("evidence_text"),
                        "english_text": row.get("english_text"),
                        "repair_hint": row.get("repair_hint"),
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
        "Landed title adjective Spanish -es suffix repair queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent key: {AGENT_KEY}",
        f"Repair reason: {REPAIR_REASON}",
        "",
        "Selection:",
        f"- Candidates available after dedupe: {len(candidates):,}",
        f"- Selected: {len(selected):,}",
        "",
        "Candidate buckets:",
        *[f"- {key}: {value:,}" for key, value in candidate_buckets.most_common()],
        "",
        "Selected buckets:",
        *[f"- {key}: {value:,}" for key, value in selected_buckets.most_common()],
        "",
        "Selected prefixes:",
        *[f"- {key}: {value:,}" for key, value in prefix_counts.most_common()],
        "",
        "Review guidance:",
        "- This queue is learning evidence only and does not authorize output writes.",
        "- Most rows likely need a PT-BR correction from Spanish final -es to an appropriate gentilic form.",
        "- The repair_hint is a hypothesis only; reviewers must fill corrected_text when decision is needs_repair.",
        "- Use needs_domain_context when the gentilic cannot be corrected without place/domain knowledge.",
        "- Use safe only if the current text is already acceptable PT-BR.",
        "",
        "Samples:",
    ]
    for row in selected[:80]:
        lines.append(
            f"- item={row['queue_item_id']} segment={row['segment_id']} {row.get('source_key')} "
            f"bucket={row['queue_bucket']} current={short(row.get('evidence_text'))} "
            f"hint={row.get('repair_hint') or '-'} english={short(row.get('english_text'), 80)}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    ledger_run_id: int | None = None,
    limit: int = 445,
    per_bucket: int = 0,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        candidates = focus_candidates(
            conn,
            ledger_run_id=selected_ledger_run_id,
            include_existing=include_existing,
        )
        selected = select_rows(candidates, limit=limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            ledger_run_id=selected_ledger_run_id,
            selected=selected,
            paths=paths,
            limit=limit,
            per_bucket=per_bucket,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            ledger_run_id=selected_ledger_run_id,
            rows=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        ledger_run_id=selected_ledger_run_id,
        candidates=candidates,
        selected=selected,
    )

    print("[issue_title_landed_adjective_spanish_es_repair_queue] Queue generated")
    print(f"[issue_title_landed_adjective_spanish_es_repair_queue] Queue run id: {queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_repair_queue] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_repair_queue] Candidates after dedupe: {len(candidates):,}")
    print(f"[issue_title_landed_adjective_spanish_es_repair_queue] Selected: {len(selected):,}")
    print(f"[issue_title_landed_adjective_spanish_es_repair_queue] Report: {paths[0]}")
    print(f"[issue_title_landed_adjective_spanish_es_repair_queue] Decisions template: {paths[3]}")
    return {
        "queue_run_id": queue_run_id,
        "ledger_run_id": selected_ledger_run_id,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "report_path": str(paths[0]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a focused queue for Spanish -es landed title adjective repairs.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=445)
    parser.add_argument("--per-bucket", type=int, default=0)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        ledger_run_id=args.ledger_run_id,
        limit=args.limit,
        per_bucket=args.per_bucket,
        include_existing=args.include_existing,
    )
