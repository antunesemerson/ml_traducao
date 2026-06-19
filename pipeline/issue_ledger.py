from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from pending_architecture_diagnostic import (
    TOKEN_PATTERN,
    domain_family,
    fetch_pending_rows,
    issue_codes,
    latest_run,
    micro_families,
    percent,
    primary_family,
    sample_text,
    text_for_analysis,
    top_package,
    word_count,
)


RULE_VERSION = "issue_ledger_v2_short_label_subtypes"

SKIP_FAMILIES = {
    "legacy_auto_confirmation_reopen",
}

AGENT_BY_FAMILY = {
    "structural_token_gate": "micro_structural_token_gate",
    "gender_token_microagent": "micro_gender_token",
    "dynamic_ck3_expression_microagent": "micro_dynamic_ck3_expression",
    "nickname_select_cstring_spanish_residual_microagent": "nickname_residual_spanish_select_cstring_boundary",
    "spanish_residual_microagent": "micro_spanish_residual",
    "surface_boundary_microagent": "micro_surface_boundary",
    "short_label_style_microagent": "micro_short_label_style",
    "autofix_unknown_microagent": "micro_autofix_unknown_router",
    "long_text_composer": "micro_long_text_composer",
    "title_policy_microagent": "micro_title_policy",
    "nickname_name_policy": "micro_nickname_name_policy",
    "religion_semantic_microagent": "micro_religion_semantic",
    "culture_semantic_microagent": "micro_culture_semantic",
    "semantic_review_router": "micro_semantic_review_router",
    "high_issue_auditor": "micro_high_issue_auditor",
    "unclassified_pending": "micro_unclassified_pending_router",
}

ROLE_BY_FAMILY = {
    "structural_token_gate": "guard",
    "gender_token_microagent": "repair",
    "dynamic_ck3_expression_microagent": "repair",
    "nickname_select_cstring_spanish_residual_microagent": "guard",
    "spanish_residual_microagent": "repair",
    "surface_boundary_microagent": "repair",
    "short_label_style_microagent": "review",
    "autofix_unknown_microagent": "route",
    "long_text_composer": "compose",
    "title_policy_microagent": "context",
    "nickname_name_policy": "context",
    "religion_semantic_microagent": "context",
    "culture_semantic_microagent": "context",
    "semantic_review_router": "review",
    "high_issue_auditor": "audit",
    "unclassified_pending": "route",
}

SEVERITY_BY_FAMILY = {
    "structural_token_gate": "critical",
    "high_issue_auditor": "high",
    "gender_token_microagent": "high",
    "dynamic_ck3_expression_microagent": "high",
    "nickname_select_cstring_spanish_residual_microagent": "high",
    "spanish_residual_microagent": "high",
    "surface_boundary_microagent": "medium",
    "long_text_composer": "medium",
    "semantic_review_router": "medium",
    "autofix_unknown_microagent": "medium",
    "title_policy_microagent": "medium",
    "nickname_name_policy": "medium",
    "religion_semantic_microagent": "medium",
    "culture_semantic_microagent": "medium",
    "short_label_style_microagent": "medium",
    "unclassified_pending": "medium",
}

PROPOSED_ACTION_BY_FAMILY = {
    "structural_token_gate": "block_until_token_or_structure_decision",
    "gender_token_microagent": "create_gender_token_issue_queue",
    "dynamic_ck3_expression_microagent": "create_dynamic_expression_issue_queue",
    "nickname_select_cstring_spanish_residual_microagent": "route_to_nickname_select_cstring_residual_boundary",
    "spanish_residual_microagent": "create_residual_spanish_repair_queue",
    "surface_boundary_microagent": "attempt_deterministic_boundary_repair_shadow",
    "short_label_style_microagent": "sample_short_label_style_policy",
    "autofix_unknown_microagent": "cluster_unknown_autofix_before_agent_creation",
    "long_text_composer": "compose_after_micro_repair_votes",
    "title_policy_microagent": "ask_title_context_policy_vote",
    "nickname_name_policy": "ask_nickname_name_policy_vote",
    "religion_semantic_microagent": "ask_religion_context_policy_vote",
    "culture_semantic_microagent": "ask_culture_context_policy_vote",
    "semantic_review_router": "route_to_human_or_semantic_specialist",
    "high_issue_auditor": "audit_high_issue_before_any_promotion",
    "unclassified_pending": "cluster_and_request_new_microagent",
}


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = reports_dir / f"{stamp}_issue_ledger"
    return base.with_suffix(".txt"), base.with_suffix(".json")


def issue_kind(row: dict[str, Any], family: str) -> str:
    codes = issue_codes(row)
    text = text_for_analysis(row)
    active_action = row.get("active_action") or ""
    candidate_action = row.get("candidate_action") or ""
    policy_action = row.get("policy_action") or ""

    if family == "structural_token_gate":
        if "blocked_structure" in {active_action, candidate_action, policy_action}:
            return "blocked_structure"
        if "token_mismatch" in codes:
            return "token_mismatch"
        return "token_or_structure_risk"
    if family == "gender_token_microagent":
        if "gender_token_extra_prefix" in codes:
            return "gender_token_extra_prefix"
        if "gender_token_extra_suffix" in codes:
            return "gender_token_extra_suffix"
        if "Select_CString" in text:
            return "select_cstring_gender_literal"
        return "gender_token_usage"
    if family == "dynamic_ck3_expression_microagent":
        if "Select_CString" in text:
            return "select_cstring_expression"
        if "Custom(" in text:
            return "custom_localization_expression"
        if "Concept(" in text:
            return "concept_expression"
        return "dynamic_expression"
    if family == "nickname_select_cstring_spanish_residual_microagent":
        return "nickname_select_cstring_spanish_residual"
    if family == "spanish_residual_microagent":
        if "spanish_residue_in_literal" in codes:
            return "spanish_residue_in_literal"
        if "spanish_punctuation" in codes:
            return "spanish_punctuation"
        if "spanish_residue" in codes:
            return "spanish_residue"
        return "suspected_spanish_residue"
    if family == "surface_boundary_microagent":
        if "missing_space_after_token" in codes:
            return "missing_space_after_token"
        if "missing_space_before_token" in codes:
            return "missing_space_before_token"
        if "space_before_punctuation" in codes:
            return "space_before_punctuation"
        return "surface_boundary"
    if family == "short_label_style_microagent":
        if "token_mismatch" in codes:
            return "short_label_token_mismatch_reopened"
        if "mojibake_or_unexpected_script" in codes:
            return "short_label_mojibake_or_script_reopened"
        if "spanish_residue_in_literal" in codes:
            if any(marker in text for marker in ("Select_CString", "LocalPlayerString", "Custom(", "Concept(")):
                return "short_label_dynamic_spanish_literal_reopened"
            return "short_label_spanish_literal_reopened"
        if "spanish_residue" in codes or "spanish_punctuation" in codes:
            return "short_label_spanish_residual_reopened"
        if any(marker in text for marker in ("Select_CString", "LocalPlayerString", "Custom(", "Concept(")):
            return "short_label_dynamic_expression_reopened"
        return "short_or_compact_label_reopened"
    if family == "long_text_composer":
        return "long_text_multi_issue_composition"
    if family == "semantic_review_router":
        return "needs_human_or_semantic_conflict"
    if family == "high_issue_auditor":
        return "high_issue_present"
    if family == "autofix_unknown_microagent":
        return "needs_autofix_unclassified"
    if family == "title_policy_microagent":
        return "title_context_policy"
    if family == "nickname_name_policy":
        return "nickname_name_policy"
    if family == "religion_semantic_microagent":
        return "religion_context_policy"
    if family == "culture_semantic_microagent":
        return "culture_context_policy"
    return "unclassified_pending"


def token_impact(row: dict[str, Any], family: str) -> str:
    token_statuses = {
        row.get("active_token_status") or "",
        row.get("candidate_token_status") or "",
    }
    if "mismatch" in token_statuses:
        return "token_mismatch"
    if family in {
        "gender_token_microagent",
        "dynamic_ck3_expression_microagent",
        "nickname_select_cstring_spanish_residual_microagent",
        "structural_token_gate",
    }:
        return "token_sensitive"
    if family in {"surface_boundary_microagent", "spanish_residual_microagent"}:
        return "usually_same_tokens"
    return "none_or_unknown"


def route_status(family: str) -> str:
    if family == "structural_token_gate":
        return "guard_block"
    if family in {"high_issue_auditor", "semantic_review_router"}:
        return "audit_required"
    if family == "autofix_unknown_microagent":
        return "cluster_required"
    return "candidate"


def item_status(family: str) -> str:
    if family == "structural_token_gate":
        return "blocked"
    return "open"


def confidence(row: dict[str, Any], family: str) -> float:
    base = {
        "structural_token_gate": 0.95,
        "gender_token_microagent": 0.88,
        "dynamic_ck3_expression_microagent": 0.84,
        "nickname_select_cstring_spanish_residual_microagent": 0.90,
        "spanish_residual_microagent": 0.82,
        "surface_boundary_microagent": 0.80,
        "short_label_style_microagent": 0.72,
        "autofix_unknown_microagent": 0.60,
        "long_text_composer": 0.70,
        "semantic_review_router": 0.76,
        "high_issue_auditor": 0.86,
    }.get(family, 0.68)
    if int(row.get("candidate_high_issue_count") or 0) > 0:
        base = max(base, 0.86)
    return base


def evidence_payload(row: dict[str, Any], family: str) -> dict[str, Any]:
    text = text_for_analysis(row)
    return {
        "domain": domain_family(row),
        "package": top_package(row.get("relative_path")),
        "issue_codes": sorted(issue_codes(row)),
        "text_length": len(text),
        "word_count": word_count(text),
        "token_count": len(TOKEN_PATTERN.findall(text)),
        "active_token_status": row.get("active_token_status"),
        "candidate_token_status": row.get("candidate_token_status"),
        "policy_group": row.get("policy_group"),
        "policy_new_safe": int(row.get("policy_new_safe") or 0),
        "family": family,
    }


def build_ledger_items(rows: list[dict[str, Any]], run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        families = [family for family in micro_families(row) if family not in SKIP_FAMILIES]
        if not families:
            families = ["unclassified_pending"]
        for family in families:
            evidence = evidence_payload(row, family)
            token_status = row.get("candidate_token_status") or row.get("active_token_status") or "unknown"
            items.append(
                {
                    "run_id": run_id,
                    "state_item_id": row.get("id"),
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "final_state": row.get("final_state"),
                    "state_group": row.get("state_group"),
                    "active_action": row.get("active_action"),
                    "candidate_action": row.get("candidate_action"),
                    "policy_action": row.get("policy_action"),
                    "confirmation_level": row.get("confirmation_level"),
                    "confirmation_label": row.get("confirmation_label"),
                    "locked": int(row.get("locked") or 0),
                    "issue_family": family,
                    "issue_kind": issue_kind(row, family),
                    "issue_role": ROLE_BY_FAMILY.get(family, "diagnostic"),
                    "issue_severity": SEVERITY_BY_FAMILY.get(family, "medium"),
                    "agent_key": AGENT_BY_FAMILY.get(family, "micro_unclassified_pending_router"),
                    "route_status": route_status(family),
                    "proposed_action": PROPOSED_ACTION_BY_FAMILY.get(
                        family,
                        "cluster_and_request_new_microagent",
                    ),
                    "proposed_repair_text": None,
                    "token_impact": token_impact(row, family),
                    "token_status": token_status,
                    "confidence_score": confidence(row, family),
                    "evidence_text": sample_text(text_for_analysis(row), 320),
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    "validation_status": "not_validated",
                    "status": item_status(family),
                    "created_at": created_at,
                }
            )
    return items


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = list(items[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    conn.executemany(
        f"""
        INSERT INTO ml_issue_ledger_items ({column_sql})
        VALUES ({placeholders})
        """,
        [tuple(item[column] for column in columns) for item in items],
    )


def summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    family_counts = Counter(item["issue_family"] for item in items)
    agent_counts = Counter(item["agent_key"] for item in items)
    role_counts = Counter(item["issue_role"] for item in items)
    severity_counts = Counter(item["issue_severity"] for item in items)
    status_counts = Counter(item["status"] for item in items)
    route_counts = Counter(item["route_status"] for item in items)
    token_counts = Counter(item["token_impact"] for item in items)
    package_by_family: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        evidence = json.loads(item["evidence_json"])
        family = item["issue_family"]
        package_by_family[family][evidence["package"]] += 1
        if len(examples[family]) < 4:
            examples[family].append(
                {
                    "segment_id": item["segment_id"],
                    "relative_path": item["relative_path"],
                    "source_key": item["source_key"],
                    "issue_kind": item["issue_kind"],
                    "proposed_action": item["proposed_action"],
                    "evidence_text": item["evidence_text"],
                }
            )
    return {
        "family_counts": dict(family_counts.most_common()),
        "agent_counts": dict(agent_counts.most_common()),
        "role_counts": dict(role_counts.most_common()),
        "severity_counts": dict(severity_counts.most_common()),
        "status_counts": dict(status_counts.most_common()),
        "route_counts": dict(route_counts.most_common()),
        "token_impact_counts": dict(token_counts.most_common()),
        "package_by_family": {
            family: dict(counter.most_common(10)) for family, counter in package_by_family.items()
        },
        "examples": dict(examples),
    }


def build_report_lines(
    run_id: int,
    state_run: dict[str, Any],
    pending_rows: list[dict[str, Any]],
    items: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[str]:
    segments_with_items = len({item["segment_id"] for item in items})
    total_pending = int(state_run.get("pending_count") or len(pending_rows))
    lines = [
        "Issue ledger report",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Ledger run id: {run_id}",
        f"Segment-state run id: {state_run['id']}",
        "",
        "Coverage:",
        f"- Pending segments in state run: {total_pending:,}",
        f"- Pending segments inspected: {len(pending_rows):,}",
        f"- Segments with ledger items: {segments_with_items:,} ({percent(segments_with_items, total_pending):.2f}%)",
        f"- Ledger items: {len(items):,}",
        f"- Items per covered segment: {(len(items) / segments_with_items if segments_with_items else 0):.2f}",
        "",
        "Interpretation:",
        "- The ledger is observational in this first version: it does not change output, confirmations or models.",
        "- Each row is a problem/habilidade to be handled by a microagent.",
        "- Multiple rows can point to the same segment; the future coordinator will compose their decisions.",
        "",
        "Issue families:",
    ]
    for family, count in summary["family_counts"].items():
        lines.append(f"- {family}: {count:,} ({percent(count, len(items)):.2f}%)")

    lines.extend(["", "Agents:"])
    for agent, count in summary["agent_counts"].items():
        lines.append(f"- {agent}: {count:,}")

    lines.extend(["", "Roles:"])
    for role, count in summary["role_counts"].items():
        lines.append(f"- {role}: {count:,}")

    lines.extend(["", "Severity:"])
    for severity, count in summary["severity_counts"].items():
        lines.append(f"- {severity}: {count:,}")

    lines.extend(["", "Route status:"])
    for status, count in summary["route_counts"].items():
        lines.append(f"- {status}: {count:,}")

    lines.extend(["", "Token impact:"])
    for token_impact, count in summary["token_impact_counts"].items():
        lines.append(f"- {token_impact}: {count:,}")

    lines.extend(
        [
            "",
            "Next implementation targets:",
            "1. Build queue generator for `micro_short_label_style` because it covers the largest block.",
            "2. Build validation queue for `micro_gender_token` because it is high-impact and token-sensitive.",
            "3. Cluster `micro_autofix_unknown_router` before creating new agents.",
            "4. Keep domain agents as context voters after micro-repairs, especially titles, religion and culture.",
        ]
    )

    lines.extend(["", "Examples:"])
    for family, examples in summary["examples"].items():
        lines.append(f"{family}:")
        for example in examples[:3]:
            lines.append(
                f"- segment {example['segment_id']} | {example['relative_path']}::{example['source_key']} | "
                f"{example['issue_kind']} -> {example['proposed_action']}"
            )
            lines.append(f"  evidence: {example['evidence_text']}")
    return lines


def main(limit: int | None = None) -> None:
    settings = db.load_settings()
    started_at = db.utc_now()
    print("[issue_ledger] Starting issue ledger")
    print(f"[issue_ledger] Rule version: {RULE_VERSION}")
    print(f"[issue_ledger] Database: {db.get_database_path(settings)}")
    print(f"[issue_ledger] Limit: {limit or 'none'}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        state_run = latest_run(conn, "segment_state_runs")
        pending_rows = fetch_pending_rows(conn, state_run, limit)
        pending_counts = Counter(primary_family(micro_families(row)) for row in pending_rows)
        run_id = conn.execute(
            """
            INSERT INTO ml_issue_ledger_runs (
                rule_version,
                segment_state_run_id,
                active_score_run_id,
                candidate_score_run_id,
                policy_run_id,
                source_scope,
                pending_segments_count,
                ledger_segment_count,
                ledger_item_count,
                actionable_item_count,
                blocked_item_count,
                primary_family_counts_json,
                notes_json,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                int(state_run["id"]),
                state_run.get("active_score_run_id"),
                state_run.get("candidate_score_run_id"),
                state_run.get("policy_run_id"),
                "pending_segment_state",
                int(state_run.get("pending_count") or len(pending_rows)),
                json.dumps(dict(pending_counts.most_common()), ensure_ascii=False),
                json.dumps({"limit": limit}, ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        ).lastrowid

        items = build_ledger_items(pending_rows, int(run_id))
        insert_items(conn, items)
        summary = summarize_items(items)
        txt_path, json_path = report_paths(settings)
        finished_at = db.utc_now()
        conn.execute(
            """
            UPDATE ml_issue_ledger_runs
            SET ledger_segment_count = ?,
                ledger_item_count = ?,
                actionable_item_count = ?,
                blocked_item_count = ?,
                family_counts_json = ?,
                agent_counts_json = ?,
                report_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len({item["segment_id"] for item in items}),
                len(items),
                sum(1 for item in items if item["status"] == "open"),
                sum(1 for item in items if item["status"] == "blocked"),
                json.dumps(summary["family_counts"], ensure_ascii=False),
                json.dumps(summary["agent_counts"], ensure_ascii=False),
                str(txt_path),
                finished_at,
                finished_at,
                int(run_id),
            ),
        )
        conn.commit()

    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger_run_id": run_id,
        "state_run": state_run,
        "summary": summary,
    }
    lines = build_report_lines(int(run_id), state_run, pending_rows, items, summary)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[issue_ledger] Ledger run id: {run_id}")
    print(f"[issue_ledger] Pending inspected: {len(pending_rows)}")
    print(f"[issue_ledger] Ledger items: {len(items)}")
    print(f"[issue_ledger] Report: {txt_path}")
    print(f"[issue_ledger] JSON: {json_path}")
    for family, count in list(summary["family_counts"].items())[:12]:
        print(f"[issue_ledger] family {family}: {count}")
    print("[issue_ledger] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Materialize issue-level ledger rows for pending segment-state items.")
    parser.add_argument("--limit", type=int, default=None, help="Optional pending row limit for dry sampling.")
    args = parser.parse_args()
    main(limit=args.limit)
