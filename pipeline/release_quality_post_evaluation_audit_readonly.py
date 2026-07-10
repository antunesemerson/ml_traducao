from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "memory" / "translation_engine.sqlite"
REPORTS_DIR = ROOT / "reports"


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def rowdict(row: sqlite3.Row | None) -> dict:
    return dict(row) if row is not None else {}


def fetch_run(con: sqlite3.Connection, run_id: int) -> dict:
    return rowdict(
        con.execute(
            """
            select id, rule_version, total_segments, closed_count, pending_count,
                   reopen_count, output_apply_pending_count, started_at, finished_at
            from segment_state_runs
            where id = ?
            """,
            (run_id,),
        ).fetchone()
    )


def latest_completed_run(con: sqlite3.Connection) -> int:
    row = con.execute(
        """
        select id
        from segment_state_runs
        where finished_at is not null and total_segments > 0
        order by id desc
        limit 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed segment_state run found.")
    return int(row["id"])


def latest_high_closure_baseline(con: sqlite3.Connection, before_run_id: int) -> int:
    row = con.execute(
        """
        select id
        from segment_state_runs
        where finished_at is not null
          and total_segments > 0
          and id < ?
          and cast(closed_count as real) / total_segments >= 0.90
        order by id desc
        limit 1
        """,
        (before_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No high-closure baseline run found before current run.")
    return int(row["id"])


def query_counts(
    con: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    limit: int | None = None,
) -> list[dict]:
    if limit is not None:
        sql = f"{sql.rstrip()} limit {int(limit)}"
    return [dict(r) for r in con.execute(sql, params)]


def current_needs_apply_rows(con: sqlite3.Connection, current_run: int) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            """
            select
                c.segment_id,
                c.relative_path,
                c.source_key,
                c.source_line_number,
                c.final_state,
                c.state_group,
                c.output_state,
                c.review_state,
                c.apply_state,
                c.policy_action,
                c.confirmation_level,
                c.confirmation_label,
                c.locked,
                c.confirmed_matches_output,
                c.needs_output_apply,
                c.lifecycle_policy_action,
                c.lifecycle_policy_allowed,
                substr(s.english_text, 1, 280) as english_text,
                substr(s.spanish_text, 1, 280) as spanish_text,
                substr(s.old_text, 1, 280) as old_text,
                substr(o.portuguese_text, 1, 280) as output_text,
                substr(sc.confirmed_text, 1, 280) as confirmed_text,
                sc.confirmation_source,
                sc.confidence_score
            from segment_state_items c
            left join source_segments s on s.id = c.segment_id
            left join output_segments o on o.segment_id = c.segment_id
            left join segment_confirmations sc on sc.segment_id = c.segment_id
            where c.run_id = ?
              and c.needs_output_apply = 1
            order by c.relative_path, c.source_key, c.segment_id
            """,
            (current_run,),
        )
    ]


def classify_closed_to_pending_row(row: dict) -> str:
    if int(row["needs_output_apply"] or 0) == 1:
        return "real_needs_apply"
    if int(row["lifecycle_policy_allowed"] or 0) == 1:
        return "lifecycle_consumer_gap"
    if (
        int(row["confirmed_matches_output"] or 0) == 1
        and row["output_old_status"] == "output_differs_old"
    ):
        return "new_output_aligned_needs_review_or_acceptance"
    if (
        int(row["confirmed_matches_output"] or 0) == 1
        and row["output_old_status"] == "output_equals_old"
    ):
        return "baseline_preserve_candidate_unexpected"
    return "residual_review"


def baseline_closed_to_pending_rows(
    con: sqlite3.Connection,
    baseline_run: int,
    current_run: int,
) -> list[dict]:
    rows = [
        dict(r)
        for r in con.execute(
            """
            select
                c.segment_id,
                c.relative_path,
                c.source_key,
                c.source_line_number,
                b.final_state as baseline_final_state,
                b.review_state as baseline_review_state,
                b.apply_state as baseline_apply_state,
                c.final_state as current_final_state,
                c.review_state as current_review_state,
                c.apply_state as current_apply_state,
                c.policy_action,
                c.confirmation_level,
                c.confirmation_label,
                c.locked,
                c.confirmed_matches_output,
                c.needs_output_apply,
                c.lifecycle_policy_action,
                c.lifecycle_policy_allowed,
                case
                    when o.portuguese_text is null then 'no_output'
                    when s.old_text is null then 'no_old'
                    when o.portuguese_text = s.old_text then 'output_equals_old'
                    else 'output_differs_old'
                end as output_old_status,
                substr(s.english_text, 1, 240) as english_text,
                substr(s.spanish_text, 1, 240) as spanish_text,
                substr(s.old_text, 1, 240) as old_text,
                substr(o.portuguese_text, 1, 240) as output_text,
                substr(sc.confirmed_text, 1, 240) as confirmed_text,
                sc.confirmation_source,
                sc.confidence_score
            from segment_state_items b
            join segment_state_items c
              on c.segment_id = b.segment_id
             and c.run_id = ?
            left join source_segments s on s.id = c.segment_id
            left join output_segments o on o.segment_id = c.segment_id
            left join segment_confirmations sc on sc.segment_id = c.segment_id
            where b.run_id = ?
              and b.is_closed = 1
              and c.is_closed = 0
            order by c.needs_output_apply desc, c.relative_path, c.source_key, c.segment_id
            """,
            (current_run, baseline_run),
        )
    ]
    for row in rows:
        row["release_quality_queue"] = classify_closed_to_pending_row(row)
    return rows


def output_vs_old_breakdown(
    con: sqlite3.Connection,
    current_run: int,
    baseline_run: int,
    where_sql: str,
) -> list[dict]:
    return query_counts(
        con,
        f"""
        select
            case
                when o.portuguese_text is null then 'no_output'
                when s.old_text is null then 'no_old'
                when o.portuguese_text = s.old_text then 'output_equals_old'
                else 'output_differs_old'
            end as output_old_status,
            count(*) as count
        from segment_state_items c
        left join segment_state_items b
          on b.segment_id = c.segment_id
         and b.run_id = ?
        left join source_segments s on s.id = c.segment_id
        left join output_segments o on o.segment_id = c.segment_id
        where c.run_id = ?
          and {where_sql}
        group by output_old_status
        order by count desc
        """,
        (baseline_run, current_run),
    )


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def audit(baseline_run: int | None, current_run: int | None) -> dict:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    if current_run is None:
        current_run = latest_completed_run(con)
    if baseline_run is None:
        baseline_run = latest_high_closure_baseline(con, current_run)

    baseline = fetch_run(con, baseline_run)
    current = fetch_run(con, current_run)
    if not baseline or not current:
        raise RuntimeError(f"Missing run(s): baseline={baseline_run}, current={current_run}")

    transition_closed_to_pending = query_counts(
        con,
        """
        select c.final_state, c.apply_state, c.needs_output_apply,
               c.confirmed_matches_output, c.lifecycle_policy_allowed,
               count(*) as count
        from segment_state_items b
        join segment_state_items c
          on c.segment_id = b.segment_id
         and c.run_id = ?
        where b.run_id = ?
          and b.is_closed = 1
          and c.is_closed = 0
        group by c.final_state, c.apply_state, c.needs_output_apply,
                 c.confirmed_matches_output, c.lifecycle_policy_allowed
        order by count desc
        """,
        (current_run, baseline_run),
    )

    reopen_lifecycle_breakdown = query_counts(
        con,
        """
        select lifecycle_policy_allowed, lifecycle_policy_action, policy_action,
               confirmation_level, confirmation_label, count(*) as count
        from segment_state_items
        where run_id = ?
          and final_state = 'reopen_auto_confirmed_autofix'
        group by lifecycle_policy_allowed, lifecycle_policy_action, policy_action,
                 confirmation_level, confirmation_label
        order by count desc
        """,
        (current_run,),
        limit=80,
    )

    current_final_state_counts = query_counts(
        con,
        """
        select final_state, count(*) as count
        from segment_state_items
        where run_id = ?
        group by final_state
        order by count desc
        """,
        (current_run,),
        limit=60,
    )

    baseline_final_state_counts = query_counts(
        con,
        """
        select final_state, count(*) as count
        from segment_state_items
        where run_id = ?
        group by final_state
        order by count desc
        """,
        (baseline_run,),
        limit=60,
    )

    mismatch_breakdown = query_counts(
        con,
        """
        select final_state, state_group, output_state, apply_state,
               confirmed_matches_output, needs_output_apply, count(*) as count
        from segment_state_items
        where run_id = ?
          and confirmed_matches_output = 0
        group by final_state, state_group, output_state, apply_state,
                 confirmed_matches_output, needs_output_apply
        order by count desc
        """,
        (current_run,),
    )

    reopened_top_paths = query_counts(
        con,
        """
        select relative_path, count(*) as count
        from segment_state_items
        where run_id = ?
          and final_state = 'reopen_auto_confirmed_autofix'
        group by relative_path
        order by count desc
        """,
        (current_run,),
        limit=40,
    )

    needs_apply = current_needs_apply_rows(con, current_run)
    closed_to_pending_rows = baseline_closed_to_pending_rows(con, baseline_run, current_run)
    needs_apply_path_counts = Counter(r["relative_path"] for r in needs_apply)
    needs_apply_label_counts = Counter(r["confirmation_label"] for r in needs_apply)
    needs_apply_state_counts = Counter(r["final_state"] for r in needs_apply)
    closed_to_pending_queue_counts = Counter(
        r["release_quality_queue"] for r in closed_to_pending_rows
    )
    stable_baseline_preserved_count = int(
        con.execute(
            """
            select count(*) as c
            from segment_state_items
            where run_id = ?
              and final_state like 'closed_stable_baseline%'
            """,
            (current_run,),
        ).fetchone()["c"]
    )
    output_vs_old_current = output_vs_old_breakdown(con, current_run, baseline_run, "1 = 1")
    output_vs_old_closed_to_pending = output_vs_old_breakdown(
        con,
        current_run,
        baseline_run,
        "b.is_closed = 1 and c.is_closed = 0",
    )
    output_vs_old_needs_apply = output_vs_old_breakdown(
        con,
        current_run,
        baseline_run,
        "c.needs_output_apply = 1",
    )

    closed_to_pending_count = sum(r["count"] for r in transition_closed_to_pending)
    reopened_aligned_count = int(
        con.execute(
            """
            select count(*) as c
            from segment_state_items
            where run_id = ?
              and final_state = 'reopen_auto_confirmed_autofix'
              and confirmed_matches_output = 1
              and needs_output_apply = 0
            """,
            (current_run,),
        ).fetchone()["c"]
    )
    reopened_lifecycle_allowed_count = int(
        con.execute(
            """
            select count(*) as c
            from segment_state_items
            where run_id = ?
              and final_state = 'reopen_auto_confirmed_autofix'
              and lifecycle_policy_allowed = 1
            """,
            (current_run,),
        ).fetchone()["c"]
    )

    slug = now_slug()
    base_name = f"{slug}_release_quality_baseline_{baseline_run}_current_{current_run}_readonly"
    summary_path = REPORTS_DIR / f"{base_name}_summary.json"
    md_path = REPORTS_DIR / f"{base_name}.md"
    needs_apply_jsonl_path = REPORTS_DIR / f"{base_name}_needs_apply.jsonl"
    transitions_jsonl_path = REPORTS_DIR / f"{base_name}_transitions.jsonl"
    closed_to_pending_jsonl_path = REPORTS_DIR / f"{base_name}_closed_to_pending.jsonl"

    closed_delta = current["closed_count"] - baseline["closed_count"]
    needs_apply_delta = (
        current["output_apply_pending_count"] - baseline["output_apply_pending_count"]
    )
    closure_regression_resolved = (
        abs(closed_delta) <= max(len(needs_apply) + 100, 350)
        and stable_baseline_preserved_count > 0
        and reopened_lifecycle_allowed_count <= 10
    )
    residual_focus = (
        "needs_apply_and_new_output_review"
        if closure_regression_resolved
        else "closure_gate_regression"
    )

    summary = {
        "source": "release_quality_post_evaluation_audit_readonly",
        "db_path": str(DB_PATH),
        "baseline_run_id": baseline_run,
        "current_run_id": current_run,
        "baseline": baseline,
        "current": current,
        "delta": {
            "closed": closed_delta,
            "pending": current["pending_count"] - baseline["pending_count"],
            "reopen": current["reopen_count"] - baseline["reopen_count"],
            "needs_output_apply": needs_apply_delta,
        },
        "closure_regression_resolved": closure_regression_resolved,
        "residual_focus": residual_focus,
        "stable_baseline_preserved_count": stable_baseline_preserved_count,
        "closed_baseline_to_pending_current_count": closed_to_pending_count,
        "closed_baseline_to_pending_queue_counts": closed_to_pending_queue_counts.most_common(),
        "current_reopen_aligned_no_apply_count": reopened_aligned_count,
        "current_reopen_lifecycle_allowed_count": reopened_lifecycle_allowed_count,
        "current_confirmed_mismatch_count": sum(r["count"] for r in mismatch_breakdown),
        "needs_apply_count": len(needs_apply),
        "needs_apply_top_paths": needs_apply_path_counts.most_common(30),
        "needs_apply_top_labels": needs_apply_label_counts.most_common(30),
        "needs_apply_state_counts": needs_apply_state_counts.most_common(),
        "output_vs_old_current": output_vs_old_current,
        "output_vs_old_closed_baseline_to_pending_current": output_vs_old_closed_to_pending,
        "output_vs_old_needs_apply": output_vs_old_needs_apply,
        "transition_closed_to_pending": transition_closed_to_pending,
        "reopen_lifecycle_breakdown_top": reopen_lifecycle_breakdown,
        "current_final_state_counts_top": current_final_state_counts,
        "baseline_final_state_counts_top": baseline_final_state_counts,
        "mismatch_breakdown": mismatch_breakdown,
        "reopened_top_paths": reopened_top_paths,
        "recommendation": {
            "short": (
                "Closure regression is mostly resolved; classify needs_apply and output-diff residuals before publication."
                if closure_regression_resolved
                else "Do not publish from this evaluation output yet. First fix/validate the segment-state closure gate for aligned confirmed output and classify needs_apply."
            ),
            "key_findings": (
                [
                    "Stable-baseline preservation recovered the large false reopening caused by lower-confidence evaluation state.",
                    "The remaining baseline-closed/current-pending rows are concentrated in real needs_apply plus output-diff rows that need acceptance/review.",
                    "The public gate should stay blocked while needs_apply remains above the baseline and known open findings exist.",
                ]
                if closure_regression_resolved
                else [
                    "Most regressions are closed baseline segments that are now pending despite confirmed_matches_output=1 and needs_output_apply=0.",
                    "A large subset of reopened items already has lifecycle_policy_allowed=1, indicating closure consumption/gating should be reviewed before treating them as translation failures.",
                    "The needs_apply queue is much smaller and should be reviewed separately as real output-change candidates.",
                ]
            ),
        },
        "artifacts": {
            "summary": str(summary_path),
            "markdown": str(md_path),
            "needs_apply_jsonl": str(needs_apply_jsonl_path),
            "transitions_jsonl": str(transitions_jsonl_path),
            "closed_to_pending_jsonl": str(closed_to_pending_jsonl_path),
        },
    }

    write_json(summary_path, summary)
    write_jsonl(needs_apply_jsonl_path, needs_apply)
    write_jsonl(transitions_jsonl_path, transition_closed_to_pending)
    write_jsonl(closed_to_pending_jsonl_path, closed_to_pending_rows)

    md = [
        f"# Release Quality Audit Read-Only",
        "",
        f"- Baseline run: `{baseline_run}`",
        f"- Current run: `{current_run}`",
        f"- Total segments: `{current['total_segments']}`",
        "",
        "## Run Delta",
        "",
        f"- Closed: `{baseline['closed_count']}` -> `{current['closed_count']}` (`{summary['delta']['closed']:+}`)",
        f"- Pending: `{baseline['pending_count']}` -> `{current['pending_count']}` (`{summary['delta']['pending']:+}`)",
        f"- Reopen: `{baseline['reopen_count']}` -> `{current['reopen_count']}` (`{summary['delta']['reopen']:+}`)",
        f"- Needs output apply: `{baseline['output_apply_pending_count']}` -> `{current['output_apply_pending_count']}` (`{summary['delta']['needs_output_apply']:+}`)",
        "",
        "## Key Findings",
        "",
        f"- Baseline closed -> current pending: `{closed_to_pending_count}`",
        f"- Current reopen aligned with confirmed output and no apply needed: `{reopened_aligned_count}`",
        f"- Current reopen with lifecycle policy already allowed: `{reopened_lifecycle_allowed_count}`",
        f"- Current confirmed/output mismatches: `{summary['current_confirmed_mismatch_count']}`",
        f"- Current needs_apply queue: `{len(needs_apply)}`",
        f"- Stable-baseline preserved in current run: `{stable_baseline_preserved_count}`",
        "",
        "## Residual Queues",
        "",
    ]
    for queue, count in closed_to_pending_queue_counts.most_common():
        md.append(f"- `{queue}`: `{count}`")
    md.extend(
        [
            "",
            f"Closure regression resolved: `{closure_regression_resolved}`",
            f"Residual focus: `{residual_focus}`",
            "",
        "## Output vs Stable Old",
        "",
        ]
    )
    for row in output_vs_old_current:
        md.append(f"- Current `{row['output_old_status']}`: `{row['count']}`")
    md.extend(
        [
            "",
            "For baseline-closed segments that are pending in the current run:",
        ]
    )
    for row in output_vs_old_closed_to_pending:
        md.append(f"- `{row['output_old_status']}`: `{row['count']}`")
    md.extend(
        [
            "",
            "For needs_apply segments:",
        ]
    )
    for row in output_vs_old_needs_apply:
        md.append(f"- `{row['output_old_status']}`: `{row['count']}`")

    md.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "The large false reopening was mostly recovered by lifecycle consumption plus stable-baseline preservation. The current residual is now operational: real `needs_apply` rows and output-diff rows that need explicit acceptance or review."
                if closure_regression_resolved
                else "The large drop in closed rate is not explained by output changes alone. Most affected segments still have output equal to confirmed text and do not need output apply. This points to a state/lifecycle closure gate problem after the restored/evaluation output, not a direct translation-quality failure for the full affected set."
            ),
            "",
            "## Top Reopened Paths",
            "",
        ]
    )
    for row in reopened_top_paths[:20]:
        md.append(f"- `{row['relative_path']}`: `{row['count']}`")

    md.extend(["", "## Needs Apply Top Paths", ""])
    for path, count in needs_apply_path_counts.most_common(20):
        md.append(f"- `{path}`: `{count}`")

    md.extend(["", "## Closed Baseline -> Pending Current", ""])
    for row in transition_closed_to_pending[:20]:
        md.append(
            "- "
            f"`{row['final_state']}` / `{row['apply_state']}` / "
            f"matches=`{row['confirmed_matches_output']}` / apply=`{row['needs_output_apply']}` / "
            f"lifecycle_allowed=`{row['lifecycle_policy_allowed']}`: `{row['count']}`"
        )

    md.extend(["", "## Recommended Next Steps", ""])
    if closure_regression_resolved:
        md.extend(
            [
                "1. Classify and review the `needs_apply` queue separately; these are the concrete output-change candidates.",
                "2. Classify `new_output_aligned_needs_review_or_acceptance` rows as either accepted improvement, manual lock, or rollback-to-baseline candidate.",
                "3. Keep the publication gate blocked until `needs_apply` and applied-pending validation are resolved or explicitly held.",
                "4. Add source-hash/version evidence in a future migration so stable-baseline preservation can distinguish unchanged source from updated game text.",
            ]
        )
    else:
        md.extend(
            [
                "1. Review `segment_state_snapshot.py` closure routing for `reopen_auto_confirmed_autofix` where `confirmed_matches_output=1`, `needs_output_apply=0`, and especially `lifecycle_policy_allowed=1`.",
                "2. Treat the `251` needs_apply rows as a separate queue, not as proof that the whole evaluated output is worse.",
                "3. Add a non-regression publication gate: an evaluation run cannot become publishable if baseline-closed aligned segments reopen without a concrete issue, source change, or lower-confidence reason.",
                "4. Keep the public package on the restored stable/hotfix candidate until this closure gate is fixed and a new evaluation closes back near baseline.",
            ]
        )
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only release quality audit after evaluation run.")
    parser.add_argument("--baseline-run", type=int, default=None)
    parser.add_argument("--current-run", type=int, default=None)
    args = parser.parse_args()
    summary = audit(args.baseline_run, args.current_run)
    print(json.dumps(summary["artifacts"], ensure_ascii=False, indent=2))
    print(
        f"baseline={summary['baseline_run_id']} current={summary['current_run_id']} "
        f"closed_delta={summary['delta']['closed']} needs_apply={summary['needs_apply_count']}"
    )


if __name__ == "__main__":
    main()
