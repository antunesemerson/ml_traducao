from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import ml_composite_review_progress
import segment_token_policy_decisions
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
)


RULE_VERSION = "ml_composite_review_ingest_v1"
DEFAULT_REVIEWER = "composite_gate_review"
REVIEWED_GLOB = "*segment_token_overlay_review_queue_active_gate_*_reviewed.jsonl"


def project_relative(path: Path) -> str:
    try:
        return str(path.relative_to(db.PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = db.project_path(str(path))
    return path


def discover_reviewed_files(settings: dict[str, Any]) -> list[Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    if not reports_dir.exists():
        return []
    return sorted(reports_dir.glob(REVIEWED_GLOB), key=lambda path: path.stat().st_mtime)


def already_ingested_paths(conn) -> set[str]:
    rows = conn.execute(
        """
        SELECT decisions_path
        FROM segment_token_policy_decision_runs
        WHERE decisions_path IS NOT NULL
        """
    ).fetchall()
    normalized: set[str] = set()
    for row in rows:
        path = Path(row["decisions_path"])
        if not path.is_absolute():
            path = db.project_path(str(path))
        normalized.add(str(path.resolve()).lower())
    return normalized


def active_policy_items(conn, *, overlay_run_id: int) -> dict[int, dict[str, Any]]:
    raw_rows = fetch_rows(conn, overlay_run_id=overlay_run_id, critical_only=False)
    rows = [enrich(row) for row in raw_rows]
    return {int(row["policy_item_id"]): row for row in rows}


def validate_decision_file(
    path: Path,
    *,
    active_items: dict[int, dict[str, Any]],
    ingested: set[str],
) -> dict[str, Any]:
    resolved = path.resolve()
    result: dict[str, Any] = {
        "path": path,
        "relative_path": project_relative(path),
        "rows_loaded": 0,
        "valid_rows": 0,
        "decision_counts": Counter(),
        "route_counts": Counter(),
        "warnings": [],
        "blockers": [],
        "already_ingested": str(resolved).lower() in ingested,
    }
    if result["already_ingested"]:
        result["warnings"].append("decision_file_already_ingested")
        return result
    if not path.exists():
        result["blockers"].append("file_not_found")
        return result

    try:
        rows = segment_token_policy_decisions.load_decision_rows(path)
    except Exception as exc:  # noqa: BLE001 - report validation blocker, do not ingest.
        result["blockers"].append(f"load_failed:{type(exc).__name__}:{exc}")
        return result

    result["rows_loaded"] = len(rows)
    seen_items: set[int] = set()
    for index, row in enumerate(rows, start=1):
        policy_item_id = segment_token_policy_decisions.extract_policy_item_id(row)
        decision = segment_token_policy_decisions.extract_decision(row)
        if policy_item_id is None:
            result["blockers"].append(f"row_{index}:missing_policy_item_id")
            continue
        if policy_item_id in seen_items:
            result["blockers"].append(f"row_{index}:duplicate_policy_item_id:{policy_item_id}")
            continue
        seen_items.add(policy_item_id)
        if policy_item_id not in active_items:
            result["blockers"].append(f"row_{index}:policy_item_not_in_active_gate:{policy_item_id}")
            continue
        if not decision:
            result["blockers"].append(f"row_{index}:missing_decision:{policy_item_id}")
            continue
        if decision not in segment_token_policy_decisions.VALID_DECISIONS:
            result["blockers"].append(f"row_{index}:invalid_decision:{policy_item_id}:{decision}")
            continue
        if decision in segment_token_policy_decisions.FIX_DECISIONS and not (
            row.get("corrected_text") or row.get("proposed_text")
        ):
            result["warnings"].append(f"row_{index}:fix_decision_without_corrected_text:{policy_item_id}")

        active_row = active_items[policy_item_id]
        result["valid_rows"] += 1
        result["decision_counts"][decision] += 1
        result["route_counts"][active_row["suggested_route"]] += 1

    if result["rows_loaded"] == 0:
        result["blockers"].append("empty_decision_file")
    return result


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    decision_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    for result in results:
        decision_counts.update(result["decision_counts"])
        route_counts.update(result["route_counts"])
    return {
        "files": len(results),
        "rows_loaded": sum(int(result["rows_loaded"]) for result in results),
        "valid_rows": sum(int(result["valid_rows"]) for result in results),
        "blockers": sum(len(result["blockers"]) for result in results),
        "warnings": sum(len(result["warnings"]) for result in results),
        "already_ingested": sum(1 for result in results if result["already_ingested"]),
        "decision_counts": decision_counts,
        "route_counts": route_counts,
    }


def write_report(
    settings: dict[str, Any],
    *,
    apply: bool,
    active_gate: dict[str, Any],
    discovered: list[Path],
    results: list[dict[str, Any]],
    started_at: datetime,
) -> Path:
    aggregate = aggregate_results(results)
    decision_lines = [f"- {key}: {value}" for key, value in aggregate["decision_counts"].most_common()]
    route_lines = [f"- {key}: {value}" for key, value in aggregate["route_counts"].most_common()]
    lines = [
        "ML composite gate review ingest",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Apply decisions to DB: {apply}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        "",
        "Summary:",
        f"- Discovered reviewed files: {len(discovered)}",
        f"- Files validated: {aggregate['files']}",
        f"- Rows loaded: {aggregate['rows_loaded']}",
        f"- Valid rows: {aggregate['valid_rows']}",
        f"- Blockers: {aggregate['blockers']}",
        f"- Warnings: {aggregate['warnings']}",
        f"- Already ingested: {aggregate['already_ingested']}",
        "",
        "Decision counts:",
        *(decision_lines or ["- none"]),
        "",
        "Route counts:",
        *(route_lines or ["- none"]),
        "",
        "Files:",
    ]
    if not results:
        lines.append("- none")
    for result in results:
        lines.extend(
            [
                f"- {result['relative_path']}",
                f"  rows_loaded: {result['rows_loaded']}",
                f"  valid_rows: {result['valid_rows']}",
                f"  already_ingested: {result['already_ingested']}",
                f"  blockers: {json.dumps(result['blockers'], ensure_ascii=False)}",
                f"  warnings: {json.dumps(result['warnings'], ensure_ascii=False)}",
            ]
        )
    return db.write_report(settings, "ml_composite_review_ingest", lines)


def ingest_valid_files(
    *,
    active_gate: dict[str, Any],
    results: list[dict[str, Any]],
    reviewer: str,
) -> list[Path]:
    ingested: list[Path] = []
    for result in results:
        if result["already_ingested"] or result["blockers"] or result["valid_rows"] == 0:
            continue
        path = result["path"]
        source_report = result["relative_path"]
        segment_token_policy_decisions.main(
            decisions_path=str(path),
            policy_run_id=int(active_gate["active_policy_run_id"]),
            source_report=source_report,
            reviewer=reviewer,
        )
        ingested.append(path)
    return ingested


def main(
    *,
    decisions: list[str] | None = None,
    apply: bool = False,
    reviewer: str = DEFAULT_REVIEWER,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        _overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        active_items = active_policy_items(conn, overlay_run_id=int(active_gate["active_overlay_run_id"]))
        ingested = already_ingested_paths(conn)

    discovered = [resolve_path(value) for value in decisions] if decisions else discover_reviewed_files(settings)
    results = [
        validate_decision_file(path, active_items=active_items, ingested=ingested)
        for path in discovered
    ]
    report_path = write_report(
        settings,
        apply=apply,
        active_gate=active_gate,
        discovered=discovered,
        results=results,
        started_at=started_at,
    )
    aggregate = aggregate_results(results)
    ingested_paths: list[Path] = []
    if apply and aggregate["blockers"] == 0:
        ingested_paths = ingest_valid_files(active_gate=active_gate, results=results, reviewer=reviewer)
        if ingested_paths:
            ml_composite_review_progress.main(gate_key=gate_key)
    elif apply and aggregate["blockers"] > 0:
        print("[ml_composite_review_ingest] Apply requested but blockers were found; nothing ingested.")

    print("[ml_composite_review_ingest] Validation complete")
    print(f"[ml_composite_review_ingest] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_review_ingest] Apply decisions to DB: {apply}")
    print(f"[ml_composite_review_ingest] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_review_ingest] Policy run id: {active_gate['active_policy_run_id']}")
    print(f"[ml_composite_review_ingest] Reviewed files found: {len(discovered)}")
    print(f"[ml_composite_review_ingest] Rows loaded: {aggregate['rows_loaded']}")
    print(f"[ml_composite_review_ingest] Valid rows: {aggregate['valid_rows']}")
    print(f"[ml_composite_review_ingest] Blockers: {aggregate['blockers']}")
    print(f"[ml_composite_review_ingest] Warnings: {aggregate['warnings']}")
    print(f"[ml_composite_review_ingest] Already ingested: {aggregate['already_ingested']}")
    print(f"[ml_composite_review_ingest] Ingested files: {len(ingested_paths)}")
    print(f"[ml_composite_review_ingest] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate and optionally ingest active composite gate reviewed JSONL files.")
    parser.add_argument("--decisions", action="append", default=None, help="Reviewed JSONL path. Can be used more than once.")
    parser.add_argument("--apply", action="store_true", help="Write validated decisions to the SQLite DB.")
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    args = parser.parse_args()
    main(decisions=args.decisions, apply=args.apply, reviewer=args.reviewer, gate_key=args.gate_key)
