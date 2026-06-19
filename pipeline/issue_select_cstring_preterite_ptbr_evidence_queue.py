from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_dynamic_select_cstring_literal_subtype_audit import SPANISH_PTBR_HINTS


RULE_VERSION = "issue_select_cstring_preterite_ptbr_evidence_queue_v1"
DEFAULT_AUDIT_GLOB = "*_issue_dynamic_select_cstring_literal_subtype_audit.csv"
TARGET_AGENT = "select_cstring_local_player_preterite_verb_rewrite"


def latest_audit_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(DEFAULT_AUDIT_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No dynamic Select_CString literal subtype audit CSV found.")
    return candidates[0]


def output_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_preterite_ptbr_evidence_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
        base.with_name(base.name + "_validation_sample").with_suffix(".jsonl"),
    )


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def verb_key(row: dict[str, Any]) -> str:
    return str(row.get("left_literal") or "").strip().lower()


def route(row: dict[str, Any], verb_count: int) -> tuple[str, str, str]:
    subtype = row.get("literal_subtype") or ""
    left = verb_key(row)
    if left in SPANISH_PTBR_HINTS:
        if verb_count >= 2:
            return "known_repeated_verb", "high", "known Spanish->PT-BR verb hint and repeated evidence"
        return "known_single_verb", "medium", "known Spanish->PT-BR verb hint with single evidence"
    if subtype == "regular_preterite_verb_shift" and verb_count >= 2:
        return "regular_repeated_verb", "medium", "regular Spanish preterite pattern with repeated evidence"
    if subtype == "regular_preterite_verb_shift":
        return "regular_single_verb", "low", "regular Spanish preterite pattern but single evidence"
    return "unexpected_preterite_bucket", "blocked", "row matched target agent but not a known/regular preterite subtype"


def select_rows(rows: list[dict[str, Any]], *, limit: int | None) -> list[dict[str, Any]]:
    target_rows = [
        row
        for row in rows
        if row.get("suggested_microagent") == TARGET_AGENT
        and row.get("maturity") == "microagent_candidate"
        and row.get("apply_allowed") in {"0", 0, None, ""}
    ]
    counts = Counter(verb_key(row) for row in target_rows)
    enriched: list[dict[str, Any]] = []
    for row in target_rows:
        left = verb_key(row)
        review_route, confidence, reason = route(row, counts[left])
        ptbr_hint = SPANISH_PTBR_HINTS.get(left, "")
        payload = dict(row)
        payload.update(
            {
                "target_agent": TARGET_AGENT,
                "verb_evidence_count": counts[left],
                "review_route": review_route,
                "confidence": confidence,
                "route_reason": reason,
                "suggested_ptbr_neutral_literal": ptbr_hint,
                "suggested_ptbr_left_literal": ptbr_hint,
                "suggested_ptbr_right_literal": ptbr_hint,
                "learning_only": 1,
                "apply_allowed": 0,
                "production_release_allowed": 0,
            }
        )
        enriched.append(payload)
    enriched.sort(
        key=lambda row: (
            {"high": 0, "medium": 1, "low": 2, "blocked": 3}.get(row["confidence"], 9),
            -int(row["verb_evidence_count"]),
            row["left_literal"].lower(),
            row["relative_path"],
            row["source_key"],
        )
    )
    if limit is not None and limit > 0:
        return enriched[:limit]
    return enriched


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    template_path: Path,
    sample_path: Path,
    audit_csv: Path,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "condition_family",
        "condition",
        "left_literal",
        "right_literal",
        "literal_subtype",
        "target_agent",
        "verb_evidence_count",
        "review_route",
        "confidence",
        "suggested_ptbr_neutral_literal",
        "suggested_ptbr_left_literal",
        "suggested_ptbr_right_literal",
        "route_reason",
        "apply_allowed",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")
    with template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "target_agent": TARGET_AGENT,
                "decision": "",
                "decision_options": [
                    "approve_ptbr_neutral_literal",
                    "edit_ptbr_neutral_literal",
                    "needs_context",
                    "manual_exception",
                    "false_positive",
                ],
                "left_literal": row.get("left_literal"),
                "right_literal": row.get("right_literal"),
                "suggested_ptbr_neutral_literal": row.get("suggested_ptbr_neutral_literal"),
                "approved_ptbr_neutral_literal": "",
                "notes": row.get("route_reason"),
                "learning_only": 1,
                "apply_allowed": 0,
                "production_release_allowed": 0,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    sample_rows = stratified_sample(rows)
    with sample_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sample_rows:
            payload = {
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "target_agent": TARGET_AGENT,
                "sample_reason": f"{row.get('confidence')}:{row.get('review_route')}",
                "decision": "",
                "decision_options": [
                    "approve_ptbr_neutral_literal",
                    "edit_ptbr_neutral_literal",
                    "needs_context",
                    "manual_exception",
                    "false_positive",
                ],
                "left_literal": row.get("left_literal"),
                "right_literal": row.get("right_literal"),
                "suggested_ptbr_neutral_literal": row.get("suggested_ptbr_neutral_literal"),
                "approved_ptbr_neutral_literal": "",
                "notes": row.get("route_reason"),
                "learning_only": 1,
                "apply_allowed": 0,
                "production_release_allowed": 0,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    route_counts = Counter(row["review_route"] for row in rows)
    confidence_counts = Counter(row["confidence"] for row in rows)
    subtype_counts = Counter(row["literal_subtype"] for row in rows)
    verb_counts = Counter(row["left_literal"].lower() for row in rows)
    path_counts = Counter(row["relative_path"] for row in rows)
    hint_count = sum(1 for row in rows if row.get("suggested_ptbr_neutral_literal"))
    lines = [
        "Select_CString preterite PT-BR evidence queue",
        f"Rule version: {RULE_VERSION}",
        f"Source audit CSV: {audit_csv}",
        "",
        "Summary:",
        f"- Queue rows: {len(rows):,}",
        f"- Validation sample rows: {len(sample_rows):,}",
        f"- Rows with PT-BR neutral hint: {hint_count:,}",
        "- Learning only: 1",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Routes:",
        *[f"- {key}: {value:,}" for key, value in route_counts.most_common()],
        "",
        "Confidence:",
        *[f"- {key}: {value:,}" for key, value in confidence_counts.most_common()],
        "",
        "Subtypes:",
        *[f"- {key}: {value:,}" for key, value in subtype_counts.most_common()],
        "",
        "Top verbs:",
        *[f"- {key}: {value:,}" for key, value in verb_counts.most_common(40)],
        "",
        "Top paths:",
        *[f"- {key}: {value:,}" for key, value in path_counts.most_common(20)],
        "",
        "Samples:",
    ]
    for row in rows[:50]:
        lines.append(
            f"- {row['confidence']} | {row['review_route']} | "
            f"{row['left_literal']!r} -> {row['right_literal']!r} | "
            f"PT-BR hint={row.get('suggested_ptbr_neutral_literal')!r} | "
            f"{row['relative_path']}::{row['source_key']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stratified_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    quotas = {
        "high": 20,
        "medium": 15,
        "low": 10,
        "blocked": 5,
    }
    selected: list[dict[str, Any]] = []
    seen_verbs: set[str] = set()
    for confidence, quota in quotas.items():
        candidates = [row for row in rows if row.get("confidence") == confidence]
        unique_first: list[dict[str, Any]] = []
        repeated: list[dict[str, Any]] = []
        for row in candidates:
            key = str(row.get("left_literal") or "").lower()
            if key not in seen_verbs:
                unique_first.append(row)
                seen_verbs.add(key)
            else:
                repeated.append(row)
        selected.extend((unique_first + repeated)[:quota])
    return selected


def main(*, audit_csv: str | None = None, limit: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    selected_audit_csv = Path(audit_csv) if audit_csv else latest_audit_csv(settings)
    rows = select_rows(load_rows(selected_audit_csv), limit=limit)
    txt_path, csv_path, jsonl_path, template_path, sample_path = output_paths(settings)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        template_path=template_path,
        sample_path=sample_path,
        audit_csv=selected_audit_csv,
        rows=rows,
    )
    print("[issue_select_cstring_preterite_ptbr_evidence_queue] Queue generated")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] Rows: {len(rows):,}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] Report: {txt_path}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] CSV: {csv_path}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] JSONL: {jsonl_path}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] Decisions template: {template_path}")
    print(f"[issue_select_cstring_preterite_ptbr_evidence_queue] Validation sample: {sample_path}")
    return {
        "rows": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(template_path),
        "validation_sample_path": str(sample_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a learning-only PT-BR evidence queue for Select_CString preterite literal repairs.")
    parser.add_argument("--audit-csv", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Optional row limit; 0 means all target rows.")
    args = parser.parse_args()
    main(audit_csv=args.audit_csv, limit=args.limit or None)
