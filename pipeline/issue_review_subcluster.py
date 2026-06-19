from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_review_subcluster_v4_short_label_subbuckets"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_number} is not an object: {path}")
        rows.append(payload)
    return rows


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def queue_text(row: dict[str, Any]) -> str:
    texts = row.get("texts") or {}
    if not isinstance(texts, dict):
        return ""
    return str(texts.get("confirmed_text") or texts.get("evidence_text") or "")


def decision_key(row: dict[str, Any]) -> tuple[int | None, int | None]:
    ledger_item_id = row.get("ledger_item_id")
    segment_id = row.get("segment_id")
    return (
        int(ledger_item_id) if ledger_item_id not in {None, ""} else None,
        int(segment_id) if segment_id not in {None, ""} else None,
    )


def is_dynamic_queue(queue_row: dict[str, Any] | None) -> bool:
    if not queue_row:
        return False
    bucket = str(queue_row.get("queue_bucket") or "")
    return queue_row.get("agent_key") == "micro_dynamic_ck3_expression" or bucket.startswith("dynamic_")


def is_nickname_select_cstring_residual_queue(queue_row: dict[str, Any] | None) -> bool:
    if not queue_row:
        return False
    bucket = str(queue_row.get("queue_bucket") or "")
    return (
        queue_row.get("agent_key") == "nickname_residual_spanish_select_cstring_boundary"
        or queue_row.get("issue_family") == "nickname_select_cstring_spanish_residual_microagent"
        or bucket.startswith("nickname_select_cstring_")
    )


def is_short_dynamic_bucket(bucket: str) -> bool:
    return bucket in {
        "short_dynamic_expression",
        "short_dynamic_spanish_literal",
    }


def dynamic_context_subcluster(bucket: str, domain: str) -> tuple[str, str, str]:
    if bucket == "dynamic_token_mismatch":
        return (
            "token_policy_context",
            "structural_token_gate",
            "route_to_token_policy_or_dynamic_microagent",
        )
    if bucket in {"dynamic_select_cstring_long", "dynamic_select_cstring_short"}:
        return (
            "dynamic_select_cstring_context",
            "micro_dynamic_ck3_expression",
            "design_select_cstring_dynamic_subpolicy",
        )
    if bucket == "dynamic_custom_localization":
        return (
            "dynamic_custom_localization_context",
            "micro_dynamic_ck3_expression",
            "design_custom_localization_subpolicy",
        )
    if bucket == "dynamic_concept_expression":
        return (
            "dynamic_concept_expression_context",
            "micro_dynamic_ck3_expression",
            "design_concept_expression_subpolicy",
        )
    if bucket in {"dynamic_many_tokens", "dynamic_very_long", "dynamic_long_text"}:
        return (
            "long_dynamic_composition_context",
            "micro_long_text_composer",
            "compose_after_dynamic_microagent_votes",
        )
    if bucket == "dynamic_events_longform" or domain == "domain_events_longform":
        return (
            "dynamic_events_context",
            "micro_dynamic_ck3_expression",
            "design_event_dynamic_context_subpolicy",
        )
    if bucket == "dynamic_interactions_activities" or domain == "domain_interactions_activities":
        return (
            "dynamic_interaction_context",
            "micro_dynamic_ck3_expression",
            "design_interaction_dynamic_context_subpolicy",
        )
    if bucket == "dynamic_rules_tooltips" or domain == "domain_rules_tooltips":
        return (
            "dynamic_rules_tooltip_context",
            "micro_dynamic_ck3_expression",
            "design_rules_tooltip_dynamic_context_subpolicy",
        )
    return (
        "dynamic_general_context",
        "micro_dynamic_ck3_expression",
        "design_dynamic_context_subpolicy",
    )


def subcluster(decision: dict[str, Any], queue_row: dict[str, Any] | None) -> tuple[str, str, str]:
    normalized = str(decision.get("decision") or "").strip()
    notes = str(decision.get("notes") or "").lower()
    bucket = str((queue_row or {}).get("queue_bucket") or "")
    relative_path = str((queue_row or {}).get("relative_path") or "")
    evidence = (queue_row or {}).get("evidence") or {}
    domain = str(evidence.get("domain") or "")
    issue_codes = evidence.get("issue_codes") if isinstance(evidence, dict) else []
    if not isinstance(issue_codes, list):
        issue_codes = []
    text = queue_text(queue_row or {})
    dynamic_queue = is_dynamic_queue(queue_row)

    if normalized in {"safe_short_label", "false_positive_reopen"}:
        if dynamic_queue:
            return (
                "dynamic_positive_candidate_requires_audit",
                "micro_dynamic_ck3_expression",
                "audit_dynamic_positive_before_ingest",
            )
        if issue_codes or " :" in text or " ," in text:
            return (
                "surface_or_text_repair_candidate",
                "micro_surface_boundary",
                "audit_surface_issue_before_positive_ingest",
            )
        return (
            "short_label_positive_candidate",
            "micro_short_label_style",
            "audit_then_ingest_positive_evidence",
        )

    if normalized == "needs_repair":
        if is_nickname_select_cstring_residual_queue(queue_row):
            return (
                "nickname_select_cstring_spanish_residual_repair_candidate",
                "nickname_residual_spanish_select_cstring_boundary",
                "create_nickname_select_cstring_residual_repair_queue",
            )
        if bucket in {"short_dynamic_spanish_literal", "short_spanish_residual_literal"}:
            return (
                "spanish_residual_repair_candidate",
                "micro_spanish_residual",
                "create_repair_queue_with_corrected_text",
            )
        if bucket == "short_mojibake_or_script":
            return (
                "encoding_repair_candidate",
                "micro_surface_boundary",
                "create_encoding_or_surface_repair_queue",
            )
        if "spanish_residual" in notes:
            return (
                "spanish_residual_repair_candidate",
                "micro_spanish_residual",
                "create_repair_queue_with_corrected_text",
            )
        if "mojibake" in notes:
            return (
                "encoding_repair_candidate",
                "micro_surface_boundary",
                "create_encoding_or_surface_repair_queue",
            )
        return (
            "surface_or_text_repair_candidate",
            "micro_surface_boundary",
            "create_surface_repair_queue",
        )

    if normalized == "needs_new_microagent":
        if "gender" in notes or "es_oa" in notes or "es_ao" in notes:
            return (
                "gender_dynamic_token_delegate",
                "micro_gender_token",
                "route_to_gender_token_issue_queue",
            )
        if dynamic_queue:
            return dynamic_context_subcluster(bucket, domain)
        if "dynamic" in notes or "select_cstring" in notes:
            return (
                "dynamic_expression_delegate",
                "micro_dynamic_ck3_expression",
                "route_to_dynamic_expression_issue_queue",
            )
        return (
            "new_microagent_candidate",
            "coordinator_ensemble_v1",
            "cluster_before_agent_creation",
        )

    if normalized == "needs_domain_context":
        if dynamic_queue:
            return dynamic_context_subcluster(bucket, domain)
        if is_short_dynamic_bucket(bucket):
            return (
                "dynamic_expression_delegate",
                "micro_dynamic_ck3_expression",
                "route_to_dynamic_expression_issue_queue",
            )
        if (
            bucket == "token_sensitive"
            or "token_sensitive" in notes
            or "token_policy" in notes
            or "token_mismatch" in notes
        ):
            return (
                "token_policy_context",
                "structural_token_gate",
                "route_to_token_policy_or_gender_dynamic_microagent",
            )
        if bucket in {"domain_events_longform", "domain_interactions_activities", "package_dlc"}:
            return (
                "long_or_dynamic_context",
                "micro_long_text_composer",
                "compose_after_microagent_votes",
            )
        if domain == "domain_religion" or relative_path.startswith("religion/"):
            return (
                "religion_context_delegate",
                "religion",
                "ask_religion_context_vote",
            )
        if domain == "domain_culture" or relative_path.startswith("culture/"):
            return (
                "culture_context_delegate",
                "culture_title_labels",
                "ask_culture_context_vote",
            )
        if domain == "domain_titles_names" or "titles" in relative_path:
            return (
                "title_context_delegate",
                "titles",
                "ask_title_context_vote",
            )
        return (
            "generic_domain_context_delegate",
            "coordinator_ensemble_v1",
            "route_to_domain_or_semantic_router",
        )

    return (
        "unclassified_decision",
        "coordinator_ensemble_v1",
        "manual_audit_required",
    )


def output_base(settings: dict[str, Any], queue_jsonl: Path, decisions_jsonl: Path) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha1(f"{queue_jsonl}|{decisions_jsonl}".encode("utf-8")).hexdigest()[:10]
    queue_name = safe_name(queue_jsonl.stem)[:36]
    return reports_dir / f"{stamp}_issue_review_subcluster_{queue_name}_{digest}"


def main(*, queue_jsonl: str, decisions_jsonl: str) -> dict[str, Any]:
    settings = db.load_settings()
    queue_path = db.project_path(queue_jsonl)
    decisions_path = db.project_path(decisions_jsonl)
    if not queue_path.exists():
        raise FileNotFoundError(queue_path)
    if not decisions_path.exists():
        raise FileNotFoundError(decisions_path)

    queue_rows = load_jsonl(queue_path)
    decision_rows = load_jsonl(decisions_path)
    queue_by_key = {decision_key(row): row for row in queue_rows}

    base = output_base(settings, queue_path, decisions_path)
    report_path = base.with_suffix(".txt")
    summary_path = base.with_suffix(".json")
    ingest_positive_path = Path(str(base) + "_positive_ingest_candidates.jsonl")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    counters: Counter[str] = Counter()
    owner_counters: Counter[str] = Counter()
    action_counters: Counter[str] = Counter()
    missing_queue_rows = 0

    for decision in decision_rows:
        key = decision_key(decision)
        queue_row = queue_by_key.get(key)
        if queue_row is None:
            missing_queue_rows += 1
        cluster, owner_agent, next_action = subcluster(decision, queue_row)
        counters[cluster] += 1
        owner_counters[owner_agent] += 1
        action_counters[next_action] += 1
        payload = {
            "cluster": cluster,
            "owner_agent": owner_agent,
            "next_action": next_action,
            "decision": decision,
            "queue": queue_row,
        }
        groups[cluster].append(payload)

    cluster_paths: dict[str, str] = {}
    for cluster, rows in sorted(groups.items()):
        path = Path(str(base) + f"_{safe_name(cluster)}.jsonl")
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        cluster_paths[cluster] = str(path)

    positive_rows = groups.get("short_label_positive_candidate", [])
    with ingest_positive_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in positive_rows:
            decision = dict(row["decision"])
            decision["notes"] = (
                str(decision.get("notes") or "")
                + f"; {RULE_VERSION}; positive_candidate_requires_final_audit"
            ).strip("; ")
            handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")

    samples: list[str] = []
    for cluster, rows in sorted(groups.items()):
        samples.append(f"{cluster}:")
        for row in rows[:5]:
            queue_row = row.get("queue") or {}
            decision = row.get("decision") or {}
            samples.append(
                "- "
                f"segment={decision.get('segment_id')} "
                f"{queue_row.get('relative_path')}::{queue_row.get('source_key')} "
                f"owner={row.get('owner_agent')} action={row.get('next_action')} "
                f"text={short(queue_text(queue_row), 120)}"
            )

    summary = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "queue_jsonl": str(queue_path),
        "decisions_jsonl": str(decisions_path),
        "queue_rows": len(queue_rows),
        "decision_rows": len(decision_rows),
        "missing_queue_rows": missing_queue_rows,
        "cluster_counts": dict(counters.most_common()),
        "owner_counts": dict(owner_counters.most_common()),
        "next_action_counts": dict(action_counters.most_common()),
        "cluster_paths": cluster_paths,
        "positive_ingest_candidates": str(ingest_positive_path),
        "report_path": str(report_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Issue review subcluster",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {summary['generated_at']}",
        f"Queue JSONL: {queue_path}",
        f"Decisions JSONL: {decisions_path}",
        "",
        "Coverage:",
        f"- Queue rows: {len(queue_rows):,}",
        f"- Decision rows: {len(decision_rows):,}",
        f"- Missing queue rows: {missing_queue_rows:,}",
        "",
        "Subclusters:",
        *[f"- {key}: {value:,}" for key, value in counters.most_common()],
        "",
        "Owner agents:",
        *[f"- {key}: {value:,}" for key, value in owner_counters.most_common()],
        "",
        "Next actions:",
        *[f"- {key}: {value:,}" for key, value in action_counters.most_common()],
        "",
        "Files:",
        *[f"- {cluster}: {path}" for cluster, path in cluster_paths.items()],
        f"- positive ingest candidates: {ingest_positive_path}",
        f"- summary json: {summary_path}",
        "",
        "Interpretation:",
        "- This is an analytical split only; it does not ingest decisions, train models or write output.",
        "- Positive candidates are isolated in an ingest-compatible file, but still require final audit before use.",
        "- Repair/context clusters should be routed to narrower microagents or domain specialists.",
        "",
        "Samples:",
        *samples,
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_review_subcluster] Subclusters generated")
    print(f"[issue_review_subcluster] Rule version: {RULE_VERSION}")
    print(f"[issue_review_subcluster] Queue rows: {len(queue_rows):,}")
    print(f"[issue_review_subcluster] Decision rows: {len(decision_rows):,}")
    for key, value in counters.most_common():
        print(f"[issue_review_subcluster] {key}: {value:,}")
    print(f"[issue_review_subcluster] Report: {report_path}")
    print(f"[issue_review_subcluster] JSON: {summary_path}")
    print(f"[issue_review_subcluster] Positive ingest candidates: {ingest_positive_path}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split issue review decisions into narrower neural subclusters.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--decisions-jsonl", required=True)
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, decisions_jsonl=args.decisions_jsonl)
