from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_event_surface_subcluster_report import (
    clean_visible,
    key_surface,
    path_domain,
    recommended_next_step,
    text_signature,
    top_package,
)


RULE_VERSION = "issue_semantic_short_label_blocked_surface_queue_v1"
QUEUE_STRATEGY = "semantic_short_label_blocked_surface_current_run"
DEFAULT_LIMIT = 180
DEFAULT_PER_CLUSTER = 20

TARGETS = {
    "micro_short_label_style": {
        "issue_family": "event_short_label_style_microagent",
        "suggested_decision": "audit_current_blocked_short_label_style",
    },
    "micro_event_dialogue_option": {
        "issue_family": "event_dialogue_option_microagent",
        "suggested_decision": "audit_current_blocked_event_dialogue_option",
    },
    "micro_event_context_composer": {
        "issue_family": "event_context_composer_microagent",
        "suggested_decision": "audit_current_blocked_event_context",
    },
    "micro_requirement_tooltip_surface": {
        "issue_family": "requirement_tooltip_surface_microagent",
        "suggested_decision": "audit_current_blocked_requirement_tooltip",
    },
    "micro_event_surface_router": {
        "issue_family": "event_surface_router_microagent",
        "suggested_decision": "audit_current_blocked_event_surface_router",
    },
    "micro_effect_reward_phrase": {
        "issue_family": "event_effect_reward_phrase_microagent",
        "suggested_decision": "audit_current_blocked_effect_reward",
    },
}


def report_paths(settings: dict[str, Any], agent_key: str, dry_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_agent = re.sub(r"[^A-Za-z0-9_]+", "_", agent_key).strip("_")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_blocked_surface_queue_{safe_agent}_dry_run_{dry_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def latest_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE finished_at IS NOT NULL
          AND blocked_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No semantic short-label guarded expansion dry-run found.")
    return int(row["id"])


def fetch_dry_run(conn, *, dry_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Dry-run not found: {dry_run_id}")
    return dict(row)


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


def fetch_ledger_item(conn, *, ledger_run_id: int, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND issue_family = 'semantic_review_router'
        ORDER BY id
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return dict(row) if row else None


def enrich_row(conn, *, dry_run: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any] | None:
    segment_id = int(raw["segment_id"])
    ledger = fetch_ledger_item(conn, ledger_run_id=int(dry_run["ledger_run_id"]), segment_id=segment_id)
    if ledger is None:
        return None
    source = conn.execute(
        """
        SELECT english_text, spanish_text
        FROM source_segments
        WHERE id = ?
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    text = raw.get("text_sample") or ""
    row = dict(raw)
    row["ledger_item_id"] = int(ledger["id"])
    row["issue_kind"] = ledger["issue_kind"] or "semantic_short_label_blocked_surface"
    row["english_text"] = source["english_text"] if source else None
    row["spanish_text"] = source["spanish_text"] if source else None
    row["current_text"] = text
    row["visible_text"] = clean_visible(text)
    row["key_surface"] = key_surface(row.get("source_key"))
    row["text_signature"] = text_signature(
        text,
        char_count=int(row.get("char_count") or 0),
        token_count=int(row.get("token_count") or 0),
    )
    row["path_domain"] = path_domain(row.get("relative_path"))
    agent, action = recommended_next_step(row)
    row["recommended_agent"] = agent
    row["recommended_action"] = action
    row["cluster_key"] = "|".join(
        [
            str(row.get("block_reason") or ""),
            agent,
            row["key_surface"],
            row["text_signature"],
            top_package(row.get("relative_path")),
        ]
    )
    return row


def fetch_candidates(
    conn,
    *,
    dry_run: dict[str, Any],
    agent_key: str,
    block_reason: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_items
        WHERE run_id = ?
          AND dry_run_allowed = 0
          AND classifier_decision = 'needs_domain_context'
        ORDER BY relative_path, source_line_number, source_key
        """,
        (int(dry_run["id"]),),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for raw_row in rows:
        row = enrich_row(conn, dry_run=dry_run, raw=dict(raw_row))
        if row is None:
            continue
        if row["recommended_agent"] != agent_key:
            continue
        if block_reason and row.get("block_reason") != block_reason:
            continue
        if not include_existing and has_existing_review(
            conn, segment_id=int(row["segment_id"]), agent_key=agent_key
        ):
            continue
        candidates.append(row)
    return candidates


def priority_score(row: dict[str, Any], cluster_size: int) -> float:
    score = 1000.0
    score += min(cluster_size, 500) * 1.5
    score += max(0, 120 - int(row.get("char_count") or 0)) / 3
    score += min(int(row.get("token_count") or 0), 4) * 12
    if row.get("text_signature") == "plain_short_label":
        score += 50
    if row.get("key_surface") in {"title_key", "modifier_key", "requirement_or_tooltip_key"}:
        score += 25
    if str(row.get("block_reason") or "").startswith("english_or_placeholder_literal"):
        score -= 250
    if str(row.get("block_reason") or "").startswith("spanish_or_bad_literal"):
        score -= 250
    return round(score + (int(row.get("segment_id") or 0) % 997) / 1000, 4)


def select_stratified(candidates: list[dict[str, Any]], *, limit: int, per_cluster: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        buckets[row["cluster_key"]].append(row)
    for rows in buckets.values():
        cluster_size = len(rows)
        for row in rows:
            row["cluster_size"] = cluster_size
            row["priority_score"] = priority_score(row, cluster_size)
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], int(item.get("source_line_number") or 0)))

    selected: list[dict[str, Any]] = []
    clusters = deque(sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])))
    while clusters and len(selected) < limit:
        cluster_key, rows = clusters.popleft()
        take = min(per_cluster, len(rows), limit - len(selected))
        selected.extend(rows[:take])
    return selected


def insert_queue_run(
    conn,
    *,
    dry_run: dict[str, Any],
    agent_key: str,
    target: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
    per_cluster: int,
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(queue_bucket_for(agent_key, row) for row in selected)
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
            int(dry_run["ledger_run_id"]),
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
    return int(cursor.lastrowid)


def insert_queue_items(
    conn,
    *,
    queue_run_id: int,
    dry_run: dict[str, Any],
    agent_key: str,
    target: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    now = db.utc_now()
    for row in rows:
        queue_bucket = f"{agent_key}:{row['text_signature']}:{row['key_surface']}"
        row["queue_bucket"] = queue_bucket
        evidence = {
            "source": "semantic_short_label_blocked_surface_queue",
            "dry_run_id": int(dry_run["id"]),
            "opportunity_run_id": int(dry_run["opportunity_run_id"]),
            "block_reason": row.get("block_reason"),
            "classifier_reason": row.get("classifier_reason"),
            "profile": row.get("profile"),
            "recommended_agent": row.get("recommended_agent"),
            "recommended_action": row.get("recommended_action"),
            "cluster_key": row.get("cluster_key"),
            "cluster_size": row.get("cluster_size"),
            "key_surface": row.get("key_surface"),
            "text_signature": row.get("text_signature"),
            "path_domain": row.get("path_domain"),
        }
        cursor = conn.execute(
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
                int(dry_run["ledger_run_id"]),
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
        row["queue_run_id"] = queue_run_id
        row["queue_item_id"] = int(cursor.lastrowid)


def queue_bucket_for(agent_key: str, row: dict[str, Any]) -> str:
    return f"{agent_key}:{row['text_signature']}:{row['key_surface']}"


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    dry_run: dict[str, Any],
    agent_key: str,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
    per_cluster: int,
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
        "agent_key",
        "queue_bucket",
        "cluster_key",
        "cluster_size",
        "priority_score",
        "block_reason",
        "classifier_reason",
        "profile",
        "text_signature",
        "key_surface",
        "path_domain",
        "recommended_action",
        "char_count",
        "token_count",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    for row in selected:
        row["agent_key"] = agent_key
        row["evidence_text"] = row.get("current_text") or ""
        row["confirmed_text"] = row.get("current_text") or ""

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in selected:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "decision": "pending",
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    candidate_clusters = Counter(row["cluster_key"] for row in candidates)
    selected_clusters = Counter(row["cluster_key"] for row in selected)
    block_counts = Counter(row.get("block_reason") for row in candidates)
    signature_counts = Counter(row.get("text_signature") for row in candidates)

    lines = [
        "Semantic + short-label blocked surface queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Dry-run id: {dry_run['id']}",
        f"Ledger run id: {dry_run['ledger_run_id']}",
        f"Agent key: {agent_key}",
        f"Queue strategy: {QUEUE_STRATEGY}",
        f"Limit: {limit}",
        f"Per cluster: {per_cluster}",
        "",
        "Summary:",
        f"- candidates after filters: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        "",
        "Candidate text signatures:",
        *[f"- {key}: {value:,}" for key, value in signature_counts.most_common(20)],
        "",
        "Candidate block reasons:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common(20)],
        "",
        "Top candidate clusters:",
    ]
    for key, value in candidate_clusters.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Selected clusters:"])
    for key, value in selected_clusters.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Samples:"])
    for row in selected[:50]:
        text = (row.get("current_text") or "").replace("\n", "\\n")
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number')} | "
            f"{row['source_key']} | {row['queue_bucket']} | {text}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Queue only: no source/output writes, no confirmations, no lifecycle closure.",
            "- The queue is built from the current semantic+short-label blocked dry-run, not the stale event checkpoint.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    dry_run_id: int | None = None,
    agent_key: str = "micro_short_label_style",
    block_reason: str = "",
    limit: int = DEFAULT_LIMIT,
    per_cluster: int = DEFAULT_PER_CLUSTER,
    include_existing: bool = False,
) -> dict[str, Any]:
    if agent_key not in TARGETS:
        raise ValueError(f"Unknown agent_key: {agent_key}. Options: {', '.join(sorted(TARGETS))}")
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_dry_run_id = dry_run_id if dry_run_id is not None else latest_dry_run_id(conn)
        dry_run = fetch_dry_run(conn, dry_run_id=selected_dry_run_id)
        candidates = fetch_candidates(
            conn,
            dry_run=dry_run,
            agent_key=agent_key,
            block_reason=block_reason,
            include_existing=include_existing,
        )
        selected = select_stratified(candidates, limit=limit, per_cluster=per_cluster)
        paths = report_paths(settings, agent_key, selected_dry_run_id)
        queue_run_id = insert_queue_run(
            conn,
            dry_run=dry_run,
            agent_key=agent_key,
            target=TARGETS[agent_key],
            selected=selected,
            paths=paths,
            limit=limit,
            per_cluster=per_cluster,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            dry_run=dry_run,
            agent_key=agent_key,
            target=TARGETS[agent_key],
            rows=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        dry_run=dry_run,
        agent_key=agent_key,
        candidates=candidates,
        selected=selected,
        limit=limit,
        per_cluster=per_cluster,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_semantic_short_label_blocked_surface_queue] Queue generated")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Queue run id: {queue_run_id}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Dry-run id: {selected_dry_run_id}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Agent key: {agent_key}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Candidates after filters: {len(candidates):,}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Selected: {len(selected):,}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Report: {txt_path}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] CSV: {csv_path}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] JSONL: {jsonl_path}")
    print(f"[issue_semantic_short_label_blocked_surface_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "dry_run_id": selected_dry_run_id,
        "agent_key": agent_key,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build review queues from current semantic+short-label blocked surfaces.")
    parser.add_argument("--dry-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default="micro_short_label_style", choices=sorted(TARGETS))
    parser.add_argument("--block-reason", default="")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--per-cluster", type=int, default=DEFAULT_PER_CLUSTER)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        dry_run_id=args.dry_run_id,
        agent_key=args.agent_key,
        block_reason=args.block_reason,
        limit=args.limit,
        per_cluster=args.per_cluster,
        include_existing=args.include_existing,
    )
