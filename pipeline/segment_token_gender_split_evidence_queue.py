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
from segment_token_gender_split_subpolicy import enrich_split_row, slugify
from segment_token_gender_subpolicy import classify_row, fetch_rows
from segment_token_policy_review_queue import latest_policy_run_id, split_csv


RULE_VERSION = "segment_token_gender_split_evidence_queue_v1"
DEFAULT_SPLIT_AGENTS = (
    "same_scope_gender_to_subject_pronoun",
    "same_scope_object_article_to_pronoun_context",
)
DEFAULT_SPLIT_MATURITY = "candidate"
EVIDENCE_LABELS = (
    "positive_evidence",
    "negative_boundary",
    "needs_more_context",
    "manual_exception_only",
)


def hypothesis_for(split_agent: str) -> str:
    known = {
        "same_scope_gender_to_subject_pronoun": (
            "Test whether a same-scope PT-BR gender suffix token can be safely replaced by "
            "GetSheHe in fluent subject-pronoun wording without changing the CK3 referent."
        ),
        "same_scope_single_gender_suffix_to_subject_pronoun": (
            "Test whether one same-scope ES_OA suffix token can be safely replaced by one or more "
            "GetSheHe subject-pronoun references, with no broader noun/adjective rewrite."
        ),
        "same_scope_repeated_gender_suffix_to_subject_pronoun_context": (
            "Test repeated same-scope ES_OA suffix removals separately because they often include "
            "noun/adjective neutralization beyond a direct pronoun substitution."
        ),
        "same_scope_neutral_noun_gender_suffix_to_subject_pronoun_context": (
            "Test neutral noun rewrites separately because ES_XA/ES_OA removal plus GetSheHe may be "
            "a larger semantic/localization rewrite rather than a reusable direct token policy."
        ),
        "same_scope_object_article_to_pronoun_context": (
            "Test whether a same-scope ES_LoLa article/object token can be safely replaced by "
            "GetHerHim/GetHerHis/GetSheHe when PT-BR fluency requires an object, possessive, "
            "or subject pronoun and the referent remains unambiguous."
        ),
        "same_scope_pronoun_method_swap": (
            "Test whether a same-scope pronoun method swap keeps the same referent and grammatical role."
        ),
        "same_scope_article_subject_to_object_pronoun_context": (
            "Test whether a same-scope article/subject-pronoun construction can be safely rewritten "
            "as an object-pronoun construction when English and PT-BR grammar both require the same "
            "object referent."
        ),
        "same_scope_multi_scope_pronoun_gender_rewrite_context": (
            "Study multi-scope pronoun/gender rewrites separately because they combine grammatical "
            "role changes with gender suffix edits and should not teach a simple method-swap rule."
        ),
        "root_self_same_scope_pronoun_context": (
            "Study ROOT.Char/GetPlayer self-reference token rewrites separately; these may be valid "
            "for first-person narration but need strict boundaries before any reusable policy."
        ),
        "root_extra_only_multi_actor_context": (
            "Study rows where PT-BR adds ROOT and actor gender/pronoun tokens without source-side "
            "token removals; these are likely handcrafted narration rewrites, not direct token swaps."
        ),
        "root_to_actor_cross_scope_context": (
            "Study rows where a ROOT/GetPlayer token is replaced by another actor's token; this is "
            "a high-risk referent transfer boundary."
        ),
        "actor_to_root_cross_scope_context": (
            "Study rows where another actor's token is replaced by ROOT/GetPlayer; this is a high-risk "
            "narrator-perspective boundary."
        ),
        "root_with_actor_overlap_context": (
            "Study rows where an actor scope overlaps across missing/extra tokens while ROOT also changes; "
            "split actor edits from narrator-self edits before promotion."
        ),
        "root_plus_actor_multi_scope_context": (
            "Study rows where ROOT overlaps and extra actor tokens are added; these are multi-scope "
            "narrative rewrites and should remain experimental until split further."
        ),
        "same_scope_article_to_subject_pronoun_context": (
            "Test whether a same-scope article token can become GetSheHe subject-pronoun wording safely."
        ),
        "extra_only_english_pronoun_context": (
            "Test whether an added English-side pronoun token is a safe PT-BR fluency improvement."
        ),
        "same_scope_gender_to_object_or_possessive_pronoun": (
            "Test whether a same-scope gender suffix token can become object/possessive pronoun wording safely."
        ),
    }
    return known.get(
        split_agent,
        "Collect positive and negative evidence before this gender-token split agent can become a guarded policy.",
    )


def default_agents(value: str | None) -> list[str]:
    if value:
        return split_csv(value)
    return list(DEFAULT_SPLIT_AGENTS)


def method_signature(row: dict[str, Any]) -> str:
    missing = ",".join(row.get("missing_methods") or [])
    extra = ",".join(row.get("extra_methods") or [])
    return f"{missing} -> {extra}"


def build_rows(
    *,
    policy_run_id: int | None,
    pending_apply_only: bool,
    skip_apply_approved: bool,
    undecided_only: bool,
    limit: int | None,
    split_agents_value: str | None,
    split_maturity_value: str | None,
) -> tuple[int, list[dict[str, Any]], list[str], list[str]]:
    settings = db.load_settings()
    split_agent_filter = default_agents(split_agents_value)
    split_maturity_filter = split_csv(split_maturity_value or DEFAULT_SPLIT_MATURITY)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        raw_rows = fetch_rows(
            conn,
            policy_run_id=selected_policy_run_id,
            pending_apply_only=pending_apply_only,
            skip_apply_approved=skip_apply_approved,
            undecided_only=undecided_only,
            limit=None,
        )

    classified = [classify_row(row) for row in raw_rows]
    rows = [enrich_split_row(row) for row in classified if row["subpolicy_status"] == "needs_split_review"]
    if split_agent_filter:
        wanted_agents = set(split_agent_filter)
        rows = [row for row in rows if row["split_agent"] in wanted_agents]
    if split_maturity_filter:
        wanted_maturity = set(split_maturity_filter)
        rows = [row for row in rows if row["split_maturity"] in wanted_maturity]

    for row in rows:
        row["evidence_hypothesis"] = hypothesis_for(row["split_agent"])
        row["evidence_default_label"] = "needs_more_context"
        row["evidence_label_options"] = list(EVIDENCE_LABELS)
        row["method_signature"] = method_signature(row)
        row["learning_only"] = 1
        row["apply_allowed"] = 0

    rows.sort(
        key=lambda row: (
            row["split_agent"],
            row["method_signature"],
            row["relative_path"],
            int(row.get("source_line_number") or 0),
            row["source_key"],
        )
    )
    if limit is not None:
        rows = rows[:limit]
    return selected_policy_run_id, rows, split_agent_filter, split_maturity_filter


def write_outputs(
    settings: dict[str, Any],
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    pending_apply_only: bool,
    skip_apply_approved: bool,
    undecided_only: bool,
    split_agent_filter: list[str],
    split_maturity_filter: list[str],
    started_at: datetime,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filter_parts: list[str] = []
    if split_agent_filter:
        filter_parts.append("agent_" + "_".join(slugify(item) for item in split_agent_filter[:2]))
    if split_maturity_filter:
        filter_parts.append("maturity_" + "_".join(slugify(item) for item in split_maturity_filter[:2]))
    filter_slug = "_" + "_".join(filter_parts) if filter_parts else ""
    base = reports_dir / f"{timestamp}_segment_token_gender_split_evidence_queue{filter_slug}"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    template_path = base.with_name(base.name + "_decisions_template").with_suffix(".jsonl")

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
        "method_signature",
        "evidence_hypothesis",
        "evidence_default_label",
        "missing_scopes",
        "extra_scopes",
        "missing_methods",
        "extra_methods",
        "missing_tokens",
        "extra_tokens",
        "confirmed_text",
        "english_text",
        "spanish_text",
        "old_text",
        "output_text",
    ]
    json_fields = {
        "missing_scopes",
        "extra_scopes",
        "missing_methods",
        "extra_methods",
        "missing_tokens",
        "extra_tokens",
    }
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row.get(key), ensure_ascii=False) if key in json_fields else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    with template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "segment_id": row["segment_id"],
                        "split_agent": row["split_agent"],
                        "method_signature": row["method_signature"],
                        "evidence_label": "needs_more_context",
                        "evidence_options": list(EVIDENCE_LABELS),
                        "corrected_text": "",
                        "reviewer": "",
                        "notes": (
                            f"{RULE_VERSION}; learning_only=1; apply_allowed=0; "
                            f"hypothesis={row['evidence_hypothesis']}; "
                            "positive_evidence means this exact narrow pattern is safe evidence; "
                            "negative_boundary means it should stay out of a reusable rule."
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    agent_counts = Counter(row["split_agent"] for row in rows)
    maturity_counts = Counter(row["split_maturity"] for row in rows)
    method_counts = Counter(row["method_signature"] for row in rows)
    subtype_counts = Counter(row["gender_subtype"] for row in rows)
    lines = [
        "Segment token gender split evidence queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Pending apply only: {pending_apply_only}",
        f"Skip apply-approved: {skip_apply_approved}",
        f"Undecided only: {undecided_only}",
        f"Split agent filter: {', '.join(split_agent_filter) if split_agent_filter else 'none'}",
        f"Split maturity filter: {', '.join(split_maturity_filter) if split_maturity_filter else 'none'}",
        "",
        "Safety contract:",
        "- Learning-only queue: yes",
        "- Apply allowed: 0",
        "- Writes output/source: no",
        "- Ingests operational token decisions: no",
        "- Default evidence label: needs_more_context",
        "",
        "Summary:",
        f"- Rows selected: {len(rows)}",
        "",
        "Split agents:",
        *[f"- {key}: {value}" for key, value in agent_counts.most_common()],
        "",
        "Maturity:",
        *[f"- {key}: {value}" for key, value in maturity_counts.most_common()],
        "",
        "Gender subtypes:",
        *[f"- {key}: {value}" for key, value in subtype_counts.most_common()],
        "",
        "Method signatures:",
        *[f"- {key}: {value}" for key, value in method_counts.most_common()],
        "",
        "Hypotheses:",
    ]
    for split_agent in sorted(agent_counts):
        lines.append(f"- {split_agent}: {hypothesis_for(split_agent)}")
    lines.extend(["", "Review sample:"])
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['split_agent']} | {row['method_signature']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  missing: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  extra: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  confirmed: {short(row.get('confirmed_text'), 260)}",
                f"  english: {short(row.get('english_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- Use positive_evidence only when the exact split-agent hypothesis holds and the confirmed text is natural PT-BR.",
            "- Use negative_boundary when the row proves the agent would overgeneralize.",
            "- Use manual_exception_only for valid handcrafted exceptions that should not become automatic policy.",
            "- This report is for neural/symbolic learning; production remains handled by the production flow.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, template_path


def main(
    *,
    policy_run_id: int | None = None,
    pending_apply_only: bool = True,
    skip_apply_approved: bool = True,
    undecided_only: bool = False,
    limit: int | None = None,
    split_agents_value: str | None = None,
    split_maturity_value: str | None = DEFAULT_SPLIT_MATURITY,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    selected_policy_run_id, rows, split_agent_filter, split_maturity_filter = build_rows(
        policy_run_id=policy_run_id,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
        undecided_only=undecided_only,
        limit=limit,
        split_agents_value=split_agents_value,
        split_maturity_value=split_maturity_value,
    )
    txt_path, csv_path, jsonl_path, template_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=rows,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
        undecided_only=undecided_only,
        split_agent_filter=split_agent_filter,
        split_maturity_filter=split_maturity_filter,
        started_at=started_at,
    )
    agent_counts = Counter(row["split_agent"] for row in rows)
    method_counts = Counter(row["method_signature"] for row in rows)
    print("[segment_token_gender_split_evidence_queue] Queue generated")
    print(f"[segment_token_gender_split_evidence_queue] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_evidence_queue] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_gender_split_evidence_queue] Rows selected: {len(rows)}")
    for key, value in agent_counts.most_common():
        print(f"[segment_token_gender_split_evidence_queue] split_agent {key}: {value}")
    for key, value in method_counts.most_common():
        print(f"[segment_token_gender_split_evidence_queue] method {key}: {value}")
    print("[segment_token_gender_split_evidence_queue] Apply allowed: 0")
    print(f"[segment_token_gender_split_evidence_queue] Report: {txt_path}")
    print(f"[segment_token_gender_split_evidence_queue] CSV: {csv_path}")
    print(f"[segment_token_gender_split_evidence_queue] JSONL: {jsonl_path}")
    print(f"[segment_token_gender_split_evidence_queue] Decisions template: {template_path}")
    return {
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(template_path),
        "policy_run_id": selected_policy_run_id,
        "rows_selected": len(rows),
        "agent_counts": dict(agent_counts),
        "method_counts": dict(method_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build learning-only evidence queues for gender-token split agents.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--include-apply-approved", action="store_true")
    parser.add_argument("--undecided-only", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--split-agents", default=None)
    parser.add_argument("--split-maturity", default=DEFAULT_SPLIT_MATURITY)
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
