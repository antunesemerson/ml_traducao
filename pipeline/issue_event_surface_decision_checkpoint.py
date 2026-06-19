from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from issue_event_surface_assisted_draft import classify as classify_surface
from issue_event_surface_subcluster_report import (
    DEFAULT_BLOCK_REASON,
    fetch_event_run,
    fetch_rows,
    latest_event_checkpoint_run_id,
)


RULE_VERSION = "issue_event_surface_decision_checkpoint_v4_small_perfect_maturity"
POLICY_NAME = "event_surface_decision_cluster_shadow_v4"
POLICY_STATUS = "shadow"
CHECKPOINT_ACTION = "stage_event_surface_decision_cluster_shadow"
MIN_SAFE_PER_CLUSTER = 8
MIN_SMALL_PERFECT_CLUSTER = 3
MAX_NON_SAFE_PER_CLUSTER = 0
MAX_NON_SAFE_RATE = 0.0
POSITIVE_DECISIONS = {"safe_short_label", "composition_ready"}
POSITIVE_EVIDENCE_LABELS = {"positive_evidence", "composition_ready"}


def parse_ids(value: str | None) -> list[int]:
    if not value:
        return []
    ids: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def latest_decision_run_ids(conn) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        WHERE run.finished_at IS NOT NULL
          AND decision.agent_key IN (
              'micro_event_dialogue_option',
              'micro_requirement_tooltip_surface',
              'micro_event_surface_router',
              'micro_event_context_composer',
              'micro_short_label_style',
              'micro_effect_reward_phrase'
          )
        ORDER BY run.id DESC
        LIMIT 2
        """
    ).fetchall()
    return sorted(int(row["id"]) for row in rows)


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_decision_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            event_checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            decision_run_ids_json TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            allowed_clusters_json TEXT,
            cluster_stats_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_decision_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            event_checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            cluster_key TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL DEFAULT '',
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_event_surface_decision_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], event_checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_surface_decision_checkpoint_event_run_{event_checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def load_cluster_stats(conn, *, decision_run_ids: list[int]) -> dict[str, dict[str, Any]]:
    if not decision_run_ids:
        return {}
    placeholders = ",".join("?" for _ in decision_run_ids)
    rows = conn.execute(
        f"""
        SELECT
            decision.normalized_decision,
            decision.evidence_label,
            decision.agent_key,
            decision.queue_bucket,
            queue.evidence_json
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id IN ({placeholders})
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.id
        """,
        decision_run_ids,
    ).fetchall()
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence: dict[str, Any] = {}
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        cluster_key = str(evidence.get("cluster_key") or "")
        if not cluster_key:
            continue
        bucket = stats.setdefault(
            cluster_key,
            {
                "total": 0,
                "safe": 0,
                "non_safe": 0,
                "decision_counts": Counter(),
                "agent_key": row["agent_key"],
                "queue_bucket": row["queue_bucket"],
            },
        )
        decision = str(row["normalized_decision"] or "")
        bucket["total"] += 1
        bucket["decision_counts"][decision] += 1
        if decision in POSITIVE_DECISIONS and row["evidence_label"] in POSITIVE_EVIDENCE_LABELS:
            bucket["safe"] += 1
        else:
            bucket["non_safe"] += 1
    return stats


def allowed_clusters(
    cluster_stats: dict[str, dict[str, Any]],
    *,
    candidate_counts: Counter[str],
    min_safe: int,
    max_non_safe: int,
    max_non_safe_rate: float,
) -> set[str]:
    allowed: set[str] = set()
    for cluster_key, stats in cluster_stats.items():
        total = int(stats["total"] or 0)
        safe = int(stats["safe"] or 0)
        non_safe = int(stats["non_safe"] or 0)
        candidate_count = int(candidate_counts.get(cluster_key, 0))
        non_safe_rate = (int(stats["non_safe"]) / total) if total else 1.0
        standard_mature = safe >= min_safe and non_safe <= max_non_safe and non_safe_rate <= max_non_safe_rate
        small_perfect_mature = (
            MIN_SMALL_PERFECT_CLUSTER <= candidate_count <= min_safe
            and safe >= candidate_count
            and non_safe == 0
        )
        if standard_mature or small_perfect_mature:
            allowed.add(cluster_key)
    return allowed


def queue_bucket(row: dict[str, Any]) -> str:
    return f"{row['text_signature']}::{row['key_surface']}::{row['path_domain']}"


def classify_candidate(row: dict[str, Any], allowed: set[str]) -> tuple[int, str, str, str, str]:
    cluster_key = row["cluster_key"]
    agent_key = row["recommended_agent"]
    subpolicy_name = row["recommended_action"]
    bucket = queue_bucket(row)

    if cluster_key not in allowed:
        return 0, "cluster_not_mature_from_review_evidence", bucket, agent_key, subpolicy_name
    draft_row = {
        "evidence_text": row.get("current_text") or "",
        "source_key": row.get("source_key") or "",
        "queue_bucket": bucket,
    }
    decision, _corrected_text, reason = classify_surface(draft_row, default_agent_key=agent_key)
    if decision not in POSITIVE_DECISIONS:
        return 0, f"row_guard:{decision}:{reason}", bucket, agent_key, subpolicy_name
    return 1, "", bucket, agent_key, subpolicy_name


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    event_run: dict[str, Any],
    decision_run_ids: list[int],
    cluster_stats: dict[str, dict[str, Any]],
    allowed: set[str],
    rows: list[dict[str, Any]],
    counts: Counter[str],
    min_safe: int,
    max_non_safe: int,
    max_non_safe_rate: float,
) -> None:
    fields = [
        "checkpoint_item_id",
        "checkpoint_allowed",
        "block_reason",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "agent_key",
        "subpolicy_name",
        "queue_bucket",
        "cluster_key",
        "checkpoint_action",
        "current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    allowed_by_cluster = Counter(row["cluster_key"] for row in rows if row["checkpoint_allowed"])
    blocks = Counter(row["block_reason"] for row in rows if not row["checkpoint_allowed"])
    cluster_lines: list[str] = []
    for cluster_key, stats in sorted(cluster_stats.items(), key=lambda item: (-int(item[1]["total"]), item[0])):
        decision_counts = stats["decision_counts"]
        decision_text = ", ".join(f"{key}={value}" for key, value in decision_counts.most_common())
        total = int(stats["total"] or 0)
        non_safe_rate = (int(stats["non_safe"]) / total) if total else 1.0
        cluster_lines.append(
            f"- {'ALLOW' if cluster_key in allowed else 'hold'} {cluster_key}: "
            f"safe={stats['safe']}, non_safe={stats['non_safe']}, total={stats['total']}, "
            f"non_safe_rate={non_safe_rate:.2%} ({decision_text})"
        )

    lines = [
        "Issue event surface decision checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME}",
        f"Policy status: {POLICY_STATUS}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Event checkpoint run id: {event_run['id']}",
        f"Ledger run id: {event_run['ledger_run_id']}",
        f"Decision run ids: {decision_run_ids}",
        "",
        "Summary:",
        f"- candidates: {len(rows):,}",
        f"- allowed: {counts['allowed']:,}",
        f"- blocked: {counts['blocked']:,}",
        f"- mature clusters: {len(allowed):,}/{len(cluster_stats):,}",
        f"- min safe per cluster: {min_safe:,}",
        f"- small perfect cluster rule: {MIN_SMALL_PERFECT_CLUSTER:,}-{min_safe:,} candidates, all reviewed safe, zero non-safe",
        f"- max non-safe per cluster: {max_non_safe:,}",
        f"- max non-safe rate: {max_non_safe_rate:.2%}",
        "",
        "Reviewed cluster gates:",
        *cluster_lines,
        "",
        "Allowed by cluster:",
        *[f"- {key}: {value:,}" for key, value in allowed_by_cluster.most_common()],
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in blocks.most_common()],
        "",
        "Allowed sample:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:40]:
        lines.append(
            f"- {row['segment_id']} | {row['cluster_key']} | {row['relative_path']}:{row.get('source_line_number') or '?'} "
            f"{row['source_key']} :: {short(row.get('current_text'), 150)}"
        )
    lines.extend(["", "Blocked sample:"])
    for row in [item for item in rows if not item["checkpoint_allowed"]][:40]:
        lines.append(
            f"- {row['block_reason']} | {row['segment_id']} | {row['cluster_key']} | "
            f"{row['relative_path']}:{row.get('source_line_number') or '?'} {row['source_key']} :: "
            f"{short(row.get('current_text'), 150)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow checkpoint only: no source/output reads, no confirmations and no production writes.",
            "- Cluster allow requires enough reviewed safe evidence and respects the configured non-safe count/rate limits.",
            "- Every row still passes event-surface row guards before being allowed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    event_checkpoint_run_id: int | None = None,
    decision_run_ids: list[int] | None = None,
    min_safe: int = MIN_SAFE_PER_CLUSTER,
    max_non_safe: int = MAX_NON_SAFE_PER_CLUSTER,
    max_non_safe_rate: float = MAX_NON_SAFE_RATE,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_event_run_id = event_checkpoint_run_id or latest_event_checkpoint_run_id(conn)
        event_run = fetch_event_run(conn, run_id=selected_event_run_id)
        selected_decision_run_ids = decision_run_ids or latest_decision_run_ids(conn)
        cluster_stats = load_cluster_stats(conn, decision_run_ids=selected_decision_run_ids)
        source_rows = fetch_rows(conn, run_id=selected_event_run_id, block_reason=DEFAULT_BLOCK_REASON)
        candidate_counts = Counter(row["cluster_key"] for row in source_rows)
        mature_clusters = allowed_clusters(
            cluster_stats,
            candidate_counts=candidate_counts,
            min_safe=min_safe,
            max_non_safe=max_non_safe,
            max_non_safe_rate=max_non_safe_rate,
        )
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        now = db.utc_now()
        for source in source_rows:
            allowed, block_reason, bucket, agent_key, subpolicy_name = classify_candidate(source, mature_clusters)
            counts["allowed" if allowed else "blocked"] += 1
            classified.append(
                {
                    "event_checkpoint_run_id": selected_event_run_id,
                    "ledger_run_id": int(event_run["ledger_run_id"]),
                    "ledger_item_id": int(source["ledger_item_id"]),
                    "segment_id": int(source["segment_id"]),
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "agent_key": agent_key,
                    "issue_family": source.get("issue_family") or "event_surface_microagent",
                    "issue_kind": source.get("issue_kind") or source.get("text_signature") or "event_surface",
                    "queue_bucket": bucket,
                    "subpolicy_name": subpolicy_name,
                    "cluster_key": source["cluster_key"],
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": source.get("token_status") or "",
                    "current_text": source.get("current_text") or "",
                    "corrected_text": "",
                    "created_at": now,
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_event_run_id)
        finished_at = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_event_surface_decision_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                event_checkpoint_run_id,
                ledger_run_id,
                decision_run_ids_json,
                candidate_count,
                allowed_count,
                blocked_count,
                allowed_clusters_json,
                cluster_stats_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                selected_event_run_id,
                int(event_run["ledger_run_id"]),
                json.dumps(selected_decision_run_ids, ensure_ascii=False),
                len(classified),
                counts["allowed"],
                counts["blocked"],
                json.dumps(sorted(mature_clusters), ensure_ascii=False),
                json.dumps(
                    {
                        cluster_key: {
                            **{key: value for key, value in stats.items() if key != "decision_counts"},
                            "decision_counts": dict(stats["decision_counts"]),
                        }
                        for cluster_key, stats in sorted(cluster_stats.items())
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                finished_at,
                finished_at,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        for row in classified:
            row["run_id"] = checkpoint_run_id
        conn.executemany(
            """
            INSERT INTO ml_issue_event_surface_decision_checkpoint_items (
                run_id,
                event_checkpoint_run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                agent_key,
                issue_family,
                issue_kind,
                queue_bucket,
                subpolicy_name,
                cluster_key,
                checkpoint_allowed,
                checkpoint_action,
                block_reason,
                token_status,
                current_text,
                corrected_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_run_id,
                    row["event_checkpoint_run_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["queue_bucket"],
                    row["subpolicy_name"],
                    row["cluster_key"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["created_at"],
                )
                for row in classified
            ],
        )
        conn.commit()

    for index, row in enumerate(classified, start=1):
        row["checkpoint_item_id"] = index
    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        event_run=event_run,
        decision_run_ids=selected_decision_run_ids,
        cluster_stats=cluster_stats,
        allowed=mature_clusters,
        rows=classified,
        counts=counts,
        min_safe=min_safe,
        max_non_safe=max_non_safe,
        max_non_safe_rate=max_non_safe_rate,
    )
    print("[issue_event_surface_decision_checkpoint] Checkpoint generated")
    print(f"[issue_event_surface_decision_checkpoint] Run id: {checkpoint_run_id}")
    print(f"[issue_event_surface_decision_checkpoint] Event checkpoint run id: {selected_event_run_id}")
    print(f"[issue_event_surface_decision_checkpoint] Decision run ids: {selected_decision_run_ids}")
    print(f"[issue_event_surface_decision_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_event_surface_decision_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_event_surface_decision_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_event_surface_decision_checkpoint] Report: {txt_path}")
    return {
        "run_id": checkpoint_run_id,
        "event_checkpoint_run_id": selected_event_run_id,
        "decision_run_ids": selected_decision_run_ids,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a guarded checkpoint from reviewed event-surface clusters.")
    parser.add_argument("--event-checkpoint-run-id", type=int, default=None)
    parser.add_argument("--decision-run-ids", default="")
    parser.add_argument("--min-safe", type=int, default=MIN_SAFE_PER_CLUSTER)
    parser.add_argument("--max-non-safe", type=int, default=MAX_NON_SAFE_PER_CLUSTER)
    parser.add_argument("--max-non-safe-rate", type=float, default=MAX_NON_SAFE_RATE)
    args = parser.parse_args()
    main(
        event_checkpoint_run_id=args.event_checkpoint_run_id,
        decision_run_ids=parse_ids(args.decision_run_ids),
        min_safe=args.min_safe,
        max_non_safe=args.max_non_safe,
        max_non_safe_rate=args.max_non_safe_rate,
    )
