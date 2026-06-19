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


RULE_VERSION = "issue_short_label_boundary_route_review_queue_v1"
QUEUE_STRATEGY = "short_label_boundary_route_stratified_learning_queue"

TARGETS: dict[str, dict[str, str]] = {
    "event_dialogue_option": {
        "agent_key": "micro_event_dialogue_option",
        "issue_family": "event_dialogue_option_microagent",
        "suggested_decision": "audit_event_dialogue_option_surface",
        "review_hint": (
            "Review event option/dialogue surfaces routed from pure no-token short labels. Mark safe_short_label only "
            "when the visible PT-BR option is natural and context-independent enough for this event option surface. "
            "Use needs_repair for visible language defects, needs_domain_context for event-specific ambiguity, and "
            "needs_new_microagent when a repeated pattern should be split into a new specialist."
        ),
    },
    "quoted_dialogue_fragment": {
        "agent_key": "micro_event_context_composer",
        "issue_family": "event_context_composer_microagent",
        "suggested_decision": "audit_quoted_dialogue_fragment_surface",
        "review_hint": (
            "Review quoted event fragments. Prefer needs_domain_context unless the quotation is visibly safe and "
            "does not depend on surrounding event grammar."
        ),
    },
    "quoted_dialogue_option": {
        "agent_key": "micro_event_dialogue_option",
        "issue_family": "event_dialogue_option_microagent",
        "suggested_decision": "audit_quoted_dialogue_option_surface",
        "review_hint": (
            "Review quoted event option surfaces routed from pure no-token labels. Mark safe_short_label only when "
            "the quoted PT-BR option is natural as an event choice/dialogue line. Use needs_repair for visible "
            "language defects and needs_domain_context for event-specific ambiguity."
        ),
    },
    "event_context_sentence": {
        "agent_key": "micro_event_context_composer",
        "issue_family": "event_context_composer_microagent",
        "suggested_decision": "audit_event_context_sentence_surface",
        "review_hint": (
            "Review event/context sentences routed from pure no-token labels. Use safe_short_label only for clean "
            "standalone prose; use needs_domain_context for text that depends on event flow."
        ),
    },
    "event_outcome_short_label": {
        "agent_key": "micro_short_label_style",
        "issue_family": "short_label_style_microagent",
        "suggested_decision": "audit_event_outcome_short_label_surface",
        "review_hint": (
            "Review compact success/failure outcome labels. Mark safe_short_label only when the visible PT-BR "
            "label is natural, concise, and does not need surrounding event context."
        ),
    },
    "trait_short_description": {
        "agent_key": "trait_description_microagent",
        "issue_family": "trait_description_microagent",
        "suggested_decision": "audit_trait_short_description_surface",
        "review_hint": (
            "Review short trait descriptions. Mark safe_short_label only when the PT-BR description is natural, "
            "semantically aligned, and not hiding grammar problems."
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


def latest_boundary_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_boundary_route_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished pure no-token boundary route diagnostic run found.")
    return int(row["id"])


def fetch_boundary_run(conn, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_boundary_route_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Boundary route diagnostic run not found: {run_id}")
    return dict(row)


def report_paths(settings: dict[str, Any], route: str, boundary_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_route = re.sub(r"[^A-Za-z0-9_]+", "_", route).strip("_")
    base = reports_dir / f"{stamp}_issue_short_label_boundary_route_queue_{safe_route}_run_{boundary_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cluster_key(row: dict[str, Any]) -> str:
    return "::".join(
        [
            str(row.get("route") or "unknown_route"),
            str(row.get("text_signature") or "unknown_signature"),
            str(row.get("key_surface") or "unknown_key"),
            str(row.get("path_domain") or "unknown_domain"),
            str(row.get("shadow_reason") or "unknown_reason"),
        ]
    )


def queue_bucket(row: dict[str, Any]) -> str:
    return "::".join(
        [
            str(row.get("route") or "unknown_route"),
            str(row.get("text_signature") or "unknown_signature"),
            str(row.get("key_surface") or "unknown_key"),
            str(row.get("path_domain") or "unknown_domain"),
        ]
    )


def has_existing_review(conn, *, segment_id: int, agent_key: str, include_existing: bool) -> bool:
    if include_existing:
        return False
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
    boundary_run_id: int,
    route: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    if route not in TARGETS:
        raise RuntimeError(f"Unknown route: {route}. Options: {', '.join(sorted(TARGETS))}")
    target = TARGETS[route]
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text
        FROM ml_issue_short_label_pure_no_token_boundary_route_items item
        LEFT JOIN source_segments source ON source.id = item.segment_id
        WHERE item.run_id = ?
          AND item.route = ?
        ORDER BY item.priority DESC, item.path_domain, item.key_surface, item.relative_path, item.source_line_number, item.segment_id
        """,
        (boundary_run_id, route),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        agent_key = target["agent_key"]
        if has_existing_review(
            conn,
            segment_id=int(row["segment_id"]),
            agent_key=agent_key,
            include_existing=include_existing,
        ):
            continue
        row["agent_key"] = agent_key
        row["issue_family"] = target["issue_family"]
        row["issue_kind"] = row.get("route") or route
        row["cluster_key"] = cluster_key(row)
        row["queue_bucket"] = queue_bucket(row)
        candidates.append(row)
    return candidates


def priority_score(row: dict[str, Any], *, cluster_size: int) -> float:
    score = 1000.0 + min(cluster_size, 500) * 1.25
    score += max(0, 120 - int(row.get("text_length") or 0)) / 4
    if row.get("key_surface") == "option_letter_key":
        score += 40
    if row.get("text_signature") == "event_key_surface":
        score += 25
    if row.get("path_domain") == "events":
        score += 15
    score += (int(row.get("segment_id") or 0) % 997) / 1000
    return round(score, 4)


def select_stratified(rows: list[dict[str, Any]], *, limit: int, per_cluster: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[row["cluster_key"]].append(row)

    for bucket_rows in buckets.values():
        cluster_size = len(bucket_rows)
        for row in bucket_rows:
            row["cluster_size"] = cluster_size
            row["priority_score"] = priority_score(row, cluster_size=cluster_size)
        bucket_rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], int(item.get("source_line_number") or 0)))

    selected: list[dict[str, Any]] = []
    for _cluster_key, bucket_rows in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(selected) >= limit:
            break
        take = min(max(1, per_cluster), len(bucket_rows), limit - len(selected))
        selected.extend(bucket_rows[:take])
    return selected


def insert_queue_run(
    conn,
    *,
    boundary_run: dict[str, Any],
    route: str,
    target: dict[str, str],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
    per_cluster: int,
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
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
            int(boundary_run["ledger_run_id"]),
            target["agent_key"],
            target["issue_family"],
            f"{QUEUE_STRATEGY}:{route}",
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
    boundary_run: dict[str, Any],
    route: str,
    target: dict[str, str],
    rows: list[dict[str, Any]],
) -> None:
    now = db.utc_now()
    for row in rows:
        evidence = {
            "boundary_route_run_id": int(boundary_run["id"]),
            "boundary_route_item_id": int(row["id"]),
            "shadow_run_id": int(row["shadow_run_id"]),
            "shadow_item_id": int(row["shadow_item_id"]),
            "source": "short_label_pure_no_token_boundary_route",
            "route": route,
            "recommended_agent": row.get("recommended_agent"),
            "recommended_action": row.get("recommended_action"),
            "shadow_reason": row.get("shadow_reason"),
            "path_domain": row.get("path_domain"),
            "key_surface": row.get("key_surface"),
            "text_signature": row.get("text_signature"),
            "cluster_key": row.get("cluster_key"),
            "cluster_size": int(row.get("cluster_size") or 0),
            "text_length": int(row.get("text_length") or 0),
            "word_count": int(row.get("word_count") or 0),
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
                int(boundary_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                target["issue_family"],
                row.get("issue_kind") or route,
                target["agent_key"],
                row["queue_bucket"],
                float(row.get("priority_score") or 0),
                target["suggested_decision"],
                row.get("evidence_text") or "",
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("evidence_text") or "",
                now,
            ),
        )
        row["queue_item_id"] = int(cursor.lastrowid)


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    boundary_run: dict[str, Any],
    route: str,
    target: dict[str, str],
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
        "queue_bucket",
        "cluster_key",
        "cluster_size",
        "priority_score",
        "path_domain",
        "key_surface",
        "text_signature",
        "shadow_reason",
        "text_length",
        "word_count",
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
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
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
                "evidence_text": row.get("evidence_text") or "",
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "cluster": {
                    "route": route,
                    "cluster_key": row.get("cluster_key"),
                    "queue_bucket": row.get("queue_bucket"),
                    "cluster_size": row.get("cluster_size"),
                    "recommended_agent": target["agent_key"],
                    "recommended_action": row.get("recommended_action"),
                    "shadow_reason": row.get("shadow_reason"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    candidate_clusters = Counter(row["cluster_key"] for row in candidates)
    selected_clusters = Counter(row["cluster_key"] for row in selected)
    lines = [
        "Short Label Boundary Route Review Queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Boundary route run id: {boundary_run['id']}",
        f"Shadow run id: {boundary_run['shadow_run_id']}",
        f"Ledger run id: {boundary_run['ledger_run_id']}",
        f"Route: {route}",
        f"Agent key: {target['agent_key']}",
        f"Issue family: {target['issue_family']}",
        f"Strategy: {QUEUE_STRATEGY}",
        "",
        "Summary:",
        f"- candidates after existing-review filter: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        f"- candidate clusters: {len(candidate_clusters):,}",
        f"- selected clusters: {len(selected_clusters):,}",
        f"- limit: {limit:,}",
        f"- per_cluster: {per_cluster:,}",
        "",
        "Selected clusters:",
        *[
            f"- {cluster}: selected={count}, candidates={candidate_clusters[cluster]}"
            for cluster, count in selected_clusters.most_common(30)
        ],
        "",
        "Samples:",
    ]
    for row in selected[:120]:
        lines.append(
            f"- queue_item={row.get('queue_item_id')} segment={row['segment_id']} "
            f"| {row['relative_path']}:{row.get('source_line_number') or '?'}::{row['source_key']} "
            f"| {row['queue_bucket']} | {short(row.get('evidence_text'), 220)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Queue only: no source/output writes, no confirmations, no lifecycle closure.",
            "- Use issue_event_surface_assisted_draft.py or manual review to create decisions.",
            "- Decisions must pass checkpoint/governance before any segment-state consumer is considered.",
            "",
            f"CSV: {csv_path}",
            f"JSONL: {jsonl_path}",
            f"Decisions template: {decisions_template_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    boundary_run_id: int | None = None,
    route: str,
    limit: int = 160,
    per_cluster: int = 40,
    include_existing: bool = False,
) -> dict[str, Any]:
    if route not in TARGETS:
        raise RuntimeError(f"Unknown route: {route}. Options: {', '.join(sorted(TARGETS))}")
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_review_tables(conn)
        selected_boundary_run_id = boundary_run_id or latest_boundary_run_id(conn)
        boundary_run = fetch_boundary_run(conn, run_id=selected_boundary_run_id)
        target = TARGETS[route]
        candidates = fetch_candidates(
            conn,
            boundary_run_id=selected_boundary_run_id,
            route=route,
            include_existing=include_existing,
        )
        selected = select_stratified(candidates, limit=max(0, limit), per_cluster=max(1, per_cluster))
        paths = report_paths(settings, route, selected_boundary_run_id)
        queue_run_id = insert_queue_run(
            conn,
            boundary_run=boundary_run,
            route=route,
            target=target,
            selected=selected,
            paths=paths,
            limit=limit,
            per_cluster=per_cluster,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            boundary_run=boundary_run,
            route=route,
            target=target,
            rows=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        boundary_run=boundary_run,
        route=route,
        target=target,
        candidates=candidates,
        selected=selected,
        limit=limit,
        per_cluster=per_cluster,
    )
    txt_path, _, jsonl_path, decisions_template_path = paths
    print("[issue_short_label_boundary_route_review_queue] Queue generated")
    print(f"[issue_short_label_boundary_route_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_boundary_route_review_queue] Boundary route run id: {selected_boundary_run_id}")
    print(f"[issue_short_label_boundary_route_review_queue] Route: {route}")
    print(f"[issue_short_label_boundary_route_review_queue] Agent key: {target['agent_key']}")
    print(f"[issue_short_label_boundary_route_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_boundary_route_review_queue] Selected: {len(selected):,}")
    print(f"[issue_short_label_boundary_route_review_queue] Report: {txt_path}")
    print(f"[issue_short_label_boundary_route_review_queue] JSONL: {jsonl_path}")
    print(f"[issue_short_label_boundary_route_review_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "boundary_run_id": selected_boundary_run_id,
        "route": route,
        "agent_key": target["agent_key"],
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a focused review queue from short-label boundary routes.")
    parser.add_argument("--boundary-run-id", type=int, default=None)
    parser.add_argument("--route", required=True, choices=sorted(TARGETS))
    parser.add_argument("--limit", type=int, default=160)
    parser.add_argument("--per-cluster", type=int, default=40)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        boundary_run_id=args.boundary_run_id,
        route=args.route,
        limit=args.limit,
        per_cluster=args.per_cluster,
        include_existing=args.include_existing,
    )
