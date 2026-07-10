from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "memory" / "translation_engine.sqlite"
REPORTS_DIR = ROOT / "reports"


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def latest_completed_run(con: sqlite3.Connection) -> int:
    row = con.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed segment-state run found.")
    return int(row["id"])


def latest_stable_baseline(con: sqlite3.Connection, current_run_id: int) -> int:
    row = con.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
          AND id < ?
          AND CAST(closed_count AS REAL) / total_segments >= 0.90
          AND output_apply_pending_count <= 5
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (current_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No stable baseline run found.")
    return int(row["id"])


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def rowdict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def fetch_run(con: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    return rowdict(
        con.execute(
            """
            SELECT id, total_segments, closed_count, pending_count, reopen_count,
                   output_apply_pending_count, started_at, finished_at
            FROM segment_state_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    )


def has_token_surface(row: dict[str, Any]) -> bool:
    text = " ".join(
        [
            as_text(row.get("old_text")),
            as_text(row.get("output_text")),
            as_text(row.get("confirmed_text")),
        ]
    )
    markers = ("[", "]", "$", "Select_CString", "Concept(", "Get", "Custom(")
    return any(marker in text for marker in markers)


def classify_needs_apply(row: dict[str, Any]) -> str:
    label = as_text(row.get("confirmation_label")).lower()
    source = as_text(row.get("confirmation_source")).lower()
    path = as_text(row.get("relative_path")).lower()
    key = as_text(row.get("source_key")).lower()

    if "in_game_feedback" in label or "feedback" in source or "visual" in source:
        return "manual_feedback_apply_priority"
    if "nickname" in label or path.endswith("nicknames_l_spanish.yml") or key.startswith("nick_"):
        return "nickname_visual_apply_review"
    if "orthographic" in label or "microfix" in label:
        return "orthographic_microfix_apply_review"
    if "pronoun" in label or "gender_longform" in label or "gender" in label:
        return "grammar_gender_context_apply_review"
    if (
        "boundary_token" in label
        or "token_policy" in label
        or "select_cstring" in label
        or has_token_surface(row)
    ):
        return "token_policy_apply_review"
    return "general_confirmed_apply_review"


def classify_output_delta(row: dict[str, Any]) -> str:
    label = as_text(row.get("confirmation_label")).lower()
    level = as_text(row.get("confirmation_level"))
    locked = int(row.get("locked") or 0) == 1
    policy_action = as_text(row.get("policy_action"))

    if level == "human_confirmed" or locked:
        return "accept_human_locked_output_delta"
    if "feedback" in label or "visual" in label:
        return "accept_visual_feedback_output_delta"
    if policy_action == "needs_autofix":
        return "review_aligned_needs_autofix_label"
    return "accept_aligned_output_delta"


def needs_apply_rows(con: sqlite3.Connection, current_run_id: int) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT
                c.segment_id,
                c.relative_path,
                c.source_key,
                c.source_line_number,
                c.final_state,
                c.state_group,
                c.apply_state,
                c.policy_action,
                c.confirmation_level,
                c.confirmation_label,
                c.locked,
                c.confirmed_matches_output,
                c.needs_output_apply,
                c.lifecycle_policy_action,
                c.lifecycle_policy_allowed,
                substr(s.english_text, 1, 260) AS english_text,
                substr(s.spanish_text, 1, 260) AS spanish_text,
                substr(s.old_text, 1, 260) AS old_text,
                substr(o.portuguese_text, 1, 260) AS output_text,
                substr(sc.confirmed_text, 1, 260) AS confirmed_text,
                sc.confirmation_source,
                sc.confidence_score
            FROM segment_state_items c
            LEFT JOIN source_segments s ON s.id = c.segment_id
            LEFT JOIN output_segments o ON o.segment_id = c.segment_id
            LEFT JOIN segment_confirmations sc ON sc.segment_id = c.segment_id
            WHERE c.run_id = ?
              AND c.needs_output_apply = 1
            ORDER BY c.relative_path, c.source_key, c.segment_id
            """,
            (current_run_id,),
        )
    ]
    for row in rows:
        row["queue"] = classify_needs_apply(row)
        row["token_surface"] = has_token_surface(row)
    return rows


def output_delta_rows(
    con: sqlite3.Connection,
    baseline_run_id: int,
    current_run_id: int,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT
                c.segment_id,
                c.relative_path,
                c.source_key,
                c.source_line_number,
                b.final_state AS baseline_final_state,
                c.final_state AS current_final_state,
                c.state_group,
                c.apply_state,
                c.policy_action,
                c.confirmation_level,
                c.confirmation_label,
                c.locked,
                c.confirmed_matches_output,
                c.needs_output_apply,
                c.lifecycle_policy_action,
                c.lifecycle_policy_allowed,
                substr(s.english_text, 1, 260) AS english_text,
                substr(s.spanish_text, 1, 260) AS spanish_text,
                substr(s.old_text, 1, 260) AS old_text,
                substr(o.portuguese_text, 1, 260) AS output_text,
                substr(sc.confirmed_text, 1, 260) AS confirmed_text,
                sc.confirmation_source,
                sc.confidence_score
            FROM segment_state_items b
            JOIN segment_state_items c
              ON c.segment_id = b.segment_id
             AND c.run_id = ?
            LEFT JOIN source_segments s ON s.id = c.segment_id
            LEFT JOIN output_segments o ON o.segment_id = c.segment_id
            LEFT JOIN segment_confirmations sc ON sc.segment_id = c.segment_id
            WHERE b.run_id = ?
              AND b.is_closed = 1
              AND c.is_closed = 0
              AND c.needs_output_apply = 0
              AND c.confirmed_matches_output = 1
              AND o.portuguese_text IS NOT NULL
              AND s.old_text IS NOT NULL
              AND o.portuguese_text != s.old_text
            ORDER BY c.relative_path, c.source_key, c.segment_id
            """,
            (current_run_id, baseline_run_id),
        )
    ]
    for row in rows:
        row["queue"] = classify_output_delta(row)
        row["token_surface"] = has_token_surface(row)
    return rows


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_markdown(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Post-Evaluation Queue Read-Only",
        "",
        f"- Baseline run: `{summary['baseline_run_id']}`",
        f"- Current run: `{summary['current_run_id']}`",
        f"- Needs apply rows: `{summary['needs_apply_count']}`",
        f"- Output-diff aligned rows: `{summary['output_delta_count']}`",
        "",
        "## Needs Apply Queues",
        "",
    ]
    for queue, count in summary["needs_apply_queue_counts"]:
        lines.append(f"- `{queue}`: `{count}`")
    lines.extend(["", "## Output-Diff Aligned Queues", ""])
    for queue, count in summary["output_delta_queue_counts"]:
        lines.append(f"- `{queue}`: `{count}`")
    lines.extend(
        [
            "",
            "## Recommended Order",
            "",
            "1. Run protected diff/apply only for `manual_feedback_apply_priority` and low-risk `orthographic_microfix_apply_review` after preview.",
            "2. Review `nickname_visual_apply_review` as a visual QA batch, because nicknames were visible regressions in game.",
            "3. Keep `token_policy_apply_review` and `grammar_gender_context_apply_review` behind stricter token/context gates.",
            "4. Convert `accept_human_locked_output_delta` into lifecycle/lock acceptance only after confirming these changes are intentional improvements over stable old.",
            "5. Do not publish a generated package until `needs_apply` is zero or explicitly held with publication rationale.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_report(baseline_run_id: int | None, current_run_id: int | None) -> dict[str, Any]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    if current_run_id is None:
        current_run_id = latest_completed_run(con)
    if baseline_run_id is None:
        baseline_run_id = latest_stable_baseline(con, current_run_id)

    baseline = fetch_run(con, baseline_run_id)
    current = fetch_run(con, current_run_id)
    apply_rows = needs_apply_rows(con, current_run_id)
    delta_rows = output_delta_rows(con, baseline_run_id, current_run_id)

    apply_queue_counts = Counter(row["queue"] for row in apply_rows)
    delta_queue_counts = Counter(row["queue"] for row in delta_rows)
    apply_path_counts = Counter(row["relative_path"] for row in apply_rows)
    apply_label_counts = Counter(row["confirmation_label"] for row in apply_rows)
    delta_path_counts = Counter(row["relative_path"] for row in delta_rows)
    delta_label_counts = Counter(row["confirmation_label"] for row in delta_rows)

    slug = now_slug()
    base = REPORTS_DIR / f"{slug}_release_quality_post_evaluation_queue_readonly"
    summary_path = base.with_name(base.name + "_summary.json")
    markdown_path = base.with_suffix(".md")
    needs_apply_path = base.with_name(base.name + "_needs_apply.jsonl")
    output_delta_path = base.with_name(base.name + "_output_delta_aligned.jsonl")

    summary: dict[str, Any] = {
        "source": "release_quality_post_evaluation_queue_readonly",
        "baseline_run_id": baseline_run_id,
        "current_run_id": current_run_id,
        "baseline": baseline,
        "current": current,
        "needs_apply_count": len(apply_rows),
        "output_delta_count": len(delta_rows),
        "needs_apply_queue_counts": apply_queue_counts.most_common(),
        "output_delta_queue_counts": delta_queue_counts.most_common(),
        "needs_apply_top_paths": apply_path_counts.most_common(25),
        "needs_apply_top_labels": apply_label_counts.most_common(25),
        "output_delta_top_paths": delta_path_counts.most_common(25),
        "output_delta_top_labels": delta_label_counts.most_common(25),
        "publication_blockers": {
            "needs_apply_nonzero": len(apply_rows) > 0,
            "output_delta_needs_acceptance": len(delta_rows) > 0,
        },
        "artifacts": {
            "summary": str(summary_path),
            "markdown": str(markdown_path),
            "needs_apply_jsonl": str(needs_apply_path),
            "output_delta_aligned_jsonl": str(output_delta_path),
        },
    }

    write_json(summary_path, summary)
    write_jsonl(needs_apply_path, apply_rows)
    write_jsonl(output_delta_path, delta_rows)
    write_markdown(markdown_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only queue classifier after evaluation run.")
    parser.add_argument("--baseline-run", type=int, default=None)
    parser.add_argument("--current-run", type=int, default=None)
    args = parser.parse_args()
    summary = build_report(args.baseline_run, args.current_run)
    print(json.dumps(summary["artifacts"], ensure_ascii=False, indent=2))
    print(
        f"baseline={summary['baseline_run_id']} current={summary['current_run_id']} "
        f"needs_apply={summary['needs_apply_count']} output_delta={summary['output_delta_count']}"
    )


if __name__ == "__main__":
    main()
