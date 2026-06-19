from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_short_label_compact_ui_semantic_route_diagnostic_v1"
AGENT_KEY = "micro_short_label_style"
QUEUE_STRATEGY = "short_label_compact_ui_semantic_stratified_learning_queue"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_compact_ui_semantic_route_diagnostic_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


def latest_queue_run_id(conn) -> int:
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
        (AGENT_KEY, QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed compact UI semantic queue found.")
    return int(row["id"])


def fetch_queue_run(conn, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_issue_review_queue_runs WHERE id = ?", (queue_run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    return dict(row)


def fetch_rows(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.id AS decision_id,
            decision.queue_run_id,
            decision.ledger_run_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.queue_bucket,
            decision.normalized_decision,
            decision.evidence_label,
            decision.notes,
            item.evidence_text,
            item.confirmed_text,
            item.english_text,
            item.spanish_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.normalized_decision, decision.queue_bucket, decision.segment_id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def note_reason(notes: str | None) -> str:
    parts = [part.strip() for part in str(notes or "").split(";") if part.strip()]
    for part in parts:
        if part.startswith("queue_bucket=") or part.startswith("source_key="):
            continue
        if part == RULE_VERSION:
            continue
        if part.startswith("issue_short_label_compact_ui_semantic_assisted_review"):
            continue
        return part
    return "unknown_reason"


def domain_from_bucket(queue_bucket: str | None) -> str:
    bucket = str(queue_bucket or "domain_unknown")
    return bucket.split("|", 1)[0]


def route_for(row: dict[str, Any]) -> str:
    decision = str(row.get("normalized_decision") or "")
    reason = note_reason(row.get("notes"))
    domain = domain_from_bucket(row.get("queue_bucket"))

    if decision == "false_positive_reopen":
        return "redundant_false_reopen_evidence"
    if decision == "needs_repair":
        if reason in {"quality_issue_codes_present", "spanish_or_bad_literal", "english_or_placeholder_literal", "awkward_ptbr_literal"}:
            return "compact_ui_literal_or_quality_repair"
        return "compact_ui_repair_review"
    if decision == "needs_new_microagent":
        if reason == "relation_or_nickname_short_label_requires_domain_policy":
            return "relation_or_nickname_domain_microagent"
        if reason == "domain_specific_short_label_requires_dedicated_policy":
            if domain == "domain_religion":
                return "religion_policy_label_microagent"
            if domain == "domain_culture":
                return "culture_policy_label_microagent"
            if domain == "domain_titles_names":
                return "title_or_landed_label_microagent"
            return "domain_specific_policy_label_microagent"
        return "new_microagent_review"
    if decision == "needs_domain_context":
        if reason in {"event_option_or_toast_requires_event_context", "quoted_or_dialogue_surface_requires_event_context"}:
            return "event_context_surface_router"
        if reason == "pronoun_surface_requires_context":
            return "pronoun_context_surface_router"
        if domain == "domain_events_longform":
            return "event_context_surface_router"
        if domain == "domain_culture":
            return "culture_context_surface_router"
        if domain == "domain_religion":
            return "religion_context_surface_router"
        if domain == "domain_titles_names":
            return "title_name_context_surface_router"
        return "general_context_surface_router"
    return "manual_route_review"


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    route_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    route_bucket_counts: Counter[str] = Counter()
    enriched: list[dict[str, Any]] = []

    for row in rows:
        reason = note_reason(row.get("notes"))
        route = route_for(row)
        decision = str(row.get("normalized_decision") or "unknown")
        bucket = str(row.get("queue_bucket") or "unknown")
        route_counts[route] += 1
        decision_counts[decision] += 1
        reason_counts[reason] += 1
        bucket_counts[bucket] += 1
        route_bucket_counts[f"{route}|{bucket}"] += 1
        enriched.append({**row, "route": route, "route_reason": reason})

    fieldnames = [
        "route",
        "route_reason",
        "normalized_decision",
        "queue_bucket",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "evidence_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in enriched:
            writer.writerow(row)

    summary = {
        "rule_version": RULE_VERSION,
        "queue_run_id": int(queue_run["id"]),
        "ledger_run_id": int(queue_run["ledger_run_id"]),
        "decision_count": len(rows),
        "route_counts": dict(route_counts.most_common()),
        "decision_counts": dict(decision_counts.most_common()),
        "reason_counts": dict(reason_counts.most_common()),
        "bucket_counts": dict(bucket_counts.most_common()),
        "route_bucket_counts": dict(route_bucket_counts.most_common()),
        "csv_path": str(csv_path),
        "report_path": str(txt_path),
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Short-label compact UI semantic route diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        "",
        "Summary:",
        f"- Reviewed decisions: {len(rows):,}",
        "",
        "Routes:",
    ]
    for route, count in route_counts.most_common():
        lines.append(f"- {route}: {count:,}")
    lines.extend(["", "Decisions:"])
    for decision, count in decision_counts.most_common():
        lines.append(f"- {decision}: {count:,}")
    lines.extend(["", "Reasons:"])
    for reason, count in reason_counts.most_common(20):
        lines.append(f"- {reason}: {count:,}")
    lines.extend(["", "Top route/bucket combinations:"])
    for key, count in route_bucket_counts.most_common(30):
        lines.append(f"- {key}: {count:,}")
    lines.extend(["", "Recommended next focus:"])
    lines.append("- Do not promote a broad compact_ui_semantic lifecycle bridge; the false-reopen slice is small and mostly redundant.")
    lines.append("- Build/extend context routers first: general_context_surface_router, event_context_surface_router, culture_context_surface_router, and pronoun_context_surface_router.")
    lines.append("- Keep literal/quality repairs separate from context routing; they likely need output corrections, not lifecycle closure.")
    lines.append("- Route relation/nickname/title/religion/culture policy labels to narrower microagents instead of the generic short-label neuron.")
    lines.extend(["", "Samples by route:"])
    sampled: set[str] = set()
    for row in enriched:
        route = str(row["route"])
        if route in sampled:
            continue
        sampled.add(route)
        lines.append(
            f"- {route}: segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | "
            f"{short(row.get('confirmed_text') or row.get('evidence_text'), 140)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Diagnostic only. No source/output writes, no confirmations, no lifecycle promotion.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, selected_queue_run_id)
        rows = fetch_rows(conn, selected_queue_run_id)
    txt_path, csv_path, json_path = report_paths(settings, selected_queue_run_id)
    summary = write_reports(txt_path=txt_path, csv_path=csv_path, json_path=json_path, queue_run=queue_run, rows=rows)
    print("[issue_short_label_compact_ui_semantic_route_diagnostic] Report generated")
    print(f"[issue_short_label_compact_ui_semantic_route_diagnostic] Queue run id: {selected_queue_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_route_diagnostic] Decisions: {len(rows):,}")
    for route, count in Counter(summary["route_counts"]).most_common(10):
        print(f"[issue_short_label_compact_ui_semantic_route_diagnostic] {route}: {count:,}")
    print(f"[issue_short_label_compact_ui_semantic_route_diagnostic] Report: {txt_path}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Route compact UI semantic short-label reviewed decisions into next microagent lanes.")
    parser.add_argument("--queue-run-id", type=int)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
