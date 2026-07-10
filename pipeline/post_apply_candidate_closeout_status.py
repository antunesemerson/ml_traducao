from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "post_apply_candidate_closeout_status_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def safe_audit_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for row in read_jsonl(path):
            if not row.get("safe_for_future_apply_batch"):
                continue
            if row.get("false_safe_risk"):
                raise SystemExit(f"false-safe row in {path}: {row.get('segment_id')}")
            if row.get("requires_lifecycle_later"):
                raise SystemExit(f"lifecycle row in {path}: {row.get('segment_id')}")
            if not row.get("token_integrity_ok"):
                raise SystemExit(f"token integrity failed in {path}: {row.get('segment_id')}")
            if not row.get("structure_integrity_ok"):
                raise SystemExit(f"structure integrity failed in {path}: {row.get('segment_id')}")
            item = dict(row)
            item["_audit_path"] = str(path)
            rows.append(item)
    return rows


def applied_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for row in read_jsonl(path):
            is_human_correction = bool(row.get("corrected_text") and row.get("snapshot_path"))
            if not row.get("applied") and not is_human_correction:
                continue
            item = dict(row)
            if "candidate_text" not in item and "corrected_text" in item:
                item["candidate_text"] = item["corrected_text"]
                item["_human_corrected"] = True
            else:
                item["_human_corrected"] = is_human_correction
            item["_apply_path"] = str(path)
            rows.append(item)
    return rows


def build_records(audit_rows: list[dict[str, Any]], applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied_by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in applied:
        applied_by_segment[int(row["segment_id"])].append(row)

    audit_by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in audit_rows:
        audit_by_segment[int(row["segment_id"])].append(row)

    records: list[dict[str, Any]] = []
    for segment_id, rows in sorted(audit_by_segment.items()):
        apply_matches = applied_by_segment.get(segment_id, [])
        candidate_texts = sorted({str(row.get("candidate_text") or "") for row in rows})
        applied_texts = sorted({str(row.get("candidate_text") or "") for row in apply_matches})
        status = "pending_apply"
        if apply_matches and set(candidate_texts) <= set(applied_texts):
            status = "applied"
        elif apply_matches and any(row.get("_human_corrected") for row in apply_matches):
            status = "human_corrected"
        elif len(rows) > 1:
            status = "duplicate_audit_pending"
        records.append(
            {
                "segment_id": segment_id,
                "status": status,
                "audit_count": len(rows),
                "apply_count": len(apply_matches),
                "candidate_types": sorted({str(row.get("candidate_type") or "") for row in rows}),
                "audit_paths": sorted({str(row["_audit_path"]) for row in rows}),
                "apply_paths": sorted({str(row["_apply_path"]) for row in apply_matches}),
                "candidate_texts": candidate_texts,
                "applied_texts": applied_texts,
            }
        )
    return records


def build_summary(records: list[dict[str, Any]], audit_rows: list[dict[str, Any]], applied: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(row["status"] for row in records)
    type_counts: Counter[str] = Counter()
    for row in records:
        for candidate_type in row["candidate_types"]:
            type_counts[candidate_type or "unknown"] += 1
    closed_statuses = {"applied", "human_corrected"}
    pending_ids = [int(row["segment_id"]) for row in records if row["status"] not in closed_statuses]
    known_closeout = not pending_ids
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "raw_safe_audit_rows": len(audit_rows),
        "unique_safe_audit_segments": len(records),
        "raw_applied_rows": len(applied),
        "unique_applied_segments": len({int(row["segment_id"]) for row in applied}),
        "status_counts": dict(sorted(status_counts.items())),
        "candidate_type_counts": dict(sorted(type_counts.items())),
        "pending_apply_segment_ids": pending_ids,
        "known_candidates_closed_out": known_closeout,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_reindex_recommended_now": False,
        "recommended_next_action": (
            "retarget_with_exclusions"
            if known_closeout
            else "dry_run_protected_apply_for_pending"
        ),
        "recommended_excluded_cohorts": [
            "semantic_review_router_plus_short_label_no_structural_overlap",
            "semantic_review_router_single_family",
        ]
        if known_closeout
        else [],
        "recommended_next_prompt": (
            "chat_exec_candidate_discovery_cohort_retarget_excluding_closed_cohorts_prompt.md"
            if known_closeout
            else "chat_exec_candidate_apply_plan_preview_prompt.md"
        ),
    }
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_post_apply_candidate_closeout_status"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post apply candidate closeout status",
        f"raw_safe_audit_rows={summary['raw_safe_audit_rows']}",
        f"unique_safe_audit_segments={summary['unique_safe_audit_segments']}",
        f"raw_applied_rows={summary['raw_applied_rows']}",
        f"unique_applied_segments={summary['unique_applied_segments']}",
        f"status_counts={json.dumps(summary['status_counts'], ensure_ascii=False, sort_keys=True)}",
        f"pending_apply_segment_ids={summary['pending_apply_segment_ids']}",
        f"known_candidates_closed_out={str(summary['known_candidates_closed_out']).lower()}",
        f"lifecycle_reindex_recommended_now={str(summary['lifecycle_reindex_recommended_now']).lower()}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        "",
        f"recommended_next_action={summary['recommended_next_action']}",
        f"recommended_excluded_cohorts={summary['recommended_excluded_cohorts']}",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-jsonl", action="append", required=True)
    parser.add_argument("--apply-jsonl", action="append", required=True)
    args = parser.parse_args()
    audit_paths = [db.project_path(path) for path in args.audit_jsonl]
    apply_paths = [db.project_path(path) for path in args.apply_jsonl]
    audit = safe_audit_rows(audit_paths)
    applied = applied_rows(apply_paths)
    records = build_records(audit, applied)
    summary = build_summary(records, audit, applied)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "raw_safe_audit_rows",
        "unique_safe_audit_segments",
        "raw_applied_rows",
        "unique_applied_segments",
        "known_candidates_closed_out",
        "pending_apply_segment_ids",
        "recommended_next_action",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
