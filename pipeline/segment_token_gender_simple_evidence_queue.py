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
from segment_token_gender_subpolicy import classify_row, fetch_rows
from segment_token_policy_review_queue import latest_policy_run_id, split_csv


RULE_VERSION = "segment_token_gender_simple_evidence_queue_v1"
DEFAULT_STATUSES = "subpolicy_candidate_review,neutralization_candidate_review"
DEFAULT_MATURITY = "candidate"
EVIDENCE_LABELS = (
    "positive_evidence",
    "negative_boundary",
    "needs_more_context",
    "manual_exception_only",
)


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


def non_empty_scopes(tokens: list[str]) -> set[str]:
    return {token_scope(token) for token in tokens if token_scope(token)}


def hypothesis_for(simple_agent: str) -> str:
    known = {
        "simple_single_gender_suffix_added": (
            "Test whether one added ES_OA suffix is a safe PT-BR agreement improvement "
            "for an adjective/noun already present in the confirmed text."
        ),
        "simple_repeated_gender_suffix_added": (
            "Test repeated ES_OA additions separately because repeated agreement edits may hide "
            "larger wording rewrites."
        ),
        "simple_indefinite_article_added": (
            "Test whether added ES_XA article/gender marker is a safe PT-BR article agreement improvement."
        ),
        "simple_article_or_preposition_added": (
            "Test whether added article/preposition gender markers are safe grammatical fixes, not referent changes."
        ),
        "simple_single_object_pronoun_added": (
            "Test whether one added GetHerHim object pronoun aligns with English/PT-BR fluency and preserves referent."
        ),
        "simple_single_possessive_pronoun_added": (
            "Test whether one added GetHerHis possessive pronoun aligns with English/PT-BR fluency and preserves referent."
        ),
        "simple_subject_pronoun_added": (
            "Test whether one added GetSheHe subject pronoun is a safe PT-BR fluency improvement."
        ),
        "simple_person_noun_pronoun_added": (
            "Test whether added GetWomanMan is a safe person-noun fluency improvement."
        ),
        "simple_multi_pronoun_added_context": (
            "Study multiple added pronouns together; this can be valid, but should stay narrower than a single-pronoun rule."
        ),
        "simple_neutral_noun_gender_removed": (
            "Test whether removing ES_OA/ES_XA is safe when PT-BR uses a neutral noun such as crianca/pessoa/alguem."
        ),
        "simple_gender_removed_context": (
            "Study removed gender markers separately; these are often valid handcrafted neutralizations."
        ),
        "simple_article_to_pronoun_context": (
            "Study article/preposition token removal plus pronoun addition as a contextual rewrite before promotion."
        ),
    }
    return known.get(
        simple_agent,
        "Collect positive and negative evidence before this simple gender-token agent can become a guarded policy.",
    )


def simple_agent_for(row: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    missing = row["missing_tokens"]
    extra = row["extra_tokens"]
    missing_methods = [token_method(token) for token in missing]
    extra_methods = [token_method(token) for token in extra]
    missing_method_set = set(missing_methods)
    extra_method_set = set(extra_methods)
    missing_scopes = non_empty_scopes(missing)
    extra_scopes = non_empty_scopes(extra)
    common_scopes = missing_scopes.intersection(extra_scopes)
    reasons: list[str] = []
    if missing_scopes:
        reasons.append("missing_scope:" + ",".join(sorted(missing_scopes)))
    if extra_scopes:
        reasons.append("extra_scope:" + ",".join(sorted(extra_scopes)))
    if common_scopes:
        reasons.append("same_scope_overlap:" + ",".join(sorted(common_scopes)))
    if len(missing) > 1:
        reasons.append(f"multiple_missing_tokens:{len(missing)}")
    if len(extra) > 1:
        reasons.append(f"multiple_extra_tokens:{len(extra)}")

    subtype = row["gender_subtype"]
    if subtype == "gender_custom_added":
        if extra_method_set <= {"Custom('ES_OA')"}:
            if len(extra) == 1:
                return (
                    "simple_single_gender_suffix_added",
                    DEFAULT_MATURITY,
                    "collect_positive_negative_evidence",
                    reasons,
                )
            return (
                "simple_repeated_gender_suffix_added",
                "experimental",
                "split_repeated_gender_suffix_edits",
                reasons,
            )
        if extra_method_set <= {"Custom('ES_XA')"}:
            return (
                "simple_indefinite_article_added",
                DEFAULT_MATURITY,
                "collect_positive_negative_evidence",
                reasons,
            )
        if extra_method_set <= {"Custom('ES_EA')", "Custom('ES_ElLa')", "Custom('ES_DelDela')"}:
            return (
                "simple_article_or_preposition_added",
                "experimental",
                "collect_article_preposition_boundaries",
                reasons,
            )
        return (
            "simple_gender_custom_added_context",
            "experimental",
            "split_gender_marker_type_before_promotion",
            reasons,
        )

    if subtype == "pronoun_added_for_pt_fluency":
        if missing and missing_method_set & {"Custom('ES_ElElla')", "Custom('ES_ElLa')", "Custom('ES_DelDela')"}:
            return (
                "simple_article_to_pronoun_context",
                "experimental",
                "split_article_to_pronoun_context_before_promotion",
                reasons,
            )
        if extra_method_set <= {"GetHerHim"} and len(extra) == 1:
            return (
                "simple_single_object_pronoun_added",
                DEFAULT_MATURITY,
                "collect_positive_negative_evidence",
                reasons,
            )
        if extra_method_set <= {"GetHerHis"} and len(extra) == 1:
            return (
                "simple_single_possessive_pronoun_added",
                DEFAULT_MATURITY,
                "collect_positive_negative_evidence",
                reasons,
            )
        if extra_method_set <= {"GetSheHe"} and len(extra) == 1:
            return (
                "simple_subject_pronoun_added",
                DEFAULT_MATURITY,
                "collect_positive_negative_evidence",
                reasons,
            )
        if extra_method_set <= {"GetWomanMan"}:
            return (
                "simple_person_noun_pronoun_added",
                "experimental",
                "collect_person_noun_pronoun_evidence",
                reasons,
            )
        return (
            "simple_multi_pronoun_added_context",
            "experimental",
            "split_multi_pronoun_context_before_promotion",
            reasons,
        )

    if subtype == "gender_custom_removed_or_literalized":
        if row.get("neutral_hint"):
            return (
                "simple_neutral_noun_gender_removed",
                DEFAULT_MATURITY,
                "collect_positive_negative_evidence",
                reasons,
            )
        return (
            "simple_gender_removed_context",
            "experimental",
            "collect_removed_gender_marker_boundaries",
            reasons,
        )

    return (
        "simple_gender_unclassified_context",
        "boundary",
        "manual_review_required",
        reasons,
    )


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
    statuses_value: str | None,
    simple_agents_value: str | None,
    maturity_value: str | None,
    limit: int | None,
) -> tuple[int, list[dict[str, Any]], list[str], list[str], list[str]]:
    settings = db.load_settings()
    status_filter = split_csv(statuses_value or DEFAULT_STATUSES)
    simple_agent_filter = split_csv(simple_agents_value)
    maturity_filter = split_csv(maturity_value)
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

    rows: list[dict[str, Any]] = []
    wanted_statuses = set(status_filter)
    for raw_row in raw_rows:
        row = classify_row(raw_row)
        if wanted_statuses and row["subpolicy_status"] not in wanted_statuses:
            continue
        simple_agent, maturity, next_action, reasons = simple_agent_for(row)
        missing = row["missing_tokens"]
        extra = row["extra_tokens"]
        enriched = {
            **row,
            "split_agent": simple_agent,
            "split_maturity": maturity,
            "split_next_action": next_action,
            "split_reasons": reasons,
            "missing_scopes": sorted(non_empty_scopes(missing)),
            "extra_scopes": sorted(non_empty_scopes(extra)),
            "missing_methods": [token_method(token) for token in missing],
            "extra_methods": [token_method(token) for token in extra],
            "learning_only": 1,
            "apply_allowed": 0,
        }
        enriched["method_signature"] = method_signature(enriched)
        enriched["evidence_hypothesis"] = hypothesis_for(simple_agent)
        enriched["evidence_default_label"] = "needs_more_context"
        enriched["evidence_label_options"] = list(EVIDENCE_LABELS)
        rows.append(enriched)

    if simple_agent_filter:
        wanted = set(simple_agent_filter)
        rows = [row for row in rows if row["split_agent"] in wanted]
    if maturity_filter:
        wanted = set(maturity_filter)
        rows = [row for row in rows if row["split_maturity"] in wanted]

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
    return selected_policy_run_id, rows, status_filter, simple_agent_filter, maturity_filter


def write_outputs(
    settings: dict[str, Any],
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    pending_apply_only: bool,
    skip_apply_approved: bool,
    undecided_only: bool,
    status_filter: list[str],
    simple_agent_filter: list[str],
    maturity_filter: list[str],
    started_at: datetime,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filter_parts: list[str] = []
    if simple_agent_filter:
        filter_parts.append("agent_" + "_".join(slugify(item) for item in simple_agent_filter[:2]))
    if maturity_filter:
        filter_parts.append("maturity_" + "_".join(slugify(item) for item in maturity_filter[:2]))
    filter_slug = "_" + "_".join(filter_parts) if filter_parts else ""
    base = reports_dir / f"{timestamp}_segment_token_gender_simple_evidence_queue{filter_slug}"
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
        "subpolicy_status",
        "split_agent",
        "split_maturity",
        "split_next_action",
        "method_signature",
        "evidence_hypothesis",
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
                            "positive_evidence means this exact simple pattern is safe evidence; "
                            "manual_exception_only means valid text but not reusable policy."
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    agent_counts = Counter(row["split_agent"] for row in rows)
    maturity_counts = Counter(row["split_maturity"] for row in rows)
    subtype_counts = Counter(row["gender_subtype"] for row in rows)
    method_counts = Counter(row["method_signature"] for row in rows)
    lines = [
        "Segment token gender simple evidence queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Pending apply only: {pending_apply_only}",
        f"Skip apply-approved: {skip_apply_approved}",
        f"Undecided only: {undecided_only}",
        f"Status filter: {', '.join(status_filter) if status_filter else 'none'}",
        f"Simple agent filter: {', '.join(simple_agent_filter) if simple_agent_filter else 'none'}",
        f"Maturity filter: {', '.join(maturity_filter) if maturity_filter else 'none'}",
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
        "Simple agents:",
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
    for simple_agent in sorted(agent_counts):
        lines.append(f"- {simple_agent}: {hypothesis_for(simple_agent)}")
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
            "- Use positive_evidence only when the exact simple-agent hypothesis holds.",
            "- Use needs_more_context for text quality issues even when the token idea looks plausible.",
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
    statuses_value: str | None = DEFAULT_STATUSES,
    simple_agents_value: str | None = None,
    maturity_value: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    selected_policy_run_id, rows, status_filter, simple_agent_filter, maturity_filter = build_rows(
        policy_run_id=policy_run_id,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
        undecided_only=undecided_only,
        statuses_value=statuses_value,
        simple_agents_value=simple_agents_value,
        maturity_value=maturity_value,
        limit=limit,
    )
    txt_path, csv_path, jsonl_path, template_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=rows,
        pending_apply_only=pending_apply_only,
        skip_apply_approved=skip_apply_approved,
        undecided_only=undecided_only,
        status_filter=status_filter,
        simple_agent_filter=simple_agent_filter,
        maturity_filter=maturity_filter,
        started_at=started_at,
    )
    agent_counts = Counter(row["split_agent"] for row in rows)
    print("[segment_token_gender_simple_evidence_queue] Queue generated")
    print(f"[segment_token_gender_simple_evidence_queue] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_simple_evidence_queue] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_gender_simple_evidence_queue] Rows selected: {len(rows)}")
    for key, value in agent_counts.most_common():
        print(f"[segment_token_gender_simple_evidence_queue] simple_agent {key}: {value}")
    print("[segment_token_gender_simple_evidence_queue] Apply allowed: 0")
    print(f"[segment_token_gender_simple_evidence_queue] Report: {txt_path}")
    print(f"[segment_token_gender_simple_evidence_queue] CSV: {csv_path}")
    print(f"[segment_token_gender_simple_evidence_queue] JSONL: {jsonl_path}")
    print(f"[segment_token_gender_simple_evidence_queue] Decisions template: {template_path}")
    return {
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(template_path),
        "policy_run_id": selected_policy_run_id,
        "rows_selected": len(rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate learning-only evidence queues for simple gender-token candidates.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--include-apply-approved", action="store_true")
    parser.add_argument("--undecided-only", action="store_true")
    parser.add_argument("--statuses", default=DEFAULT_STATUSES)
    parser.add_argument("--simple-agents", default=None)
    parser.add_argument("--maturity", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        pending_apply_only=not args.include_all,
        skip_apply_approved=not args.include_apply_approved,
        undecided_only=args.undecided_only,
        statuses_value=args.statuses,
        simple_agents_value=args.simple_agents,
        maturity_value=args.maturity,
        limit=args.limit,
    )
