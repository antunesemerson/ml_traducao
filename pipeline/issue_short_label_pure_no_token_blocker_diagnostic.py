from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_pure_no_token_blocker_diagnostic_v1"


def latest_bridge_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label pure no-token bridge proposal run found.")
    return int(row["id"])


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_issue_short_label_pure_no_token_blocker_diagnostic"


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return list(payload.keys())
    return []


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_rows(conn, *, bridge_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs
        WHERE id = ?
        """,
        (bridge_run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"Bridge proposal run not found: {bridge_run_id}")

    rows = conn.execute(
        """
        SELECT
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.bridge_status,
            item.blocking_reason,
            item.total_issue_count,
            item.covered_issue_count,
            item.open_issue_count,
            item.blocked_issue_count,
            item.pure_no_token_checkpoint_issue_count,
            item.coverage_sources_json,
            cov.issue_families_json,
            cov.covered_families_json,
            cov.open_families_json,
            cov.covered_agents_json,
            cov.coverage_sources_json AS cov_coverage_sources_json,
            cov.reviewed_decisions_json,
            cov.evidence_json AS cov_evidence_json
        FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items item
        LEFT JOIN ml_issue_partial_coverage_items cov
          ON cov.id = item.source_coverage_item_id
        WHERE item.run_id = ?
          AND item.bridge_status = 'blocked'
        ORDER BY item.relative_path, item.source_line_number, item.segment_id
        """,
        (bridge_run_id,),
    ).fetchall()
    return dict(run), [dict(row) for row in rows]


def lane_for_open_family(family: str) -> str:
    if family == "short_label_style_microagent":
        return "same_family_short_label_split"
    if family in {"semantic_review_router", "culture_semantic_microagent", "religion_semantic_microagent"}:
        return "semantic_router_or_domain_specialist"
    if family in {"dynamic_ck3_expression_microagent", "gender_token_microagent"}:
        return "dynamic_or_gender_cross_agent"
    if family == "autofix_unknown_microagent":
        return "autofix_unknown_cluster"
    if family in {"title_policy_microagent", "nickname_name_policy"}:
        return "name_title_policy"
    if family in {"spanish_residual_microagent", "surface_boundary_microagent"}:
        return "surface_or_residual_repair"
    if family == "high_issue_auditor":
        return "high_issue_audit"
    return "other_or_unknown"


def package_from_path(relative_path: str | None) -> str:
    value = str(relative_path or "").replace("\\", "/")
    if not value:
        return "unknown"
    parts = value.split("/")
    return parts[0] if len(parts) > 1 else "root"


def main(*, bridge_run_id: int | None = None, sample_limit: int = 25) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = bridge_run_id or latest_bridge_run_id(conn)
        run, rows = fetch_rows(conn, bridge_run_id=selected_run_id)

    block_counts: Counter[str] = Counter()
    open_family_counts: Counter[str] = Counter()
    covered_family_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    open_combo_counts: Counter[str] = Counter()
    issue_shape_counts: Counter[str] = Counter()
    package_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    lane_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    output_rows: list[dict[str, Any]] = []
    for row in rows:
        block_reasons = [part for part in (row.get("blocking_reason") or "").split(";") if part]
        open_families = [str(x) for x in parse_json_list(row.get("open_families_json"))]
        covered_families = [str(x) for x in parse_json_list(row.get("covered_families_json"))]
        evidence = parse_json_dict(row.get("cov_evidence_json"))
        package = str(evidence.get("package") or package_from_path(row.get("relative_path")))
        domain = str(evidence.get("domain") or "unknown")

        for reason in block_reasons:
            block_counts[reason] += 1
        for family in open_families:
            open_family_counts[family] += 1
        for family in covered_families:
            covered_family_counts[family] += 1

        primary_open_family = open_families[0] if open_families else "unknown"
        lane = lane_for_open_family(primary_open_family)
        lane_counts[lane] += 1
        combo = "+".join(open_families[:5]) if open_families else "none"
        open_combo_counts[combo] += 1
        issue_shape = f"{int(row.get('covered_issue_count') or 0)}/{int(row.get('total_issue_count') or 0)}"
        issue_shape_counts[issue_shape] += 1
        package_counts[package] += 1
        domain_counts[domain] += 1

        out = {
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "source_line_number": row["source_line_number"],
            "lane": lane,
            "primary_open_family": primary_open_family,
            "open_families": "|".join(open_families),
            "covered_families": "|".join(covered_families),
            "blocking_reason": row.get("blocking_reason"),
            "issue_shape": issue_shape,
            "package": package,
            "domain": domain,
        }
        output_rows.append(out)
        if len(lane_examples[lane]) < sample_limit:
            lane_examples[lane].append(out)

    base = report_base(settings)
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "segment_id",
                "relative_path",
                "source_key",
                "source_line_number",
                "lane",
                "primary_open_family",
                "open_families",
                "covered_families",
                "blocking_reason",
                "issue_shape",
                "package",
                "domain",
            ],
        )
        writer.writeheader()
        for out in output_rows:
            writer.writerow(out)

    lines = [
        "Short-label Pure No-token Blocker Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge proposal run id: {selected_run_id}",
        f"Source coverage run id: {run.get('source_coverage_run_id')}",
        f"Source checkpoint run id: {run.get('source_checkpoint_run_id')}",
        f"Blocked rows: {len(rows):,}",
        "",
        "Block reasons:",
    ]
    for key, value in block_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Open issue families:"])
    for key, value in open_family_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Blocked lanes:"])
    for key, value in lane_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Issue coverage shapes:"])
    for key, value in issue_shape_counts.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Open family combos:"])
    for key, value in open_combo_counts.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Covered families present:"])
    for key, value in covered_family_counts.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Top packages:"])
    for key, value in package_counts.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Domains:"])
    for key, value in domain_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Lane samples:"])
    for lane, examples in lane_examples.items():
        lines.append(f"- {lane}:")
        for ex in examples[:sample_limit]:
            lines.append(
                f"  - segment={ex['segment_id']} | {ex['issue_shape']} | "
                f"{ex['primary_open_family']} | {ex['relative_path']}:{ex['source_line_number']} | {ex['source_key']}"
            )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- These rows were not rejected because pure no-token evidence is bad; most were blocked because another issue family remains open.",
            "- The next useful work is not more generic short-label review, but lane-specific completion of the remaining open family.",
            "- This diagnostic is read-only and grants no production authority.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bridge_run_id": selected_run_id,
        "source_coverage_run_id": run.get("source_coverage_run_id"),
        "source_checkpoint_run_id": run.get("source_checkpoint_run_id"),
        "blocked_rows": len(rows),
        "block_counts": dict(block_counts),
        "open_family_counts": dict(open_family_counts),
        "covered_family_counts": dict(covered_family_counts),
        "lane_counts": dict(lane_counts),
        "issue_shape_counts": dict(issue_shape_counts),
        "open_combo_counts": dict(open_combo_counts),
        "package_counts": dict(package_counts),
        "domain_counts": dict(domain_counts),
        "lane_examples": lane_examples,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Bridge proposal run: {selected_run_id}")
    print(f"Blocked rows: {len(rows):,}")
    for lane, count in lane_counts.most_common():
        print(f"{lane}: {count:,}")
    print(f"Report: {txt_path}")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-run-id", type=int)
    parser.add_argument("--sample-limit", type=int, default=25)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(bridge_run_id=args.bridge_run_id, sample_limit=args.sample_limit)
