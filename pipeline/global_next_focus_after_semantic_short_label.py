from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RECENTLY_REVIEWED = {
    frozenset({"semantic_review_router", "short_label_style_microagent"}): "yes_recent_semantic_short_label_cycle_saturated",
    frozenset({"autofix_unknown_microagent", "semantic_review_router"}): "yes_recent_autofix_semantic_cycle_avoid_broad_batch",
}

AVOID_FOCI = {
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
    if not result.get("finished_at") or int(result.get("total_segments") or 0) == 0:
        raise SystemExit(f"segment_state_run_id is incomplete or empty: {run_id}")
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
    if not result.get("finished_at") or int(result.get("ledger_item_count") or 0) == 0:
        raise SystemExit(f"ledger_run_id is incomplete or empty: {run_id}")
    return result


def fetch_pending_issue_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
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
        ORDER BY s.priority_score DESC, li.segment_id, li.issue_family
        """,
        (segment_state_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def path_group(relative_path: str) -> str:
    path = relative_path.replace("\\", "/")
    return path.split("/", 1)[0] if "/" in path else Path(path).stem


def reviewed_recently(families: tuple[str, ...]) -> str:
    family_set = frozenset(families)
    if family_set in RECENTLY_REVIEWED:
        return RECENTLY_REVIEWED[family_set]
    if "short_label_style_microagent" in family_set or "semantic_review_router" in family_set:
        return "partial_recent_semantic_short_label_activity"
    return "not_recently_exhausted_in_semantic_short_label_cycle"


def classify_focus(families: tuple[str, ...], single_count: int, multi_count: int) -> tuple[str, str, str]:
    family_set = frozenset(families)
    if family_set in AVOID_FOCI:
        return (
            "avoid_for_now",
            "high",
            "recent cycles show this broad combo is saturated; avoid spending another wide batch here now",
        )
    if "dynamic_ck3_expression_microagent" in family_set and "gender_token_microagent" in family_set:
        return (
            "review_batch",
            "medium",
            "large dynamic+gender overlap can be split into token policy, custom loc and CK3 expression lanes",
        )
    if "dynamic_ck3_expression_microagent" in family_set:
        return (
            "dynamic_policy_microagent",
            "medium",
            "dynamic CK3 expressions remain one of the largest families and need narrow policy reviews",
        )
    if "gender_token_microagent" in family_set:
        return (
            "dynamic_policy_microagent",
            "medium",
            "gender token segments are numerous and best handled by guarded policy review",
        )
    if "spanish_residual_microagent" in family_set and single_count >= multi_count:
        return (
            "residual_apply_protected",
            "medium",
            "single-family residual items may support protected dry-run/apply after review",
        )
    if "autofix_unknown_microagent" in family_set and len(family_set) == 1:
        return (
            "review_batch",
            "medium",
            "single-family unknowns are broad but can be sampled to find the next narrow sublane",
        )
    if "culture_semantic_microagent" in family_set or "religion_semantic_microagent" in family_set:
        return (
            "context_composer",
            "medium",
            "domain semantic families are sizeable but should use focused context composer reviews",
        )
    if len(family_set) == 1 and single_count:
        return (
            "lifecycle_direct_or_single_family_review",
            "low",
            "single-family pending segments are easier to audit in a narrow batch",
        )
    return (
        "review_batch",
        "medium",
        "multi-family overlap needs a focused review before any closure",
    )


def build_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)

    family_segments: dict[str, set[int]] = defaultdict(set)
    family_issues: Counter[str] = Counter()
    path_segments: dict[str, set[int]] = defaultdict(set)
    issue_distribution: Counter[str] = Counter()
    combo_segments: dict[tuple[str, ...], set[int]] = defaultdict(set)
    combo_single: Counter[tuple[str, ...]] = Counter()
    combo_multi: Counter[tuple[str, ...]] = Counter()

    for segment_id, segment_rows in by_segment.items():
        families = tuple(sorted({row["issue_family"] for row in segment_rows}))
        issue_count = len(segment_rows)
        issue_distribution["1_issue" if issue_count == 1 else "2_issues" if issue_count == 2 else "3_plus_issues"] += 1
        combo_segments[families].add(segment_id)
        if issue_count == 1:
            combo_single[families] += 1
        else:
            combo_multi[families] += 1
        for row in segment_rows:
            family_segments[row["issue_family"]].add(segment_id)
            family_issues[row["issue_family"]] += 1
            path_segments[path_group(row["relative_path"])].add(segment_id)

    top_families_by_segments = [
        {"issue_family": family, "pending_segments": len(ids), "open_issues": family_issues[family]}
        for family, ids in sorted(family_segments.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    top_families_by_issues = sorted(
        top_families_by_segments,
        key=lambda row: (-row["open_issues"], -row["pending_segments"], row["issue_family"]),
    )
    top_paths = [
        {"path_group": group, "pending_segments": len(ids)}
        for group, ids in sorted(path_segments.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    top_combinations = [
        {
            "focus": " + ".join(families),
            "families": list(families),
            "pending_segments": len(ids),
            "single_issue_segments": combo_single[families],
            "multi_issue_segments": combo_multi[families],
        }
        for families, ids in sorted(combo_segments.items(), key=lambda item: (-len(item[1]), item[0]))
    ]

    candidates: list[dict[str, Any]] = []
    for combo in top_combinations[:60]:
        families = tuple(combo["families"])
        step, risk, notes = classify_focus(families, combo["single_issue_segments"], combo["multi_issue_segments"])
        candidates.append(
            {
                **combo,
                "already_reviewed_recently": reviewed_recently(families),
                "expected_best_next_step": step,
                "recommended_next_step": step,
                "risk_level": risk,
                "why_now": notes,
                "notes": notes,
            }
        )

    recommendation = choose_recommendation(candidates)
    samples = sample_focus(rows, recommendation["best"]["focus"])
    return {
        "pending_issue_segments": len(by_segment),
        "open_issues": len(rows),
        "issue_count_distribution": dict(issue_distribution),
        "top_families_by_segments": top_families_by_segments,
        "top_families_by_issues": top_families_by_issues,
        "top_combinations": top_combinations,
        "top_paths": top_paths,
        "candidates": candidates,
        "recommendation": recommendation,
        "sample_segments": samples,
    }


def candidate_score(row: dict[str, Any]) -> tuple[int, int, int, int]:
    if row["recommended_next_step"] == "avoid_for_now":
        avoid_score = -100
    else:
        avoid_score = 0
    step_score = {
        "review_batch": 5,
        "dynamic_policy_microagent": 4,
        "context_composer": 3,
        "residual_apply_protected": 3,
        "lifecycle_direct_or_single_family_review": 2,
    }.get(row["recommended_next_step"], 1)
    combo_bonus = 2 if "dynamic_ck3_expression_microagent" in row["families"] and "gender_token_microagent" in row["families"] else 0
    return (avoid_score, step_score + combo_bonus, int(row["pending_segments"]), -len(row["families"]))


def choose_recommendation(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    viable = [row for row in candidates if row["recommended_next_step"] != "avoid_for_now"]
    best = sorted(viable, key=candidate_score, reverse=True)[0] if viable else candidates[0]
    alternative = next(
        (
            row
            for row in sorted(viable, key=candidate_score, reverse=True)
            if row["focus"] != best["focus"] and row["recommended_next_step"] != best["recommended_next_step"]
        ),
        next((row for row in viable if row["focus"] != best["focus"]), best),
    )
    avoid = next(
        (
            row
            for row in candidates
            if row["focus"] == "autofix_unknown_microagent + semantic_review_router"
        ),
        {
            "focus": "autofix_unknown_microagent + semantic_review_router",
            "recommended_next_step": "avoid_for_now",
            "notes": "avoid broad autofix+semantic batch for now",
        },
    )
    return {"best": best, "alternative": alternative, "avoid": avoid}


def sample_focus(rows: list[dict[str, Any]], focus: str, limit: int = 20) -> list[dict[str, Any]]:
    target = set(focus.split(" + "))
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)
    samples: list[dict[str, Any]] = []
    for segment_id, segment_rows in by_segment.items():
        families = {row["issue_family"] for row in segment_rows}
        if families != target:
            continue
        first = segment_rows[0]
        samples.append(
            {
                "segment_id": segment_id,
                "relative_path": first["relative_path"],
                "source_key": first["source_key"],
                "source_line_number": first["source_line_number"],
                "priority_score": first["priority_score"],
                "families": sorted(families),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_next_focus_after_semantic_short_label"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def write_reports(segment_run: dict[str, Any], ledger_run: dict[str, Any], diagnostic: dict[str, Any]) -> tuple[Path, Path]:
    txt_path, jsonl_path = output_paths()
    lines = [
        "Global next focus after semantic + short_label",
        f"segment_state_run_id: {segment_run['id']}",
        f"ledger_run_id: {ledger_run['id']}",
        f"segment_run_finished_at: {segment_run['finished_at']}",
        f"ledger_run_finished_at: {ledger_run['finished_at']}",
        f"ledger_original_segment_state_run_id: {ledger_run['segment_state_run_id']}",
        f"closed_count: {int(segment_run['closed_count'])}",
        f"pending_count: {int(segment_run['pending_count'])}",
        f"output_apply_pending_count: {int(segment_run['output_apply_pending_count'])}",
        f"pending_issue_segments_crossed: {diagnostic['pending_issue_segments']}",
        f"open_issues_crossed: {diagnostic['open_issues']}",
        "",
        "Issue distribution by segment:",
    ]
    for key in ("1_issue", "2_issues", "3_plus_issues"):
        lines.append(f"- {key}: {diagnostic['issue_count_distribution'].get(key, 0)}")
    lines.extend(["", "Top families by pending segments:"])
    for row in diagnostic["top_families_by_segments"][:10]:
        lines.append(f"- {row['issue_family']}: pending_segments={row['pending_segments']}, open_issues={row['open_issues']}")
    lines.extend(["", "Top families by open issues:"])
    for row in diagnostic["top_families_by_issues"][:10]:
        lines.append(f"- {row['issue_family']}: open_issues={row['open_issues']}, pending_segments={row['pending_segments']}")
    lines.extend(["", "Top family combinations:"])
    for row in diagnostic["top_combinations"][:15]:
        lines.append(
            f"- {row['focus']}: pending_segments={row['pending_segments']}, "
            f"single={row['single_issue_segments']}, multi={row['multi_issue_segments']}"
        )
    lines.extend(["", "Top path groups:"])
    for row in diagnostic["top_paths"][:10]:
        lines.append(f"- {row['path_group']}: pending_segments={row['pending_segments']}")
    lines.extend(["", "Top focus candidates:"])
    for rank, row in enumerate(sorted(diagnostic["candidates"], key=candidate_score, reverse=True)[:10], start=1):
        lines.append(
            f"{rank}. {row['focus']}: pending_segments={row['pending_segments']}, "
            f"single={row['single_issue_segments']}, multi={row['multi_issue_segments']}, "
            f"reviewed={row['already_reviewed_recently']}, next={row['recommended_next_step']}, risk={row['risk_level']}"
        )
        lines.append(f"   why_now: {row['why_now']}")
    rec = diagnostic["recommendation"]
    lines.extend(
        [
            "",
            "Prioritized recommendation:",
            f"1. next_best_prompt: {rec['best']['focus']} -> {rec['best']['recommended_next_step']}",
            f"2. alternative: {rec['alternative']['focus']} -> {rec['alternative']['recommended_next_step']}",
            f"3. avoid_for_now: {rec['avoid']['focus']} -> {rec['avoid']['recommended_next_step']}",
            "",
            "Avoid now:",
            "- broad autofix_unknown_microagent + semantic_review_router batch",
            "- deeper semantic_review_router + short_label_style_microagent scope/getter loops",
            "- concept/domain loops without ready lifecycle yield",
            "",
            "Sample recommended focus:",
        ]
    )
    for sample in diagnostic["sample_segments"]:
        lines.append(
            f"- segment_id={sample['segment_id']} path={sample['relative_path']} "
            f"key={sample['source_key']} line={sample['source_line_number']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_rows: list[dict[str, Any]] = []
    ranked = sorted(diagnostic["candidates"], key=candidate_score, reverse=True)
    for rank, row in enumerate(ranked, start=1):
        json_rows.append({"type": "focus_candidate", "rank": rank, **row})
    json_rows.append(
        {
            "type": "summary",
            "segment_state_run": segment_run,
            "ledger_run": ledger_run,
            "diagnostic": diagnostic,
        }
    )
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in json_rows) + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    args = parser.parse_args()
    with connect_readonly() as conn:
        segment_run = fetch_segment_run(conn, args.segment_state_run_id)
        ledger_run = fetch_ledger_run(conn, args.ledger_run_id)
        rows = fetch_pending_issue_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    diagnostic = build_diagnostic(rows)
    txt_path, jsonl_path = write_reports(segment_run, ledger_run, diagnostic)
    rec = diagnostic["recommendation"]
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"ledger_run_id={args.ledger_run_id}")
    print(f"pending_issue_segments={diagnostic['pending_issue_segments']}")
    print(f"open_issues={diagnostic['open_issues']}")
    print(f"issue_distribution={json.dumps(diagnostic['issue_count_distribution'], sort_keys=True)}")
    print(f"next_best={rec['best']['focus']} -> {rec['best']['recommended_next_step']}")
    print(f"alternative={rec['alternative']['focus']} -> {rec['alternative']['recommended_next_step']}")
    print(f"avoid={rec['avoid']['focus']} -> {rec['avoid']['recommended_next_step']}")
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")


if __name__ == "__main__":
    main()
