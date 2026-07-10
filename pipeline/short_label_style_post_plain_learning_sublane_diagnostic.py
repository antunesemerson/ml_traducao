from __future__ import annotations

import json
from pathlib import Path

import short_label_style_run406_sublane_diagnostic as diagnostic


SOURCE = "short_label_style_post_plain_learning_sublane_diagnostic_v1"
RECENT_LEARNING_RUN_IDS = (691, 692, 693, 694, 695, 696, 697, 698)
RECENT_HOLD_SEGMENTS = {
    281274,
    9291,
    3934,
    153501,
    22963,
    22974,
    22977,
    23005,
    34132,
    71234,
}


def configure_diagnostic_module() -> None:
    diagnostic.SOURCE = SOURCE
    diagnostic.RECENT_LEARNING_RUN_IDS = RECENT_LEARNING_RUN_IDS
    diagnostic.RECENT_HOLD_SEGMENTS = RECENT_HOLD_SEGMENTS


def write_outputs(summary: dict, rows: list[dict]) -> tuple[Path, Path, Path]:
    base = diagnostic.reports_dir() / f"{diagnostic.stamp()}_short_label_style_post_plain_learning_sublane_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = diagnostic.reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(diagnostic.enrich(row), ensure_ascii=False, sort_keys=True) + "\n")
    summary["source"] = SOURCE
    summary["recent_learning_run_ids"] = list(RECENT_LEARNING_RUN_IDS)
    summary["recent_hold_segment_ids"] = sorted(RECENT_HOLD_SEGMENTS)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "short label style post plain learning sublane diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"excluded_segment_count={summary['excluded_segment_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "shape_counts:"])
    for item in summary["shape_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "risk_counts:"])
    for item in summary["risk_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"recommended_decision={summary['recommended_decision']}",
            f"recommended_decision_count={summary['recommended_decision_count']}",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    configure_diagnostic_module()
    preflight_path, preflight_excluded = diagnostic.load_preflight_exclusions()
    with diagnostic.connect_readonly() as conn:
        excluded = preflight_excluded | diagnostic.recent_learning_segments(conn) | RECENT_HOLD_SEGMENTS
        rows = diagnostic.fetch_rows(conn, excluded)
    summary = diagnostic.build_summary(rows, preflight_path, len(excluded))
    txt_path, jsonl_path, summary_path = write_outputs(summary, rows)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_reviewed={summary['rows_reviewed']}")
    print(f"excluded_segment_count={summary['excluded_segment_count']}")
    print(f"recommended_decision={summary['recommended_decision']}")
    print(f"recommended_decision_count={summary['recommended_decision_count']}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
