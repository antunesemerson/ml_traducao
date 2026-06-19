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
from issue_event_gender_suffix_blocker_queue import ensure_review_tables
from issue_event_surface_subcluster_report import (
    DEFAULT_BLOCK_REASON,
    RULE_VERSION as REPORT_RULE_VERSION,
    fetch_event_run,
    fetch_rows,
    latest_event_checkpoint_run_id,
    short,
)


RULE_VERSION = "issue_event_surface_subcluster_queue_v2"
QUEUE_STRATEGY = "event_surface_subcluster_stratified_learning_queue"
DEFAULT_LIMIT = 60
DEFAULT_PER_CLUSTER = 8

TARGETS = {
    "micro_event_dialogue_option": {
        "issue_family": "event_dialogue_option_microagent",
        "suggested_decision": "audit_event_dialogue_option_surface",
        "review_hint": (
            "Review short event option/dialogue surfaces. Mark safe_short_label only when the current PT-BR "
            "is natural, complete in-game UI text, and all CK3 tokens are intact. Use needs_repair when the "
            "line needs a local rewrite; use needs_domain_context when the option needs surrounding event text."
        ),
    },
    "micro_requirement_tooltip_surface": {
        "issue_family": "requirement_tooltip_surface_microagent",
        "suggested_decision": "audit_requirement_tooltip_surface",
        "review_hint": (
            "Review requirement/tooltip-like event surfaces. Mark safe_short_label only when it is a clean "
            "UI requirement/effect line; use needs_repair for malformed PT-BR or residual Spanish; use "
            "needs_new_microagent if it is a distinct policy gap."
        ),
    },
    "micro_short_label_style": {
        "issue_family": "event_short_label_style_microagent",
        "suggested_decision": "audit_event_short_label_style",
        "review_hint": (
            "Review short labels from event surfaces. Mark safe_short_label only when the label is complete, "
            "natural, and not a fragment requiring event context."
        ),
    },
    "micro_effect_reward_phrase": {
        "issue_family": "event_effect_reward_phrase_microagent",
        "suggested_decision": "audit_effect_reward_phrase",
        "review_hint": (
            "Review effect/reward labels such as monthly experience or lifestyle XP modifiers. Mark safe_short_label "
            "only when the PT-BR label is canonical, concise, and preserves CK3 terms/tokens. Use needs_repair for "
            "duplicated XP/experience wording, wrong lifestyle names, or malformed reward text."
        ),
    },
    "micro_event_context_composer": {
        "issue_family": "event_context_composer_microagent",
        "suggested_decision": "audit_event_context_surface",
        "review_hint": (
            "Review event context/title/description fragments. Prefer needs_domain_context unless the visible "
            "surface is independently safe. Use composition_ready only when other microagents can close the "
            "remaining issues without wider event context."
        ),
    },
    "micro_event_surface_router": {
        "issue_family": "event_surface_router_microagent",
        "suggested_decision": "audit_event_surface_router",
        "review_hint": (
            "Review mixed event surfaces that the current router could not classify well. Choose the smallest "
            "useful next route: safe_short_label, needs_repair, needs_domain_context, needs_new_microagent, "
            "manual_exception, or composition_ready."
        ),
    },
}

DECISION_OPTIONS = [
    "safe_short_label",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
    "composition_ready",
]


def report_paths(settings: dict[str, Any], agent_key: str, run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_agent = re.sub(r"[^A-Za-z0-9_]+", "_", agent_key).strip("_")
    base = reports_dir / f"{stamp}_issue_event_surface_subcluster_queue_{safe_agent}_run_{run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def has_existing_review(conn, *, segment_id: int, agent_key: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM ml_issue_review_queue_items item
        WHERE item.segment_id = ?
          AND item.agent_key = ?
          AND item.review_status NOT IN ('superseded', 'stale_superseded')
        LIMIT 1
        """,
        (segment_id, agent_key),
    ).fetchone()
    if row is not None:
        return True
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
        (segment_id, agent_key),
    ).fetchone()
    return row is not None


def fetch_candidates(
    conn,
    *,
    event_checkpoint_run_id: int,
    target_agent: str,
    block_reason: str,
    cluster_key: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    rows = fetch_rows(conn, run_id=event_checkpoint_run_id, block_reason=block_reason)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["recommended_agent"] != target_agent:
            continue
        if cluster_key and row["cluster_key"] != cluster_key:
            continue
        if not include_existing and has_existing_review(
            conn, segment_id=int(row["segment_id"]), agent_key=target_agent
        ):
            continue
        source = conn.execute(
            """
            SELECT english_text, spanish_text
            FROM source_segments
            WHERE id = ?
            LIMIT 1
            """,
            (int(row["segment_id"]),),
        ).fetchone()
        if source:
            row["english_text"] = source["english_text"]
            row["spanish_text"] = source["spanish_text"]
        else:
            row["english_text"] = None
            row["spanish_text"] = None
        candidates.append(row)
    return candidates


def priority_score(row: dict[str, Any], cluster_size: int) -> float:
    score = 1000.0
    score += min(cluster_size, 500) * 1.5
    score += max(0, 120 - int(row.get("char_count") or 0)) / 3
    score += min(int(row.get("token_count") or 0), 4) * 12
    if row.get("key_surface") == "option_letter_key":
        score += 60
    if row.get("text_signature") == "dialogue_option_phrase":
        score += 45
    if row.get("path_domain") == "events":
        score += 20
    score += (int(row.get("segment_id") or 0) % 997) / 1000
    return round(score, 4)


def select_stratified(candidates: list[dict[str, Any]], *, limit: int, per_cluster: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        buckets[row["cluster_key"]].append(row)
    for cluster_key, rows in buckets.items():
        cluster_size = len(rows)
        for row in rows:
            row["cluster_size"] = cluster_size
            row["priority_score"] = priority_score(row, cluster_size)
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], int(item.get("source_line_number") or 0)))

    ordered_clusters = sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))
    selected: list[dict[str, Any]] = []
    for _, rows in ordered_clusters:
        if len(selected) >= limit:
            break
        take = min(max(1, per_cluster), len(rows), limit - len(selected))
        selected.extend(rows[:take])
    return selected


def insert_queue_run(
    conn,
    *,
    event_run: dict[str, Any],
    agent_key: str,
    target: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
    per_cluster: int,
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
            int(event_run["ledger_run_id"]),
            agent_key,
            target["issue_family"],
            QUEUE_STRATEGY,
            limit,
            per_cluster,
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


def insert_queue_items(
    conn,
    *,
    queue_run_id: int,
    event_run: dict[str, Any],
    agent_key: str,
    target: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    now = db.utc_now()
    for row in rows:
        evidence = {
            "event_checkpoint_run_id": int(event_run["id"]),
            "event_checkpoint_item_id": int(row["id"]),
            "event_report_rule_version": REPORT_RULE_VERSION,
            "dry_run_id": int(event_run["dry_run_id"]),
            "source_block_reason": row.get("block_reason"),
            "queue_strategy": QUEUE_STRATEGY,
            "path_domain": row.get("path_domain"),
            "top_package": row.get("top_package"),
            "key_surface": row.get("key_surface"),
            "text_signature": row.get("text_signature"),
            "cluster_key": row.get("cluster_key"),
            "cluster_size": int(row.get("cluster_size") or 0),
            "recommended_action": row.get("recommended_action"),
            "char_count": int(row.get("char_count") or 0),
            "token_count": int(row.get("token_count") or 0),
        }
        queue_bucket = f"{row['text_signature']}::{row['key_surface']}::{row['path_domain']}"
        row["queue_bucket"] = queue_bucket
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
                int(event_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                target["issue_family"],
                row.get("issue_kind") or row["text_signature"],
                agent_key,
                queue_bucket,
                float(row.get("priority_score") or 0),
                target["suggested_decision"],
                row.get("current_text") or "",
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("current_text") or "",
                now,
            ),
        )
        row["queue_item_id"] = int(cur.lastrowid)


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    event_run: dict[str, Any],
    agent_key: str,
    target: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
    per_cluster: int,
    cluster_key: str,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fields = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "cluster_size",
        "priority_score",
        "text_signature",
        "key_surface",
        "path_domain",
        "recommended_action",
        "char_count",
        "token_count",
        "evidence_text",
        "english_text",
        "spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("current_text") or ""
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("current_text") or ""
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row["segment_id"],
                "decision": "pending",
                "corrected_text": "",
                "notes": "",
                "decision_options": DECISION_OPTIONS,
                "review_hint": target["review_hint"],
                "evidence_text": row.get("current_text") or "",
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "cluster": {
                    "text_signature": row.get("text_signature"),
                    "key_surface": row.get("key_surface"),
                    "path_domain": row.get("path_domain"),
                    "cluster_size": row.get("cluster_size"),
                    "recommended_action": row.get("recommended_action"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    cluster_counts = Counter(row["cluster_key"] for row in selected)
    candidate_clusters = Counter(row["cluster_key"] for row in candidates)
    lines = [
        "Issue event surface subcluster queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Event checkpoint run id: {event_run['id']}",
        f"Agent key: {agent_key}",
        f"Issue family: {target['issue_family']}",
        f"Limit: {limit}",
        f"Per cluster: {per_cluster}",
        f"Cluster filter: {cluster_key or 'none'}",
        "",
        "Summary:",
        f"- candidates: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        f"- candidate clusters: {len(candidate_clusters):,}",
        f"- selected clusters: {len(cluster_counts):,}",
        "",
        "Selected clusters:",
    ]
    for cluster, count in cluster_counts.most_common(30):
        lines.append(f"- {cluster}: selected={count}, candidates={candidate_clusters[cluster]}")
    lines.append("")
    lines.append("Samples:")
    for row in selected[:120]:
        lines.append(
            f"- queue_item={row.get('queue_item_id')} segment={row['segment_id']} "
            f"| cluster={row['text_signature']}/{row['key_surface']}/{row['path_domain']} "
            f"| {row['relative_path']}:{row.get('source_line_number') or '?'} "
            f"| {row['source_key']} :: {short(row.get('current_text'), 220)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Queue only: no source/output file reads, no confirmation updates and no output writes.",
            "- Decisions must be ingested later with issue_review_ingest after human/model review.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    event_checkpoint_run_id: int | None = None,
    agent_key: str,
    block_reason: str = DEFAULT_BLOCK_REASON,
    cluster_key: str = "",
    limit: int = DEFAULT_LIMIT,
    per_cluster: int = DEFAULT_PER_CLUSTER,
    include_existing: bool = False,
) -> dict[str, Any]:
    if agent_key not in TARGETS:
        raise RuntimeError(f"Unknown target agent: {agent_key}. Options: {', '.join(sorted(TARGETS))}")

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_review_tables(conn)
        selected_event_run_id = event_checkpoint_run_id or latest_event_checkpoint_run_id(conn)
        event_run = fetch_event_run(conn, run_id=selected_event_run_id)
        candidates = fetch_candidates(
            conn,
            event_checkpoint_run_id=selected_event_run_id,
            target_agent=agent_key,
            block_reason=block_reason,
            cluster_key=cluster_key,
            include_existing=include_existing,
        )
        selected = select_stratified(
            candidates,
            limit=max(0, limit),
            per_cluster=max(1, per_cluster),
        )
        target = TARGETS[agent_key]
        paths = report_paths(settings, agent_key, selected_event_run_id)
        queue_run_id = insert_queue_run(
            conn,
            event_run=event_run,
            agent_key=agent_key,
            target=target,
            selected=selected,
            paths=paths,
            limit=limit,
            per_cluster=per_cluster,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            event_run=event_run,
            agent_key=agent_key,
            target=target,
            rows=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        event_run=event_run,
        agent_key=agent_key,
        target=target,
        candidates=candidates,
        selected=selected,
        limit=limit,
        per_cluster=per_cluster,
        cluster_key=cluster_key,
    )
    txt_path, _, jsonl_path, decisions_template_path = paths
    print("[issue_event_surface_subcluster_queue] Queue generated")
    print(f"[issue_event_surface_subcluster_queue] Queue run id: {queue_run_id}")
    print(f"[issue_event_surface_subcluster_queue] Event checkpoint run id: {selected_event_run_id}")
    print(f"[issue_event_surface_subcluster_queue] Agent key: {agent_key}")
    print(f"[issue_event_surface_subcluster_queue] Candidates: {len(candidates):,}")
    print(f"[issue_event_surface_subcluster_queue] Selected: {len(selected):,}")
    print(f"[issue_event_surface_subcluster_queue] Report: {txt_path}")
    print(f"[issue_event_surface_subcluster_queue] JSONL: {jsonl_path}")
    print(f"[issue_event_surface_subcluster_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "event_checkpoint_run_id": selected_event_run_id,
        "agent_key": agent_key,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a focused review queue from event surface subclusters.")
    parser.add_argument("--event-checkpoint-run-id", type=int, default=None)
    parser.add_argument("--agent-key", required=True, choices=sorted(TARGETS))
    parser.add_argument("--block-reason", default=DEFAULT_BLOCK_REASON)
    parser.add_argument("--cluster-key", default="")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--per-cluster", type=int, default=DEFAULT_PER_CLUSTER)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        event_checkpoint_run_id=args.event_checkpoint_run_id,
        agent_key=args.agent_key,
        block_reason=args.block_reason,
        cluster_key=args.cluster_key,
        limit=args.limit,
        per_cluster=args.per_cluster,
        include_existing=args.include_existing,
    )
