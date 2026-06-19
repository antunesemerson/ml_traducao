from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_subtype_diagnostic_v1"
TARGET_FAMILY = "short_label_style_microagent"


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No finished ml_issue_ledger_runs found.")
    return int(row["id"])


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_issue_short_label_subtype_diagnostic"


def fetch_rows(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                item.id AS ledger_item_id,
                item.run_id AS ledger_run_id,
                item.segment_id,
                item.relative_path,
                item.source_key,
                item.source_line_number,
                item.issue_kind,
                item.issue_severity,
                item.route_status,
                item.proposed_action,
                item.token_impact,
                item.token_status,
                item.confidence_score,
                item.evidence_text,
                item.evidence_json,
                item.validation_status,
                item.status,
                state.final_state,
                state.state_group,
                state.review_state,
                state.apply_state,
                state.active_action,
                state.candidate_action,
                state.policy_action
            FROM ml_issue_ledger_items item
            LEFT JOIN segment_state_items state
              ON state.id = item.state_item_id
            WHERE item.run_id = ?
              AND item.issue_family = ?
            ORDER BY item.issue_kind, item.segment_id
            """,
            (ledger_run_id, TARGET_FAMILY),
        )
    ]


def safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def action_lane(issue_kind: str, token_count: int, domain: str) -> str:
    if issue_kind in {"short_label_dynamic_expression_reopened", "short_label_dynamic_spanish_literal_reopened"}:
        return "dynamic_short_label_subagent"
    if issue_kind in {"short_label_spanish_literal_reopened", "short_label_spanish_residual_reopened"}:
        return "residual_short_label_repair"
    if token_count == 0 and domain == "domain_general":
        return "pure_no_token_label_policy"
    if issue_kind == "short_or_compact_label_reopened":
        return "compact_ui_label_policy"
    return "short_label_review_router"


def main(*, ledger_run_id: int | None = None, sample_per_lane: int = 25) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run = ledger_run_id or latest_ledger_run_id(conn)
        rows = fetch_rows(conn, ledger_run_id=selected_run)

    lane_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    package_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    lane_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lane_by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    lane_by_kind: dict[str, Counter[str]] = defaultdict(Counter)

    output_rows: list[dict[str, Any]] = []
    for row in rows:
        evidence = safe_json(row.get("evidence_json"))
        domain = evidence.get("domain") or "unknown"
        package = evidence.get("package") or "unknown"
        token_count = int(evidence.get("token_count") or 0)
        issue_kind = row.get("issue_kind") or "unknown"
        lane = action_lane(issue_kind, token_count, domain)

        lane_counts[lane] += 1
        kind_counts[issue_kind] += 1
        domain_counts[domain] += 1
        package_counts[package] += 1
        token_counts[f"tokens_{token_count if token_count < 9 else '9_plus'}"] += 1
        lane_by_domain[lane][domain] += 1
        lane_by_kind[lane][issue_kind] += 1

        out = {
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "source_line_number": row["source_line_number"],
            "lane": lane,
            "issue_kind": issue_kind,
            "domain": domain,
            "package": package,
            "token_count": token_count,
            "confidence_score": row["confidence_score"],
            "active_action": row["active_action"],
            "candidate_action": row["candidate_action"],
            "policy_action": row["policy_action"],
            "evidence_text": row["evidence_text"],
        }
        output_rows.append(out)
        if len(lane_examples[lane]) < sample_per_lane:
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
                "issue_kind",
                "domain",
                "package",
                "token_count",
                "confidence_score",
                "active_action",
                "candidate_action",
                "policy_action",
                "evidence_text",
            ],
        )
        writer.writeheader()
        for row in output_rows:
            writer.writerow(row)

    lines = [
        "Short Label Subtype Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Ledger run id: {selected_run}",
        f"Rows: {len(rows):,}",
        "",
        "Recommended split:",
    ]
    for lane, count in lane_counts.most_common():
        lines.append(f"- {lane}: {count:,}")
        for kind, kind_count in lane_by_kind[lane].most_common(5):
            lines.append(f"  - {kind}: {kind_count:,}")
    lines.extend(["", "Issue kinds:"])
    for key, value in kind_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Domains:"])
    for key, value in domain_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Top packages:"])
    for key, value in package_counts.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Token counts:"])
    for key, value in token_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(
        [
            "",
            "Next recommendation:",
            "- Start with pure_no_token_label_policy if precision looks high: it is the safest broad short-label lane.",
            "- Keep dynamic_short_label_subagent separate; it should reuse dynamic CK3 neurons instead of a generic label policy.",
            "- Use residual_short_label_repair only with explicit Spanish residual evidence.",
            "- Use compact_ui_label_policy as the broad style learner, but require sample validation before lifecycle.",
        ]
    )

    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger_run_id": selected_run,
        "total_rows": len(rows),
        "lane_counts": dict(lane_counts),
        "issue_kind_counts": dict(kind_counts),
        "domain_counts": dict(domain_counts),
        "package_counts": dict(package_counts),
        "token_counts": dict(token_counts),
        "lane_by_domain": {key: dict(value) for key, value in lane_by_domain.items()},
        "lane_by_kind": {key: dict(value) for key, value in lane_by_kind.items()},
        "samples": lane_examples,
        "csv_path": str(csv_path),
        "report_path": str(txt_path),
    }

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("[issue_short_label_subtype_diagnostic] Diagnostic generated")
    print(f"[issue_short_label_subtype_diagnostic] Ledger run id: {selected_run}")
    print(f"[issue_short_label_subtype_diagnostic] Rows: {len(rows):,}")
    for lane, count in lane_counts.most_common():
        print(f"[issue_short_label_subtype_diagnostic] lane {lane}: {count:,}")
    print(f"[issue_short_label_subtype_diagnostic] Report: {txt_path}")
    print(f"[issue_short_label_subtype_diagnostic] CSV: {csv_path}")
    print(f"[issue_short_label_subtype_diagnostic] JSON: {json_path}")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split short-label pending issues into actionable subtype lanes.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--sample-per-lane", type=int, default=25)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, sample_per_lane=args.sample_per_lane)
