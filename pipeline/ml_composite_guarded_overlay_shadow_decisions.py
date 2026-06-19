from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_composite_subpolicy_promotion_audit import (
    NAME_METHODS,
    only_methods,
    same_tokens_after_style_strip,
    token_scopes,
)
from ml_composite_guarded_overlay_checkpoint import (
    DYNAMIC_SCOPE_TARGET_ACTION,
    TARGET_ACTIONS,
    TARGET_PROFILES,
    TOKEN_STYLE_TARGET_ACTION,
)


RULE_VERSION = "ml_composite_guarded_overlay_shadow_decisions_v1"
SOURCE_MODE = "guarded_overlay_shadow_validation"
SHADOW_QUEUE_KIND = "guarded_release_shadow"
REVIEWER = "codex_shadow_validation"
STRUCTURAL_ACCEPT_DECISIONS = {"accept_policy_candidate", "encoding_cleanup_required"}
PRONOUN_MISSING_TOKEN_RELEASE_RULES = {
    "single_scope_missing_gender_to_object_pronoun_english_aligned": {"GetHerHim"},
    "single_scope_missing_article_to_repeated_subject_pronoun_english_aligned": {"GetSheHe"},
    "single_scope_missing_article_to_subject_pronoun_english_aligned": {"GetSheHe"},
    "single_scope_missing_gender_suffix_to_subject_pronoun_clean_context": {"GetSheHe"},
}
GENDER_ARTICLE_RELEASE_ACTION = "dry_run_release_to_guarded_gender_article_removal_policy"
GENDER_FORM_SWAP_RELEASE_ACTION = "dry_run_release_to_guarded_gender_form_swap_policy"
PRONOUN_FORM_SWAP_RELEASE_ACTION = "dry_run_release_to_guarded_pronoun_form_swap_policy"
NAME_FORM_STYLE_RELEASE_ACTION = "dry_run_release_to_guarded_name_form_style_policy"
TUTORIAL_CONCEPT_RELEASE_ACTION = "dry_run_release_to_guarded_tutorial_concept_policy"


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def token_scope_method(token: str) -> tuple[str, str]:
    inner = token.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    inner = inner.split("|", 1)[0]
    if "." not in inner:
        return ("", inner)
    scope, method = inner.rsplit(".", 1)
    return scope, method


def has_style_modifier(tokens: list[str]) -> bool:
    return any("|" in token for token in tokens)


def token_contains_any(tokens: list[str], markers: set[str]) -> bool:
    return any(marker in token for token in tokens for marker in markers)


def latest_shadow_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_gate_queue_runs
        WHERE shadow_queue_kind = ?
          AND total_rows > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SHADOW_QUEUE_KIND,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No {SHADOW_QUEUE_KIND!r} queue run found.")
    return int(row["id"])


def fetch_queue_run(conn, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_gate_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run {queue_run_id} was not found.")
    return dict(row)


def fetch_queue_rows(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            qi.queue_run_id,
            qi.guarded_checkpoint_id,
            qi.overlay_run_id,
            qi.source_policy_run_id,
            qi.policy_item_id,
            qi.segment_id,
            qi.priority_bucket,
            qi.rule_key AS queue_rule_key,
            qi.hygiene_flags_json,
            qi.relative_path,
            qi.source_key,
            qi.source_line_number,
            oi.original_policy_bucket,
            oi.original_risk_level,
            oi.overlay_policy_bucket,
            oi.overlay_risk_level,
            oi.overlay_action,
            oi.overlay_agent_key,
            oi.apply_allowed,
            oi.decision AS overlay_decision,
            oi.rule_key AS overlay_rule_key,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            d.id AS existing_decision_id,
            d.decision AS existing_decision,
            d.approved_for_apply AS existing_approved_for_apply,
            d.notes AS existing_notes,
            sc.confirmed_text
        FROM ml_composite_gate_queue_items qi
        JOIN segment_token_policy_overlay_items oi
          ON oi.run_id = qi.overlay_run_id
         AND oi.source_policy_item_id = qi.policy_item_id
        JOIN segment_token_policy_items i ON i.id = qi.policy_item_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = qi.segment_id
        LEFT JOIN segment_token_policy_decisions d
          ON d.policy_run_id = qi.source_policy_run_id
         AND d.policy_item_id = qi.policy_item_id
        WHERE qi.queue_run_id = ?
        ORDER BY
            CASE qi.priority_bucket
                WHEN 'text_hygiene_shadow_review' THEN 0
                WHEN 'clean_token_shadow_review' THEN 1
                ELSE 9
            END,
            qi.rule_key,
            qi.relative_path,
            qi.source_line_number
        """,
        (queue_run_id,),
    ).fetchall()
    enriched: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        row["hygiene_flags"] = parse_json_list(row.get("hygiene_flags_json"))
        row["missing_tokens"] = parse_json_list(row.get("missing_tokens_json"))
        row["extra_tokens"] = parse_json_list(row.get("extra_tokens_json"))
        row["issue_flags"] = parse_json_list(row.get("issue_flags_json"))
        enriched.append(row)
    return enriched


def invariant_failures(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    profile = TARGET_PROFILES.get(row.get("overlay_action"))
    if row["overlay_action"] not in TARGET_ACTIONS or profile is None:
        failures.append("unsupported_overlay_action")
        return failures
    if row["original_policy_bucket"] != profile["original_bucket"]:
        failures.append("original_bucket_mismatch")
    if row["original_risk_level"] != profile["original_risk"]:
        failures.append("original_risk_mismatch")
    if row["overlay_policy_bucket"] != profile["target_bucket"]:
        failures.append("overlay_bucket_mismatch")
    if row["overlay_risk_level"] != profile["target_risk"]:
        failures.append("overlay_risk_mismatch")
    if row["overlay_agent_key"] != profile["target_agent"]:
        failures.append("overlay_agent_mismatch")
    if int(row.get("apply_allowed") or 0) != 0:
        failures.append("overlay_apply_allowed_not_zero")
    if row.get("overlay_decision") != "ready_for_guarded_policy_review":
        failures.append("overlay_decision_not_ready")
    if row.get("queue_rule_key") != row.get("overlay_rule_key"):
        failures.append("queue_rule_mismatch")
    allowed_missing_methods = PRONOUN_MISSING_TOKEN_RELEASE_RULES.get(row.get("queue_rule_key"))
    if row["overlay_action"] == GENDER_ARTICLE_RELEASE_ACTION:
        if len(row["missing_tokens"]) != 1:
            failures.append("gender_article_release_requires_one_missing_token")
        if not any("ES_DelDela" in token for token in row["missing_tokens"]):
            failures.append("gender_article_release_requires_es_deldela_missing_token")
        if row["extra_tokens"]:
            failures.append("gender_article_release_requires_no_extra_tokens")
    elif row["overlay_action"] == GENDER_FORM_SWAP_RELEASE_ACTION:
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if len(missing_tokens) != 1:
            failures.append("gender_form_swap_release_requires_one_missing_token")
        if len(extra_tokens) != 1:
            failures.append("gender_form_swap_release_requires_one_extra_token")
        if missing_tokens and extra_tokens:
            missing_scope, missing_method = token_scope_method(missing_tokens[0])
            extra_scope, extra_method = token_scope_method(extra_tokens[0])
            if not missing_method.startswith("Custom"):
                failures.append("gender_form_swap_missing_token_must_be_custom")
            if not extra_method.startswith("Custom"):
                failures.append("gender_form_swap_extra_token_must_be_custom")
            if not missing_scope or missing_scope != extra_scope:
                failures.append("gender_form_swap_requires_same_non_empty_scope")
        if not any("ES_OA" in token for token in missing_tokens):
            failures.append("gender_form_swap_requires_es_oa_missing_token")
        if not any("ES_XA" in token for token in extra_tokens):
            failures.append("gender_form_swap_requires_es_xa_extra_token")
        if has_style_modifier(missing_tokens + extra_tokens):
            failures.append("gender_form_swap_release_requires_no_style_modifier")
        if any(
            marker in token
            for token in missing_tokens + extra_tokens
            for marker in (
                "Select_CString(",
                "ES_EA",
                "ES_ElLa",
                "ES_ElElla",
                "ES_DelDela",
                "ES_AlAla",
                "ES_LoLa",
            )
        ):
            failures.append("gender_form_swap_release_has_disallowed_gender_marker")
    elif row["overlay_action"] == PRONOUN_FORM_SWAP_RELEASE_ACTION:
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if len(missing_tokens) != 1:
            failures.append("pronoun_form_swap_release_requires_one_missing_token")
        if len(extra_tokens) != 1:
            failures.append("pronoun_form_swap_release_requires_one_extra_token")
        if missing_tokens and extra_tokens:
            missing_scope, missing_method = token_scope_method(missing_tokens[0])
            extra_scope, extra_method = token_scope_method(extra_tokens[0])
            if missing_method != "GetSheHe":
                failures.append("pronoun_form_swap_missing_token_must_be_subject_pronoun")
            if extra_method != "GetHerHim":
                failures.append("pronoun_form_swap_extra_token_must_be_object_pronoun")
            if not missing_scope or missing_scope != extra_scope:
                failures.append("pronoun_form_swap_requires_same_non_empty_scope")
        if has_style_modifier(missing_tokens + extra_tokens):
            failures.append("pronoun_form_swap_release_requires_no_style_modifier")
        if any(
            marker in token
            for token in missing_tokens + extra_tokens
            for marker in ("Custom(", "Select_CString(", "ES_")
        ):
            failures.append("pronoun_form_swap_release_has_manual_context_marker")
    elif row["overlay_action"] == NAME_FORM_STYLE_RELEASE_ACTION:
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if not missing_tokens or not extra_tokens:
            failures.append("name_form_style_release_requires_missing_and_extra_tokens")
        if len(missing_tokens) != len(extra_tokens):
            failures.append("name_form_style_release_requires_equal_token_counts")
        if not only_methods(missing_tokens + extra_tokens, NAME_METHODS):
            failures.append("name_form_style_release_requires_only_name_methods")
        if token_scopes(missing_tokens) != token_scopes(extra_tokens):
            failures.append("name_form_style_release_requires_same_scopes")
        if not same_tokens_after_style_strip(missing_tokens, extra_tokens):
            failures.append("name_form_style_release_requires_same_tokens_after_style_strip")
        if not has_style_modifier(missing_tokens):
            failures.append("name_form_style_release_requires_missing_style_modifier")
        if has_style_modifier(extra_tokens):
            failures.append("name_form_style_release_requires_extra_without_style_modifier")
        if any(
            marker in token
            for token in missing_tokens + extra_tokens
            for marker in ("Custom(", "Select_CString(", "Glossary(", "ES_")
        ):
            failures.append("name_form_style_release_has_manual_context_marker")
    elif row["overlay_action"] == TOKEN_STYLE_TARGET_ACTION:
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("queue_rule_key") != "same_token_style_modifier_english_aligned":
            failures.append("unexpected_token_style_rule_key")
        if not missing_tokens or not extra_tokens:
            failures.append("token_style_release_requires_missing_and_extra_tokens")
        if len(missing_tokens) != len(extra_tokens):
            failures.append("token_style_release_requires_equal_token_counts")
        if token_scopes(missing_tokens) != token_scopes(extra_tokens):
            failures.append("token_style_release_requires_same_scopes")
        if not same_tokens_after_style_strip(missing_tokens, extra_tokens):
            failures.append("token_style_release_requires_same_tokens_after_style_strip")
        if has_style_modifier(missing_tokens):
            failures.append("token_style_release_requires_missing_without_style_modifier")
        if not has_style_modifier(extra_tokens):
            failures.append("token_style_release_requires_extra_style_modifier")
        if token_contains_any(missing_tokens + extra_tokens, {"Custom(", "Select_CString(", "Glossary(", "ES_"}):
            failures.append("token_style_release_has_manual_context_marker")
    elif row["overlay_action"] == DYNAMIC_SCOPE_TARGET_ACTION:
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        confirmed_text = row.get("confirmed_text") or ""
        if row.get("queue_rule_key") != "debate_progress_marker_localplayer_scope_rephrase":
            failures.append("unexpected_dynamic_scope_rule_key")
        if str(row.get("relative_path") or "") != "activities/debate_activity_l_spanish.yml":
            failures.append("dynamic_scope_release_requires_debate_activity_path")
        if not str(row.get("source_key") or "").startswith("DEBATE_PROGRESS_MARKER_"):
            failures.append("dynamic_scope_release_requires_debate_progress_marker_key")
        if len(missing_tokens) != 1:
            failures.append("dynamic_scope_release_requires_one_missing_token")
        if not token_contains_any(missing_tokens, {"LocalPlayerString("}):
            failures.append("dynamic_scope_release_requires_localplayerstring_missing_token")
        if extra_tokens:
            failures.append("dynamic_scope_release_requires_no_extra_tokens")
        if " tem " not in f" {confirmed_text} ":
            failures.append("dynamic_scope_release_requires_neutral_tem_wording")
    elif row["overlay_action"] == TUTORIAL_CONCEPT_RELEASE_ACTION:
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if not str(row.get("relative_path") or "").startswith("tutorial/"):
            failures.append("tutorial_concept_release_requires_tutorial_path")
        if not extra_tokens or not any("$game_concept_" in token for token in extra_tokens):
            failures.append("tutorial_concept_release_requires_game_concept_extra_token")
        if any("@" in token for token in missing_tokens + extra_tokens):
            failures.append("tutorial_concept_release_disallows_icon_tokens")
        if row.get("queue_rule_key") == "tutorial_game_concept_addition" and missing_tokens:
            failures.append("tutorial_game_concept_addition_requires_no_missing_tokens")
        if row.get("queue_rule_key") == "tutorial_concept_placeholder_rewrite" and not any("Concept(" in token for token in missing_tokens):
            failures.append("tutorial_placeholder_rewrite_requires_missing_concept_placeholder")
    elif row["missing_tokens"] and profile["original_bucket"] != "review_select_cstring_change":
        if allowed_missing_methods is None:
            failures.append("missing_tokens_not_allowed")
        elif not any(
            any(method in token for method in allowed_missing_methods)
            for token in row.get("extra_tokens") or []
        ):
            failures.append("missing_expected_object_pronoun_extra_token")
    if set(row["issue_flags"]) != profile["issue_flags"]:
        failures.append("unexpected_issue_flags")
    return failures


def decide_row(row: dict[str, Any]) -> dict[str, Any]:
    failures = invariant_failures(row)
    if row.get("existing_decision_id") is not None:
        decision = "needs_subpolicy"
        reason = "already_reviewed_skip_duplicate_shadow_decision"
    elif failures:
        decision = "needs_subpolicy"
        reason = "shadow_invariant_failed:" + ",".join(failures)
    elif row["hygiene_flags"]:
        decision = "encoding_cleanup_required"
        reason = "token_release_safe_but_text_hygiene_blocks_apply:" + ",".join(row["hygiene_flags"])
    elif row["priority_bucket"] == "clean_token_shadow_review":
        decision = "accept_policy_candidate"
        reason = "clean_guarded_release_shadow_validated"
    else:
        decision = "needs_subpolicy"
        reason = "unknown_shadow_priority_bucket"
    return {
        "policy_item_id": row["policy_item_id"],
        "decision": decision,
        "corrected_text": "",
        "reviewer": REVIEWER,
        "notes": (
            f"{RULE_VERSION}; queue_run={row['queue_run_id']}; guarded_checkpoint={row['guarded_checkpoint_id']}; "
            f"overlay={row['overlay_run_id']}; priority={row['priority_bucket']}; rule_key={row['queue_rule_key']}; "
            f"reason={reason}"
        ),
    }


def write_outputs(
    settings: dict[str, Any],
    *,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    started_at: datetime,
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_ml_composite_guarded_release_shadow_decisions_reviewed"
    jsonl_path = base.with_suffix(".jsonl")
    report_path = base.with_suffix(".txt")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, ensure_ascii=False) + "\n")

    decision_counts = Counter(row["decision"] for row in decisions)
    priority_counts = Counter(row["priority_bucket"] for row in rows)
    rule_counts = Counter(row.get("queue_rule_key") or "missing_rule_key" for row in rows)
    invariant_fail_count = sum(1 for row in rows if invariant_failures(row))
    existing_count = sum(1 for row in rows if row.get("existing_decision_id") is not None)
    existing_accept_count = sum(
        1
        for row in rows
        if row.get("existing_decision_id") is not None
        and row.get("existing_decision") in STRUCTURAL_ACCEPT_DECISIONS
    )
    existing_non_accept_count = sum(
        1
        for row in rows
        if row.get("existing_decision_id") is not None
        and row.get("existing_decision") not in STRUCTURAL_ACCEPT_DECISIONS
    )
    lines = [
        "ML composite guarded overlay shadow decisions",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Source mode: {SOURCE_MODE}",
        f"Queue run id: {queue_run['id']}",
        f"Guarded checkpoint id: {queue_run['guarded_checkpoint_id']}",
        f"Overlay run id: {queue_run['overlay_run_id']}",
        f"Source policy run id: {queue_run['source_policy_run_id']}",
        "",
        "Summary:",
        f"- Rows loaded: {len(rows)}",
        f"- Decisions written: {len(decisions)}",
        f"- Existing decisions encountered: {existing_count}",
        f"- Existing structural accepts: {existing_accept_count}",
        f"- Existing non-accept decisions: {existing_non_accept_count}",
        f"- Invariant failures: {invariant_fail_count}",
        "",
        "Decision counts:",
        *[f"- {key}: {value}" for key, value in decision_counts.most_common()],
        "",
        "Priority counts:",
        *[f"- {key}: {value}" for key, value in priority_counts.most_common()],
        "",
        "Rule counts:",
        *[f"- {key}: {value}" for key, value in rule_counts.most_common()],
        "",
        "Interpretation:",
        "- Clean guarded release rows are accepted as token policy candidates.",
        "- Rows with text hygiene flags are explicitly blocked from apply as encoding cleanup.",
        "- Rows failing invariants stay as needs_subpolicy.",
        "- Rows already reviewed are counted here, but not duplicated in the output decisions file.",
        "- This command only writes a reviewed decisions file; ingestion is a separate step.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path, jsonl_path


def main(queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_shadow_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, selected_queue_run_id)
        if queue_run.get("shadow_queue_kind") != SHADOW_QUEUE_KIND:
            raise RuntimeError(f"Queue run {selected_queue_run_id} is not a {SHADOW_QUEUE_KIND!r} queue.")
        rows = fetch_queue_rows(conn, selected_queue_run_id)

    decisions = [decide_row(row) for row in rows if row.get("existing_decision_id") is None]
    report_path, jsonl_path = write_outputs(
        settings,
        queue_run=queue_run,
        rows=rows,
        decisions=decisions,
        started_at=started_at,
    )
    counts = Counter(row["decision"] for row in decisions)
    print("[ml_composite_guarded_overlay_shadow_decisions] Decisions drafted")
    print(f"[ml_composite_guarded_overlay_shadow_decisions] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_guarded_overlay_shadow_decisions] Queue run id: {queue_run['id']}")
    print(f"[ml_composite_guarded_overlay_shadow_decisions] Rows loaded: {len(rows)}")
    print(f"[ml_composite_guarded_overlay_shadow_decisions] Decisions written: {len(decisions)}")
    for key, value in counts.most_common():
        print(f"[ml_composite_guarded_overlay_shadow_decisions] {key}: {value}")
    print(f"[ml_composite_guarded_overlay_shadow_decisions] Report: {report_path}")
    print(f"[ml_composite_guarded_overlay_shadow_decisions] JSONL: {jsonl_path}")
    return {"report_path": str(report_path), "jsonl_path": str(jsonl_path), "decisions": len(decisions)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Draft reviewed decisions for a guarded overlay shadow queue.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
