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


AVOID_TOPICS = {
    "dynamic_ck3_expression_microagent+gender_token_microagent",
    "gender_token_microagent+dynamic_ck3_expression_microagent",
}


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_run(conn: sqlite3.Connection, table: str, run_id: int) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise SystemExit(f"run not found: {table}.id={run_id}")
    return dict(row)


def fetch_pending_segments(conn: sqlite3.Connection, segment_state_run_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT segment_id
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
          AND is_closed = 0
        """,
        (segment_state_run_id,),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def fetch_open_issues(conn: sqlite3.Connection, ledger_run_id: int, pending_ids: set[int]) -> list[dict[str, Any]]:
    if not pending_ids:
        return []
    rows = conn.execute(
        """
        SELECT segment_id, relative_path, source_key, issue_family, issue_kind,
               issue_role, issue_severity, agent_key, route_status, proposed_action,
               token_impact, validation_status, status
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
        """,
        (ledger_run_id,),
    ).fetchall()
    return [dict(row) for row in rows if int(row["segment_id"]) in pending_ids]


def exact_families_by_segment(issues: list[dict[str, Any]]) -> dict[int, tuple[str, ...]]:
    by_segment: dict[int, set[str]] = defaultdict(set)
    for issue in issues:
        by_segment[int(issue["segment_id"])].add(issue["issue_family"])
    return {segment_id: tuple(sorted(families)) for segment_id, families in by_segment.items()}


def issue_count_distribution(families_by_segment: dict[int, tuple[str, ...]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for families in families_by_segment.values():
        issue_count = len(families)
        if issue_count == 1:
            counts["1_issue"] += 1
        elif issue_count == 2:
            counts["2_issues"] += 1
        else:
            counts["3_plus_issues"] += 1
    return counts


def family_segment_counts(families_by_segment: dict[int, tuple[str, ...]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for families in families_by_segment.values():
        for family in families:
            counts[family] += 1
    return counts


def combo_counts(families_by_segment: dict[int, tuple[str, ...]]) -> Counter[tuple[str, ...]]:
    return Counter(families_by_segment.values())


def pair_counts(families_by_segment: dict[int, tuple[str, ...]], family: str) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for families in families_by_segment.values():
        if family not in families or len(families) < 2:
            continue
        for left, right in combinations(families, 2):
            if family in (left, right):
                counts[(left, right)] += 1
    return counts


def single_issue_counts(families_by_segment: dict[int, tuple[str, ...]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for families in families_by_segment.values():
        if len(families) == 1:
            counts[families[0]] += 1
    return counts


def recommended_prompt_type(families: tuple[str, ...]) -> str:
    family_set = set(families)
    if len(families) == 1:
        return "single_family_review"
    if "dynamic_ck3_expression_microagent" in family_set:
        return "focused_combo_review"
    if "semantic_review_router" in family_set:
        return "semantic_combo_review"
    return "review_batch"


def should_avoid_combo(families: tuple[str, ...]) -> bool:
    key = "+".join(families)
    family_set = set(families)
    if key in AVOID_TOPICS:
        return True
    if {"dynamic_ck3_expression_microagent", "gender_token_microagent"} <= family_set:
        return True
    if "autofix_unknown_microagent" in family_set and "semantic_review_router" in family_set:
        return True
    if "semantic_review_router" in family_set and "short_label_style_microagent" in family_set:
        return True
    return False


def choose_recommendation(
    combo_counter: Counter[tuple[str, ...]],
    family_counter: Counter[str],
    single_counter: Counter[str],
) -> tuple[str, str, str, str]:
    eligible_combos = [(combo, count) for combo, count in combo_counter.most_common() if not should_avoid_combo(combo)]
    best_combo, best_combo_count = eligible_combos[0] if eligible_combos else ((), 0)

    eligible_singles = [
        (family, count)
        for family, count in single_counter.most_common()
        if family not in {"semantic_review_router", "short_label_style_microagent", "dynamic_ck3_expression_microagent"}
    ]
    best_single, best_single_count = eligible_singles[0] if eligible_singles else ("", 0)

    if best_single_count >= best_combo_count and best_single:
        best = f"single_family_review:{best_single}"
        why = f"single-issue family has {best_single_count} pending segments and avoids saturated combo trees"
    elif best_combo:
        best = f"{recommended_prompt_type(best_combo)}:{' + '.join(best_combo)}"
        why = f"top eligible exact combo has {best_combo_count} pending segments after excluding recently saturated branches"
    else:
        best = "global_diagnostic_followup"
        why = "no eligible non-saturated combo found"

    alternative = (
        f"{recommended_prompt_type(best_combo)}:{' + '.join(best_combo)} ({best_combo_count})"
        if best_combo
        else "none"
    )
    avoid = (
        "avoid dynamic+gender local_player_select_cstring, broad autofix_unknown+semantic_review, and broad semantic+short_label repeats"
    )
    return best, alternative, avoid, why


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_next_focus_after_dynamic_gender"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(
    segment_state_run_id: int,
    ledger_run_id: int,
    pending_crossed: int,
    open_issue_count: int,
    issue_dist: Counter[str],
    family_counter: Counter[str],
    combo_counter: Counter[tuple[str, ...]],
    dynamic_pairs: Counter[tuple[str, str]],
    semantic_pairs: Counter[tuple[str, str]],
    single_counter: Counter[str],
) -> tuple[Path, Path, tuple[str, str, str, str]]:
    jsonl_path, txt_path = output_paths()
    recommendation = choose_recommendation(combo_counter, family_counter, single_counter)

    records: list[dict[str, Any]] = []
    for rank, (family, count) in enumerate(family_counter.most_common(50), start=1):
        records.append({"kind": "family_rank", "rank": rank, "family": family, "pending_segments": count})
    for rank, (families, count) in enumerate(combo_counter.most_common(50), start=1):
        records.append(
            {
                "kind": "combo_rank",
                "rank": rank,
                "families": list(families),
                "pending_segments": count,
                "recommended_prompt_type": recommended_prompt_type(families),
                "avoid_now": should_avoid_combo(families),
            }
        )
    for rank, (pair, count) in enumerate(dynamic_pairs.most_common(25), start=1):
        records.append({"kind": "dynamic_pair_rank", "rank": rank, "families": list(pair), "pending_segments": count})
    for rank, (pair, count) in enumerate(semantic_pairs.most_common(25), start=1):
        records.append({"kind": "semantic_pair_rank", "rank": rank, "families": list(pair), "pending_segments": count})
    for rank, (family, count) in enumerate(single_counter.most_common(25), start=1):
        records.append({"kind": "single_issue_family_rank", "rank": rank, "family": family, "pending_segments": count})
    records.append(
        {
            "kind": "recommendation",
            "best_next_prompt": recommendation[0],
            "alternative": recommendation[1],
            "avoid_now": recommendation[2],
            "justification": recommendation[3],
        }
    )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Global next focus after dynamic + gender",
        "",
        f"segment_state_run_id: {segment_state_run_id}",
        f"ledger_run_id: {ledger_run_id}",
        f"pending_segments_crossed: {pending_crossed}",
        f"open_issues_in_pending: {open_issue_count}",
        "",
        "Issue count distribution:",
        f"- 1_issue: {issue_dist.get('1_issue', 0)}",
        f"- 2_issues: {issue_dist.get('2_issues', 0)}",
        f"- 3_plus_issues: {issue_dist.get('3_plus_issues', 0)}",
        "",
        "Top 10 families by pending segments:",
    ]
    lines.extend(f"- {family}: {count}" for family, count in family_counter.most_common(10))
    lines.extend(["", "Top 15 exact family combinations:"])
    lines.extend(f"- {' + '.join(families)}: {count}" for families, count in combo_counter.most_common(15))
    lines.extend(["", "Top 10 pairs involving dynamic_ck3_expression_microagent:"])
    lines.extend(f"- {' + '.join(pair)}: {count}" for pair, count in dynamic_pairs.most_common(10))
    lines.extend(["", "Top 10 pairs involving semantic_review_router:"])
    lines.extend(f"- {' + '.join(pair)}: {count}" for pair, count in semantic_pairs.most_common(10))
    lines.extend(["", "Top 10 single-issue families:"])
    lines.extend(f"- {family}: {count}" for family, count in single_counter.most_common(10))
    lines.extend(
        [
            "",
            "Prioritized recommendation:",
            f"- best_next_prompt: {recommendation[0]}",
            f"- alternative: {recommendation[1]}",
            f"- avoid_now: {recommendation[2]}",
            f"- numeric_justification: {recommendation[3]}",
            "",
            "Safety: read-only diagnostic; no lifecycle, apply, segment-state, issue-ledger, confirmations, production, reindex, training, source edits, or output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, recommendation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    args = parser.parse_args()

    with connect_readonly() as conn:
        fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
        fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
        pending_ids = fetch_pending_segments(conn, args.segment_state_run_id)
        issues = fetch_open_issues(conn, args.ledger_run_id, pending_ids)

    families_by_segment = exact_families_by_segment(issues)
    issue_dist = issue_count_distribution(families_by_segment)
    family_counter = family_segment_counts(families_by_segment)
    combo_counter = combo_counts(families_by_segment)
    dynamic_pairs = pair_counts(families_by_segment, "dynamic_ck3_expression_microagent")
    semantic_pairs = pair_counts(families_by_segment, "semantic_review_router")
    single_counter = single_issue_counts(families_by_segment)

    jsonl_path, txt_path, recommendation = write_reports(
        args.segment_state_run_id,
        args.ledger_run_id,
        len(families_by_segment),
        len(issues),
        issue_dist,
        family_counter,
        combo_counter,
        dynamic_pairs,
        semantic_pairs,
        single_counter,
    )

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"ledger_run_id={args.ledger_run_id}")
    print(f"pending_segments_crossed={len(families_by_segment)}")
    print(f"open_issues_in_pending={len(issues)}")
    print("issue_distribution=" + json.dumps(dict(issue_dist), sort_keys=True))
    print("top_families=" + json.dumps(family_counter.most_common(5), ensure_ascii=False))
    print("top_combos=" + json.dumps([[" + ".join(combo), count] for combo, count in combo_counter.most_common(5)], ensure_ascii=False))
    print("recommendation=" + json.dumps({"best": recommendation[0], "alternative": recommendation[1]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
