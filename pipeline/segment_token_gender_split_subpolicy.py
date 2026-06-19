from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from segment_token_gender_subpolicy import (
    PRONOUN_MARKERS,
    classify_row,
    fetch_rows,
)
from segment_token_policy_review_queue import latest_policy_run_id, split_csv


RULE_VERSION = "segment_token_gender_split_subpolicy_v1"

ARTICLE_METHOD_HINTS = (
    "ES_ElLa",
    "ES_ElElla",
    "ES_DelDela",
    "ES_AlAla",
    "ES_LoLa",
)

ROOT_SCOPES = {"ROOT.Char", "GetPlayer"}


def slugify(value: str) -> str:
    safe = []
    for char in value.lower():
        if char.isalnum() or char in {"_", "-"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "filtered"


def token_inner(token: str) -> str:
    inner = token.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return inner.split("|", 1)[0]


def token_scope(token: str) -> str:
    inner = token_inner(token)
    if "." not in inner:
        return ""
    return inner.rsplit(".", 1)[0]


def token_method(token: str) -> str:
    inner = token_inner(token)
    if "." not in inner:
        return inner
    return inner.rsplit(".", 1)[1]


def token_has_any(token: str, markers: tuple[str, ...]) -> bool:
    return any(marker in token for marker in markers)


def is_pronoun_token(token: str) -> bool:
    return token_has_any(token, PRONOUN_MARKERS)


def is_article_token(token: str) -> bool:
    return token_has_any(token, ARTICLE_METHOD_HINTS)


def non_empty_scopes(tokens: list[str]) -> set[str]:
    return {token_scope(token) for token in tokens if token_scope(token)}


def split_agent_for(row: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    missing = row["missing_tokens"]
    extra = row["extra_tokens"]
    subtype = row["gender_subtype"]
    missing_scopes = non_empty_scopes(missing)
    extra_scopes = non_empty_scopes(extra)
    common_scopes = missing_scopes.intersection(extra_scopes)
    all_scopes = missing_scopes.union(extra_scopes)
    has_root = bool(all_scopes.intersection(ROOT_SCOPES))
    missing_root_scopes = missing_scopes.intersection(ROOT_SCOPES)
    extra_root_scopes = extra_scopes.intersection(ROOT_SCOPES)
    missing_non_root_scopes = missing_scopes - ROOT_SCOPES
    extra_non_root_scopes = extra_scopes - ROOT_SCOPES
    has_relationship = bool(row.get("relationship_hint"))
    missing_pronouns = [token for token in missing if is_pronoun_token(token)]
    extra_pronouns = [token for token in extra if is_pronoun_token(token)]
    missing_articles = [token for token in missing if is_article_token(token)]
    extra_articles = [token for token in extra if is_article_token(token)]
    extra_non_pronoun_tokens = [token for token in extra if not is_pronoun_token(token)]
    reasons: list[str] = []

    if common_scopes:
        reasons.append("same_scope_overlap:" + ",".join(sorted(common_scopes)))
    if missing_scopes - extra_scopes:
        reasons.append("missing_only_scope:" + ",".join(sorted(missing_scopes - extra_scopes)))
    if extra_scopes - missing_scopes:
        reasons.append("extra_only_scope:" + ",".join(sorted(extra_scopes - missing_scopes)))
    if has_root:
        reasons.append("root_or_player_scope")
    if has_relationship:
        reasons.append("relationship_phrase_hint")
    if len(missing) > 1:
        reasons.append(f"multiple_missing_tokens:{len(missing)}")
    if len(extra_pronouns) > 1:
        reasons.append(f"multiple_extra_pronouns:{len(extra_pronouns)}")

    if has_root:
        if all_scopes <= ROOT_SCOPES and common_scopes:
            return (
                "root_self_same_scope_pronoun_context",
                "experimental",
                "study_root_self_pronoun_boundary",
                reasons,
            )
        if not missing_scopes and extra_root_scopes and extra_non_root_scopes:
            return (
                "root_extra_only_multi_actor_context",
                "experimental",
                "study_added_root_and_actor_tokens_separately",
                reasons,
            )
        if missing_scopes <= ROOT_SCOPES and extra_non_root_scopes and not common_scopes:
            return (
                "root_to_actor_cross_scope_context",
                "experimental",
                "study_root_to_actor_transfer_boundary",
                reasons,
            )
        if extra_scopes <= ROOT_SCOPES and missing_non_root_scopes and not common_scopes:
            return (
                "actor_to_root_cross_scope_context",
                "experimental",
                "study_actor_to_root_transfer_boundary",
                reasons,
            )
        if common_scopes - ROOT_SCOPES:
            return (
                "root_with_actor_overlap_context",
                "experimental",
                "split_root_self_edits_from_actor_overlap",
                reasons,
            )
        if common_scopes.intersection(ROOT_SCOPES) and (missing_non_root_scopes or extra_non_root_scopes):
            return (
                "root_plus_actor_multi_scope_context",
                "experimental",
                "split_root_overlap_from_extra_actor_edits",
                reasons,
            )
        return (
            "root_perspective_context",
            "experimental",
            "needs_root_perspective_boundary",
            reasons,
        )

    if has_relationship and not common_scopes:
        return (
            "relationship_cross_scope_context",
            "experimental",
            "needs_relationship_boundary",
            reasons,
        )

    if subtype == "gender_custom_form_swap":
        if common_scopes and len(all_scopes) == len(common_scopes):
            return (
                "same_scope_gender_suffix_form_swap",
                "candidate",
                "collect_positive_negative_evidence",
                reasons,
            )
        return (
            "cross_scope_gender_agreement_swap",
            "boundary",
            "keep_manual_exception_until_more_evidence",
            reasons,
        )

    if subtype == "pronoun_form_swap":
        extra_pronoun_methods = {token_method(token) for token in extra_pronouns}
        missing_article_methods = {token_method(token) for token in missing_articles}
        if common_scopes and len(all_scopes) == len(common_scopes):
            if len(all_scopes) > 1 or extra_non_pronoun_tokens:
                return (
                    "same_scope_multi_scope_pronoun_gender_rewrite_context",
                    "experimental",
                    "split_multi_scope_or_gender_suffix_rewrite_before_promotion",
                    reasons,
                )
            if (
                missing_articles
                and extra_pronouns
                and missing_article_methods
                <= {
                    "Custom('ES_ElLa')",
                    "Custom('ES_ElElla')",
                    "Custom('ES_DelDela')",
                    "Custom('ES_AlAla')",
                }
                and extra_pronoun_methods <= {"GetHerHim"}
            ):
                return (
                    "same_scope_article_subject_to_object_pronoun_context",
                    "candidate",
                    "collect_positive_negative_evidence",
                    reasons,
                )
            return (
                "same_scope_pronoun_method_swap",
                "candidate",
                "collect_positive_negative_evidence",
                reasons,
            )
        if missing_articles and extra_pronouns and common_scopes:
            if (missing_scopes - extra_scopes) or (extra_scopes - missing_scopes):
                return (
                    "mixed_scope_article_to_pronoun_context",
                    "experimental",
                    "split_scope_boundary_before_promotion",
                    reasons,
                )
            return (
                "same_scope_article_to_pronoun_swap",
                "candidate",
                "collect_positive_negative_evidence",
                reasons,
            )
        return (
            "cross_scope_pronoun_transfer",
            "boundary",
            "keep_manual_exception_until_more_evidence",
            reasons,
        )

    if subtype == "pronoun_added_with_gender_context":
        extra_pronoun_methods = {token_method(token) for token in extra_pronouns}
        missing_article_methods = {token_method(token) for token in missing_articles}
        if not missing and extra:
            return (
                "extra_only_english_pronoun_context",
                "candidate",
                "collect_positive_negative_evidence",
                reasons,
            )
        if missing_articles and extra_pronouns and common_scopes:
            if (missing_scopes - extra_scopes) or (extra_scopes - missing_scopes):
                return (
                    "mixed_scope_article_to_pronoun_context",
                    "experimental",
                    "split_scope_boundary_before_promotion",
                    reasons,
                )
            if extra_non_pronoun_tokens:
                return (
                    "same_scope_article_pronoun_with_gender_suffix_context",
                    "experimental",
                    "split_article_pronoun_from_gender_suffix_edits",
                    reasons,
                )
            if missing_article_methods <= {"Custom('ES_LoLa')"} and extra_pronoun_methods <= {
                "GetHerHim",
                "GetHerHis",
                "GetSheHe",
            }:
                return (
                    "same_scope_object_article_to_pronoun_context",
                    "candidate",
                    "collect_positive_negative_evidence",
                    reasons,
                )
            if missing_article_methods <= {
                "Custom('ES_ElLa')",
                "Custom('ES_ElElla')",
                "Custom('ES_DelDela')",
                "Custom('ES_AlAla')",
            } and extra_pronoun_methods <= {"GetSheHe"}:
                return (
                    "same_scope_article_to_subject_pronoun_context",
                    "candidate",
                    "collect_positive_negative_evidence",
                    reasons,
                )
            return (
                "same_scope_article_to_mixed_pronoun_context",
                "experimental",
                "split_by_article_and_pronoun_method",
                reasons,
            )
        if common_scopes and len(all_scopes) == len(common_scopes):
            if extra_pronouns and extra_pronoun_methods <= {"GetSheHe"} and not extra_non_pronoun_tokens:
                missing_methods = [token_method(token) for token in missing]
                missing_method_set = set(missing_methods)
                if len(missing) == 1 and missing_method_set <= {"Custom('ES_OA')"}:
                    return (
                        "same_scope_single_gender_suffix_to_subject_pronoun",
                        "candidate",
                        "collect_positive_negative_evidence",
                        reasons,
                    )
                if missing_method_set <= {"Custom('ES_OA')"}:
                    return (
                        "same_scope_repeated_gender_suffix_to_subject_pronoun_context",
                        "experimental",
                        "split_repeated_gender_suffix_rewrite_before_promotion",
                        reasons,
                    )
                if "Custom('ES_XA')" in missing_method_set or row.get("neutral_hint"):
                    return (
                        "same_scope_neutral_noun_gender_suffix_to_subject_pronoun_context",
                        "experimental",
                        "split_neutral_noun_and_gender_suffix_rewrite_before_promotion",
                        reasons,
                    )
                return (
                    "same_scope_gender_to_subject_pronoun",
                    "experimental",
                    "split_subject_pronoun_pattern_before_promotion",
                    reasons,
                )
            if extra_pronouns and extra_pronoun_methods <= {"GetHerHim", "GetHerHis"}:
                return (
                    "same_scope_gender_to_object_or_possessive_pronoun",
                    "candidate",
                    "collect_positive_negative_evidence",
                    reasons,
                )
            return (
                "same_scope_mixed_gender_pronoun_context",
                "experimental",
                "split_gender_and_pronoun_edits_before_promotion",
                reasons,
            )
        if common_scopes:
            return (
                "mixed_same_and_cross_scope_context",
                "experimental",
                "split_into_independent_actor_edits",
                reasons,
            )
        return (
            "multi_actor_independent_context_rewrite",
            "boundary",
            "keep_manual_exception_until_more_evidence",
            reasons,
        )

    return (
        "unclassified_gender_split",
        "boundary",
        "manual_review_required",
        reasons,
    )


def enrich_split_row(row: dict[str, Any]) -> dict[str, Any]:
    split_agent, maturity, next_action, reasons = split_agent_for(row)
    missing = row["missing_tokens"]
    extra = row["extra_tokens"]
    return {
        **row,
        "split_agent": split_agent,
        "split_maturity": maturity,
        "split_next_action": next_action,
        "split_reasons": reasons,
        "missing_scopes": sorted(non_empty_scopes(missing)),
        "extra_scopes": sorted(non_empty_scopes(extra)),
        "missing_methods": [token_method(token) for token in missing],
        "extra_methods": [token_method(token) for token in extra],
        "apply_allowed": 0,
    }


def write_outputs(
    settings: dict[str, Any],
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    pending_apply_only: bool,
    skip_apply_approved: bool,
    split_agent_filter: list[str],
    split_maturity_filter: list[str],
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filter_parts: list[str] = []
    if split_agent_filter:
        filter_parts.append("agent_" + "_".join(slugify(item) for item in split_agent_filter[:2]))
    if split_maturity_filter:
        filter_parts.append("maturity_" + "_".join(slugify(item) for item in split_maturity_filter[:2]))
    filter_slug = "_" + "_".join(filter_parts) if filter_parts else ""
    base = reports_dir / f"{timestamp}_segment_token_gender_split_subpolicy{filter_slug}"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "policy_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "gender_subtype",
        "split_agent",
        "split_maturity",
        "split_next_action",
        "split_reasons",
        "missing_scopes",
        "extra_scopes",
        "missing_methods",
        "extra_methods",
        "missing_tokens",
        "extra_tokens",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key
                    in {
                        "split_reasons",
                        "missing_scopes",
                        "extra_scopes",
                        "missing_methods",
                        "extra_methods",
                        "missing_tokens",
                        "extra_tokens",
                    }
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    subtype_counts = Counter(row["gender_subtype"] for row in rows)
    agent_counts = Counter(row["split_agent"] for row in rows)
    maturity_counts = Counter(row["split_maturity"] for row in rows)
    next_action_counts = Counter(row["split_next_action"] for row in rows)
    missing_method_counts = Counter(method for row in rows for method in row["missing_methods"])
    extra_method_counts = Counter(method for row in rows for method in row["extra_methods"])

    lines = [
        "Segment token gender split subpolicy diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Pending apply only: {pending_apply_only}",
        f"Skip apply-approved: {skip_apply_approved}",
        f"Split agent filter: {', '.join(split_agent_filter) if split_agent_filter else 'none'}",
        f"Split maturity filter: {', '.join(split_maturity_filter) if split_maturity_filter else 'none'}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Candidate subneuron rows: {maturity_counts['candidate']}",
        f"- Experimental subneuron rows: {maturity_counts['experimental']}",
        f"- Boundary/manual rows: {maturity_counts['boundary']}",
        "- Apply allowed: 0",
        "",
        "Gender subtypes:",
        *[f"- {key}: {value}" for key, value in subtype_counts.most_common()],
        "",
        "Split agents:",
        *[f"- {key}: {value}" for key, value in agent_counts.most_common()],
        "",
        "Maturity:",
        *[f"- {key}: {value}" for key, value in maturity_counts.most_common()],
        "",
        "Next actions:",
        *[f"- {key}: {value}" for key, value in next_action_counts.most_common()],
        "",
        "Top missing methods:",
        *[f"- {key}: {value}" for key, value in missing_method_counts.most_common(12)],
        "",
        "Top extra methods:",
        *[f"- {key}: {value}" for key, value in extra_method_counts.most_common(12)],
        "",
        "Priority samples:",
    ]
    for row in rows[:60]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['split_agent']} | {row['split_maturity']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  subtype: {row['gender_subtype']}",
                f"  next: {row['split_next_action']}",
                f"  scopes missing/extra: {json.dumps(row['missing_scopes'], ensure_ascii=False)} -> {json.dumps(row['extra_scopes'], ensure_ascii=False)}",
                f"  methods missing/extra: {json.dumps(row['missing_methods'], ensure_ascii=False)} -> {json.dumps(row['extra_methods'], ensure_ascii=False)}",
                f"  reasons: {', '.join(row['split_reasons']) or 'none'}",
                f"  text: {short(row.get('confirmed_text'), 260)}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This is a learning diagnostic only; it does not approve apply and does not write output.",
            "- Candidate agents are narrow enough for positive/negative evidence queues.",
            "- Boundary rows are valid manual/contextual edits unless future evidence forms a safer rule.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(
    *,
    policy_run_id: int | None = None,
    pending_apply_only: bool = True,
    skip_apply_approved: bool = True,
    undecided_only: bool = False,
    limit: int | None = None,
    split_agents_value: str | None = None,
    split_maturity_value: str | None = None,
) -> None:
    settings = db.load_settings()
    split_agent_filter = split_csv(split_agents_value)
    split_maturity_filter = split_csv(split_maturity_value)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        classified = [
            classify_row(row)
            for row in fetch_rows(
                conn,
                policy_run_id=selected_policy_run_id,
                pending_apply_only=pending_apply_only,
                skip_apply_approved=skip_apply_approved,
                undecided_only=undecided_only,
                limit=None,
            )
        ]
    rows = [enrich_split_row(row) for row in classified if row["subpolicy_status"] == "needs_split_review"]
    if split_agent_filter:
        wanted = set(split_agent_filter)
        rows = [row for row in rows if row["split_agent"] in wanted]
    if split_maturity_filter:
        wanted = set(split_maturity_filter)
        rows = [row for row in rows if row["split_maturity"] in wanted]
    if limit is not None:
        rows = rows[:limit]

    txt_path, csv_path, jsonl_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=rows,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
        split_agent_filter=split_agent_filter,
        split_maturity_filter=split_maturity_filter,
    )

    agent_counts = Counter(row["split_agent"] for row in rows)
    maturity_counts = Counter(row["split_maturity"] for row in rows)
    print("[segment_token_gender_split_subpolicy] Diagnostic generated")
    print(f"[segment_token_gender_split_subpolicy] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_subpolicy] Policy run id: {selected_policy_run_id}")
    print(
        "[segment_token_gender_split_subpolicy] Split agent filter: "
        f"{', '.join(split_agent_filter) if split_agent_filter else 'none'}"
    )
    print(
        "[segment_token_gender_split_subpolicy] Split maturity filter: "
        f"{', '.join(split_maturity_filter) if split_maturity_filter else 'none'}"
    )
    print(f"[segment_token_gender_split_subpolicy] Rows inspected: {len(rows)}")
    for key, value in agent_counts.most_common():
        print(f"[segment_token_gender_split_subpolicy] split_agent {key}: {value}")
    for key, value in maturity_counts.most_common():
        print(f"[segment_token_gender_split_subpolicy] maturity {key}: {value}")
    print("[segment_token_gender_split_subpolicy] Apply allowed: 0")
    print(f"[segment_token_gender_split_subpolicy] Report: {txt_path}")
    print(f"[segment_token_gender_split_subpolicy] CSV: {csv_path}")
    print(f"[segment_token_gender_split_subpolicy] JSONL: {jsonl_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split gender-token review rows into narrower subpolicy agents.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--include-apply-approved", action="store_true")
    parser.add_argument("--undecided-only", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--split-agents", default=None)
    parser.add_argument("--split-maturity", default=None)
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        pending_apply_only=not args.include_all,
        skip_apply_approved=not args.include_apply_approved,
        undecided_only=args.undecided_only,
        limit=args.limit,
        split_agents_value=args.split_agents,
        split_maturity_value=args.split_maturity,
    )
