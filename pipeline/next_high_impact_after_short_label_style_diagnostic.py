from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SEGMENT_STATE_RUN_ID = 382
LEDGER_RUN_ID = 76
BATCH3_JSONL = Path(
    "reports/20260619_171202_072876_short_label_style_current_high_impact_sublane_review_batch3.jsonl"
)
SHORT_LABEL_STATES = {
    "closed_auto_confirmed_short_label_style_current_short_phrase_lifecycle",
    "closed_auto_confirmed_short_label_style_current_compact_ui_label_lifecycle",
    "closed_auto_confirmed_short_label_style_current_plain_noop_lifecycle",
}
LIFECYCLE_DECISIONS = {
    "lifecycle_ready_plain_noop",
    "lifecycle_ready_compact_ui_label",
    "lifecycle_ready_short_phrase",
}


def read_batch3() -> list[dict[str, Any]]:
    path = db.project_path(BATCH3_JSONL)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def classify_route(families: set[str], kinds: set[str], relative_path: str) -> tuple[str, str]:
    other_families = families - {"short_label_style_microagent"}
    family_text = " ".join(sorted(other_families | kinds)).lower()
    path = relative_path.lower()
    if any(family in other_families for family in {"semantic_review_router", "autofix_unknown_microagent"}):
        if any(marker in family_text for marker in ("dynamic", "expression", "select_cstring", "custom")):
            return "dynamic_agent", "semantic/autofix companion has dynamic expression markers"
        return "companion_bridge", "short-label issue paired with semantic/autofix companion"
    if any(marker in family_text for marker in ("dynamic", "expression", "select_cstring", "custom")):
        return "dynamic_agent", "dynamic expression family or issue kind present"
    if any(marker in family_text for marker in ("spanish", "residual", "english")):
        return "residual_repair", "residual language issue present"
    if any(marker in path for marker in ("religion", "culture", "title", "nicknames", "traits", "law", "accolade")):
        return "domain_policy", "domain-sensitive path needs policy/context"
    if len(other_families) == 1:
        return "companion_bridge", "single companion family may be recoverable by governed bridge"
    if other_families:
        return "context_composer", "multiple non-short-label signals need context composition"
    return "blocked_uncertain", "no clear companion route from open issue set"


def rows_to_lines(title: str, rows: list[dict[str, Any]], key_fields: list[str]) -> list[str]:
    lines = [title]
    if not rows:
        return lines + ["- none"]
    for row in rows:
        parts = [f"{field}={row[field]}" for field in key_fields]
        lines.append("- " + " | ".join(parts))
    return lines


def main() -> None:
    settings = db.load_settings()
    conn = db.connect()
    state_run = conn.execute(
        """
        SELECT id, total_segments, closed_count, pending_count, output_apply_pending_count, finished_at
        FROM segment_state_runs
        WHERE id = ?
        """,
        (SEGMENT_STATE_RUN_ID,),
    ).fetchone()
    ledger_run = conn.execute(
        """
        SELECT id, ledger_item_count, finished_at
        FROM ml_issue_ledger_runs
        WHERE id = ?
        """,
        (LEDGER_RUN_ID,),
    ).fetchone()
    if not state_run or not ledger_run or int(ledger_run["ledger_item_count"] or 0) <= 0:
        raise RuntimeError("Required snapshot/ledger not available or ledger is empty")

    pending_family_segments = conn.execute(
        """
        SELECT issue.issue_family, COUNT(DISTINCT issue.segment_id) AS segments
        FROM ml_issue_ledger_items issue
        JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = issue.segment_id
        WHERE issue.run_id = ?
          AND issue.status = 'open'
          AND state.state_group = 'pending'
        GROUP BY issue.issue_family
        ORDER BY segments DESC, issue.issue_family
        LIMIT 20
        """,
        (SEGMENT_STATE_RUN_ID, LEDGER_RUN_ID),
    ).fetchall()
    open_issue_families = conn.execute(
        """
        SELECT issue_family, COUNT(*) AS issues, COUNT(DISTINCT segment_id) AS segments
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
        GROUP BY issue_family
        ORDER BY issues DESC, issue_family
        LIMIT 20
        """,
        (LEDGER_RUN_ID,),
    ).fetchall()
    issue_count_distribution = conn.execute(
        """
        WITH per_segment AS (
            SELECT state.segment_id, COUNT(issue.id) AS issue_count
            FROM segment_state_items state
            JOIN ml_issue_ledger_items issue
              ON issue.run_id = ?
             AND issue.status = 'open'
             AND issue.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.state_group = 'pending'
            GROUP BY state.segment_id
        )
        SELECT
            CASE
                WHEN issue_count = 1 THEN '1 issue'
                WHEN issue_count = 2 THEN '2 issues'
                ELSE '3+ issues'
            END AS bucket,
            COUNT(*) AS segments
        FROM per_segment
        GROUP BY bucket
        ORDER BY MIN(issue_count)
        """,
        (LEDGER_RUN_ID, SEGMENT_STATE_RUN_ID),
    ).fetchall()
    combo_rows = conn.execute(
        """
        WITH families AS (
            SELECT state.segment_id, issue.issue_family
            FROM segment_state_items state
            JOIN ml_issue_ledger_items issue
              ON issue.run_id = ?
             AND issue.status = 'open'
             AND issue.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.state_group = 'pending'
            GROUP BY state.segment_id, issue.issue_family
        ),
        combo AS (
            SELECT segment_id, GROUP_CONCAT(issue_family, ' + ') AS issue_families, COUNT(*) AS family_count
            FROM families
            GROUP BY segment_id
            HAVING family_count >= 2
        )
        SELECT issue_families, COUNT(*) AS segments
        FROM combo
        GROUP BY issue_families
        ORDER BY segments DESC, issue_families
        LIMIT 20
        """,
        (LEDGER_RUN_ID, SEGMENT_STATE_RUN_ID),
    ).fetchall()

    batch3 = read_batch3()
    lifecycle_ids = [
        int(row["segment_id"])
        for row in batch3
        if row.get("lifecycle_candidate") is True and row.get("decision") in LIFECYCLE_DECISIONS
    ]
    placeholders = ",".join("?" for _ in lifecycle_ids)
    state_by_segment = {
        int(row["segment_id"]): row
        for row in conn.execute(
            f"""
            SELECT segment_id, final_state
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            """,
            (SEGMENT_STATE_RUN_ID, *lifecycle_ids),
        ).fetchall()
    }
    issue_rows = conn.execute(
        f"""
        SELECT segment_id, issue_family, issue_kind, issue_severity, relative_path, source_key
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        ORDER BY segment_id, issue_family, issue_kind
        """,
        (LEDGER_RUN_ID, *lifecycle_ids),
    ).fetchall()
    issues_by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in issue_rows:
        issues_by_segment[int(row["segment_id"])].append(dict(row))

    batch_by_id = {int(row["segment_id"]): row for row in batch3}
    blocked_items = []
    for segment_id in lifecycle_ids:
        state = state_by_segment.get(segment_id)
        if state and state["final_state"] in SHORT_LABEL_STATES:
            continue
        issues = issues_by_segment.get(segment_id, [])
        high_out_of_scope = [
            issue
            for issue in issues
            if issue["issue_family"] != "short_label_style_microagent"
            and str(issue["issue_severity"]).lower() in {"high", "error", "critical"}
        ]
        if not high_out_of_scope:
            continue
        families = {str(issue["issue_family"]) for issue in issues}
        kinds = {str(issue["issue_kind"]) for issue in issues}
        batch_row = batch_by_id[segment_id]
        route, notes = classify_route(families, kinds, str(batch_row["relative_path"]))
        blocked_items.append(
            {
                "segment_id": segment_id,
                "key": batch_row["source_key"],
                "relative_path": batch_row["relative_path"],
                "batch3_decision": batch_row["decision"],
                "block_reason": "high_issue_out_of_scope",
                "open_issue_families": sorted(families),
                "open_issue_kinds": sorted(kinds),
                "recommended_route": route,
                "notes": notes,
            }
        )

    route_counts = Counter(item["recommended_route"] for item in blocked_items)
    family_combo_counts = Counter(" + ".join(item["open_issue_families"]) for item in blocked_items)
    other_family_counts = Counter(
        family
        for item in blocked_items
        for family in item["open_issue_families"]
        if family != "short_label_style_microagent"
    )

    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    txt_path = reports_dir / f"{stamp}_next_high_impact_after_short_label_style.txt"
    jsonl_path = reports_dir / f"{stamp}_next_high_impact_after_short_label_style.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in blocked_items) + "\n",
        encoding="utf-8",
    )

    lines = [
        "Next high-impact diagnostic after short_label_style",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"segment_state_run_id: {SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id: {LEDGER_RUN_ID}",
        f"snapshot_closed_count: {state_run['closed_count']}",
        f"snapshot_pending_count: {state_run['pending_count']}",
        f"snapshot_output_apply_pending_count: {state_run['output_apply_pending_count']}",
        "",
    ]
    lines.extend(rows_to_lines("Top pending segments by issue family:", [dict(r) for r in pending_family_segments[:10]], ["issue_family", "segments"]))
    lines.append("")
    lines.extend(rows_to_lines("Top open issues by issue family:", [dict(r) for r in open_issue_families[:10]], ["issue_family", "issues", "segments"]))
    lines.append("")
    lines.extend(rows_to_lines("Open issue count distribution among pending segments:", [dict(r) for r in issue_count_distribution], ["bucket", "segments"]))
    lines.append("")
    lines.extend(rows_to_lines("Top issue-family combinations among pending segments with 2+ families:", [dict(r) for r in combo_rows[:10]], ["issue_families", "segments"]))
    lines.extend(
        [
            "",
            "Batch 3 high_issue_out_of_scope classification:",
            f"- lifecycle_ready candidates checked: {len(lifecycle_ids)}",
            f"- high_issue_out_of_scope blocked items: {len(blocked_items)}",
            "- recommended routes:",
        ]
    )
    for route, count in route_counts.most_common():
        lines.append(f"  - {route}: {count}")
    lines.append("- other issue families in blocked items:")
    for family, count in other_family_counts.most_common():
        lines.append(f"  - {family}: {count}")
    lines.append("- combinations with short_label_style_microagent:")
    for combo, count in family_combo_counts.most_common(10):
        lines.append(f"  - {combo}: {count}")
    lines.extend(
        [
            "",
            "Prioritized recommendation:",
            "1. Build a governed companion-bridge review for batch3 blocked items, focusing on short_label_style_microagent + semantic_review_router / autofix_unknown_microagent pairs; it is read-only lifecycle-friendly and can recover part of the 118 without apply.",
            "2. Run a focused review batch for the global semantic_review_router + autofix_unknown_microagent backlog, because those families dominate pending multi-issue combinations and likely feed companion bridges.",
            "3. Do not prioritize short_label_style_current batch4 yet; batch3 clean yield dropped to 63/181 because 118 candidates are gated by high companion issues, so unlocking companion/domain blockers should pay better than another plain batch.",
            "",
            "Safety confirmation: read-only diagnostic only; no production, no apply, no lifecycle closure, no confirmations, no reindex, no training/model promotion, no source/output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"blocked_items={len(blocked_items)}")
    print(f"route_counts={dict(route_counts)}")
    conn.close()


if __name__ == "__main__":
    main()
