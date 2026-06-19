from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_event_surface_assisted_draft import classify as classify_surface
from issue_semantic_short_label_blocked_surface_queue import (
    TARGETS,
    fetch_candidates,
    fetch_dry_run,
    latest_dry_run_id,
    queue_bucket_for,
)


RULE_VERSION = "issue_semantic_short_label_blocked_surface_checkpoint_v2_strict_label_guard"
POLICY_NAME = "semantic_short_label_blocked_surface_shadow"
POLICY_STATUS = "shadow"
CHECKPOINT_ACTION = "stage_semantic_short_label_blocked_surface_shadow"
POSITIVE_DECISIONS = {"safe_short_label", "composition_ready"}
POSITIVE_EVIDENCE_LABELS = {"positive_evidence", "composition_ready"}
DEFAULT_MIN_SAFE_PER_CLUSTER = 12
DEFAULT_MAX_NON_SAFE_PER_CLUSTER = 0
DEFAULT_MAX_NON_SAFE_RATE = 0.0
DEFAULT_ROW_GUARD_PROFILE = "strict_label_v1"
ROW_GUARD_PROFILES = {"legacy", "strict_label_v1"}


def parse_ids(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def latest_decision_run_ids(conn, *, agent_key: str, limit: int = 4) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        JOIN ml_issue_review_queue_runs queue_run ON queue_run.id = decision.queue_run_id
        WHERE run.finished_at IS NOT NULL
          AND decision.agent_key = ?
          AND queue_run.queue_strategy = 'semantic_short_label_blocked_surface_current_run'
        ORDER BY run.id DESC
        LIMIT ?
        """,
        (agent_key, limit),
    ).fetchall()
    return sorted(int(row["id"]) for row in rows)


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_blocked_surface_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            dry_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            row_guard_profile TEXT NOT NULL DEFAULT 'legacy',
            decision_run_ids_json TEXT NOT NULL,
            min_safe_per_cluster INTEGER NOT NULL,
            max_non_safe_per_cluster INTEGER NOT NULL,
            max_non_safe_rate REAL NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            mature_cluster_count INTEGER NOT NULL DEFAULT 0,
            reviewed_cluster_count INTEGER NOT NULL DEFAULT 0,
            allowed_clusters_json TEXT,
            cluster_stats_json TEXT,
            block_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_blocked_surface_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            dry_run_id INTEGER NOT NULL,
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
            classifier_reason TEXT,
            profile TEXT,
            token_count INTEGER NOT NULL DEFAULT 0,
            char_count INTEGER NOT NULL DEFAULT 0,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_semantic_short_label_blocked_surface_checkpoint_runs(id)
              ON DELETE CASCADE
        )
        """
    )
    db.ensure_columns(
        conn,
        "ml_issue_semantic_short_label_blocked_surface_checkpoint_runs",
        [("row_guard_profile", "TEXT NOT NULL DEFAULT 'legacy'")],
    )


def report_paths(settings: dict[str, Any], dry_run_id: int, agent_key: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_blocked_surface_checkpoint_{agent_key}_dry_run_{dry_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def load_cluster_stats(
    conn,
    *,
    decision_run_ids: list[int],
    agent_key: str,
    dry_run_id: int,
) -> dict[str, dict[str, Any]]:
    if not decision_run_ids:
        return {}
    placeholders = ",".join("?" for _ in decision_run_ids)
    params: list[Any] = [*decision_run_ids, agent_key, dry_run_id]
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
        JOIN ml_issue_review_queue_runs queue_run ON queue_run.id = queue.run_id
        WHERE decision.run_id IN ({placeholders})
          AND decision.agent_key = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND queue_run.queue_strategy = 'semantic_short_label_blocked_surface_current_run'
        ORDER BY decision.id
        """,
        tuple(params[:-1]),
    ).fetchall()

    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence: dict[str, Any] = {}
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        if int(evidence.get("dry_run_id") or -1) != dry_run_id:
            continue
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


def mature_clusters(
    cluster_stats: dict[str, dict[str, Any]],
    *,
    min_safe: int,
    max_non_safe: int,
    max_non_safe_rate: float,
) -> set[str]:
    mature: set[str] = set()
    for cluster_key, stats in cluster_stats.items():
        total = int(stats.get("total") or 0)
        safe = int(stats.get("safe") or 0)
        non_safe = int(stats.get("non_safe") or 0)
        non_safe_rate = (non_safe / total) if total else 1.0
        if safe >= min_safe and non_safe <= max_non_safe and non_safe_rate <= max_non_safe_rate:
            mature.add(cluster_key)
    return mature


def visible_words(text: str) -> list[str]:
    return [part for part in text.strip().split() if part]


def strict_label_block_reason(row: dict[str, Any]) -> str:
    text = str(row.get("current_text") or "").strip()
    if not text:
        return "strict_label_v1:empty_text"
    if "\n" in text or "\r" in text:
        return "strict_label_v1:linebreak"
    if any(marker in text for marker in ("#", "$", "[", "]")):
        return "strict_label_v1:dynamic_marker"
    if any(mark in text for mark in (",", ".", "!", "?", ";", ":")):
        return "strict_label_v1:comma_or_sentence_punctuation"
    if len(text) > 32:
        return "strict_label_v1:too_long"
    if len(visible_words(text)) > 3:
        return "strict_label_v1:too_many_words"
    return ""


def classify_candidate(
    row: dict[str, Any],
    mature: set[str],
    agent_key: str,
    row_guard_profile: str,
) -> tuple[int, str, str, str]:
    cluster_key = str(row.get("cluster_key") or "")
    queue_bucket = queue_bucket_for(agent_key, row)
    subpolicy_name = str(row.get("recommended_action") or "")
    if cluster_key not in mature:
        return 0, "cluster_not_mature_from_review_evidence", queue_bucket, subpolicy_name
    if row_guard_profile == "strict_label_v1":
        strict_block = strict_label_block_reason(row)
        if strict_block:
            return 0, strict_block, queue_bucket, subpolicy_name
    draft_row = {
        "evidence_text": row.get("current_text") or "",
        "source_key": row.get("source_key") or "",
        "queue_bucket": queue_bucket,
    }
    decision, _corrected_text, reason = classify_surface(draft_row, default_agent_key=agent_key)
    if decision not in POSITIVE_DECISIONS:
        return 0, f"row_guard:{decision}:{reason}", queue_bucket, subpolicy_name
    return 1, "", queue_bucket, subpolicy_name


def serialize_cluster_stats(cluster_stats: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for cluster_key, stats in cluster_stats.items():
        total = int(stats["total"] or 0)
        non_safe = int(stats["non_safe"] or 0)
        payload[cluster_key] = {
            "total": total,
            "safe": int(stats["safe"] or 0),
            "non_safe": non_safe,
            "non_safe_rate": (non_safe / total) if total else 1.0,
            "decision_counts": dict(stats["decision_counts"]),
            "agent_key": stats.get("agent_key"),
            "queue_bucket": stats.get("queue_bucket"),
        }
    return payload


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    dry_run: dict[str, Any],
    agent_key: str,
    row_guard_profile: str,
    decision_run_ids: list[int],
    cluster_stats: dict[str, dict[str, Any]],
    mature: set[str],
    rows: list[dict[str, Any]],
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

    counts = Counter("allowed" if row["checkpoint_allowed"] else "blocked" for row in rows)
    allowed_by_cluster = Counter(row["cluster_key"] for row in rows if row["checkpoint_allowed"])
    blocks = Counter(row["block_reason"] for row in rows if not row["checkpoint_allowed"])
    cluster_lines: list[str] = []
    for cluster_key, stats in sorted(cluster_stats.items(), key=lambda item: (-int(item[1]["total"]), item[0])):
        decision_text = ", ".join(f"{key}={value}" for key, value in stats["decision_counts"].most_common())
        total = int(stats["total"] or 0)
        non_safe_rate = (int(stats["non_safe"]) / total) if total else 1.0
        cluster_lines.append(
            f"- {'ALLOW' if cluster_key in mature else 'hold'} {cluster_key}: "
            f"safe={stats['safe']}, non_safe={stats['non_safe']}, total={stats['total']}, "
            f"non_safe_rate={non_safe_rate:.2%} ({decision_text})"
        )

    lines = [
        "Semantic + short-label blocked surface checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME}",
        f"Policy status: {POLICY_STATUS}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Dry-run id: {dry_run['id']}",
        f"Ledger run id: {dry_run['ledger_run_id']}",
        f"Agent key: {agent_key}",
        f"Row guard profile: {row_guard_profile}",
        f"Decision run ids: {decision_run_ids}",
        "",
        "Summary:",
        f"- candidates: {len(rows):,}",
        f"- allowed: {counts['allowed']:,}",
        f"- blocked: {counts['blocked']:,}",
        f"- mature clusters: {len(mature):,}/{len(cluster_stats):,}",
        f"- min safe per cluster: {min_safe:,}",
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
            f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number') or ''} "
            f"| {row['source_key']} | {row['current_text']}"
        )
    lines.extend(["", "Blocked sample:"])
    for row in [item for item in rows if not item["checkpoint_allowed"]][:40]:
        lines.append(
            f"- {row['block_reason']} | segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number') or ''} "
            f"| {row['source_key']} | {row['current_text']}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations, no lifecycle closure.",
            "- Lifecycle integration must be handled separately after this shadow checkpoint is reviewed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    dry_run_id: int | None = None,
    agent_key: str = "micro_short_label_style",
    decision_run_ids: list[int] | None = None,
    min_safe_per_cluster: int = DEFAULT_MIN_SAFE_PER_CLUSTER,
    max_non_safe_per_cluster: int = DEFAULT_MAX_NON_SAFE_PER_CLUSTER,
    max_non_safe_rate: float = DEFAULT_MAX_NON_SAFE_RATE,
    row_guard_profile: str = DEFAULT_ROW_GUARD_PROFILE,
) -> dict[str, Any]:
    if row_guard_profile not in ROW_GUARD_PROFILES:
        raise RuntimeError(f"Unsupported row guard profile: {row_guard_profile}")
    settings = db.load_settings()
    conn = db.connect(settings)
    try:
        ensure_tables(conn)
        resolved_dry_run_id = dry_run_id if dry_run_id is not None else latest_dry_run_id(conn)
        dry_run = fetch_dry_run(conn, dry_run_id=resolved_dry_run_id)
        if agent_key not in TARGETS:
            raise RuntimeError(f"Unsupported agent key: {agent_key}")
        resolved_decision_run_ids = decision_run_ids or latest_decision_run_ids(conn, agent_key=agent_key)
        cluster_stats = load_cluster_stats(
            conn,
            decision_run_ids=resolved_decision_run_ids,
            agent_key=agent_key,
            dry_run_id=resolved_dry_run_id,
        )
        mature = mature_clusters(
            cluster_stats,
            min_safe=min_safe_per_cluster,
            max_non_safe=max_non_safe_per_cluster,
            max_non_safe_rate=max_non_safe_rate,
        )
        candidates = fetch_candidates(
            conn,
            dry_run=dry_run,
            agent_key=agent_key,
            block_reason="",
            include_existing=True,
        )

        now = db.utc_now()
        txt_path, csv_path, jsonl_path = report_paths(settings, resolved_dry_run_id, agent_key)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_semantic_short_label_blocked_surface_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                dry_run_id,
                ledger_run_id,
                agent_key,
                row_guard_profile,
                decision_run_ids_json,
                min_safe_per_cluster,
                max_non_safe_per_cluster,
                max_non_safe_rate,
                candidate_count,
                allowed_count,
                blocked_count,
                mature_cluster_count,
                reviewed_cluster_count,
                allowed_clusters_json,
                cluster_stats_json,
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                resolved_dry_run_id,
                int(dry_run["ledger_run_id"]),
                agent_key,
                row_guard_profile,
                json.dumps(resolved_decision_run_ids, ensure_ascii=False, sort_keys=True),
                min_safe_per_cluster,
                max_non_safe_per_cluster,
                max_non_safe_rate,
                len(mature),
                len(cluster_stats),
                json.dumps(sorted(mature), ensure_ascii=False, sort_keys=True),
                json.dumps(serialize_cluster_stats(cluster_stats), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)

        rows: list[dict[str, Any]] = []
        block_counts: Counter[str] = Counter()
        target = TARGETS[agent_key]
        for candidate in candidates:
            allowed, block_reason, queue_bucket, subpolicy_name = classify_candidate(
                candidate,
                mature,
                agent_key,
                row_guard_profile,
            )
            if not allowed:
                block_counts[block_reason] += 1
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_semantic_short_label_blocked_surface_checkpoint_items (
                    run_id,
                    dry_run_id,
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
                    classifier_reason,
                    profile,
                    token_count,
                    char_count,
                    current_text,
                    corrected_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    checkpoint_run_id,
                    resolved_dry_run_id,
                    int(dry_run["ledger_run_id"]),
                    int(candidate["ledger_item_id"]),
                    int(candidate["segment_id"]),
                    candidate["relative_path"],
                    candidate["source_key"],
                    candidate.get("source_line_number"),
                    agent_key,
                    target["issue_family"],
                    candidate.get("issue_kind") or candidate.get("text_signature") or "",
                    queue_bucket,
                    subpolicy_name,
                    candidate["cluster_key"],
                    allowed,
                    CHECKPOINT_ACTION,
                    block_reason,
                    candidate.get("classifier_reason") or "",
                    candidate.get("profile") or "",
                    int(candidate.get("token_count") or 0),
                    int(candidate.get("char_count") or 0),
                    candidate.get("current_text") or "",
                    now,
                ),
            )
            rows.append(
                {
                    "checkpoint_item_id": int(item_cursor.lastrowid),
                    "checkpoint_allowed": allowed,
                    "block_reason": block_reason,
                    "ledger_item_id": int(candidate["ledger_item_id"]),
                    "segment_id": int(candidate["segment_id"]),
                    "relative_path": candidate["relative_path"],
                    "source_key": candidate["source_key"],
                    "source_line_number": candidate.get("source_line_number"),
                    "agent_key": agent_key,
                    "subpolicy_name": subpolicy_name,
                    "queue_bucket": queue_bucket,
                    "cluster_key": candidate["cluster_key"],
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "current_text": candidate.get("current_text") or "",
                    "corrected_text": None,
                }
            )

        counts = Counter("allowed" if row["checkpoint_allowed"] else "blocked" for row in rows)
        conn.execute(
            """
            UPDATE ml_issue_semantic_short_label_blocked_surface_checkpoint_runs
            SET
                candidate_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                block_counts_json = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                counts["allowed"],
                counts["blocked"],
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                now,
                now,
                checkpoint_run_id,
            ),
        )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            dry_run=dry_run,
            agent_key=agent_key,
            row_guard_profile=row_guard_profile,
            decision_run_ids=resolved_decision_run_ids,
            cluster_stats=cluster_stats,
            mature=mature,
            rows=rows,
            min_safe=min_safe_per_cluster,
            max_non_safe=max_non_safe_per_cluster,
            max_non_safe_rate=max_non_safe_rate,
        )
        conn.commit()
    finally:
        conn.close()

    result = {
        "checkpoint_run_id": checkpoint_run_id,
        "dry_run_id": resolved_dry_run_id,
        "ledger_run_id": int(dry_run["ledger_run_id"]),
        "agent_key": agent_key,
        "row_guard_profile": row_guard_profile,
        "decision_run_ids": resolved_decision_run_ids,
        "candidates": len(rows),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "mature_clusters": len(mature),
        "reviewed_clusters": len(cluster_stats),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    print("[issue_semantic_short_label_blocked_surface_checkpoint] Checkpoint complete")
    for key, value in result.items():
        print(f"[issue_semantic_short_label_blocked_surface_checkpoint] {key}: {value}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint current semantic+short-label blocked surface evidence.")
    parser.add_argument("--dry-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default="micro_short_label_style")
    parser.add_argument("--decision-run-ids", default=None)
    parser.add_argument("--min-safe-per-cluster", type=int, default=DEFAULT_MIN_SAFE_PER_CLUSTER)
    parser.add_argument("--max-non-safe-per-cluster", type=int, default=DEFAULT_MAX_NON_SAFE_PER_CLUSTER)
    parser.add_argument("--max-non-safe-rate", type=float, default=DEFAULT_MAX_NON_SAFE_RATE)
    parser.add_argument("--row-guard-profile", default=DEFAULT_ROW_GUARD_PROFILE, choices=sorted(ROW_GUARD_PROFILES))
    args = parser.parse_args()
    main(
        dry_run_id=args.dry_run_id,
        agent_key=args.agent_key,
        decision_run_ids=parse_ids(args.decision_run_ids),
        min_safe_per_cluster=args.min_safe_per_cluster,
        max_non_safe_per_cluster=args.max_non_safe_per_cluster,
        max_non_safe_rate=args.max_non_safe_rate,
        row_guard_profile=args.row_guard_profile,
    )
