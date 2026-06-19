from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "actionable_pending_review_queue_v1"
AGENT_KEY = "actionable_pending_lifecycle_gap_microagent"
ISSUE_FAMILY = "actionable_pending_lifecycle_gap"
QUEUE_STRATEGY = "run143_non_lifecycle_actionable_review"

REPAIR_TEXT_BY_SEGMENT_ID = {
    165552: "a",
    21232: "Todas as mudanças em [house_relation|El] são quadruplicadas",
}


def repair_bp3_journey(text: str) -> str:
    return text.replace(
        "diz uma[local_character.Custom('ES_OA')]",
        "diz um[local_character.Custom('ES_XA')]",
    )


def short(value: Any, limit: int = 260) -> str:
    text = ("" if value is None else str(value)).replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = reports_dir / f"{stamp}_actionable_pending_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
        base.with_name(base.name + "_codex_decisions").with_suffix(".jsonl"),
    )


def fetch_rows(conn, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            state.id AS state_item_id,
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.final_state,
            state.state_group,
            state.active_action,
            state.candidate_action,
            state.policy_action,
            state.lifecycle_policy_allowed,
            state.lifecycle_policy_action,
            state.reasons_json,
            source.english_text,
            source.spanish_text,
            source.old_text,
            confirmation.confirmed_text,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked,
            output.portuguese_text AS output_text
        FROM segment_state_items state
        JOIN source_segments source
          ON source.id = state.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.state_group = 'pending'
          AND state.final_state IN ('reopen_auto_confirmed', 'pending_unknown_open')
          AND COALESCE(state.lifecycle_policy_allowed, 0) = 0
        ORDER BY state.final_state, state.relative_path, state.source_line_number
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def candidate_text(row: dict[str, Any]) -> str:
    return row.get("confirmed_text") or row.get("output_text") or ""


def corrected_text(row: dict[str, Any]) -> str:
    segment_id = int(row["segment_id"])
    if segment_id in REPAIR_TEXT_BY_SEGMENT_ID:
        return REPAIR_TEXT_BY_SEGMENT_ID[segment_id]
    if segment_id == 3459:
        return repair_bp3_journey(candidate_text(row))
    return ""


def decision(row: dict[str, Any]) -> str:
    if corrected_text(row):
        return "needs_repair"
    return "false_positive_reopen"


def issue_kind(row: dict[str, Any]) -> str:
    segment_id = int(row["segment_id"])
    if row.get("final_state") == "pending_unknown_open":
        return "missing_confirmation_article_residue"
    if segment_id == 21232:
        return "non_latin_residue_repair"
    if segment_id == 3459:
        return "gender_token_prefix_repair"
    return "blocked_structure_false_positive_without_current_issue"


def queue_bucket(row: dict[str, Any]) -> str:
    kind = issue_kind(row)
    if kind in {"non_latin_residue_repair", "gender_token_prefix_repair", "missing_confirmation_article_residue"}:
        return kind
    source = row.get("confirmation_source") or "no_confirmation"
    if source == "controlled_token_subpolicy_production":
        return "controlled_token_subpolicy_false_reopen"
    if "learned" in source:
        return "learned_confirmation_false_reopen"
    if "curated" in source:
        return "curated_confirmation_false_reopen"
    return "other_confirmation_false_reopen"


def evidence_payload(row: dict[str, Any]) -> dict[str, Any]:
    current = candidate_text(row)
    proposed = corrected_text(row)
    validation_current = local_quality_validator.validate_text(current)
    validation_proposed = local_quality_validator.validate_text(proposed) if proposed else {"issues": []}
    current_issues = validation_current.get("issues") or []
    proposed_issues = validation_proposed.get("issues") or []
    return {
        "rule_version": RULE_VERSION,
        "source_segment_state": row.get("final_state"),
        "active_action": row.get("active_action"),
        "candidate_action": row.get("candidate_action"),
        "policy_action": row.get("policy_action"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "lifecycle_policy_allowed": int(row.get("lifecycle_policy_allowed") or 0),
        "lifecycle_policy_action": row.get("lifecycle_policy_action"),
        "current_issue_codes": [issue.get("code") for issue in current_issues],
        "current_high_issue_count": sum(1 for issue in current_issues if issue.get("severity") == "high"),
        "proposed_issue_codes": [issue.get("code") for issue in proposed_issues],
        "proposed_high_issue_count": sum(1 for issue in proposed_issues if issue.get("severity") == "high"),
        "token_status": (
            "same_structural_tokens"
            if structural_tokens(current) == structural_tokens(proposed or current)
            else "structural_token_change_review_required"
        ),
        "decision": decision(row),
    }


def insert_ledger(conn, *, segment_state_run_id: int, rows: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    now = db.utc_now()
    family_counts = Counter(queue_bucket(row) for row in rows)
    cur = conn.execute(
        """
        INSERT INTO ml_issue_ledger_runs (
            rule_version,
            segment_state_run_id,
            source_scope,
            pending_segments_count,
            ledger_segment_count,
            ledger_item_count,
            actionable_item_count,
            blocked_item_count,
            primary_family_counts_json,
            notes_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            segment_state_run_id,
            "non_lifecycle_actionable_pending",
            len(rows),
            json.dumps(dict(family_counts.most_common()), ensure_ascii=False, sort_keys=True),
            json.dumps({"selected_rows": len(rows)}, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    ledger_run_id = int(cur.lastrowid)
    items = []
    for row in rows:
        evidence = evidence_payload(row)
        items.append(
            {
                "run_id": ledger_run_id,
                "state_item_id": row.get("state_item_id"),
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "final_state": row.get("final_state"),
                "state_group": row.get("state_group"),
                "active_action": row.get("active_action"),
                "candidate_action": row.get("candidate_action"),
                "policy_action": row.get("policy_action"),
                "confirmation_level": None,
                "confirmation_label": row.get("confirmation_label"),
                "locked": int(row.get("locked") or 0),
                "issue_family": ISSUE_FAMILY,
                "issue_kind": issue_kind(row),
                "issue_role": "repair" if decision(row) == "needs_repair" else "guard",
                "issue_severity": "high" if decision(row) == "needs_repair" else "medium",
                "agent_key": AGENT_KEY,
                "route_status": "candidate",
                "proposed_action": "repair_or_mark_false_positive_reopen",
                "proposed_repair_text": corrected_text(row) or None,
                "token_impact": evidence["token_status"],
                "token_status": evidence["token_status"],
                "confidence_score": 0.93 if decision(row) == "false_positive_reopen" else 0.86,
                "evidence_text": short(candidate_text(row), 360),
                "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                "validation_status": "not_validated",
                "status": "open",
                "created_at": now,
            }
        )
    if items:
        columns = list(items[0].keys())
        conn.executemany(
            f"""
            INSERT INTO ml_issue_ledger_items ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            """,
            [tuple(item[column] for column in columns) for item in items],
        )
    inserted = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM ml_issue_ledger_items WHERE run_id = ? ORDER BY id",
            (ledger_run_id,),
        ).fetchall()
    ]
    finished = db.utc_now()
    conn.execute(
        """
        UPDATE ml_issue_ledger_runs
        SET ledger_segment_count = ?,
            ledger_item_count = ?,
            actionable_item_count = ?,
            family_counts_json = ?,
            agent_counts_json = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len({item["segment_id"] for item in inserted}),
            len(inserted),
            len(inserted),
            json.dumps({ISSUE_FAMILY: len(inserted)}, ensure_ascii=False, sort_keys=True),
            json.dumps({AGENT_KEY: len(inserted)}, ensure_ascii=False, sort_keys=True),
            finished,
            finished,
            ledger_run_id,
        ),
    )
    return ledger_run_id, inserted


def insert_queue(
    conn,
    *,
    ledger_run_id: int,
    ledger_items: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    by_segment = {int(row["segment_id"]): row for row in source_rows}
    bucket_counts = Counter(json.loads(item["evidence_json"]).get("decision") for item in ledger_items)
    report_path, csv_path, jsonl_path, decisions_template_path, _ = paths
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
            len(ledger_items),
            len(ledger_items),
            len(ledger_items),
            len(ledger_items),
            json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
            str(report_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    queue_run_id = int(cur.lastrowid)
    values = []
    for item in ledger_items:
        source = by_segment[int(item["segment_id"])]
        bucket = queue_bucket(source)
        values.append(
            (
                queue_run_id,
                ledger_run_id,
                int(item["id"]),
                int(item["segment_id"]),
                item["relative_path"],
                item["source_key"],
                item["source_line_number"],
                ISSUE_FAMILY,
                item["issue_kind"],
                AGENT_KEY,
                bucket,
                1000.0 + int(item["segment_id"]) % 17 / 100,
                "open",
                "repair_or_false_positive_reopen",
                item["evidence_text"],
                item["evidence_json"],
                source.get("english_text"),
                source.get("spanish_text"),
                candidate_text(source),
                None,
                None,
                corrected_text(source) or None,
                now,
                None,
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
            reviewer_decision,
            reviewer_notes,
            corrected_text,
            created_at,
            reviewed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return queue_run_id


def hydrate_queue_items(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM ml_issue_review_queue_items WHERE run_id = ? ORDER BY id",
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def write_artifacts(
    *,
    source_rows: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path, Path],
) -> None:
    by_segment = {int(row["segment_id"]): row for row in source_rows}
    report_path, csv_path, jsonl_path, template_path, decisions_path = paths
    decision_counts = Counter(decision(by_segment[int(item["segment_id"])]) for item in queue_items)
    bucket_counts = Counter(item["queue_bucket"] for item in queue_items)
    lines = [
        "Actionable pending review queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue items: {len(queue_items)}",
        "",
        "Decision counts:",
    ]
    for key, value in decision_counts.most_common():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Buckets:"])
    for key, value in bucket_counts.most_common():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Rows:"])
    for item in queue_items:
        source = by_segment[int(item["segment_id"])]
        evidence = json.loads(item["evidence_json"])
        lines.extend(
            [
                f"- queue_item: {item['id']}",
                f"  segment_id: {item['segment_id']}",
                f"  source: {item['relative_path']}:{item['source_line_number']}::{item['source_key']}",
                f"  bucket: {item['queue_bucket']}",
                f"  decision: {decision(source)}",
                f"  token_status: {evidence.get('token_status')}",
                f"  current_high_issue_count: {evidence.get('current_high_issue_count')}",
                f"  proposed_high_issue_count: {evidence.get('proposed_high_issue_count')}",
                f"  confirmed/output: {short(candidate_text(source))}",
                f"  corrected: {short(corrected_text(source)) if corrected_text(source) else ''}",
            ]
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fields = [
        "queue_item_id",
        "queue_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "queue_bucket",
        "issue_kind",
        "decision",
        "english_text",
        "spanish_text",
        "confirmed_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in queue_items:
            source = by_segment[int(item["segment_id"])]
            writer.writerow(
                {
                    "queue_item_id": item["id"],
                    "queue_run_id": item["run_id"],
                    "segment_id": item["segment_id"],
                    "relative_path": item["relative_path"],
                    "source_line_number": item["source_line_number"],
                    "source_key": item["source_key"],
                    "queue_bucket": item["queue_bucket"],
                    "issue_kind": item["issue_kind"],
                    "decision": decision(source),
                    "english_text": source.get("english_text"),
                    "spanish_text": source.get("spanish_text"),
                    "confirmed_text": candidate_text(source),
                    "corrected_text": corrected_text(source),
                }
            )
    with jsonl_path.open("w", encoding="utf-8") as queue_handle, template_path.open(
        "w", encoding="utf-8"
    ) as template_handle, decisions_path.open("w", encoding="utf-8") as decisions_handle:
        for item in queue_items:
            source = by_segment[int(item["segment_id"])]
            payload = {
                "queue_item_id": item["id"],
                "queue_run_id": item["run_id"],
                "segment_id": item["segment_id"],
                "relative_path": item["relative_path"],
                "source_line_number": item["source_line_number"],
                "source_key": item["source_key"],
                "agent_key": item["agent_key"],
                "issue_family": item["issue_family"],
                "issue_kind": item["issue_kind"],
                "queue_bucket": item["queue_bucket"],
                "suggested_decision": item["suggested_decision"],
                "decision": decision(source),
                "english_text": source.get("english_text"),
                "spanish_text": source.get("spanish_text"),
                "old_text": source.get("old_text"),
                "confirmed_text": candidate_text(source),
                "output_text": source.get("output_text"),
                "corrected_text": corrected_text(source),
                "evidence_json": json.loads(item["evidence_json"]),
            }
            queue_handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            template = {
                "queue_item_id": item["id"],
                "queue_run_id": item["run_id"],
                "segment_id": item["segment_id"],
                "decision": "pending",
                "corrected_text": corrected_text(source),
                "notes": "",
            }
            template_handle.write(json.dumps(template, ensure_ascii=False, sort_keys=True) + "\n")
            reviewed = {
                "queue_item_id": item["id"],
                "queue_run_id": item["run_id"],
                "segment_id": item["segment_id"],
                "decision": decision(source),
                "corrected_text": corrected_text(source),
                "notes": (
                    "non_lifecycle_actionable: current blocked_structure reopen is false positive with no validator high issue"
                    if decision(source) == "false_positive_reopen"
                    else "non_lifecycle_actionable: confirmed/output requires targeted repair from reviewed DB evidence"
                ),
                "reviewer": "codex_learning_front",
            }
            decisions_handle.write(json.dumps(reviewed, ensure_ascii=False, sort_keys=True) + "\n")


def main(*, segment_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = segment_state_run_id or latest_segment_state_run_id(conn)
        rows = fetch_rows(conn, selected_run_id)
        ledger_run_id, ledger_items = insert_ledger(conn, segment_state_run_id=selected_run_id, rows=rows)
        queue_run_id = insert_queue(conn, ledger_run_id=ledger_run_id, ledger_items=ledger_items, source_rows=rows, paths=paths)
        queue_items = hydrate_queue_items(conn, queue_run_id)
        conn.commit()
    write_artifacts(source_rows=rows, queue_items=queue_items, paths=paths)
    report_path, csv_path, jsonl_path, template_path, decisions_path = paths
    print(f"[actionable_pending_review_queue] Segment-state run id: {selected_run_id}")
    print(f"[actionable_pending_review_queue] Ledger run id: {ledger_run_id}")
    print(f"[actionable_pending_review_queue] Queue run id: {queue_run_id}")
    print(f"[actionable_pending_review_queue] Selected: {len(queue_items)}")
    print(f"[actionable_pending_review_queue] Report: {report_path}")
    print(f"[actionable_pending_review_queue] Decisions: {decisions_path}")
    return {
        "segment_state_run_id": selected_run_id,
        "ledger_run_id": ledger_run_id,
        "queue_run_id": queue_run_id,
        "selected": len(queue_items),
        "report_path": str(report_path),
        "jsonl_path": str(jsonl_path),
        "decisions_path": str(decisions_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create review queue for non-lifecycle actionable pending rows.")
    parser.add_argument("--segment-state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(segment_state_run_id=args.segment_state_run_id)
