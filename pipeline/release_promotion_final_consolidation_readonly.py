from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "release_promotion_final_consolidation_readonly_v1"
CORRECTED_IDS = {30553, 30554, 30555, 30556, 30557, 58592, 58593, 229510}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Consolidate the final state of the reviewed promotion cohort.")
    parser.add_argument("--audit-jsonl", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=67)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    args = parse_args()
    audit = read_jsonl(args.audit_jsonl)
    if len(audit) != args.expected_count:
        raise SystemExit(f"audit count mismatch: expected {args.expected_count}, got {len(audit)}")
    ids = sorted(int(row["segment_id"]) for row in audit)
    placeholders = ",".join("?" for _ in ids)
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    run = conn.execute(
        """
        SELECT id FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    if not run:
        raise SystemExit("No completed segment-state run found.")
    run_id = int(run["id"])
    rows = conn.execute(
        f"""
        SELECT state.segment_id, state.final_state, state.needs_output_apply,
               state.confirmed_matches_output, state.lifecycle_policy_allowed,
               state.lifecycle_policy_action, output.portuguese_text AS output_text,
               confirm.confirmed_text, confirm.confirmation_source,
               confirm.confirmation_label, confirm.locked
        FROM segment_state_items state
        JOIN output_segments output ON output.segment_id=state.segment_id
        LEFT JOIN segment_confirmations confirm ON confirm.segment_id=state.segment_id
        WHERE state.run_id=? AND state.segment_id IN ({placeholders})
        """,
        [run_id, *ids],
    ).fetchall()
    conn.close()
    live = {int(row["segment_id"]): dict(row) for row in rows}
    records: list[dict[str, Any]] = []
    for audit_row in audit:
        segment_id = int(audit_row["segment_id"])
        row = live.get(segment_id) or {}
        output = str(row.get("output_text") or "")
        confirmed = str(row.get("confirmed_text") or "")
        final_state = str(row.get("final_state") or "")
        failures: list[str] = []
        if not row:
            failures.append("missing_live_state")
        if not final_state.startswith("closed_"):
            failures.append("not_closed")
        if int(row.get("needs_output_apply") or 0) != 0:
            failures.append("needs_output_apply")
        if canonical_localization_text(output) != canonical_localization_text(confirmed):
            failures.append("output_confirmation_mismatch")
        if structural_tokens(output) != structural_tokens(confirmed):
            failures.append("token_signature_mismatch")
        records.append({
            "schema_version": 1,
            "source": SOURCE,
            "record_type": "release_promotion_final_consolidation_item",
            "segment_id": segment_id,
            "relative_path": audit_row.get("relative_path"),
            "source_key": audit_row.get("source_key"),
            "initial_lane": audit_row.get("lane"),
            "final_state": final_state,
            "needs_output_apply": int(row.get("needs_output_apply") or 0),
            "output_matches_confirmation": canonical_localization_text(output) == canonical_localization_text(confirmed),
            "token_integrity_ok": structural_tokens(output) == structural_tokens(confirmed),
            "confirmation_source": row.get("confirmation_source"),
            "confirmation_label": row.get("confirmation_label"),
            "locked": int(row.get("locked") or 0),
            "output_corrected_in_final_cycle": segment_id in CORRECTED_IDS,
            "status": "closed_validated" if not failures else "blocked",
            "validation_failures": failures,
        })
    statuses = Counter(record["status"] for record in records)
    states = Counter(record["final_state"] for record in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "segment_state_run_id": run_id,
        "record_count": len(records),
        "closed_validated_count": statuses["closed_validated"],
        "blocked_count": statuses["blocked"],
        "corrected_output_count": sum(record["output_corrected_in_final_cycle"] for record in records),
        "lifecycle_only_count": len(records) - sum(record["output_corrected_in_final_cycle"] for record in records),
        "needs_output_apply_count": sum(record["needs_output_apply"] for record in records),
        "output_confirmation_match_count": sum(record["output_matches_confirmation"] for record in records),
        "token_integrity_ok_count": sum(record["token_integrity_ok"] for record in records),
        "final_state_counts": dict(states),
        "blocked_segment_ids": [record["segment_id"] for record in records if record["status"] == "blocked"],
        "ready_for_release_candidate": statuses["blocked"] == 0 and all(record["needs_output_apply"] == 0 for record in records),
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_release_promotion_final_consolidation_readonly"
    jsonl_path = base.with_suffix(".jsonl")
    md_path = base.with_suffix(".md")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_path.write_text("\n".join([
        "# Release Promotion Final Consolidation (read-only)", "",
        f"- segment-state: `{run_id}`", f"- reviewed cohort: `{len(records)}`",
        f"- closed and validated: `{summary['closed_validated_count']}`",
        f"- lifecycle-only consolidation: `{summary['lifecycle_only_count']}`",
        f"- corrected output: `{summary['corrected_output_count']}`",
        f"- needs output apply: `{summary['needs_output_apply_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        f"- release candidate ready: `{str(summary['ready_for_release_candidate']).lower()}`",
        "- source/output changed by this audit: `false`",
    ]) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary, "artifacts": [str(md_path), str(jsonl_path), str(summary_path)]}, ensure_ascii=False, indent=2))
    return 0 if summary["blocked_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
