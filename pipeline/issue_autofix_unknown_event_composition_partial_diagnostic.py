from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_event_composition_partial_diagnostic_v1"


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_event_composition_partial_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json"), base.with_suffix(".csv")


def latest_bridge_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_autofix_unknown_event_composition_bridge_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No autofix_unknown event composition bridge run found.")
    return int(row["id"])


def fetch_rows(conn, bridge_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bridge_run = conn.execute(
        """
        SELECT *
        FROM ml_issue_autofix_unknown_event_composition_bridge_runs
        WHERE id = ?
        """,
        (bridge_run_id,),
    ).fetchone()
    if not bridge_run:
        raise RuntimeError(f"Bridge run not found: {bridge_run_id}")
    run = dict(bridge_run)
    rows = conn.execute(
        """
        WITH partial AS (
            SELECT item.*
            FROM ml_issue_autofix_unknown_event_composition_bridge_items item
            WHERE item.run_id = ?
              AND item.bridge_status = 'partial_coverage'
        )
        SELECT
            partial.id AS bridge_item_id,
            partial.segment_id,
            partial.relative_path,
            partial.source_key,
            partial.evidence_text AS bridge_evidence_text,
            partial.segment_open_issue_count,
            partial.covered_issue_count,
            partial.block_reason,
            state.final_state AS current_final_state,
            state.review_state AS current_review_state,
            state.apply_state AS current_apply_state,
            state.is_closed AS current_is_closed,
            ledger.id AS ledger_item_id,
            ledger.issue_family,
            ledger.issue_kind,
            ledger.issue_role,
            ledger.issue_severity,
            ledger.agent_key,
            ledger.route_status,
            ledger.proposed_action,
            ledger.token_impact,
            ledger.token_status,
            ledger.confidence_score,
            ledger.evidence_text AS ledger_evidence_text
        FROM partial
        LEFT JOIN segment_state_items state
          ON state.segment_id = partial.segment_id
         AND state.run_id = partial.segment_state_run_id
        LEFT JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = partial.ledger_run_id
         AND ledger.segment_id = partial.segment_id
         AND ledger.status = 'open'
        ORDER BY partial.segment_id, ledger.issue_family, ledger.issue_kind, ledger.id
        """,
        (bridge_run_id,),
    ).fetchall()
    return run, [dict(row) for row in rows]


def sample(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\r\n", "\\n").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)

    companion_family_counts: Counter[str] = Counter()
    companion_agent_counts: Counter[str] = Counter()
    open_issue_count_distribution: Counter[str] = Counter()
    token_status_counts: Counter[str] = Counter()
    issue_kind_counts: Counter[str] = Counter()
    for segment_rows in by_segment.values():
        first = segment_rows[0]
        open_issue_count_distribution[str(first.get("segment_open_issue_count") or 0)] += 1
        families = {
            str(row.get("issue_family") or "unknown")
            for row in segment_rows
            if row.get("issue_family") and row.get("issue_family") != "autofix_unknown_microagent"
        }
        agents = {
            str(row.get("agent_key") or "unknown")
            for row in segment_rows
            if row.get("agent_key") and row.get("issue_family") != "autofix_unknown_microagent"
        }
        for family in families:
            companion_family_counts[family] += 1
        for agent in agents:
            companion_agent_counts[agent] += 1
        for row in segment_rows:
            if row.get("issue_family") == "autofix_unknown_microagent":
                continue
            if row.get("token_status"):
                token_status_counts[str(row["token_status"])] += 1
            if row.get("issue_kind"):
                issue_kind_counts[str(row["issue_kind"])] += 1

    return {
        "partial_segments": len(by_segment),
        "open_issue_rows": len([row for row in rows if row.get("ledger_item_id") is not None]),
        "companion_family_counts": dict(companion_family_counts.most_common()),
        "companion_agent_counts": dict(companion_agent_counts.most_common()),
        "open_issue_count_distribution": dict(open_issue_count_distribution.most_common()),
        "token_status_counts": dict(token_status_counts.most_common()),
        "issue_kind_counts": dict(issue_kind_counts.most_common(25)),
    }


def write_outputs(
    *,
    txt_path: Path,
    json_path: Path,
    csv_path: Path,
    bridge_run: dict[str, Any],
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)

    payload = {
        "rule_version": RULE_VERSION,
        "bridge_run": bridge_run,
        "summary": summary,
        "segments": [
            {
                "segment_id": segment_id,
                "relative_path": segment_rows[0].get("relative_path"),
                "source_key": segment_rows[0].get("source_key"),
                "current_final_state": segment_rows[0].get("current_final_state"),
                "segment_open_issue_count": segment_rows[0].get("segment_open_issue_count"),
                "open_issues": [
                    {
                        "ledger_item_id": row.get("ledger_item_id"),
                        "issue_family": row.get("issue_family"),
                        "issue_kind": row.get("issue_kind"),
                        "agent_key": row.get("agent_key"),
                        "route_status": row.get("route_status"),
                        "proposed_action": row.get("proposed_action"),
                        "token_impact": row.get("token_impact"),
                        "token_status": row.get("token_status"),
                        "evidence_text": row.get("ledger_evidence_text"),
                    }
                    for row in segment_rows
                    if row.get("ledger_item_id") is not None
                ],
            }
            for segment_id, segment_rows in sorted(by_segment.items())
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    columns = [
        "segment_id",
        "relative_path",
        "source_key",
        "current_final_state",
        "segment_open_issue_count",
        "ledger_item_id",
        "issue_family",
        "issue_kind",
        "agent_key",
        "route_status",
        "proposed_action",
        "token_impact",
        "token_status",
        "ledger_evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})

    lines = [
        "Autofix unknown event composition partial diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {bridge_run['id']}",
        f"Checkpoint run id: {bridge_run['source_checkpoint_run_id']}",
        f"Source ledger run id: {bridge_run['source_ledger_run_id']}",
        f"Source segment-state run id: {bridge_run['source_segment_state_run_id']}",
        "",
        "Summary:",
        f"- partial_segments: {summary['partial_segments']:,}",
        f"- open_issue_rows: {summary['open_issue_rows']:,}",
        f"- bridge_ready_count: {int(bridge_run['ready_count'] or 0):,}",
        f"- bridge_partial_count: {int(bridge_run['partial_count'] or 0):,}",
        f"- bridge_blocked_count: {int(bridge_run['blocked_count'] or 0):,}",
        "",
        "Companion issue families still blocking these segments:",
    ]
    companion = summary["companion_family_counts"]
    if companion:
        lines.extend(f"- {family}: {count:,}" for family, count in companion.items())
    else:
        lines.append("- none")
    lines.extend(["", "Companion agents:"])
    agents = summary["companion_agent_counts"]
    if agents:
        lines.extend(f"- {agent}: {count:,}" for agent, count in agents.items())
    else:
        lines.append("- none")
    lines.extend(["", "Open issue count distribution:"])
    lines.extend(f"- {count}: {total:,}" for count, total in summary["open_issue_count_distribution"].items())
    lines.extend(["", "Segments:"])
    for segment_id, segment_rows in sorted(by_segment.items())[:120]:
        first = segment_rows[0]
        issue_bits = [
            f"{row.get('issue_family')}::{row.get('agent_key')}"
            for row in segment_rows
            if row.get("ledger_item_id") is not None
        ]
        lines.append(
            "- "
            f"segment={segment_id} | open={first.get('segment_open_issue_count')} | "
            f"{first.get('relative_path')}::{first.get('source_key')} | "
            f"issues={'; '.join(issue_bits)} | "
            f"text={sample(first.get('bridge_evidence_text'))}"
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- The event composer learned reusable clean-context evidence, but all sampled rows still need at least one companion microagent before segment closure.",
            "- This is partial coverage, not production closure.",
            "- Next best move is to target the dominant companion family/agent shown above.",
            "",
            "Files:",
            f"- JSON: {json_path}",
            f"- CSV: {csv_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, bridge_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, json_path, csv_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_bridge_run_id = bridge_run_id or latest_bridge_run_id(conn)
        bridge_run, rows = fetch_rows(conn, selected_bridge_run_id)
    summary = build_summary(rows)
    write_outputs(
        txt_path=txt_path,
        json_path=json_path,
        csv_path=csv_path,
        bridge_run=bridge_run,
        rows=rows,
        summary=summary,
    )
    print("[issue_autofix_unknown_event_composition_partial_diagnostic] Diagnostic generated")
    print(f"[issue_autofix_unknown_event_composition_partial_diagnostic] Bridge run id: {bridge_run['id']}")
    print(f"[issue_autofix_unknown_event_composition_partial_diagnostic] Partial segments: {summary['partial_segments']}")
    print(f"[issue_autofix_unknown_event_composition_partial_diagnostic] Companion families: {summary['companion_family_counts']}")
    print(f"[issue_autofix_unknown_event_composition_partial_diagnostic] Report: {txt_path}")
    print(f"[issue_autofix_unknown_event_composition_partial_diagnostic] JSON: {json_path}")
    print(f"[issue_autofix_unknown_event_composition_partial_diagnostic] CSV: {csv_path}")
    return {
        "bridge_run_id": int(bridge_run["id"]),
        "partial_segments": summary["partial_segments"],
        "report_path": str(txt_path),
        "json_path": str(json_path),
        "csv_path": str(csv_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose partial coverage from autofix_unknown event composition bridge.")
    parser.add_argument("--bridge-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(bridge_run_id=args.bridge_run_id)
