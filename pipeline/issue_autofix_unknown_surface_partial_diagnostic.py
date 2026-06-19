from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_surface_partial_diagnostic_v1"


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_surface_partial_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json"), base.with_suffix(".csv")


def latest_bridge_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_autofix_unknown_surface_bridge_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No autofix_unknown surface bridge run found.")
    return int(row["id"])


def fetch_rows(conn, bridge_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bridge_run = conn.execute(
        """
        SELECT *
        FROM ml_issue_autofix_unknown_surface_bridge_runs
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
            FROM ml_issue_autofix_unknown_surface_bridge_items item
            WHERE item.run_id = ?
              AND item.bridge_status = 'partial_coverage'
        )
        SELECT
            partial.id AS bridge_item_id,
            partial.segment_id,
            partial.relative_path,
            partial.source_key,
            partial.cluster,
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
         AND state.run_id = (
             SELECT id
             FROM segment_state_runs
             WHERE finished_at IS NOT NULL
             ORDER BY id DESC
             LIMIT 1
         )
        LEFT JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = ?
         AND ledger.segment_id = partial.segment_id
         AND ledger.status = 'open'
        ORDER BY partial.segment_id, ledger.issue_family, ledger.issue_kind, ledger.id
        """,
        (bridge_run_id, int(run["source_ledger_run_id"])),
    ).fetchall()
    return run, [dict(row) for row in rows]


def sample(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\r\n", "\\n").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)

    family_counts: Counter[str] = Counter()
    agent_counts: Counter[str] = Counter()
    cluster_counts: Counter[str] = Counter()
    companion_family_counts: Counter[str] = Counter()
    for segment_rows in by_segment.values():
        first = segment_rows[0]
        cluster_counts[str(first.get("cluster") or "unknown")] += 1
        families = {
            str(row.get("issue_family") or "unknown")
            for row in segment_rows
            if row.get("issue_family")
        }
        agents = {
            str(row.get("agent_key") or "unknown")
            for row in segment_rows
            if row.get("agent_key")
        }
        for family in families:
            family_counts[family] += 1
            if family != "autofix_unknown_microagent":
                companion_family_counts[family] += 1
        for agent in agents:
            agent_counts[agent] += 1

    return {
        "partial_segments": len(by_segment),
        "open_issue_rows": len([row for row in rows if row.get("ledger_item_id") is not None]),
        "cluster_counts": dict(cluster_counts.most_common()),
        "open_family_counts": dict(family_counts.most_common()),
        "companion_family_counts": dict(companion_family_counts.most_common()),
        "agent_counts": dict(agent_counts.most_common()),
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
                "cluster": segment_rows[0].get("cluster"),
                "current_final_state": segment_rows[0].get("current_final_state"),
                "open_issues": [
                    {
                        "ledger_item_id": row.get("ledger_item_id"),
                        "issue_family": row.get("issue_family"),
                        "issue_kind": row.get("issue_kind"),
                        "agent_key": row.get("agent_key"),
                        "route_status": row.get("route_status"),
                        "proposed_action": row.get("proposed_action"),
                        "token_impact": row.get("token_impact"),
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
        "cluster",
        "current_final_state",
        "ledger_item_id",
        "issue_family",
        "issue_kind",
        "agent_key",
        "route_status",
        "proposed_action",
        "token_impact",
        "ledger_evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})

    lines = [
        "Autofix unknown surface partial diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {bridge_run['id']}",
        f"Source ledger run id: {bridge_run['source_ledger_run_id']}",
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

    lines.extend(["", "Clusters:"])
    lines.extend(f"- {cluster}: {count:,}" for cluster, count in summary["cluster_counts"].items())
    lines.extend(["", "Segments:"])
    for segment_id, segment_rows in sorted(by_segment.items()):
        first = segment_rows[0]
        issue_bits = [
            f"{row.get('issue_family')}::{row.get('agent_key')}"
            for row in segment_rows
            if row.get("ledger_item_id") is not None
        ]
        lines.append(
            "- "
            f"segment={segment_id} | {first.get('cluster')} | "
            f"{first.get('relative_path')}::{first.get('source_key')} | "
            f"state={first.get('current_final_state')} | "
            f"open={'; '.join(issue_bits)} | "
            f"text={sample(first.get('bridge_evidence_text'))}"
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- The autofix_unknown surface layer covered its own issue on these segments.",
            "- Remaining blockers are companion issues; do not lifecycle-close until those issue families are covered too.",
            "- If the companion family is semantic_review_router, next work should target semantic surface routing rather than more autofix_unknown review.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(bridge_run_id: int | None = None) -> None:
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
    print("[issue_autofix_unknown_surface_partial_diagnostic] Diagnostic generated")
    print(f"[issue_autofix_unknown_surface_partial_diagnostic] Bridge run id: {bridge_run['id']}")
    print(f"[issue_autofix_unknown_surface_partial_diagnostic] Partial segments: {summary['partial_segments']:,}")
    print(f"[issue_autofix_unknown_surface_partial_diagnostic] Companion families: {summary['companion_family_counts']}")
    print(f"[issue_autofix_unknown_surface_partial_diagnostic] Report: {txt_path}")
    print(f"[issue_autofix_unknown_surface_partial_diagnostic] JSON: {json_path}")
    print(f"[issue_autofix_unknown_surface_partial_diagnostic] CSV: {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose partial autofix_unknown surface bridge candidates.")
    parser.add_argument("--bridge-run-id", type=int)
    args = parser.parse_args()
    main(args.bridge_run_id)
