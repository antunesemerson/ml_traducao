from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_composite_review_progress import fetch_queued_policy_item_ids, pct
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
)


RULE_VERSION = "ml_composite_subpolicy_diagnostic_v1"
POSITIVE_DECISIONS = {"accept_policy_candidate", "keep_manual_exception_only"}
FIX_DECISIONS = {"fix_confirmed_text", "encoding_cleanup_required", "manual_token_rewrite_required"}
NEGATIVE_DECISIONS = {"reject_policy_candidate", "needs_subpolicy"}
RISK_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}
STATUS_PRIORITY = {
    "policy_candidate_review": 0,
    "ready_to_design_subpolicy": 1,
    "negative_boundary_learned": 2,
    "conflicting_evidence": 3,
    "queued_waiting_review": 4,
    "needs_more_review": 5,
    "needs_queue": 6,
    "evidence_gathering": 7,
}


TOKEN_FAMILY_PATTERNS: list[tuple[str, str]] = [
    ("select_cstring", "Select_CString("),
    ("glossary", "Glossary("),
    ("concept", "Concept("),
    ("custom2_relation", ".Custom2("),
    ("title_lookup", "GetTitleByKey("),
    ("gender_suffix_custom", "ES_OA"),
    ("gender_suffix_custom", "ES_XA"),
    ("gender_suffix_custom", "ES_EA"),
    ("article_preposition_custom", "ES_ElLa"),
    ("article_preposition_custom", "ES_DelDela"),
    ("article_preposition_custom", "ES_AlAla"),
    ("article_preposition_custom", "ES_LoLa"),
    ("article_preposition_custom", "ES_XA"),
    ("pronoun", "GetSheHe"),
    ("pronoun", "GetHerHim"),
    ("pronoun", "GetHerHis"),
    ("pronoun", "GetHerselfHimself"),
    ("name_form", "GetTitledFirstName"),
    ("name_form", "GetFirstName"),
    ("name_form", "GetNameNoTooltip"),
    ("name_form", "GetBaseName"),
    ("name_form", "GetDynasty.GetName"),
    ("name_form", "GetHouse.GetName"),
    ("style_pipe", "|<STYLE>"),
]


def risk_sort_value(value: str) -> int:
    return RISK_PRIORITY.get(value, 9)


def canonical_token(token: str) -> str:
    return re.sub(r"\s+", "", token).lower()


def tokens_for(row: dict[str, Any]) -> list[str]:
    return [str(item) for item in (row.get("missing_tokens") or []) + (row.get("extra_tokens") or [])]


def token_families(tokens: list[str]) -> set[str]:
    families: set[str] = set()
    for token in tokens:
        for family, marker in TOKEN_FAMILY_PATTERNS:
            if marker in token:
                families.add(family)
    return families or {"other"}


def has_family(families: set[str], *names: str) -> bool:
    return any(name in families for name in names)


def is_formatting_equivalent(row: dict[str, Any]) -> bool:
    missing = [canonical_token(token) for token in row.get("missing_tokens") or []]
    extra = [canonical_token(token) for token in row.get("extra_tokens") or []]
    return bool(missing and extra and sorted(missing) == sorted(extra))


GLOSSARY_RE = re.compile(r"Glossary\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")


def glossary_keys(tokens: list[str]) -> set[str]:
    keys = set()
    for token in tokens:
        for _label, key in GLOSSARY_RE.findall(token):
            keys.add(key)
    return keys


def is_glossary_label_translation(row: dict[str, Any]) -> bool:
    missing = row.get("missing_tokens") or []
    extra = row.get("extra_tokens") or []
    if not missing or not extra:
        return False
    if not all("Glossary(" in token for token in missing + extra):
        return False
    return bool(glossary_keys(missing) & glossary_keys(extra))


def classify_token_subtype(row: dict[str, Any]) -> tuple[str, set[str]]:
    tokens = tokens_for(row)
    families = token_families(tokens)
    route = row["suggested_route"]
    missing_select_count = sum(1 for token in row.get("missing_tokens") or [] if "Select_CString(" in token)

    if is_formatting_equivalent(row):
        return "formatting_equivalent_token", families
    if is_glossary_label_translation(row):
        return "glossary_label_translation", families

    if route == "select_cstring_dynamic_context_review":
        if missing_select_count >= 2:
            return "select_cstring_multi_dynamic_rewrite", families
        if has_family(families, "pronoun"):
            return "select_cstring_to_pronoun_rewrite", families
        if has_family(families, "gender_suffix_custom", "article_preposition_custom"):
            return "select_cstring_to_custom_gender_rewrite", families
        if has_family(families, "name_form"):
            return "select_cstring_to_name_form_rewrite", families
        return "select_cstring_literalized_context", families

    if route == "mixed_token_change_review":
        if has_family(families, "title_lookup"):
            return "title_lookup_gender_title_rewrite", families
        if has_family(families, "name_form") and has_family(
            families, "pronoun", "gender_suffix_custom", "article_preposition_custom"
        ):
            return "name_and_gender_token_rewrite", families
        if has_family(families, "name_form"):
            return "name_form_rewrite", families
        if has_family(families, "pronoun"):
            return "pronoun_perspective_rewrite", families
        if has_family(families, "gender_suffix_custom", "article_preposition_custom"):
            return "gender_article_custom_rewrite", families
        if has_family(families, "concept"):
            return "concept_link_rewrite", families
        return "mixed_unclassified_token_rewrite", families

    if route == "dynamic_scope_token_review":
        if has_family(families, "title_lookup"):
            return "dynamic_title_lookup_rewrite", families
        if has_family(families, "custom2_relation"):
            return "dynamic_relation_custom_rewrite", families
        if has_family(families, "name_form"):
            return "dynamic_name_form_rewrite", families
        if has_family(families, "pronoun", "gender_suffix_custom", "article_preposition_custom"):
            return "dynamic_gender_pronoun_rewrite", families
        return "dynamic_scope_rewrite", families

    if route in {"gender_token_subspecialist_review", "gender_pronoun_english_aligned_subpolicy"}:
        missing = " ".join(row.get("missing_tokens") or [])
        extra = " ".join(row.get("extra_tokens") or [])
        missing_pronoun = any(name in missing for name in ("GetSheHe", "GetHerHim", "GetHerHis", "GetHerselfHimself"))
        extra_pronoun = any(name in extra for name in ("GetSheHe", "GetHerHim", "GetHerHis", "GetHerselfHimself"))
        missing_gender = "ES_OA" in missing or "ES_XA" in missing or "ES_EA" in missing
        extra_gender = "ES_OA" in extra or "ES_XA" in extra or "ES_EA" in extra
        if missing_pronoun and extra_pronoun:
            return "pronoun_form_swap", families
        if extra_pronoun:
            return "pronoun_added_for_pt_fluency", families
        if missing_pronoun:
            return "pronoun_removed_or_literalized", families
        if missing_gender and extra_gender:
            return "gender_custom_form_swap", families
        if extra_gender:
            return "gender_custom_added", families
        if missing_gender:
            return "gender_custom_removed_or_literalized", families
        return "gender_token_rewrite", families

    if route == "token_added_review":
        return "token_added_contextual_exception", families
    if route == "token_removed_review":
        return "token_removed_contextual_exception", families
    if route == "tutorial_concept_exception_subpolicy_review":
        return "tutorial_concept_exception_candidate", families
    return "unclassified_subpolicy_candidate", families


def fetch_decisions(conn, *, policy_run_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            policy_item_id,
            decision,
            approved_for_apply,
            corrected_text,
            notes,
            reviewer,
            updated_at
        FROM segment_token_policy_decisions
        WHERE policy_run_id = ?
        """,
        (policy_run_id,),
    ).fetchall()
    return {int(row["policy_item_id"]): dict(row) for row in rows}


def is_structural_accept_with_text_cleanup(decision: dict[str, Any]) -> bool:
    return (
        decision.get("decision") == "encoding_cleanup_required"
        and "token_release_safe_but_text_hygiene_blocks_apply" in (decision.get("notes") or "")
    )


def decision_counters(group: list[dict[str, Any]], decisions: dict[int, dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in group:
        decision = decisions.get(int(row["policy_item_id"]))
        if not decision:
            counts["pending_items"] += 1
            continue
        decision_name = decision["decision"]
        counts["reviewed_items"] += 1
        counts[f"decision_{decision_name}"] += 1
        if decision_name == "accept_policy_candidate" or is_structural_accept_with_text_cleanup(decision):
            counts["accept_count"] += 1
        if is_structural_accept_with_text_cleanup(decision):
            counts["text_cleanup_block_count"] += 1
        if decision_name == "keep_manual_exception_only":
            counts["keep_manual_exception_count"] += 1
        if int(decision["approved_for_apply"] or 0):
            counts["approved_for_apply_count"] += 1
        if decision_name == "reject_policy_candidate":
            counts["reject_count"] += 1
        if decision_name == "needs_subpolicy":
            counts["needs_subpolicy_count"] += 1
        if decision_name in FIX_DECISIONS and not is_structural_accept_with_text_cleanup(decision):
            counts["fix_count"] += 1
    return counts


def maturity_for(counts: Counter[str], *, queued_items: int, min_evidence: int, min_positive: int) -> tuple[str, str, str]:
    reviewed = counts["reviewed_items"]
    positive = counts["accept_count"] + counts["keep_manual_exception_count"]
    rejected = counts["reject_count"]
    needs_subpolicy = counts["needs_subpolicy_count"]
    fixes = counts["fix_count"]

    if reviewed == 0:
        if queued_items > 0:
            return ("queued_waiting_review", "low", "review_queued_samples")
        return ("needs_queue", "low", "generate_review_queue")
    if positive >= min_positive and rejected == 0 and fixes == 0:
        return ("policy_candidate_review", "candidate", "audit_for_guarded_policy_promotion")
    if positive and rejected:
        return ("conflicting_evidence", "mixed", "split_subtype_or_add_negative_review")
    if needs_subpolicy >= min_evidence:
        return ("ready_to_design_subpolicy", "design", "design_narrow_subpolicy_and_collect_positive_negative_pairs")
    if rejected >= min_evidence and positive == 0:
        return ("negative_boundary_learned", "blocked", "keep_blocked_and_use_as_negative_boundary")
    if reviewed < min_evidence:
        return ("needs_more_review", "low", "review_more_samples_for_this_subtype")
    return ("evidence_gathering", "medium", "continue_collecting_evidence")


def build_groups(
    rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    queued_policy_item_ids: set[int],
    *,
    min_evidence: int,
    min_positive: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    families_by_key: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        subtype, families = classify_token_subtype(row)
        key = (row["suggested_route"], subtype, row["overlay_policy_bucket"], row["overlay_risk_level"])
        grouped[key].append(row)
        families_by_key[key].update(families)

    items: list[dict[str, Any]] = []
    for (route, subtype, bucket, risk), group in grouped.items():
        counts = decision_counters(group, decisions)
        queued_items = sum(1 for row in group if int(row["policy_item_id"]) in queued_policy_item_ids)
        status, confidence, action = maturity_for(
            counts,
            queued_items=queued_items,
            min_evidence=min_evidence,
            min_positive=min_positive,
        )
        sample_rows = sorted(
            group,
            key=lambda row: (
                0 if int(row["policy_item_id"]) in decisions else 1,
                row["relative_path"],
                int(row.get("source_line_number") or 0),
            ),
        )[:8]
        sample_paths = [
            f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
            for row in sample_rows[:5]
        ]
        total = len(group)
        items.append(
            {
                "suggested_route": route,
                "token_subtype": subtype,
                "overlay_policy_bucket": bucket,
                "overlay_risk_level": risk,
                "total_items": total,
                "queued_items": queued_items,
                "unqueued_items": total - queued_items,
                "reviewed_items": counts["reviewed_items"],
                "pending_items": counts["pending_items"],
                "approved_for_apply_count": counts["approved_for_apply_count"],
                "accept_count": counts["accept_count"],
                "keep_manual_exception_count": counts["keep_manual_exception_count"],
                "reject_count": counts["reject_count"],
                "needs_subpolicy_count": counts["needs_subpolicy_count"],
                "fix_count": counts["fix_count"],
                "review_coverage_pct": pct(counts["reviewed_items"], total),
                "queue_coverage_pct": pct(queued_items, total),
                "maturity_status": status,
                "confidence_band": confidence,
                "recommended_action": action,
                "sample_policy_item_ids": [int(row["policy_item_id"]) for row in sample_rows],
                "sample_paths": sample_paths,
                "token_families": sorted(families_by_key[(route, subtype, bucket, risk)]),
            }
        )
    return sorted(
        items,
        key=lambda row: (
            STATUS_PRIORITY.get(row["maturity_status"], 99),
            risk_sort_value(row["overlay_risk_level"]),
            -int(row["reviewed_items"]),
            -int(row["total_items"]),
            row["suggested_route"],
            row["token_subtype"],
        ),
    )


def write_outputs(
    settings: dict[str, Any],
    *,
    active_gate: dict[str, Any],
    groups: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    queued_policy_item_ids: set[int],
    min_evidence: int,
    min_positive: int,
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = reports_dir / f"{timestamp}_ml_composite_subpolicy_diagnostic"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    total_counts = decision_counters(rows, decisions)
    queued_count = sum(1 for row in rows if int(row["policy_item_id"]) in queued_policy_item_ids)
    status_counts = Counter(row["maturity_status"] for row in groups)
    route_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for group in groups:
        route_counts[group["suggested_route"]][group["maturity_status"]] += 1

    lines = [
        "ML composite subpolicy diagnostic",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        "",
        "Thresholds:",
        f"- Min evidence for design candidate: {min_evidence}",
        f"- Min positive for policy candidate: {min_positive}",
        "",
        "Summary:",
        f"- Active gate items: {len(rows)}",
        f"- Queued items: {queued_count}",
        f"- Queue coverage: {pct(queued_count, len(rows)):.2f}%",
        f"- Reviewed items: {total_counts['reviewed_items']}",
        f"- Review coverage: {pct(total_counts['reviewed_items'], len(rows)):.2f}%",
        f"- Pending items: {total_counts['pending_items']}",
        f"- Subpolicy groups: {len(groups)}",
        "",
        "Maturity distribution:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in status_counts.most_common())
    lines.extend(["", "Route maturity matrix:"])
    for route, counts in sorted(route_counts.items()):
        parts = ", ".join(f"{key}={value}" for key, value in counts.most_common())
        lines.append(f"- {route}: {parts}")

    lines.extend(["", "Top groups:"])
    for group in groups[:30]:
        lines.append(
            "- {route} / {subtype} | {status} | risk={risk} total={total} queued={queued} "
            "reviewed={reviewed} pending={pending} needs_subpolicy={needs} accept={accept} "
            "keep_manual={manual} reject={reject} action={action}".format(
                route=group["suggested_route"],
                subtype=group["token_subtype"],
                status=group["maturity_status"],
                risk=group["overlay_risk_level"],
                total=group["total_items"],
                queued=group["queued_items"],
                reviewed=group["reviewed_items"],
                pending=group["pending_items"],
                needs=group["needs_subpolicy_count"],
                accept=group["accept_count"],
                manual=group["keep_manual_exception_count"],
                reject=group["reject_count"],
                action=group["recommended_action"],
            )
        )
        lines.append(f"  families: {', '.join(group['token_families'])}")
        lines.append(f"  samples: {json.dumps(group['sample_policy_item_ids'], ensure_ascii=False)}")

    lines.extend(
        [
            "",
            "Interpretation:",
            "- needs_subpolicy is evidence that a smaller specialist rule may be needed, not approval to apply output.",
            "- policy_candidate_review requires positive decisions and still needs guarded audit before promotion.",
            "- This diagnostic writes reports and DB rows only; it does not train models or change output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "suggested_route",
        "token_subtype",
        "overlay_policy_bucket",
        "overlay_risk_level",
        "total_items",
        "queued_items",
        "unqueued_items",
        "reviewed_items",
        "pending_items",
        "approved_for_apply_count",
        "accept_count",
        "keep_manual_exception_count",
        "reject_count",
        "needs_subpolicy_count",
        "fix_count",
        "review_coverage_pct",
        "queue_coverage_pct",
        "maturity_status",
        "confidence_band",
        "recommended_action",
        "sample_policy_item_ids",
        "sample_paths",
        "token_families",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for group in groups:
            writer.writerow(
                {
                    key: json.dumps(group[key], ensure_ascii=False)
                    if key in {"sample_policy_item_ids", "sample_paths", "token_families"}
                    else group[key]
                    for key in fieldnames
                }
            )
    json_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return txt_path, csv_path, json_path


def insert_run(
    conn,
    *,
    active_gate: dict[str, Any],
    groups: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    json_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    total_counts = decision_counters(rows, decisions)
    status_counts = Counter(group["maturity_status"] for group in groups)
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_subpolicy_diagnostic_runs (
            rule_version,
            gate_key,
            checkpoint_id,
            overlay_run_id,
            source_policy_run_id,
            total_items,
            reviewed_items,
            pending_items,
            grouped_subpolicies,
            design_candidate_count,
            policy_candidate_count,
            needs_more_review_count,
            queue_review_candidate_count,
            report_path,
            csv_path,
            json_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            active_gate["gate_key"],
            active_gate["active_checkpoint_id"],
            active_gate["active_overlay_run_id"],
            active_gate["active_policy_run_id"],
            len(rows),
            total_counts["reviewed_items"],
            total_counts["pending_items"],
            len(groups),
            status_counts["ready_to_design_subpolicy"],
            status_counts["policy_candidate_review"],
            status_counts["needs_more_review"],
            status_counts["queued_waiting_review"],
            str(report_path),
            str(csv_path),
            str(json_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for group in groups:
        conn.execute(
            """
            INSERT INTO ml_composite_subpolicy_diagnostic_items (
                run_id,
                suggested_route,
                token_subtype,
                overlay_policy_bucket,
                overlay_risk_level,
                total_items,
                queued_items,
                unqueued_items,
                reviewed_items,
                pending_items,
                approved_for_apply_count,
                accept_count,
                keep_manual_exception_count,
                reject_count,
                needs_subpolicy_count,
                fix_count,
                review_coverage_pct,
                queue_coverage_pct,
                maturity_status,
                confidence_band,
                recommended_action,
                sample_policy_item_ids_json,
                sample_paths_json,
                token_families_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                group["suggested_route"],
                group["token_subtype"],
                group["overlay_policy_bucket"],
                group["overlay_risk_level"],
                group["total_items"],
                group["queued_items"],
                group["unqueued_items"],
                group["reviewed_items"],
                group["pending_items"],
                group["approved_for_apply_count"],
                group["accept_count"],
                group["keep_manual_exception_count"],
                group["reject_count"],
                group["needs_subpolicy_count"],
                group["fix_count"],
                group["review_coverage_pct"],
                group["queue_coverage_pct"],
                group["maturity_status"],
                group["confidence_band"],
                group["recommended_action"],
                json.dumps(group["sample_policy_item_ids"], ensure_ascii=False),
                json.dumps(group["sample_paths"], ensure_ascii=False),
                json.dumps(group["token_families"], ensure_ascii=False),
                now,
            ),
        )
    return run_id


def main(
    *,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    min_evidence: int = 10,
    min_positive: int = 5,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        raw_rows = fetch_rows(conn, overlay_run_id=overlay_run_id, critical_only=False)
        rows = [enrich(row) for row in raw_rows]
        decisions = fetch_decisions(conn, policy_run_id=int(active_gate["active_policy_run_id"]))
        queued_policy_item_ids = fetch_queued_policy_item_ids(
            conn,
            gate_key=active_gate["gate_key"],
            overlay_run_id=int(active_gate["active_overlay_run_id"]),
        )
        groups = build_groups(
            rows,
            decisions,
            queued_policy_item_ids,
            min_evidence=min_evidence,
            min_positive=min_positive,
        )
        report_path, csv_path, json_path = write_outputs(
            settings,
            active_gate=active_gate,
            groups=groups,
            rows=rows,
            decisions=decisions,
            queued_policy_item_ids=queued_policy_item_ids,
            min_evidence=min_evidence,
            min_positive=min_positive,
            started_at=started_at,
        )
        run_id = insert_run(
            conn,
            active_gate=active_gate,
            groups=groups,
            rows=rows,
            decisions=decisions,
            report_path=report_path,
            csv_path=csv_path,
            json_path=json_path,
            started_at=started_at,
        )
        conn.commit()

    status_counts = Counter(group["maturity_status"] for group in groups)
    print("[ml_composite_subpolicy_diagnostic] Diagnostic generated")
    print(f"[ml_composite_subpolicy_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_subpolicy_diagnostic] Run id: {run_id}")
    print(f"[ml_composite_subpolicy_diagnostic] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_subpolicy_diagnostic] Overlay run id: {active_gate['active_overlay_run_id']}")
    print(f"[ml_composite_subpolicy_diagnostic] Policy run id: {active_gate['active_policy_run_id']}")
    print(f"[ml_composite_subpolicy_diagnostic] Groups: {len(groups)}")
    for key, value in status_counts.most_common():
        print(f"[ml_composite_subpolicy_diagnostic] {key}: {value}")
    print(f"[ml_composite_subpolicy_diagnostic] Report: {report_path}")
    print(f"[ml_composite_subpolicy_diagnostic] CSV: {csv_path}")
    print(f"[ml_composite_subpolicy_diagnostic] JSON: {json_path}")
    return {
        "run_id": run_id,
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "groups": len(groups),
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose active composite gate subpolicy candidates by route and token subtype.")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--min-evidence", type=int, default=10)
    parser.add_argument("--min-positive", type=int, default=5)
    args = parser.parse_args()
    main(gate_key=args.gate_key, min_evidence=args.min_evidence, min_positive=args.min_positive)
