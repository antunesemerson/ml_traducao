from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_specialist_models import SPECIALIST_GROUPS, SPECIALISTS


RULE_VERSION = "ml_agent_audit_queue_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def next_lote_number(conn) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review_%_lote%'
        """
    ).fetchall()
    lote_numbers = []
    for row in rows:
        match = re.search(r"lote(\d+)", str(row["mode"] or ""))
        if match:
            lote_numbers.append(int(match.group(1)))
    return (max(lote_numbers) + 1) if lote_numbers else 1


def latest_routing_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_agent_routing_runs
        WHERE EXISTS (
            SELECT 1
            FROM ml_agent_routing_items i
            WHERE i.run_id = ml_agent_routing_runs.id
        )
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No materialized ml_agent_routing_items found. Run ml-agent-architecture first.")
    return int(row["id"])


def resolve_specialists(value: str) -> list[str]:
    if value in SPECIALIST_GROUPS:
        names = list(SPECIALIST_GROUPS[value])
    elif value in SPECIALISTS:
        names = [value]
    else:
        raise RuntimeError(f"Unknown specialist/group: {value}")
    return [name for name in names if name in SPECIALISTS and name != "titles"]


def action_for(general_action: str | None, specialist_action: str | None) -> str:
    general = general_action or "unknown"
    specialist = specialist_action or "unknown"
    if general != "auto_safe" and specialist == "auto_safe":
        return "specialist_new_safe_review"
    if general == "auto_safe" and specialist != "auto_safe":
        return "specialist_demoted_review"
    if general == "auto_safe" and specialist == "auto_safe":
        return "auto_safe_agree"
    return "needs_human_agree"


def load_candidates(
    conn,
    routing_run_id: int,
    specialists: list[str],
    limit: int,
    include_reviewed: bool,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in specialists)
    reviewed_clause = ""
    if not include_reviewed:
        reviewed_clause = """
          AND NOT EXISTS (
            SELECT 1
            FROM local_learning_candidates c
            WHERE c.segment_id = i.segment_id
              AND c.local_status = 'reviewed_human'
          )
        """
    rows = conn.execute(
        f"""
        SELECT
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.route_agent_key,
            i.route_agent_type,
            i.route_status,
            i.route_confidence,
            i.general_action,
            i.policy_action,
            i.specialist_action,
            i.route_reason,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text
        FROM ml_agent_routing_items i
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.route_agent_key IN ({placeholders})
          AND i.route_status = 'experimental'
          AND (
            (i.specialist_action = 'auto_safe' AND COALESCE(i.general_action, '') <> 'auto_safe')
            OR (COALESCE(i.specialist_action, '') <> 'auto_safe' AND i.general_action = 'auto_safe')
          )
          {reviewed_clause}
        ORDER BY
          CASE
            WHEN i.specialist_action = 'auto_safe' AND COALESCE(i.general_action, '') <> 'auto_safe' THEN 0
            WHEN COALESCE(i.specialist_action, '') <> 'auto_safe' AND i.general_action = 'auto_safe' THEN 1
            ELSE 2
          END,
          i.route_agent_key,
          i.route_confidence DESC,
          i.relative_path,
          i.source_key
        LIMIT ?
        """,
        (routing_run_id, *specialists, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def round_robin_by_agent(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("route_agent_key") or "unknown"), []).append(row)
    selected: list[dict[str, Any]] = []
    while buckets and len(selected) < limit:
        for key in sorted(list(buckets)):
            bucket = buckets.get(key) or []
            if not bucket:
                buckets.pop(key, None)
                continue
            selected.append(bucket.pop(0))
            if len(selected) >= limit:
                break
    return selected


def load_calibration_candidates(
    conn,
    routing_run_id: int,
    specialists: list[str],
    exclude_segments: set[int],
    limit: int,
    include_reviewed: bool,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    placeholders = ",".join("?" for _ in specialists)
    reviewed_clause = ""
    if not include_reviewed:
        reviewed_clause = """
          AND NOT EXISTS (
            SELECT 1
            FROM local_learning_candidates c
            WHERE c.segment_id = i.segment_id
              AND c.local_status = 'reviewed_human'
          )
        """
    excluded_clause = ""
    excluded_params: list[Any] = []
    if exclude_segments:
        excluded_placeholders = ",".join("?" for _ in exclude_segments)
        excluded_clause = f"AND i.segment_id NOT IN ({excluded_placeholders})"
        excluded_params = list(sorted(exclude_segments))
    rows = conn.execute(
        f"""
        SELECT
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.route_agent_key,
            i.route_agent_type,
            i.route_status,
            i.route_confidence,
            i.general_action,
            i.policy_action,
            i.specialist_action,
            i.route_reason,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text
        FROM ml_agent_routing_items i
        JOIN source_segments s ON s.id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.route_agent_key IN ({placeholders})
          AND i.route_status = 'experimental'
          AND NOT (
            (i.specialist_action = 'auto_safe' AND COALESCE(i.general_action, '') <> 'auto_safe')
            OR (COALESCE(i.specialist_action, '') <> 'auto_safe' AND i.general_action = 'auto_safe')
          )
          {reviewed_clause}
          {excluded_clause}
        ORDER BY
          i.route_agent_key,
          CASE
            WHEN i.specialist_action = 'auto_safe' THEN 0
            ELSE 1
          END,
          i.route_confidence DESC,
          i.relative_path,
          i.source_key
        LIMIT ?
        """,
        (routing_run_id, *specialists, *excluded_params, limit * 5),
    ).fetchall()
    return round_robin_by_agent([dict(row) for row in rows], limit)


def candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    auditor_action = action_for(row.get("general_action"), row.get("specialist_action"))
    confidence = float(row.get("route_confidence") or 0.0)
    candidate_text = row.get("output_text") or row.get("old_text") or row.get("spanish_text") or ""
    reasons = [
        f"agent:{row.get('route_agent_key') or 'unknown'}",
        f"route_status:{row.get('route_status') or 'unknown'}",
        f"review_kind:{row.get('review_kind') or 'divergence'}",
        f"policy_action:{row.get('policy_action') or 'unknown'}",
        f"general_action:{row.get('general_action') or 'unknown'}",
        f"specialist_action:{row.get('specialist_action') or 'unknown'}",
        f"specialist_safe_probability:{confidence:.6f}",
    ]
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": int(row["source_line_number"]) if row.get("source_line_number") else None,
        "source_section": "ML agent audit queue",
        "focus_group": row.get("route_agent_key") or auditor_action,
        "auditor_action": auditor_action,
        "agent_key": row.get("route_agent_key"),
        "route_status": row.get("route_status"),
        "general_action": row.get("general_action") or "unknown",
        "specialist_action": row.get("specialist_action") or "unknown",
        "policy_action": row.get("policy_action") or "unknown",
        "general_safe_probability": 0.0,
        "specialist_safe_probability": confidence,
        "final_action": row.get("specialist_action") or auditor_action,
        "risk_class": auditor_action,
        "model_safe_probability": confidence,
        "issue_count": None,
        "token_status": "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": candidate_text,
        "candidate_text": candidate_text,
        "auditor_reasons_json": json.dumps(reasons, ensure_ascii=False),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def build_batches(
    rows: list[dict[str, Any]],
    first_lote: int,
    batch_size: int,
) -> list[dict[str, Any]]:
    batches = []
    for offset in range(0, len(rows), batch_size):
        batch_rows = rows[offset : offset + batch_size]
        if not batch_rows:
            continue
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "ML agent audit queue",
                "focus_group": "agent_audit",
                "queue_source": "ml_specialist_auditor",
                "candidates": [candidate_payload(row) for row in batch_rows],
            }
        )
    return batches


def write_report(
    settings: dict[str, Any],
    routing_run_id: int,
    specialist: str,
    candidates: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    counts = Counter(
        action_for(row.get("general_action"), row.get("specialist_action")) for row in candidates
    )
    agent_counts = Counter(str(row.get("route_agent_key") or "unknown") for row in candidates)
    kind_counts = Counter(str(row.get("review_kind") or "divergence") for row in candidates)
    lines = [
        "ML agent audit queue",
        f"Prepared at: {now()}",
        f"Rule version: {RULE_VERSION}",
        f"Routing run id: {routing_run_id}",
        f"Requested specialist/group: {specialist}",
        f"Candidates: {len(candidates)}",
        f"Decision template: {output_path}",
        "",
        "Auditor actions:",
        *[f"- {key}: {counts[key]}" for key in sorted(counts)],
        "",
        "Agents:",
        *[f"- {key}: {count}" for key, count in agent_counts.most_common()],
        "",
        "Review kinds:",
        *[f"- {key}: {count}" for key, count in kind_counts.most_common()],
        "",
        "Interpretation:",
        "- This queue prioritizes disagreements between the macro model and experimental subagents.",
        "- When no new disagreements exist, it fills with calibration cases from unreviewed agent scope.",
        "- Positive labels teach when the subagent can safely recover coverage.",
        "- Negative labels teach when the macro model was right to remain cautious.",
        "- This queue does not train, score, promote, or write output.",
    ]
    return db.write_report(settings, "ml_agent_audit_queue", lines)


def main(
    specialist: str = "religion_promising_subspecialists",
    routing_run_id: int | None = None,
    limit: int = 60,
    batch_size: int = 20,
    output: str | None = None,
    include_reviewed: bool = False,
) -> None:
    settings = db.load_settings()
    specialists = resolve_specialists(specialist)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        routing_run_id = routing_run_id or latest_routing_run_id(conn)
        first_lote = next_lote_number(conn)
        rows = load_candidates(
            conn,
            routing_run_id,
            specialists,
            limit=limit,
            include_reviewed=include_reviewed,
        )
        for row in rows:
            row["review_kind"] = "divergence"
        if len(rows) < limit:
            exclude_segments = {int(row["segment_id"]) for row in rows}
            calibration_rows = load_calibration_candidates(
                conn,
                routing_run_id,
                specialists,
                exclude_segments=exclude_segments,
                limit=limit - len(rows),
                include_reviewed=include_reviewed,
            )
            for row in calibration_rows:
                row["review_kind"] = "calibration"
            rows.extend(calibration_rows)
    batches = build_batches(rows, first_lote, batch_size)
    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_specialist_auditor",
        "score_run_id": None,
        "source_report": f"agent_audit:routing_run_{routing_run_id}:{specialist}",
        "routing_run_id": routing_run_id,
        "specialist": specialist,
        "resolved_specialists": specialists,
        "batch_size": batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "major_fix",
                "semantic_error",
                "residual_spanish",
                "structure_error",
                "token_mismatch",
                "rejected",
                "rejected_suggestion",
            ],
            "recommended_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "semantic_error",
                "structure_error",
                "token_mismatch",
                "rejected_suggestion",
            ],
            "label_guidance": {
                "specialist_new_safe_review": "Use correct/contextual_exception when the subagent can safely recover coverage; use a negative label when the macro model was right to block.",
                "specialist_demoted_review": "Use correct/contextual_exception if the macro auto_safe is safe; use a negative label if the subagent exposed a false-safe risk.",
                "auto_safe_agree": "Calibration case where macro and subagent agree that the output is safe.",
                "needs_human_agree": "Calibration case where macro and subagent agree that the output still needs review.",
                "contextual_exception": "Use for intentional non-mirror localization choices that are safe in game context.",
            },
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "apply output"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output) if output else reports_dir / f"{timestamp()}_ml_agent_audit_review_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = write_report(settings, routing_run_id, specialist, rows, output_path)
    print("[ml_agent_audit_queue] Starting agent audit queue")
    print(f"[ml_agent_audit_queue] Rule version: {RULE_VERSION}")
    print(f"[ml_agent_audit_queue] Routing run id: {routing_run_id}")
    print(f"[ml_agent_audit_queue] Specialist/group: {specialist}")
    print(f"[ml_agent_audit_queue] Resolved: {', '.join(specialists)}")
    print(f"[ml_agent_audit_queue] Candidates: {len(rows)}")
    print(f"[ml_agent_audit_queue] Batches: {len(batches)}")
    print(f"[ml_agent_audit_queue] Decision template: {output_path}")
    print(f"[ml_agent_audit_queue] Report: {report_path}")
    print("[ml_agent_audit_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare review queue from materialized ML agent routing.")
    parser.add_argument("--specialist", default="religion_promising_subspecialists")
    parser.add_argument("--routing-run-id", type=int)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--output")
    parser.add_argument("--include-reviewed", action="store_true")
    args = parser.parse_args()
    main(
        specialist=args.specialist,
        routing_run_id=args.routing_run_id,
        limit=args.limit,
        batch_size=args.batch_size,
        output=args.output,
        include_reviewed=args.include_reviewed,
    )
