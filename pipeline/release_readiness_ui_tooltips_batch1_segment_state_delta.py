from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_batch1_segment_state_delta_v1"
BEFORE_RUN_ID = 544
AFTER_RUN_ID = 545
APPLY_JSONL = Path("reports/20260702_143725_050904_release_readiness_ui_tooltips_batch1_protected_apply_apply.jsonl")
EXPECTED_COUNT = 12


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Segment-state delta for release readiness UI/tooltips batch1.")
    parser.add_argument("--before-run-id", type=int, default=BEFORE_RUN_ID)
    parser.add_argument("--after-run-id", type=int, default=AFTER_RUN_ID)
    parser.add_argument("--apply-jsonl", type=Path, default=APPLY_JSONL)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                ids.append(int(json.loads(line)["segment_id"]))
    return sorted(set(ids))


def run_row(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing run {run_id}")
    return dict(row)


def states(conn, run_id: int, ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
          segment_id, relative_path, source_key, final_state, state_group,
          review_state, apply_state, policy_action, confirmation_level,
          confirmation_label, locked, confirmed_matches_output,
          needs_output_apply, needs_reopen, is_closed,
          lifecycle_policy_action, lifecycle_policy_allowed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        [run_id, *ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def main() -> None:
    args = parse_args()
    ids = read_ids(args.apply_jsonl)
    if len(ids) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count}, got {len(ids)}")
    conn = db.connect(db.load_settings())
    before_run = run_row(conn, args.before_run_id)
    after_run = run_row(conn, args.after_run_id)
    before = states(conn, args.before_run_id, ids)
    after = states(conn, args.after_run_id, ids)
    records: list[dict[str, Any]] = []
    transitions: Counter[str] = Counter()
    after_states: Counter[str] = Counter()
    for segment_id in ids:
        b = before.get(segment_id)
        a = after.get(segment_id)
        if not b or not a:
            records.append({"segment_id": segment_id, "validation_status": "missing_state"})
            continue
        transitions[f"{b['final_state']} -> {a['final_state']}"] += 1
        after_states[str(a["final_state"])] += 1
        records.append(
            {
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": a["relative_path"],
                "source_key": a["source_key"],
                "before_final_state": b["final_state"],
                "after_final_state": a["final_state"],
                "before_state_group": b["state_group"],
                "after_state_group": a["state_group"],
                "before_review_state": b["review_state"],
                "after_review_state": a["review_state"],
                "before_confirmation_label": b["confirmation_label"],
                "after_confirmation_label": a["confirmation_label"],
                "before_locked": int(b["locked"] or 0),
                "after_locked": int(a["locked"] or 0),
                "before_confirmed_matches_output": int(b["confirmed_matches_output"] or 0),
                "after_confirmed_matches_output": int(a["confirmed_matches_output"] or 0),
                "before_needs_output_apply": int(b["needs_output_apply"] or 0),
                "after_needs_output_apply": int(a["needs_output_apply"] or 0),
                "after_lifecycle_policy_allowed": int(a["lifecycle_policy_allowed"] or 0),
                "after_lifecycle_policy_action": a["lifecycle_policy_action"],
                "validation_status": "ok",
            }
        )

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "before_run_id": args.before_run_id,
        "after_run_id": args.after_run_id,
        "focus_record_count": len(records),
        "transition_counts": dict(sorted(transitions.items())),
        "after_state_counts": dict(after_states.most_common()),
        "global_delta": {
            "closed_count": int(after_run["closed_count"] or 0) - int(before_run["closed_count"] or 0),
            "pending_count": int(after_run["pending_count"] or 0) - int(before_run["pending_count"] or 0),
            "reopen_count": int(after_run["reopen_count"] or 0) - int(before_run["reopen_count"] or 0),
            "output_apply_pending_count": int(after_run["output_apply_pending_count"] or 0)
            - int(before_run["output_apply_pending_count"] or 0),
        },
        "remaining_needs_output_apply_count": int(after_run["output_apply_pending_count"] or 0),
        "output_apply_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "single_operational_recommendation": (
            "Batch is synchronized and measured. Remaining open issues still keep these items pending; next step is issue-ledger triage or small human packet continuation, not lifecycle."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_batch1_segment_state_delta"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips batch1 segment-state delta",
                f"before_run_id={args.before_run_id}",
                f"after_run_id={args.after_run_id}",
                f"focus_record_count={summary['focus_record_count']}",
                f"transition_counts={json.dumps(summary['transition_counts'], ensure_ascii=False, sort_keys=True)}",
                f"after_state_counts={json.dumps(summary['after_state_counts'], ensure_ascii=False, sort_keys=True)}",
                f"global_delta={json.dumps(summary['global_delta'], ensure_ascii=False, sort_keys=True)}",
                f"remaining_needs_output_apply_count={summary['remaining_needs_output_apply_count']}",
                "output_apply_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
