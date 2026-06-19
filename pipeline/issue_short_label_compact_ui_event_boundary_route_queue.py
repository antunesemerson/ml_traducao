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
from apply_segment_state_updates import short
from issue_short_label_compact_ui_semantic_route_diagnostic import note_reason


RULE_VERSION = "issue_short_label_compact_ui_event_boundary_route_queue_v1"
QUEUE_STRATEGY_PREFIX = "short_label_boundary_route_stratified_learning_queue"
SOURCE_AGENT_KEY = "micro_short_label_style"
SOURCE_QUEUE_STRATEGY = "short_label_compact_ui_semantic_stratified_learning_queue"

TARGETS: dict[str, dict[str, str]] = {
    "event_dialogue_option": {
        "agent_key": "micro_event_dialogue_option",
        "issue_family": "event_dialogue_option_microagent",
        "suggested_decision": "audit_event_dialogue_option_surface",
        "review_hint": (
            "Review compact event option/toast/dialogue surfaces routed from compact UI semantic short labels. "
            "Use safe_short_label only when the PT-BR is natural and context-independent enough for an event option/toast. "
            "Use needs_repair for visible defects and needs_domain_context for event-flow ambiguity."
        ),
    },
    "event_context_sentence": {
        "agent_key": "micro_event_context_composer",
        "issue_family": "event_context_composer_microagent",
        "suggested_decision": "audit_event_context_sentence_surface",
        "review_hint": (
            "Review compact event/context sentence fragments routed from compact UI semantic short labels. "
            "Use composition_ready only for clean standalone event prose; use needs_domain_context when surrounding event grammar matters."
        ),
    },
}

DECISION_OPTIONS = [
    "safe_short_label",
    "composition_ready",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
]

EVENT_OPTION_OR_TOAST_RE = re.compile(r"(?:\.[a-z](?:\.|$)|\.toast(?:\.|$)|_toast(?:_|$)|\.option(?:\.|$))", re.IGNORECASE)


def report_paths(settings: dict[str, Any], route: str, source_queue_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_boundary_route_queue_{route}_from_compact_ui_queue_{source_queue_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def latest_source_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND queue_strategy = ?
          AND reviewed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (SOURCE_AGENT_KEY, SOURCE_QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed compact UI semantic source queue found.")
    return int(row["id"])


def fetch_source_queue_run(conn, source_queue_run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_issue_review_queue_runs WHERE id = ?", (source_queue_run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Source queue run not found: {source_queue_run_id}")
    return dict(row)


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


def target_route(row: dict[str, Any]) -> str | None:
    reason = note_reason(row.get("notes"))
    key = str(row.get("source_key") or "")
    bucket = str(row.get("source_queue_bucket") or row.get("queue_bucket") or "")
    if reason == "event_option_or_toast_requires_event_context":
        return "event_dialogue_option"
    if reason == "quoted_or_dialogue_surface_requires_event_context":
        if EVENT_OPTION_OR_TOAST_RE.search(key):
            return "event_dialogue_option"
        return "event_context_sentence"
    if reason == "not_preselected_as_false_positive_reopen" and "domain_events_longform" in bucket:
        if EVENT_OPTION_OR_TOAST_RE.search(key):
            return "event_dialogue_option"
        return "event_context_sentence"
    return None


def queue_bucket(row: dict[str, Any], route: str) -> str:
    reason = note_reason(row.get("notes"))
    source_key = str(row.get("source_key") or "")
    key_surface = "option_or_toast" if EVENT_OPTION_OR_TOAST_RE.search(source_key) else "event_sentence"
    source_bucket = str(row.get("source_queue_bucket") or row.get("queue_bucket") or "unknown_bucket")
    return "::".join([route, key_surface, reason, source_bucket])


def cluster_key(row: dict[str, Any], route: str) -> str:
    source_key = str(row.get("source_key") or "")
    key_surface = "option_or_toast" if EVENT_OPTION_OR_TOAST_RE.search(source_key) else "event_sentence"
    reason = note_reason(row.get("notes"))
    path_group = str(row.get("relative_path") or "").split("/", 1)[0]
    return "::".join([route, key_surface, reason, path_group])


def fetch_candidates(
    conn,
    *,
    source_queue_run_id: int,
    route: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    if route not in TARGETS:
        raise RuntimeError(f"Unknown route: {route}. Options: {', '.join(sorted(TARGETS))}")
    target = TARGETS[route]
    rows = conn.execute(
        """
        SELECT
            decision.id AS source_decision_id,
            decision.queue_run_id AS source_queue_run_id,
            decision.queue_item_id AS source_queue_item_id,
            decision.ledger_run_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.queue_bucket AS source_queue_bucket,
            decision.notes,
            item.evidence_text,
            item.confirmed_text,
            item.english_text,
            item.spanish_text,
            item.evidence_json
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND decision.normalized_decision = 'needs_domain_context'
        ORDER BY decision.relative_path, decision.source_line_number, decision.source_key
        """,
        (source_queue_run_id,),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if target_route(row) != route:
            continue
        if has_existing_review(
            conn,
            segment_id=int(row["segment_id"]),
            agent_key=target["agent_key"],
            include_existing=include_existing,
        ):
            continue
        row["route"] = route
        row["agent_key"] = target["agent_key"]
        row["issue_family"] = target["issue_family"]
        row["issue_kind"] = route
        row["route_reason"] = note_reason(row.get("notes"))
        row["queue_bucket"] = queue_bucket(row, route)
        row["cluster_key"] = cluster_key(row, route)
        row["evidence_json"] = enrich_evidence(row)
        row["priority_score"] = priority_score(row)
        candidates.append(row)
    return candidates


def enrich_evidence(row: dict[str, Any]) -> str:
    try:
        evidence = json.loads(row.get("evidence_json") or "{}")
    except json.JSONDecodeError:
        evidence = {}
    evidence.update(
        {
            "source": "short_label_compact_ui_semantic_route_diagnostic",
            "source_queue_run_id": int(row["source_queue_run_id"]),
            "source_queue_item_id": int(row["source_queue_item_id"]),
            "source_decision_id": int(row["source_decision_id"]),
            "route": row["route"],
            "route_reason": row["route_reason"],
            "cluster_key": row["cluster_key"],
            "source_queue_bucket": row.get("source_queue_bucket"),
        }
    )
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True)


def priority_score(row: dict[str, Any]) -> float:
    text = str(row.get("evidence_text") or row.get("confirmed_text") or "")
    score = 1000.0
    score -= min(len(text), 180) * 0.08
    if row["route_reason"] == "event_option_or_toast_requires_event_context":
        score += 40
    if row["route_reason"] == "quoted_or_dialogue_surface_requires_event_context":
        score += 25
    if row["relative_path"] and "event_localization" in str(row["relative_path"]):
        score += 15
    score += (int(row["segment_id"]) % 997) / 1000
    return round(score, 4)


def select_stratified(rows: list[dict[str, Any]], *, limit: int, per_cluster: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["cluster_key"]].append(row)
    for bucket_rows in grouped.values():
        for row in bucket_rows:
            row["cluster_size"] = len(bucket_rows)
        bucket_rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_line_number"] or 0))

    selected: list[dict[str, Any]] = []
    for _cluster, bucket_rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(selected) >= limit:
            break
        selected.extend(bucket_rows[: max(1, min(per_cluster, limit - len(selected)))])
    return selected[:limit]


def insert_queue_run(
    conn,
    *,
    source_queue_run: dict[str, Any],
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
    row = conn.execute(
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
            int(source_queue_run["ledger_run_id"]),
            target["agent_key"],
            target["issue_family"],
            f"{QUEUE_STRATEGY_PREFIX}:{route}:from_compact_ui_semantic",
            limit,
            per_cluster,
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
    return int(row.lastrowid)


def insert_queue_items(
    conn,
    *,
    queue_run_id: int,
    rows: list[dict[str, Any]],
    target: dict[str, str],
) -> None:
    now = db.utc_now()
    for row in rows:
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
                int(row["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                target["issue_family"],
                row["issue_kind"],
                target["agent_key"],
                row["queue_bucket"],
                float(row["priority_score"]),
                target["suggested_decision"],
                row.get("evidence_text") or row.get("confirmed_text") or "",
                row["evidence_json"],
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text") or row.get("evidence_text") or "",
                now,
            ),
        )
        row["queue_item_id"] = int(cursor.lastrowid)


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    source_queue_run: dict[str, Any],
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
        "route_reason",
        "priority_score",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
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
            payload.update(
                {
                    "queue_run_id": queue_run_id,
                    "agent_key": target["agent_key"],
                    "issue_family": target["issue_family"],
                    "issue_kind": row["issue_kind"],
                    "suggested_decision": target["suggested_decision"],
                    "texts": {
                        "english_text": row.get("english_text"),
                        "spanish_text": row.get("spanish_text"),
                        "confirmed_text": row.get("confirmed_text"),
                        "evidence_text": row.get("evidence_text"),
                    },
                }
            )
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
                    "route_reason": row.get("route_reason"),
                    "source_queue_run_id": int(source_queue_run["id"]),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    candidate_clusters = Counter(row["cluster_key"] for row in candidates)
    selected_clusters = Counter(row["cluster_key"] for row in selected)
    reason_counts = Counter(row["route_reason"] for row in selected)
    lines = [
        "Compact UI -> Event Boundary Route Queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Source compact UI queue run id: {source_queue_run['id']}",
        f"Ledger run id: {source_queue_run['ledger_run_id']}",
        f"Route: {route}",
        f"Agent key: {target['agent_key']}",
        f"Issue family: {target['issue_family']}",
        f"Strategy: {QUEUE_STRATEGY_PREFIX}:{route}:from_compact_ui_semantic",
        "",
        "Summary:",
        f"- candidates after existing-review filter: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        f"- candidate clusters: {len(candidate_clusters):,}",
        f"- selected clusters: {len(selected_clusters):,}",
        f"- limit: {limit:,}",
        f"- per_cluster: {per_cluster:,}",
        "",
        "Route reasons:",
        *[f"- {reason}: {count:,}" for reason, count in reason_counts.most_common()],
        "",
        "Selected clusters:",
        *[
            f"- {cluster}: selected={count}, candidates={candidate_clusters[cluster]}"
            for cluster, count in selected_clusters.most_common(30)
        ],
        "",
        "Samples:",
    ]
    for row in selected[:100]:
        lines.append(
            f"- queue_item={row.get('queue_item_id')} segment={row['segment_id']} "
            f"| {row['relative_path']}:{row.get('source_line_number') or '?'}::{row['source_key']} "
            f"| {row['queue_bucket']} | {short(row.get('evidence_text') or row.get('confirmed_text'), 220)}"
        )
    lines.extend(
        [
            "",
            "Files:",
            f"- CSV: {csv_path}",
            f"- JSONL: {jsonl_path}",
            f"- Decisions template: {decisions_template_path}",
            "",
            "Safety note:",
            "- Queue only. No source/output writes, no confirmations, no lifecycle promotion.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    source_queue_run_id: int | None = None,
    route: str,
    limit: int,
    per_cluster: int,
    include_existing: bool,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_source_queue_run_id = source_queue_run_id or latest_source_queue_run_id(conn)
        source_queue_run = fetch_source_queue_run(conn, selected_source_queue_run_id)
        target = TARGETS[route]
        candidates = fetch_candidates(
            conn,
            source_queue_run_id=selected_source_queue_run_id,
            route=route,
            include_existing=include_existing,
        )
        if not candidates:
            raise RuntimeError(f"No candidates for route {route}.")
        selected = select_stratified(candidates, limit=limit, per_cluster=per_cluster)
        paths = report_paths(settings, route, selected_source_queue_run_id)
        queue_run_id = insert_queue_run(
            conn,
            source_queue_run=source_queue_run,
            route=route,
            target=target,
            selected=selected,
            paths=paths,
            limit=limit,
            per_cluster=per_cluster,
        )
        insert_queue_items(conn, queue_run_id=queue_run_id, rows=selected, target=target)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        source_queue_run=source_queue_run,
        route=route,
        target=target,
        candidates=candidates,
        selected=selected,
        limit=limit,
        per_cluster=per_cluster,
    )
    txt_path, _csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_short_label_compact_ui_event_boundary_route_queue] Queue generated")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Route: {route}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Source queue run id: {selected_source_queue_run_id}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Selected: {len(selected):,}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Report: {txt_path}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] JSONL: {jsonl_path}")
    print(f"[issue_short_label_compact_ui_event_boundary_route_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "source_queue_run_id": selected_source_queue_run_id,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create event boundary route queues from compact UI semantic reviewed decisions.")
    parser.add_argument("--source-queue-run-id", type=int)
    parser.add_argument("--route", required=True, choices=sorted(TARGETS))
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--per-cluster", type=int, default=40)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        source_queue_run_id=args.source_queue_run_id,
        route=args.route,
        limit=args.limit,
        per_cluster=args.per_cluster,
        include_existing=args.include_existing,
    )
