from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "v3_improvement_shadow_queue_v1"
DEFAULT_FAMILIES = (
    "autofix_unknown_microagent",
    "short_label_style_microagent",
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def paths() -> dict[str, Path]:
    root = db.project_path(db.load_settings()["reports_dir"])
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"{stamp()}_v3_improvement_shadow_queue_readonly"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }


def latest_backlog_run(conn: sqlite3.Connection, requested_id: int | None) -> dict[str, Any]:
    if requested_id is None:
        row = conn.execute(
            """
            SELECT *
            FROM v3_improvement_backlog_runs
            WHERE backlog_status = 'open' AND finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM v3_improvement_backlog_runs WHERE id = ?",
            (requested_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("No open V3 improvement backlog run was found.")
    return dict(row)


def collect_family(
    conn: sqlite3.Connection,
    backlog_run_id: int,
    family: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            backlog.segment_id,
            backlog.relative_path,
            backlog.source_key,
            backlog.source_line_number,
            backlog.primary_family,
            backlog.families_json,
            backlog.learning_lane,
            backlog.old_score,
            backlog.candidate_score,
            backlog.review_priority,
            backlog.confirmation_locked,
            backlog.source_delta_status,
            source.english_text,
            source.spanish_text,
            source.old_text,
            output.portuguese_text AS output_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label
        FROM v3_improvement_backlog_items backlog
        JOIN source_segments source ON source.id = backlog.segment_id
        JOIN output_segments output ON output.segment_id = backlog.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = backlog.segment_id
        WHERE backlog.run_id = ?
          AND backlog.backlog_status = 'open'
          AND backlog.primary_family = ?
          AND backlog.confirmation_locked = 0
        ORDER BY
          COALESCE(backlog.candidate_score, -1) DESC,
          backlog.review_priority DESC,
          backlog.segment_id ASC
        LIMIT ?
        """,
        (backlog_run_id, family, limit),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for rank, raw in enumerate(rows, start=1):
        row = dict(raw)
        score = row.get("candidate_score")
        route = (
            "calibration_false_reopen_review"
            if score is not None and float(score) >= 0.75
            else "specialist_rule_discovery"
        )
        row.update(
            {
                "queue_rank_within_family": rank,
                "shadow_route": route,
                "shadow_only": True,
                "candidate_generation_allowed": False,
                "apply_allowed": False,
                "human_decision": None,
                "suggested_text": None,
            }
        )
        records.append(row)
    return records


def write_reports(
    output_paths: dict[str, Path],
    backlog: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    family_counts = Counter(row["primary_family"] for row in records)
    route_counts = Counter(row["shadow_route"] for row in records)
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "backlog_run_id": int(backlog["id"]),
        "source_segment_state_run_id": int(backlog["source_segment_state_run_id"]),
        "record_count": len(records),
        "family_counts": dict(family_counts),
        "route_counts": dict(route_counts),
        "candidate_generation": 0,
        "apply": 0,
        "source_changed": False,
        "output_changed": False,
    }
    output_paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with output_paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# V3 improvement shadow queue",
        "",
        f"- Backlog run: `{backlog['id']}`",
        f"- Source segment-state: `{backlog['source_segment_state_run_id']}`",
        f"- Records: `{len(records)}`",
        "- Candidate generation: `0`",
        "- Apply: `0`",
        "",
        "This queue looks for model/action contradictions and reusable specialist rules. ",
        "Operational package closure is not changed by this review.",
        "",
        "## Split",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in family_counts.items())
    lines.extend(["", "## Items", ""])
    for row in records:
        lines.extend(
            [
                f"### {row['segment_id']} - {row['primary_family']}",
                "",
                f"- Path/key: `{row['relative_path']} :: {row['source_key']}`",
                f"- Score: `{row.get('candidate_score')}`",
                f"- Route: `{row['shadow_route']}`",
                f"- English: {row.get('english_text') or ''}",
                f"- Spanish: {row.get('spanish_text') or ''}",
                f"- Stable PT-BR: {row.get('output_text') or ''}",
                "",
            ]
        )
    output_paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Build a read-only V3 improvement shadow queue.")
    parser.add_argument("--backlog-run-id", type=int)
    parser.add_argument("--per-family", type=int, default=20)
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        backlog = latest_backlog_run(conn, args.backlog_run_id)
        records: list[dict[str, Any]] = []
        for family in DEFAULT_FAMILIES:
            records.extend(collect_family(conn, int(backlog["id"]), family, args.per_family))
    output_paths = paths()
    summary = write_reports(output_paths, backlog, records)
    print("[v3-shadow] Read-only queue completed")
    print(f"[v3-shadow] Backlog run: {summary['backlog_run_id']}")
    print(f"[v3-shadow] Records: {summary['record_count']}")
    print(f"[v3-shadow] Markdown: {output_paths['markdown']}")
    print(f"[v3-shadow] JSONL: {output_paths['jsonl']}")
    print(f"[v3-shadow] Summary: {output_paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
