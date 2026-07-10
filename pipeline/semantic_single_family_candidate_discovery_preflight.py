from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import semantic_single_family_candidate_discovery as legacy


RULE_VERSION = "semantic_single_family_candidate_discovery_preflight_v1"
COHORT_KEY = "semantic_review_router_single_family"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def load_preflight(path_arg: str | None) -> tuple[Path, set[int], dict[str, Any]]:
    path = Path(path_arg) if path_arg else latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        raise SystemExit("missing candidate_generation_preflight_guard summary")
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    return path, blocked | superseded, data


def fetch_universe(
    conn: sqlite3.Connection,
    run_id: int,
    ledger_run_id: int,
    excluded_segment_ids: set[int],
) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
        SELECT
            l.segment_id,
            GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
            GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
            COUNT(*) AS issue_count
        FROM ml_issue_ledger_items l
        JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
        WHERE l.run_id = ?
          AND l.status = 'open'
          AND s.state_group = 'pending'
          AND s.is_closed = 0
          AND s.needs_output_apply = 0
          AND s.confirmed_matches_output = 1
        GROUP BY l.segment_id
        HAVING all_families = 'semantic_review_router'
    )
    SELECT
        f.segment_id,
        f.all_families,
        f.all_agents,
        src.old_text,
        out.portuguese_text AS current_output_text
    FROM fam f
    LEFT JOIN source_segments src ON src.id = f.segment_id
    LEFT JOIN output_segments out ON out.segment_id = f.segment_id
    """
    rows: list[dict[str, Any]] = []
    for row in conn.execute(query, (run_id, ledger_run_id)):
        segment_id = int(row["segment_id"])
        if segment_id in excluded_segment_ids:
            continue
        families = {part for part in str(row["all_families"] or "").split(",") if part}
        rows.append(
            {
                "segment_id": segment_id,
                "open_families": sorted(families),
                "all_families": sorted(families),
                "all_agents": sorted(part for part in str(row["all_agents"] or "").split(",") if part),
                "structural_guard_overlap": bool(families - {"semantic_review_router"}),
                "original_text": str(row["old_text"] or ""),
                "current_output_text": str(row["current_output_text"] or ""),
            }
        )
    return rows


def build_summary(
    records: list[dict[str, Any]],
    total_universe: int,
    run_id: int,
    ledger_run_id: int,
    preflight_path: Path,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    decisions = Counter(row["decision"] for row in records)
    candidate_types = Counter(row["candidate_type"] for row in records if row["candidate_type"])
    candidates = [row for row in records if row["would_change_output"]]
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "cohort_key": COHORT_KEY,
        "preflight_summary_path": str(preflight_path),
        "preflight_blocked_segment_count": int(preflight.get("blocked_segment_count") or 0),
        "preflight_superseded_segment_count": int(preflight.get("superseded_segment_count") or 0),
        "total_universe": total_universe,
        "total_reviewed": len(records),
        "candidate_count": len(candidates),
        "would_change_output_count": sum(1 for row in records if row["would_change_output"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "requires_human_review_count": sum(1 for row in records if row["requires_human_review"]),
        "decision_counts": dict(sorted(decisions.items())),
        "top_candidate_types": dict(candidate_types.most_common(10)),
        "sample_candidate_ids": [int(row["segment_id"]) for row in candidates[:80]],
        "small_audit_recommended": 0 < len(candidates) < 20,
        "focused_audit_recommended": len(candidates) >= 20,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "recommended_next_action": (
            "generate_semantic_single_family_human_audit_packet"
            if candidates
            else "return_to_retarget_with_more_exclusions_or_hold"
        ),
    }
    if summary["false_safe_risk_count"] != 0:
        raise SystemExit("false-safe guard failed")
    if summary["requires_lifecycle_later_count"] != 0:
        raise SystemExit("lifecycle guard failed")
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_single_family_candidate_discovery_preflight"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic single-family candidate discovery with preflight",
        f"source={RULE_VERSION}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"preflight_summary_path={summary['preflight_summary_path']}",
        f"total_universe={summary['total_universe']}",
        f"total_reviewed={summary['total_reviewed']}",
        f"candidate_count={summary['candidate_count']}",
        f"would_change_output_count={summary['would_change_output_count']}",
        f"false_safe_risk_count={summary['false_safe_risk_count']}",
        f"requires_apply_later_count={summary['requires_apply_later_count']}",
        f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
        f"requires_human_review_count={summary['requires_human_review_count']}",
        f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}",
        f"top_candidate_types={json.dumps(summary['top_candidate_types'], ensure_ascii=False, sort_keys=True)}",
        f"sample_candidate_ids={summary['sample_candidate_ids']}",
        "",
        f"apply_ready_now={summary['apply_ready_now']}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"recommended_next_action={summary['recommended_next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--preflight-summary")
    args = parser.parse_args()
    preflight_path, excluded_segment_ids, preflight = load_preflight(args.preflight_summary)
    with connect_readonly() as conn:
        universe = fetch_universe(conn, args.segment_state_run_id, args.ledger_run_id, excluded_segment_ids)
    universe.sort(key=legacy.row_priority)
    selected = universe[: args.limit]
    records = [legacy.classify(row) for row in selected]
    summary = build_summary(
        records,
        len(universe),
        args.segment_state_run_id,
        args.ledger_run_id,
        preflight_path,
        preflight,
    )
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_universe",
        "total_reviewed",
        "candidate_count",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "requires_human_review_count",
        "apply_ready_now",
        "production_full_recommended_now",
        "recommended_next_action",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
