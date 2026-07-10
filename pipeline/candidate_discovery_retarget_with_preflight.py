from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import candidate_discovery_cohort_retarget as legacy
import db


RULE_VERSION = "candidate_discovery_retarget_with_preflight_v1"


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


def latest_run_id(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT MAX(id) AS id FROM {table_name}").fetchone()
    if not row or row["id"] is None:
        raise SystemExit(f"missing latest run in {table_name}")
    return int(row["id"])


def load_preflight(path_arg: str | None) -> tuple[Path, set[int], dict[str, Any]]:
    path = Path(path_arg) if path_arg else latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        raise SystemExit("missing candidate_generation_preflight_guard summary")
    data = read_json(path)
    blocked = {int(segment_id) for segment_id in data.get("blocked_segments", {})}
    superseded = {int(segment_id) for segment_id in data.get("superseded_by_human_correction_segments", {})}
    excluded = blocked | superseded
    return path, excluded, data


def fetch_pending_universe(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    ledger_run_id: int,
    excluded_segment_ids: set[int],
) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
        SELECT
            l.segment_id,
            GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
            GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
            COUNT(*) AS open_issue_count
        FROM ml_issue_ledger_items l
        JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
        WHERE l.run_id = ?
          AND l.status = 'open'
          AND s.state_group = 'pending'
          AND s.is_closed = 0
          AND s.needs_output_apply = 0
          AND s.confirmed_matches_output = 1
        GROUP BY l.segment_id
    )
    SELECT
        f.segment_id,
        f.all_families,
        f.all_agents,
        f.open_issue_count,
        src.old_text,
        out.portuguese_text AS current_output_text
    FROM fam f
    LEFT JOIN source_segments src ON src.id = f.segment_id
    LEFT JOIN output_segments out ON out.segment_id = f.segment_id
    """
    rows: list[dict[str, Any]] = []
    for row in conn.execute(query, (segment_state_run_id, ledger_run_id)):
        segment_id = int(row["segment_id"])
        if segment_id in excluded_segment_ids:
            continue
        families = {part for part in str(row["all_families"] or "").split(",") if part}
        text = str(row["current_output_text"] or "")
        rows.append(
            {
                "segment_id": segment_id,
                "families": families,
                "agents": {part for part in str(row["all_agents"] or "").split(",") if part},
                "open_issue_count": int(row["open_issue_count"] or 0),
                "original_text": str(row["old_text"] or ""),
                "current_output_text": text,
                "dynamic": bool(legacy.DYNAMIC_RE.search(text)),
                "short_label_surface": bool(legacy.SHORT_LABEL_RE.search(text)) and len(text) <= 140,
                "autofix_literal_surface": bool(legacy.AUTOFIX_LITERAL_RE.search(text)),
            }
        )
    return rows


def context_risk_count(universe: list[dict[str, Any]]) -> int:
    regex = re.compile(r"\bmuch[oa]s?\s+m(?:á|Ã¡|a)s\b", re.IGNORECASE)
    return sum(
        1
        for row in universe
        if regex.search(row.get("original_text") or "") or regex.search(row.get("current_output_text") or "")
    )


def summarize_state(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    return {
        "run_id": run_id,
        "total_segments": int(row["total_segments"] or 0),
        "closed_count": int(row["closed_count"] or 0),
        "pending_count": int(row["pending_count"] or 0),
        "output_apply_pending_count": int(row["output_apply_pending_count"] or 0),
        "reopen_count": int(row["reopen_count"] or 0),
    }


def build_summary(
    records: list[dict[str, Any]],
    recommended: dict[str, Any],
    preflight_path: Path,
    preflight: dict[str, Any],
    excluded_cohorts: set[str],
    state: dict[str, Any],
    ledger_run_id: int,
    universe_count: int,
    context_risk_remaining: int,
) -> dict[str, Any]:
    viable = [
        row
        for row in records
        if row["eligible_count"] > 0
        and row["expected_false_safe_risk"] != "high"
        and row["recommended_discovery_type"] != "hold"
        and row["cohort_key"] not in excluded_cohorts
    ]
    recommended_is_excluded = recommended["cohort_key"] in excluded_cohorts
    no_viable_unexcluded = len(viable) == 0
    operational_recommendation_allowed = not recommended_is_excluded and not no_viable_unexcluded
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": state["run_id"],
        "ledger_run_id": ledger_run_id,
        "preflight_summary_path": str(preflight_path),
        "preflight_blocked_segment_count": int(preflight.get("blocked_segment_count") or 0),
        "preflight_superseded_segment_count": int(preflight.get("superseded_segment_count") or 0),
        "pending_total": state["pending_count"],
        "pending_needs_output_apply": state["output_apply_pending_count"],
        "eligible_universe_after_preflight": universe_count,
        "context_risk_remaining_in_universe": context_risk_remaining,
        "cohorts_reviewed": len(records),
        "viable_cohort_count": len(viable),
        "recommended_cohort_key": recommended["cohort_key"],
        "recommended_limit": min(int(recommended["recommended_limit"] or 0), 600),
        "recommended_discovery_type": recommended["recommended_discovery_type"],
        "recommended_false_safe_risk": recommended["expected_false_safe_risk"],
        "recommended_reason": recommended["notes"],
        "excluded_cohorts": sorted(excluded_cohorts),
        "recommended_is_excluded": recommended_is_excluded,
        "no_viable_unexcluded_cohort": no_viable_unexcluded,
        "operational_recommendation_allowed": operational_recommendation_allowed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": (
            "hold_no_discovery_no_audit_packet"
            if not operational_recommendation_allowed
            else "generate_human_audit_packet_for_recommended_cohort_only_if_discovery_returns_candidates"
        ),
    }


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_candidate_discovery_retarget_with_preflight"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    public_records = [{key: value for key, value in record.items() if not key.startswith("_")} for record in records]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in public_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recommended = next(record for record in public_records if record["recommended"])
    lines = [
        "candidate discovery retarget with preflight",
        f"source={RULE_VERSION}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"preflight_summary_path={summary['preflight_summary_path']}",
        f"pending_total={summary['pending_total']}",
        f"pending_needs_output_apply={summary['pending_needs_output_apply']}",
        f"eligible_universe_after_preflight={summary['eligible_universe_after_preflight']}",
        f"context_risk_remaining_in_universe={summary['context_risk_remaining_in_universe']}",
        f"preflight_blocked_segment_count={summary['preflight_blocked_segment_count']}",
        f"preflight_superseded_segment_count={summary['preflight_superseded_segment_count']}",
        "",
        "recommended:",
        f"recommended_cohort_key={summary['recommended_cohort_key']}",
        f"recommended_limit={summary['recommended_limit']}",
        f"recommended_discovery_type={summary['recommended_discovery_type']}",
        f"recommended_false_safe_risk={summary['recommended_false_safe_risk']}",
        f"recommended_reason={summary['recommended_reason']}",
        f"recommended_is_excluded={str(summary['recommended_is_excluded']).lower()}",
        f"no_viable_unexcluded_cohort={str(summary['no_viable_unexcluded_cohort']).lower()}",
        f"operational_recommendation_allowed={str(summary['operational_recommendation_allowed']).lower()}",
        f"recommended_metrics={json.dumps(recommended, ensure_ascii=False, sort_keys=True)}",
        "",
        f"apply_ready_now={summary['apply_ready_now']}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"next_action={summary['next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--preflight-summary")
    parser.add_argument("--exclude-cohort", action="append", default=[])
    args = parser.parse_args()
    preflight_path, excluded_segment_ids, preflight = load_preflight(args.preflight_summary)
    with connect_readonly() as conn:
        state_run_id = args.segment_state_run_id or latest_run_id(conn, "segment_state_runs")
        ledger_run_id = args.ledger_run_id or latest_run_id(conn, "ml_issue_ledger_runs")
        state = summarize_state(conn, state_run_id)
        universe = fetch_pending_universe(conn, state_run_id, ledger_run_id, excluded_segment_ids)
    records = legacy.build_records(universe)
    excluded_cohorts = set(args.exclude_cohort or [])
    recommended = legacy.choose_recommendation(records, excluded_cohorts)
    summary = build_summary(
        records,
        recommended,
        preflight_path,
        preflight,
        excluded_cohorts,
        state,
        ledger_run_id,
        len(universe),
        context_risk_count(universe),
    )
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "segment_state_run_id",
        "ledger_run_id",
        "eligible_universe_after_preflight",
        "recommended_cohort_key",
        "recommended_limit",
        "recommended_discovery_type",
        "recommended_false_safe_risk",
        "recommended_is_excluded",
        "no_viable_unexcluded_cohort",
        "operational_recommendation_allowed",
        "apply_ready_now",
        "production_full_recommended_now",
        "next_action",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
