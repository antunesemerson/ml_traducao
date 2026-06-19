from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import issue_short_label_context_lane_checkpoint as base_checkpoint
from apply_segment_state_updates import short


RULE_VERSION = "issue_short_label_context_lane_partial_promotion_checkpoint_v1"
CHECKPOINT_NAME = "short_label_context_lane_partial_file_profile_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_context_lane_partial_file_profile_safe_fragment"
POLICY_NAME = "short_label_context_lane_partial_file_profile_checkpoint"
SOURCE_KIND = "partial_file_profile_promotion_dry_run"
AGENT_KEY = base_checkpoint.AGENT_KEY
SAFE_DECISION = base_checkpoint.SAFE_DECISION


def report_paths(settings: dict[str, Any], dry_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_partial_promotion_checkpoint_dry_run_{dry_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def table_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def add_column_if_missing(conn, table_name: str, column_name: str, column_sql: str) -> None:
    if column_name not in table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def ensure_tables(conn) -> None:
    base_checkpoint.ensure_tables(conn)

    for table_name, columns in {
        "ml_issue_short_label_context_lane_checkpoint_runs": [
            ("source_kind", "source_kind TEXT"),
            ("source_run_id", "source_run_id INTEGER"),
            ("source_audit_run_id", "source_audit_run_id INTEGER"),
            ("source_shadow_run_id", "source_shadow_run_id INTEGER"),
            ("eligible_file_count", "eligible_file_count INTEGER NOT NULL DEFAULT 0"),
            ("evaluated_count", "evaluated_count INTEGER NOT NULL DEFAULT 0"),
            ("source_summary_json", "source_summary_json TEXT"),
        ],
        "ml_issue_short_label_context_lane_checkpoint_items": [
            ("source_kind", "source_kind TEXT"),
            ("source_run_id", "source_run_id INTEGER"),
            ("source_item_id", "source_item_id INTEGER"),
            ("diagnostic_item_id", "diagnostic_item_id INTEGER"),
            ("classifier_reason", "classifier_reason TEXT"),
        ],
    }.items():
        for column_name, column_sql in columns:
            add_column_if_missing(conn, table_name, column_name, column_sql)

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_context_lane_checkpoint_runs_source
        ON ml_issue_short_label_context_lane_checkpoint_runs(source_kind, source_run_id);

        CREATE INDEX IF NOT EXISTS idx_short_label_context_lane_checkpoint_items_source
        ON ml_issue_short_label_context_lane_checkpoint_items(source_kind, source_run_id, source_item_id);
        """
    )


def latest_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_context_lane_partial_promotion_dry_run_runs
        WHERE dry_run_status = 'ready_for_checkpoint_dry_run'
          AND production_release_allowed = 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No partial-promotion dry-run ready for checkpoint was found.")
    return int(row["id"])


def fetch_dry_run(conn, *, dry_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_context_lane_partial_promotion_dry_run_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial-promotion dry-run not found: {dry_run_id}")
    dry_run = dict(row)
    if dry_run.get("dry_run_status") != "ready_for_checkpoint_dry_run":
        raise RuntimeError(f"Dry-run {dry_run_id} is not ready for checkpoint: {dry_run.get('dry_run_status')}")
    if int(dry_run.get("production_release_allowed") or 0) != 0:
        raise RuntimeError(f"Dry-run {dry_run_id} unexpectedly allows production release.")
    return dry_run


def existing_checkpoint_run_id(conn, *, dry_run_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_context_lane_checkpoint_runs
        WHERE source_kind = ?
          AND source_run_id = ?
          AND rule_version = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (SOURCE_KIND, dry_run_id, RULE_VERSION),
    ).fetchone()
    return int(row["id"]) if row else None


def source_key_is_sensitive(source_key: str) -> bool:
    normalized = source_key.strip().lower()
    return normalized in {"adjective_khanal", "adjective_khaganal"}


def subpolicy_from_path(relative_path: str) -> str:
    stem = Path(relative_path).stem
    if stem.endswith("_l_spanish"):
        stem = stem.removesuffix("_l_spanish")
    return f"file_profile:{stem}"


def fetch_ready_items(conn, *, dry_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            ledger.run_id AS ledger_run_id,
            ledger.issue_family,
            ledger.issue_kind,
            EXISTS (
                SELECT 1
                FROM ml_issue_short_label_context_lane_checkpoint_items existing
                WHERE existing.ledger_item_id = item.ledger_item_id
                  AND existing.checkpoint_allowed = 1
            ) AS already_checkpointed
        FROM ml_issue_short_label_context_lane_partial_promotion_dry_run_items item
        JOIN ml_issue_ledger_items ledger ON ledger.id = item.ledger_item_id
        WHERE item.dry_run_id = ?
          AND item.promotion_status = 'promotion_ready'
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (dry_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def blocked_summary(conn, *, dry_run_id: int) -> Counter[str]:
    rows = conn.execute(
        """
        SELECT COALESCE(block_reason, 'unknown') AS reason, COUNT(*) AS n
        FROM ml_issue_short_label_context_lane_partial_promotion_dry_run_items
        WHERE dry_run_id = ?
          AND promotion_status != 'promotion_ready'
        GROUP BY COALESCE(block_reason, 'unknown')
        ORDER BY n DESC
        """,
        (dry_run_id,),
    ).fetchall()
    return Counter({str(row["reason"]): int(row["n"] or 0) for row in rows})


def classify_ready_row(row: dict[str, Any]) -> tuple[int, str]:
    if str(row.get("classifier_decision") or "") != SAFE_DECISION:
        return 0, "classifier_not_safe"
    if source_key_is_sensitive(str(row.get("source_key") or "")):
        return 0, "sensitive_domain_key"
    if int(row.get("already_checkpointed") or 0):
        return 0, "already_checkpointed"
    return 1, ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    dry_run: dict[str, Any],
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    blocked_from_dry_run: Counter[str],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "source_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "issue_family",
        "issue_kind",
        "subpolicy_name",
        "classifier_decision",
        "classifier_reason",
        "checkpoint_action",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows + skipped:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows + skipped:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    ready_by_file = Counter(str(row["relative_path"]) for row in rows)
    skipped_by_reason = Counter(str(row["block_reason"] or "unknown") for row in skipped)
    subpolicy_counts = Counter(str(row["subpolicy_name"]) for row in rows)

    lines = [
        "Short-label Context Lane Partial Promotion Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Source dry-run id: {dry_run['id']}",
        f"Source audit run id: {dry_run['audit_run_id']}",
        f"Source shadow run id: {dry_run['shadow_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Source eligible files: {int(dry_run['eligible_file_count']):,}",
        f"- Source evaluated rows: {int(dry_run['evaluated_count']):,}",
        f"- Source promotion-ready rows: {int(dry_run['promotion_ready_count']):,}",
        f"- Source blocked rows: {int(dry_run['blocked_count']):,}",
        f"- Checkpoint allowed issue-items: {counts['allowed']:,}",
        f"- Checkpoint skipped issue-items: {counts['skipped']:,}",
        "",
        "Allowed by file:",
    ]
    for path, value in ready_by_file.most_common():
        lines.append(f"- {path}: {value:,}")

    lines.extend(["", "Allowed by subpolicy:"])
    for subpolicy, value in subpolicy_counts.most_common():
        lines.append(f"- {subpolicy}: {value:,}")

    lines.extend(["", "Skipped during checkpoint materialization:"])
    if skipped_by_reason:
        for reason, value in skipped_by_reason.most_common():
            lines.append(f"- {reason}: {value:,}")
    else:
        lines.append("- none: 0")

    lines.extend(["", "Dry-run blocked rows kept out of checkpoint:"])
    if blocked_from_dry_run:
        for reason, value in blocked_from_dry_run.most_common():
            lines.append(f"- {reason}: {value:,}")
    else:
        lines.append("- none: 0")

    lines.extend(["", "Allowed samples:"])
    for row in rows[:50]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | {short(row['evidence_text'], 120)}"
        )

    lines.extend(
        [
            "",
            "Safety note:",
            "- This materializes issue-level coverage only.",
            "- It does not write source/output, create confirmations, or promote lifecycle policy.",
            "- These issue fragments still require segment-level composition before a segment can be closed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, dry_run_id: int | None = None, force: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_dry_run_id = dry_run_id or latest_dry_run_id(conn)
        dry_run = fetch_dry_run(conn, dry_run_id=selected_dry_run_id)

        existing_run_id = existing_checkpoint_run_id(conn, dry_run_id=selected_dry_run_id)
        if existing_run_id is not None and not force:
            raise RuntimeError(
                f"Dry-run {selected_dry_run_id} was already materialized as checkpoint run {existing_run_id}. "
                "Use --force to create another audit run."
            )

        candidates = fetch_ready_items(conn, dry_run_id=selected_dry_run_id)
        blocked_from_dry_run = blocked_summary(conn, dry_run_id=selected_dry_run_id)
        ledger_run_ids = {int(row["ledger_run_id"]) for row in candidates}
        if not candidates:
            raise RuntimeError(f"Dry-run {selected_dry_run_id} has no promotion-ready rows.")
        if len(ledger_run_ids) != 1:
            raise RuntimeError(f"Dry-run {selected_dry_run_id} spans multiple ledger runs: {sorted(ledger_run_ids)}")

        rows: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for candidate in candidates:
            allowed, reason = classify_ready_row(candidate)
            subpolicy_name = subpolicy_from_path(str(candidate["relative_path"]))
            row = {
                "queue_run_id": 0,
                "queue_item_id": 0,
                "ledger_run_id": int(candidate["ledger_run_id"]),
                "ledger_item_id": int(candidate["ledger_item_id"]),
                "segment_id": int(candidate["segment_id"]),
                "relative_path": str(candidate["relative_path"]),
                "source_key": str(candidate["source_key"]),
                "source_line_number": candidate.get("source_line_number"),
                "queue_bucket": f"short_label_context_file_profile:{subpolicy_name}",
                "issue_family": str(candidate.get("issue_family") or "short_label_style_microagent"),
                "issue_kind": str(candidate.get("issue_kind") or "unknown_issue"),
                "normalized_decision": SAFE_DECISION,
                "evidence_label": "file_profile_promoted_safe_short_label",
                "agent_key": AGENT_KEY,
                "subpolicy_name": subpolicy_name,
                "checkpoint_action": CHECKPOINT_ACTION,
                "checkpoint_allowed": allowed,
                "block_reason": reason,
                "evidence_text": candidate.get("evidence_text") or "",
                "source_kind": SOURCE_KIND,
                "source_run_id": selected_dry_run_id,
                "source_item_id": int(candidate["id"]),
                "diagnostic_item_id": int(candidate["diagnostic_item_id"]),
                "classifier_decision": str(candidate.get("classifier_decision") or ""),
                "classifier_reason": str(candidate.get("classifier_reason") or ""),
            }
            if allowed:
                rows.append(row)
                counts["allowed"] += 1
            else:
                skipped.append(row)
                counts["skipped"] += 1
                counts[f"skip:{reason}"] += 1

        checkpoint_status = "ready_for_coverage" if rows else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_dry_run_id)
        now = db.utc_now()
        summary = {
            "source_kind": SOURCE_KIND,
            "source_run_id": selected_dry_run_id,
            "source_audit_run_id": int(dry_run["audit_run_id"]),
            "source_shadow_run_id": int(dry_run["shadow_run_id"]),
            "source_promotion_ready": int(dry_run["promotion_ready_count"]),
            "source_blocked": int(dry_run["blocked_count"]),
            "checkpoint_allowed": counts["allowed"],
            "checkpoint_skipped": counts["skipped"],
            "dry_run_blockers": dict(blocked_from_dry_run),
            "production_release_allowed": 0,
        }
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_context_lane_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                queue_run_id,
                ledger_run_id,
                decision_count,
                safe_decision_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                decision_counts_json,
                blocker_counts_json,
                subpolicy_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at,
                source_kind,
                source_run_id,
                source_audit_run_id,
                source_shadow_run_id,
                eligible_file_count,
                evaluated_count,
                source_summary_json
            )
            VALUES (?, ?, ?, ?, 'checkpoint_only', ?, 0, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                AGENT_KEY,
                next(iter(ledger_run_ids)),
                len(rows) + len(skipped),
                len(rows),
                counts["allowed"],
                counts["skipped"],
                json.dumps({SAFE_DECISION: len(rows)}, ensure_ascii=False, sort_keys=True),
                json.dumps(
                    {key.removeprefix("skip:"): value for key, value in counts.items() if key.startswith("skip:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(Counter(row["subpolicy_name"] for row in rows), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
                SOURCE_KIND,
                selected_dry_run_id,
                int(dry_run["audit_run_id"]),
                int(dry_run["shadow_run_id"]),
                int(dry_run["eligible_file_count"]),
                int(dry_run["evaluated_count"]),
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_context_lane_checkpoint_items (
                checkpoint_run_id,
                queue_run_id,
                queue_item_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                queue_bucket,
                issue_family,
                issue_kind,
                normalized_decision,
                evidence_label,
                agent_key,
                subpolicy_name,
                checkpoint_action,
                checkpoint_allowed,
                block_reason,
                evidence_text,
                created_at,
                source_kind,
                source_run_id,
                source_item_id,
                diagnostic_item_id,
                classifier_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_run_id,
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    row["evidence_text"],
                    now,
                    row["source_kind"],
                    row["source_run_id"],
                    row["source_item_id"],
                    row["diagnostic_item_id"],
                    row["classifier_reason"],
                )
                for row in rows + skipped
            ],
        )
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        dry_run=dry_run,
        rows=rows,
        skipped=skipped,
        blocked_from_dry_run=blocked_from_dry_run,
        counts=counts,
    )

    print("[issue_short_label_context_lane_partial_promotion_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_context_lane_partial_promotion_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_short_label_context_lane_partial_promotion_checkpoint] Dry-run id: {selected_dry_run_id}")
    print(f"[issue_short_label_context_lane_partial_promotion_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_short_label_context_lane_partial_promotion_checkpoint] Skipped: {counts['skipped']:,}")
    print(f"[issue_short_label_context_lane_partial_promotion_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "dry_run_id": selected_dry_run_id,
        "allowed": counts["allowed"],
        "skipped": counts["skipped"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Materialize partial file-profile promotion as issue-level checkpoint coverage.")
    parser.add_argument("--dry-run-id", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Create a new audit checkpoint even if this dry-run was already materialized.")
    args = parser.parse_args()
    main(dry_run_id=args.dry_run_id, force=args.force)
