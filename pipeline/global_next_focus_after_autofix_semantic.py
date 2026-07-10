from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RECENTLY_REVIEWED_FOCUS = {
    frozenset({"autofix_unknown_microagent", "semantic_review_router"}): "yes_saturated_recent_autofix_semantic_cycle",
    frozenset({"semantic_review_router", "short_label_style_microagent"}): "yes_recent_short_label_style_batches",
}


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
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
    if not result.get("finished_at"):
        raise SystemExit(f"segment_state_run_id is not finished: {run_id}")
    return result


def fetch_pending_issue_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            li.segment_id,
            li.issue_family,
            li.issue_kind,
            li.issue_severity,
            li.relative_path,
            li.source_key,
            li.source_line_number,
            s.final_state,
            s.state_group,
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


def family_path_group(relative_path: str) -> str:
    value = relative_path.replace("\\", "/")
    if "/" in value:
        return value.split("/", 1)[0] or "root"
    return Path(value).stem or "root"


def expected_step(families: tuple[str, ...], *, single_issue_segments: int, multi_issue_segments: int) -> tuple[str, str, str]:
    family_set = set(families)
    if "autofix_unknown_microagent" in family_set and "semantic_review_router" in family_set:
        return (
            "avoid_or_only_narrow_policy",
            "high",
            "recent diagnostics show broad autofix+semantic is saturated; remaining tree is dynamic/domain-heavy",
        )
    if "short_label_style_microagent" in family_set and "semantic_review_router" in family_set:
        return (
            "review_batch",
            "medium",
            "large multi-issue overlap can likely be split into short-label semantic review batches",
        )
    if "dynamic_ck3_expression_microagent" in family_set:
        return (
            "dynamic_policy_microagent",
            "high",
            "dynamic issues are numerous and high severity, but need narrow policy guards",
        )
    if "gender_token_microagent" in family_set:
        return (
            "dynamic_policy_microagent",
            "high",
            "gender token family is high severity and should be handled with guarded token policy",
        )
    if "spanish_residual_microagent" in family_set:
        return (
            "residual_apply_protected",
            "medium",
            "residual issues may support protected repair batches after review",
        )
    if "semantic_review_router" in family_set and multi_issue_segments > single_issue_segments:
        return (
            "context_composer_or_review_batch",
            "medium",
            "semantic router mostly appears in combinations, so batch review should split companion/context/policy lanes",
        )
    if single_issue_segments >= multi_issue_segments:
        return (
            "lifecycle_or_single_family_review",
            "low",
            "mostly single-family pending segments are easier to audit and close in focused batches",
        )
    return (
        "review_batch",
        "medium",
        "multi-issue overlap needs a focused review before any lifecycle closure",
    )


def reviewed_recently_label(families: tuple[str, ...]) -> str:
    family_set = frozenset(families)
    if family_set in RECENTLY_REVIEWED_FOCUS:
        return RECENTLY_REVIEWED_FOCUS[family_set]
    if "autofix_unknown_microagent" in family_set or "short_label_style_microagent" in family_set:
        return "partial_recent_activity"
    return "not_in_recent_autofix_semantic_cycle"


def build_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)

    family_segments: dict[str, set[int]] = defaultdict(set)
    family_issues: Counter[str] = Counter()
    path_segments: dict[str, set[int]] = defaultdict(set)
    issue_count_distribution: Counter[str] = Counter()
    combo_segments: dict[tuple[str, ...], set[int]] = defaultdict(set)
    combo_single: Counter[tuple[str, ...]] = Counter()
    combo_multi: Counter[tuple[str, ...]] = Counter()

    for segment_id, segment_rows in by_segment.items():
        families = tuple(sorted({row["issue_family"] for row in segment_rows}))
        issue_count = len(segment_rows)
        if issue_count == 1:
            issue_count_distribution["1_issue"] += 1
        elif issue_count == 2:
            issue_count_distribution["2_issues"] += 1
        else:
            issue_count_distribution["3_plus_issues"] += 1
        combo_segments[families].add(segment_id)
        if issue_count == 1:
            combo_single[families] += 1
        else:
            combo_multi[families] += 1
        for row in segment_rows:
            family = row["issue_family"]
            family_segments[family].add(segment_id)
            family_issues[family] += 1
            path_segments[family_path_group(row["relative_path"])].add(segment_id)

    top_families_by_segments = [
        {
            "issue_family": family,
            "pending_segments": len(segment_ids),
            "open_issues": family_issues[family],
        }
        for family, segment_ids in sorted(
            family_segments.items(),
            key=lambda item: (-len(item[1]), -family_issues[item[0]], item[0]),
        )
    ]
    top_families_by_issues = sorted(
        top_families_by_segments,
        key=lambda row: (-row["open_issues"], -row["pending_segments"], row["issue_family"]),
    )
    top_paths = [
        {"path_group": path, "pending_segments": len(segment_ids)}
        for path, segment_ids in sorted(path_segments.items(), key=lambda item: (-len(item[1]), item[0]))
    ]

    candidates: list[dict[str, Any]] = []
    for families, segment_ids in sorted(combo_segments.items(), key=lambda item: (-len(item[1]), item[0]))[:50]:
        single_issue_segments = combo_single[families]
        multi_issue_segments = combo_multi[families]
        next_step, risk, why_now = expected_step(
            families,
            single_issue_segments=single_issue_segments,
            multi_issue_segments=multi_issue_segments,
        )
        candidates.append(
            {
                "focus": " + ".join(families),
                "families": list(families),
                "pending_segments": len(segment_ids),
                "single_issue_segments": single_issue_segments,
                "multi_issue_segments": multi_issue_segments,
                "already_reviewed_recently": reviewed_recently_label(families),
                "recommended_next_step": next_step,
                "risk_level": risk,
                "notes": why_now,
            }
        )

    top_combinations = [
        {
            "families": list(families),
            "focus": " + ".join(families),
            "pending_segments": len(segment_ids),
            "single_issue_segments": combo_single[families],
            "multi_issue_segments": combo_multi[families],
        }
        for families, segment_ids in sorted(combo_segments.items(), key=lambda item: (-len(item[1]), item[0]))
    ]

    recommended = choose_recommendation(candidates, top_families_by_segments)
    samples = sample_focus(rows, recommended["best"]["focus"])
    return {
        "segment_count": len(by_segment),
        "open_issue_count": len(rows),
        "issue_count_distribution": dict(issue_count_distribution),
        "top_families_by_segments": top_families_by_segments,
        "top_families_by_issues": top_families_by_issues,
        "top_paths": top_paths,
        "top_combinations": top_combinations,
        "candidates": candidates,
        "recommended": recommended,
        "sample_segments": samples,
    }


def candidate_score(candidate: dict[str, Any]) -> tuple[int, int, int]:
    step = candidate["recommended_next_step"]
    avoid_penalty = -1 if step.startswith("avoid") else 0
    risk_penalty = {"low": 2, "medium": 1, "high": 0}.get(candidate["risk_level"], 0)
    return (avoid_penalty, risk_penalty, int(candidate["pending_segments"]))


def choose_recommendation(candidates: list[dict[str, Any]], top_families: list[dict[str, Any]]) -> dict[str, Any]:
    viable = [row for row in candidates if not row["recommended_next_step"].startswith("avoid")]
    if viable:
        best = sorted(viable, key=candidate_score, reverse=True)[0]
    elif candidates:
        best = candidates[0]
    else:
        best = {
            "focus": "none",
            "pending_segments": 0,
            "recommended_next_step": "none",
            "risk_level": "unknown",
            "notes": "no pending issue rows found",
        }
    alternative = next(
        (
            row
            for row in viable
            if row["focus"] != best["focus"]
            and row["recommended_next_step"] != best["recommended_next_step"]
        ),
        viable[1] if len(viable) > 1 else best,
    )
    avoid = next(
        (row for row in candidates if "autofix_unknown_microagent + semantic_review_router" == row["focus"]),
        None,
    )
    return {
        "best": best,
        "alternative": alternative,
        "avoid": avoid
        or {
            "focus": "autofix_unknown_microagent + semantic_review_router",
            "recommended_next_step": "avoid_broad_batch",
            "notes": "recent diagnostics show low yield for broad continuation",
        },
        "top_family": top_families[0] if top_families else None,
    }


def sample_focus(rows: list[dict[str, Any]], focus: str, limit: int = 20) -> list[dict[str, Any]]:
    focus_families = set(focus.split(" + "))
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_segment[int(row["segment_id"])].append(row)
    samples: list[dict[str, Any]] = []
    for segment_id, segment_rows in by_segment.items():
        families = {row["issue_family"] for row in segment_rows}
        if families == focus_families:
            first = segment_rows[0]
            samples.append(
                {
                    "segment_id": segment_id,
                    "relative_path": first["relative_path"],
                    "source_key": first["source_key"],
                    "source_line_number": first["source_line_number"],
                    "families": sorted(families),
                    "priority_score": first["priority_score"],
                }
            )
        if len(samples) >= limit:
            break
    return samples


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_next_focus_after_autofix_semantic"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def write_reports(run: dict[str, Any], ledger_run_id: int, diagnostic: dict[str, Any]) -> tuple[Path, Path]:
    txt_path, jsonl_path = output_paths()
    lines = [
        "Global next focus after autofix + semantic",
        f"segment_state_run_id: {run['id']}",
        f"ledger_run_id: {ledger_run_id}",
        f"run_finished_at: {run['finished_at']}",
        f"closed_count: {int(run['closed_count']):,}",
        f"pending_count: {int(run['pending_count']):,}",
        f"output_apply_pending_count: {int(run['output_apply_pending_count']):,}",
        "",
        "Issue distribution by segment:",
    ]
    for key in ("1_issue", "2_issues", "3_plus_issues"):
        lines.append(f"- {key}: {int(diagnostic['issue_count_distribution'].get(key, 0)):,}")
    lines.extend(["", "Top families by pending segments:"])
    for row in diagnostic["top_families_by_segments"][:10]:
        lines.append(f"- {row['issue_family']}: pending_segments={row['pending_segments']:,}, open_issues={row['open_issues']:,}")
    lines.extend(["", "Top families by open issues:"])
    for row in diagnostic["top_families_by_issues"][:10]:
        lines.append(f"- {row['issue_family']}: open_issues={row['open_issues']:,}, pending_segments={row['pending_segments']:,}")
    lines.extend(["", "Top family combinations:"])
    for row in diagnostic["top_combinations"][:15]:
        lines.append(
            f"- {row['focus']}: pending_segments={row['pending_segments']:,}, "
            f"single={row['single_issue_segments']:,}, multi={row['multi_issue_segments']:,}"
        )
    lines.extend(["", "Top path groups:"])
    for row in diagnostic["top_paths"][:10]:
        lines.append(f"- {row['path_group']}: pending_segments={row['pending_segments']:,}")
    lines.extend(["", "Top focus candidates:"])
    for index, row in enumerate(diagnostic["candidates"][:10], start=1):
        lines.append(
            f"{index}. {row['focus']}: pending_segments={row['pending_segments']:,}, "
            f"single={row['single_issue_segments']:,}, multi={row['multi_issue_segments']:,}, "
            f"reviewed={row['already_reviewed_recently']}, next={row['recommended_next_step']}, risk={row['risk_level']}"
        )
        lines.append(f"   why_now: {row['notes']}")
    rec = diagnostic["recommended"]
    lines.extend(
        [
            "",
            "Recommendation:",
            f"- next_best_prompt: {rec['best']['focus']} -> {rec['best']['recommended_next_step']}",
            f"- alternative: {rec['alternative']['focus']} -> {rec['alternative']['recommended_next_step']}",
            f"- avoid_for_now: {rec['avoid']['focus']} -> {rec['avoid']['recommended_next_step']}",
            "",
            "Sample recommended focus:",
        ]
    )
    for sample in diagnostic["sample_segments"]:
        lines.append(
            f"- segment_id={sample['segment_id']} path={sample['relative_path']} key={sample['source_key']} "
            f"line={sample['source_line_number']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(diagnostic["candidates"], start=1):
        json_rows.append({"type": "candidate", "rank": rank, **row})
    json_rows.append({"type": "summary", "run": run, "ledger_run_id": ledger_run_id, **diagnostic})
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in json_rows) + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with connect_readonly() as conn:
        run = fetch_run(conn, args.segment_state_run_id)
        rows = fetch_pending_issue_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    diagnostic = build_diagnostic(rows)
    txt_path, jsonl_path = write_reports(run, args.ledger_run_id, diagnostic)
    rec = diagnostic["recommended"]
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"ledger_run_id={args.ledger_run_id}")
    print(f"pending_issue_segments={diagnostic['segment_count']}")
    print(f"open_issues={diagnostic['open_issue_count']}")
    print(f"next_best={rec['best']['focus']} -> {rec['best']['recommended_next_step']}")
    print(f"alternative={rec['alternative']['focus']} -> {rec['alternative']['recommended_next_step']}")
    print(f"avoid={rec['avoid']['focus']} -> {rec['avoid']['recommended_next_step']}")
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")


if __name__ == "__main__":
    main()
