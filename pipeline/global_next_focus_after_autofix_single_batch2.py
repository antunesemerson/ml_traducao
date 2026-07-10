from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import db


FOCUS_FAMILIES = (
    "dynamic_ck3_expression_microagent",
    "semantic_review_router",
    "short_label_style_microagent",
    "autofix_unknown_microagent",
)

AVOID_EXACT = {
    frozenset({"dynamic_ck3_expression_microagent", "gender_token_microagent"}),
    frozenset({"dynamic_ck3_expression_microagent", "gender_token_microagent", "local_player_context_microagent"}),
    frozenset({"autofix_unknown_microagent", "semantic_review_router"}),
    frozenset({"semantic_review_router", "short_label_style_microagent"}),
}


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_segment_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, total_segments, closed_count, pending_count, output_apply_pending_count, finished_at
        FROM segment_state_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"segment_state_run_id not found: {run_id}")
    result = dict(row)
    if not result["finished_at"] or int(result["total_segments"] or 0) == 0:
        raise SystemExit(f"segment_state_run_id is not finalized: {run_id}")
    return result


def fetch_ledger_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, segment_state_run_id, ledger_segment_count, ledger_item_count, finished_at
        FROM ml_issue_ledger_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"ledger_run_id not found: {run_id}")
    result = dict(row)
    if not result["finished_at"] or int(result["ledger_item_count"] or 0) == 0:
        raise SystemExit(f"ledger_run_id is not finalized: {run_id}")
    return result


def fetch_pending_open_issues(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            li.segment_id,
            li.issue_family,
            li.issue_kind,
            li.issue_severity,
            li.agent_key,
            li.relative_path,
            li.source_key,
            li.source_line_number,
            s.final_state,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.needs_reopen,
            s.confirmed_matches_output,
            s.priority_score
        FROM ml_issue_ledger_items li
        JOIN segment_state_items s
          ON s.segment_id = li.segment_id
         AND s.run_id = ?
        WHERE li.run_id = ?
          AND li.status = 'open'
          AND s.state_group = 'pending'
          AND COALESCE(s.is_closed, 0) = 0
        ORDER BY s.priority_score DESC, li.segment_id, li.issue_family
        """,
        (segment_state_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def recommended_prompt_type(families: tuple[str, ...]) -> str:
    family_set = set(families)
    if len(families) == 1:
        if "spanish_residual_microagent" in family_set:
            return "residual_review"
        if "autofix_unknown_microagent" in family_set:
            return "single_family_sublane_review"
        return "single_family_review"
    if "dynamic_ck3_expression_microagent" in family_set:
        return "narrow_dynamic_policy_review"
    if "semantic_review_router" in family_set:
        return "context_or_semantic_split_review"
    return "review_batch"


def avoid_reason(families: tuple[str, ...]) -> str:
    family_set = frozenset(families)
    if family_set in AVOID_EXACT:
        return "recently_saturated_or_fragmented_exact_focus"
    if {"dynamic_ck3_expression_microagent", "gender_token_microagent"} <= family_set:
        return "avoid_repeating_dynamic_gender_local_player_tree"
    if {"autofix_unknown_microagent", "semantic_review_router"} <= family_set:
        return "avoid_broad_autofix_unknown_semantic_without_new_gain"
    if {"semantic_review_router", "short_label_style_microagent"} <= family_set:
        return "avoid_broad_semantic_short_label_repeat"
    return ""


def build_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)

    issue_distribution: Counter[str] = Counter()
    family_segments: dict[str, set[int]] = defaultdict(set)
    family_issue_count: Counter[str] = Counter()
    combo_segments: Counter[tuple[str, ...]] = Counter()
    single_issue_family: Counter[str] = Counter()
    focus_pairs: dict[str, Counter[tuple[str, str]]] = {family: Counter() for family in FOCUS_FAMILIES}

    for segment_id, segment_rows in by_segment.items():
        issue_count = len(segment_rows)
        if issue_count == 1:
            issue_distribution["1_issue"] += 1
        elif issue_count == 2:
            issue_distribution["2_issues"] += 1
        else:
            issue_distribution["3_plus_issues"] += 1

        families = tuple(sorted({row["issue_family"] for row in segment_rows}))
        combo_segments[families] += 1
        if issue_count == 1:
            single_issue_family[families[0]] += 1

        for row in segment_rows:
            family = row["issue_family"]
            family_segments[family].add(segment_id)
            family_issue_count[family] += 1

        for left, right in combinations(families, 2):
            for focus_family in FOCUS_FAMILIES:
                if focus_family in (left, right):
                    focus_pairs[focus_family][(left, right)] += 1

    family_rank = Counter({family: len(segment_ids) for family, segment_ids in family_segments.items()})
    return {
        "pending_segments_crossed": len(by_segment),
        "open_issues_crossed": len(rows),
        "issue_distribution": issue_distribution,
        "family_rank": family_rank,
        "family_issue_count": family_issue_count,
        "combo_rank": combo_segments,
        "single_issue_family_rank": single_issue_family,
        "focus_pairs": focus_pairs,
    }


def score_combo(item: tuple[tuple[str, ...], int]) -> tuple[int, int, int]:
    families, count = item
    avoid = avoid_reason(families)
    step = recommended_prompt_type(families)
    step_score = {
        "narrow_dynamic_policy_review": 5,
        "single_family_sublane_review": 4,
        "context_or_semantic_split_review": 3,
        "review_batch": 3,
        "single_family_review": 2,
        "residual_review": 2,
    }.get(step, 1)
    avoid_score = -100 if avoid else 0
    return (avoid_score, step_score, count)


def choose_recommendation(diagnostic: dict[str, Any]) -> dict[str, Any]:
    combos = diagnostic["combo_rank"]
    ranked = sorted(combos.items(), key=score_combo, reverse=True)
    viable = [(families, count) for families, count in ranked if not avoid_reason(families)]
    best_families, best_count = viable[0] if viable else ranked[0]
    alternative = next(
        ((families, count) for families, count in viable if families != best_families),
        (best_families, best_count),
    )
    avoid_now = [
        "requirement_scope_getter batch2 deepening",
        "dynamic + gender + local_player repeat",
        "broad autofix_unknown + semantic_review repeat",
        "broad semantic + short_label repeat",
    ]
    return {
        "best_next_prompt": recommended_prompt_type(best_families),
        "best_focus": list(best_families),
        "best_pending_segments": best_count,
        "alternative_prompt": recommended_prompt_type(alternative[0]),
        "alternative_focus": list(alternative[0]),
        "alternative_pending_segments": alternative[1],
        "avoid_now": avoid_now,
        "numeric_justification": (
            f"best eligible exact combo has {best_count} pending segments after excluding recent saturated focuses"
        ),
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_next_focus_after_autofix_single_batch2"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def write_jsonl(path: Path, diagnostic: dict[str, Any], recommendation: dict[str, Any]) -> None:
    records: list[dict[str, Any]] = []
    for rank, (family, count) in enumerate(diagnostic["family_rank"].most_common(50), start=1):
        records.append(
            {
                "kind": "family_rank",
                "rank": rank,
                "family": family,
                "pending_segments": count,
                "open_issues": diagnostic["family_issue_count"][family],
            }
        )
    for rank, (families, count) in enumerate(diagnostic["combo_rank"].most_common(75), start=1):
        records.append(
            {
                "kind": "combo_rank",
                "rank": rank,
                "families": list(families),
                "pending_segments": count,
                "recommended_prompt_type": recommended_prompt_type(families),
                "avoid_now": bool(avoid_reason(families)),
                "avoid_reason": avoid_reason(families),
            }
        )
    for rank, (family, count) in enumerate(diagnostic["single_issue_family_rank"].most_common(50), start=1):
        records.append(
            {
                "kind": "single_issue_family_rank",
                "rank": rank,
                "family": family,
                "pending_segments": count,
            }
        )
    for focus_family, pairs in diagnostic["focus_pairs"].items():
        for rank, (pair, count) in enumerate(pairs.most_common(25), start=1):
            records.append(
                {
                    "kind": "focus_pair_rank",
                    "focus_family": focus_family,
                    "rank": rank,
                    "families": list(pair),
                    "pending_segments": count,
                }
            )
    records.append({"kind": "recommendation", **recommendation})
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def append_rank_lines(lines: list[str], title: str, rows: list[tuple[Any, int]], limit: int) -> None:
    lines.extend(["", title])
    if not rows:
        lines.append("- none: 0")
        return
    for key, count in rows[:limit]:
        label = " + ".join(key) if isinstance(key, tuple) else str(key)
        lines.append(f"- {label}: {count}")


def write_txt(
    path: Path,
    segment_run: dict[str, Any],
    ledger_run: dict[str, Any],
    diagnostic: dict[str, Any],
    recommendation: dict[str, Any],
) -> None:
    lines = [
        "Global next focus after autofix single-family batch2",
        "",
        f"segment_state_run_id: {segment_run['id']}",
        f"ledger_run_id: {ledger_run['id']}",
        f"segment_run_finished_at: {segment_run['finished_at']}",
        f"ledger_run_finished_at: {ledger_run['finished_at']}",
        f"closed_count: {int(segment_run['closed_count'])}",
        f"pending_count: {int(segment_run['pending_count'])}",
        f"output_apply_pending_count: {int(segment_run['output_apply_pending_count'])}",
        f"pending_segments_crossed: {diagnostic['pending_segments_crossed']}",
        f"open_issues_in_pending: {diagnostic['open_issues_crossed']}",
        "",
        "Issue count distribution:",
    ]
    for key in ("1_issue", "2_issues", "3_plus_issues"):
        lines.append(f"- {key}: {diagnostic['issue_distribution'].get(key, 0)}")

    append_rank_lines(lines, "Top 10 families by pending segments:", diagnostic["family_rank"].most_common(10), 10)
    append_rank_lines(lines, "Top 15 exact family combinations:", diagnostic["combo_rank"].most_common(15), 15)
    append_rank_lines(lines, "Top 10 single-issue families:", diagnostic["single_issue_family_rank"].most_common(10), 10)
    for family in FOCUS_FAMILIES:
        append_rank_lines(
            lines,
            f"Top 10 pairs involving {family}:",
            diagnostic["focus_pairs"][family].most_common(10),
            10,
        )

    lines.extend(
        [
            "",
            "Prioritized recommendation:",
            f"- best_next_prompt: {recommendation['best_next_prompt']}",
            f"- best_focus: {' + '.join(recommendation['best_focus'])}",
            f"- best_pending_segments: {recommendation['best_pending_segments']}",
            f"- alternative: {recommendation['alternative_prompt']} -> {' + '.join(recommendation['alternative_focus'])}",
            f"- alternative_pending_segments: {recommendation['alternative_pending_segments']}",
            "- avoid_now:",
        ]
    )
    lines.extend(f"  - {item}" for item in recommendation["avoid_now"])
    lines.extend(
        [
            f"- numeric_justification: {recommendation['numeric_justification']}",
            "",
            "Safety:",
            "- read-only diagnostic script",
            "- no lifecycle, apply, segment-state, issue-ledger, confirmations, production, reindex, training, source edits, or output edits",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with connect_readonly() as conn:
        segment_run = fetch_segment_run(conn, args.segment_state_run_id)
        ledger_run = fetch_ledger_run(conn, args.ledger_run_id)
        rows = fetch_pending_open_issues(conn, args.segment_state_run_id, args.ledger_run_id)

    diagnostic = build_diagnostic(rows)
    recommendation = choose_recommendation(diagnostic)
    txt_path, jsonl_path = output_paths()
    write_txt(txt_path, segment_run, ledger_run, diagnostic, recommendation)
    write_jsonl(jsonl_path, diagnostic, recommendation)

    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"ledger_run_id={args.ledger_run_id}")
    print(f"pending_segments_crossed={diagnostic['pending_segments_crossed']}")
    print(f"open_issues_in_pending={diagnostic['open_issues_crossed']}")
    print(f"issue_distribution={json.dumps(diagnostic['issue_distribution'], sort_keys=True)}")
    print(f"top_families={json.dumps(diagnostic['family_rank'].most_common(5), ensure_ascii=False)}")
    print(
        "top_combos="
        + json.dumps(
            [[" + ".join(families), count] for families, count in diagnostic["combo_rank"].most_common(5)],
            ensure_ascii=False,
        )
    )
    print(f"recommendation={json.dumps(recommendation, ensure_ascii=False, sort_keys=True)}")
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")


if __name__ == "__main__":
    main()
