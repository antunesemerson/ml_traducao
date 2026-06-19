from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_review_queue_v4_action_path_filters"
DEFAULT_AGENT_KEY = "micro_short_label_style"
DEFAULT_LIMIT = 120
DEFAULT_PER_BUCKET = 20
DYNAMIC_AGENT_KEY = "micro_dynamic_ck3_expression"
GENDER_AGENT_KEY = "micro_gender_token"
NICKNAME_SELECT_CSTRING_RESIDUAL_AGENT_KEY = "nickname_residual_spanish_select_cstring_boundary"

DYNAMIC_BUCKET_ORDER = [
    "dynamic_token_mismatch",
    "dynamic_select_cstring_long",
    "dynamic_select_cstring_short",
    "dynamic_custom_localization",
    "dynamic_concept_expression",
    "dynamic_many_tokens",
    "dynamic_very_long",
    "dynamic_long_text",
    "dynamic_events_longform",
    "dynamic_interactions_activities",
    "dynamic_rules_tooltips",
    "dynamic_general_context",
]

GENDER_BUCKET_ORDER = [
    "gender_token_extra_prefix",
    "gender_token_extra_suffix",
    "gender_select_cstring_long",
    "gender_select_cstring_short",
    "gender_pronoun_or_article_context",
    "gender_custom_many_tokens",
    "gender_rules_tooltips",
    "gender_events_longform",
    "gender_long_context",
    "gender_token_usage_general",
]

NICKNAME_SELECT_CSTRING_RESIDUAL_BUCKET_ORDER = [
    "nickname_select_cstring_spanish_verb",
    "nickname_select_cstring_possessive_or_pronoun",
    "nickname_select_cstring_gender_token_mixed",
    "nickname_select_cstring_long_desc",
    "nickname_select_cstring_general_residual",
]


def latest_id(conn, table_name: str, where_sql: str = "1 = 1") -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return float(part) / float(total) * 100


def short(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def report_paths(settings: dict[str, Any], agent_key: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_agent = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in agent_key)
    base = reports_dir / f"{stamp}_issue_review_queue_{safe_agent}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def package_name(relative_path: str | None) -> str:
    path = relative_path or "unknown"
    if "/" in path:
        return path.split("/", 1)[0]
    return path


def evidence_int(row: dict[str, Any], key: str) -> int:
    evidence = parse_json(row.get("evidence_json"), {})
    try:
        return int(evidence.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def evidence_domain(row: dict[str, Any]) -> str:
    evidence = parse_json(row.get("evidence_json"), {})
    return str(evidence.get("domain") or "domain_unknown")


def is_dynamic_agent(row: dict[str, Any]) -> bool:
    return row.get("agent_key") == DYNAMIC_AGENT_KEY


def is_gender_agent(row: dict[str, Any]) -> bool:
    return row.get("agent_key") == GENDER_AGENT_KEY


def is_nickname_select_cstring_residual_agent(row: dict[str, Any]) -> bool:
    return row.get("agent_key") == NICKNAME_SELECT_CSTRING_RESIDUAL_AGENT_KEY


def dynamic_queue_bucket(row: dict[str, Any]) -> str:
    issue_kind = str(row.get("issue_kind") or "")
    token_impact = row.get("token_impact") or "unknown"
    token_status = row.get("token_status") or "unknown"
    domain = evidence_domain(row)
    text_length = evidence_int(row, "text_length")
    token_count = evidence_int(row, "token_count")

    if token_impact == "token_mismatch" or token_status not in {"ok", "none", "", "unknown"}:
        return "dynamic_token_mismatch"
    if issue_kind == "select_cstring_expression":
        return "dynamic_select_cstring_long" if text_length > 180 or token_count >= 6 else "dynamic_select_cstring_short"
    if issue_kind == "custom_localization_expression":
        return "dynamic_custom_localization"
    if issue_kind == "concept_expression":
        return "dynamic_concept_expression"
    if token_count >= 9:
        return "dynamic_many_tokens"
    if text_length > 600:
        return "dynamic_very_long"
    if text_length > 300:
        return "dynamic_long_text"
    if domain == "domain_events_longform":
        return "dynamic_events_longform"
    if domain == "domain_interactions_activities":
        return "dynamic_interactions_activities"
    if domain == "domain_rules_tooltips":
        return "dynamic_rules_tooltips"
    return "dynamic_general_context"


def gender_queue_bucket(row: dict[str, Any]) -> str:
    issue_kind = str(row.get("issue_kind") or "")
    domain = evidence_domain(row)
    text = str(row.get("evidence_text") or "").casefold()
    text_length = evidence_int(row, "text_length")
    token_count = evidence_int(row, "token_count")
    word_count = evidence_int(row, "word_count")

    if issue_kind == "gender_token_extra_prefix":
        return "gender_token_extra_prefix"
    if issue_kind == "gender_token_extra_suffix":
        return "gender_token_extra_suffix"
    if issue_kind == "select_cstring_gender_literal":
        return "gender_select_cstring_long" if text_length > 180 or token_count >= 5 else "gender_select_cstring_short"
    if any(marker in text for marker in ("getherhim", "getherhis", "getshehe", "getwomanman")):
        return "gender_pronoun_or_article_context"
    if any(marker in text for marker in ("custom('es_", 'custom("es_')):
        if token_count >= 5:
            return "gender_custom_many_tokens"
    if domain == "domain_rules_tooltips":
        return "gender_rules_tooltips"
    if domain == "domain_events_longform":
        return "gender_events_longform"
    if text_length > 320 or word_count > 60:
        return "gender_long_context"
    return "gender_token_usage_general"


def nickname_select_cstring_residual_bucket(row: dict[str, Any]) -> str:
    text = (row.get("evidence_text") or "").casefold()
    text_length = evidence_int(row, "text_length")
    if any(
        marker in text
        for marker in (
            "habl",
            "luch",
            "pued",
            "aparec",
            "comes",
            "come'",
            "apoya",
            "alab",
            "posees",
            "inspiras",
            "hacerte",
            "termina",
        )
    ):
        return "nickname_select_cstring_spanish_verb"
    if any(marker in text for marker in ("'tus'", "'sus'", "'tu'", "'su'", "sua", "seu")):
        return "nickname_select_cstring_possessive_or_pronoun"
    if "custom('es_" in text or 'custom("es_' in text:
        return "nickname_select_cstring_gender_token_mixed"
    if text_length > 220:
        return "nickname_select_cstring_long_desc"
    return "nickname_select_cstring_general_residual"


def short_label_queue_bucket(row: dict[str, Any]) -> str:
    evidence = parse_json(row.get("evidence_json"), {})
    issue_codes = set(evidence.get("issue_codes") or [])
    issue_kind = str(row.get("issue_kind") or "")
    text = str(row.get("evidence_text") or "").casefold()
    token_impact = row.get("token_impact") or "unknown"
    token_status = row.get("token_status") or "unknown"
    active_action = row.get("active_action") or ""
    candidate_action = row.get("candidate_action") or ""
    policy_action = row.get("policy_action") or ""
    domain = evidence.get("domain") or "domain_unknown"
    pkg = package_name(row.get("relative_path"))

    if token_impact == "token_mismatch" or token_status not in {"ok", "none", ""}:
        return "token_sensitive"
    if "mojibake_or_unexpected_script" in issue_codes or "mojibake" in issue_kind:
        return "short_mojibake_or_script"
    if (
        "spanish_residue_in_literal" in issue_codes
        and any(marker in text for marker in ("select_cstring", "localplayerstring", "custom(", "concept("))
    ) or "dynamic_spanish_literal" in issue_kind:
        return "short_dynamic_spanish_literal"
    if "spanish_residue_in_literal" in issue_codes or "spanish_residue" in issue_codes:
        return "short_spanish_residual_literal"
    if any(marker in text for marker in ("select_cstring", "localplayerstring", "custom(", "concept(")):
        return "short_dynamic_expression"
    if active_action == "auto_safe" and candidate_action == "needs_autofix":
        return "active_safe_candidate_autofix"
    if active_action == "needs_human" or policy_action == "needs_human":
        return "needs_human_conflict"
    if domain in {"domain_titles_names", "domain_religion", "domain_culture"}:
        return domain
    if domain in {"domain_rules_tooltips", "domain_events_longform", "domain_interactions_activities"}:
        return domain
    if pkg in {"dlc", "event_localization", "effects_l_spanish.yml", "triggers"}:
        return f"package_{pkg}"
    return "general_short_label"


def queue_bucket(row: dict[str, Any]) -> str:
    if is_dynamic_agent(row):
        return dynamic_queue_bucket(row)
    if is_gender_agent(row):
        return gender_queue_bucket(row)
    if is_nickname_select_cstring_residual_agent(row):
        return nickname_select_cstring_residual_bucket(row)
    return short_label_queue_bucket(row)


def priority_score(row: dict[str, Any]) -> float:
    if is_nickname_select_cstring_residual_agent(row):
        bucket = queue_bucket(row)
        score = {
            "nickname_select_cstring_spanish_verb": 1200,
            "nickname_select_cstring_possessive_or_pronoun": 980,
            "nickname_select_cstring_gender_token_mixed": 900,
            "nickname_select_cstring_long_desc": 820,
            "nickname_select_cstring_general_residual": 720,
        }.get(bucket, 600)
        score += min(evidence_int(row, "token_count"), 20) * 14
        score += min(evidence_int(row, "text_length"), 400) / 25
        score += int(row.get("segment_id") or 0) % 17 / 100
        return round(score, 4)

    if is_dynamic_agent(row):
        bucket = queue_bucket(row)
        score = 0.0
        bucket_bonus = {
            "dynamic_token_mismatch": 1400,
            "dynamic_select_cstring_long": 1200,
            "dynamic_many_tokens": 1125,
            "dynamic_very_long": 1050,
            "dynamic_select_cstring_short": 1000,
            "dynamic_custom_localization": 900,
            "dynamic_concept_expression": 820,
            "dynamic_long_text": 760,
            "dynamic_events_longform": 680,
            "dynamic_interactions_activities": 620,
            "dynamic_rules_tooltips": 580,
            "dynamic_general_context": 420,
        }
        score += bucket_bonus.get(bucket, 300)
        score += min(evidence_int(row, "token_count"), 20) * 18
        score += min(evidence_int(row, "word_count"), 120) * 2
        score += min(evidence_int(row, "text_length"), 1200) / 20
        if row.get("issue_severity") == "high":
            score += 250
        score += int(row.get("segment_id") or 0) % 17 / 100
        return round(score, 4)

    if is_gender_agent(row):
        bucket = queue_bucket(row)
        bucket_bonus = {
            "gender_token_extra_prefix": 1450,
            "gender_token_extra_suffix": 1425,
            "gender_select_cstring_long": 1200,
            "gender_select_cstring_short": 1100,
            "gender_pronoun_or_article_context": 980,
            "gender_custom_many_tokens": 880,
            "gender_rules_tooltips": 760,
            "gender_events_longform": 700,
            "gender_long_context": 620,
            "gender_token_usage_general": 480,
        }
        score = float(bucket_bonus.get(bucket, 420))
        score += min(evidence_int(row, "token_count"), 20) * 16
        score += min(evidence_int(row, "word_count"), 100) * 2
        score += min(evidence_int(row, "text_length"), 900) / 24
        if row.get("issue_severity") == "high":
            score += 260
        score += int(row.get("segment_id") or 0) % 17 / 100
        return round(score, 4)

    evidence = parse_json(row.get("evidence_json"), {})
    score = 0.0
    bucket = queue_bucket(row)
    if bucket == "token_sensitive":
        score += 1000
    if bucket == "short_dynamic_spanish_literal":
        score += 900
    if bucket == "short_mojibake_or_script":
        score += 850
    if bucket == "short_spanish_residual_literal":
        score += 800
    if bucket == "short_dynamic_expression":
        score += 700
    if bucket == "active_safe_candidate_autofix":
        score += 500
    if bucket == "needs_human_conflict":
        score += 450
    if row.get("issue_severity") == "high":
        score += 300
    if row.get("route_status") == "audit_required":
        score += 200
    token_count = int(evidence.get("token_count") or 0)
    word_count = int(evidence.get("word_count") or 0)
    score += min(token_count, 8) * 12
    if word_count <= 8:
        score += 60
    elif word_count <= 16:
        score += 35
    else:
        score += 10
    score += int(row.get("segment_id") or 0) % 17 / 100
    return round(score, 4)


def suggested_decision(row: dict[str, Any]) -> str:
    if is_nickname_select_cstring_residual_agent(row):
        return "classify_nickname_select_cstring_residual_boundary"

    if is_dynamic_agent(row):
        bucket = queue_bucket(row)
        if bucket == "dynamic_token_mismatch":
            return "review_token_policy_or_dynamic_repair"
        if bucket in {"dynamic_select_cstring_long", "dynamic_select_cstring_short"}:
            return "classify_select_cstring_dynamic_expression"
        if bucket == "dynamic_custom_localization":
            return "classify_custom_localization_expression"
        if bucket == "dynamic_concept_expression":
            return "classify_concept_expression"
        if bucket in {"dynamic_many_tokens", "dynamic_very_long", "dynamic_long_text"}:
            return "decompose_long_dynamic_expression"
        return "classify_dynamic_ck3_expression"

    if is_gender_agent(row):
        bucket = queue_bucket(row)
        if bucket in {"gender_token_extra_prefix", "gender_token_extra_suffix"}:
            return "classify_gender_token_boundary_or_repair"
        if bucket in {"gender_select_cstring_long", "gender_select_cstring_short"}:
            return "classify_select_cstring_gender_literal"
        if bucket == "gender_pronoun_or_article_context":
            return "classify_gender_pronoun_or_article_context"
        if bucket in {"gender_custom_many_tokens", "gender_long_context"}:
            return "split_gender_token_context_before_policy"
        return "classify_gender_token_usage"

    bucket = queue_bucket(row)
    if bucket == "token_sensitive":
        return "review_token_sensitive_short_label"
    if bucket == "short_mojibake_or_script":
        return "review_mojibake_or_script_short_label"
    if bucket == "short_dynamic_spanish_literal":
        return "review_dynamic_spanish_literal_short_label"
    if bucket == "short_spanish_residual_literal":
        return "review_residual_spanish_short_label"
    if bucket == "short_dynamic_expression":
        return "review_dynamic_short_label"
    if bucket == "active_safe_candidate_autofix":
        return "decide_if_active_safe_or_candidate_autofix"
    if bucket == "needs_human_conflict":
        return "semantic_review_short_label"
    return "classify_short_label_style"


def fetch_candidates(
    conn,
    ledger_run_id: int,
    agent_key: str,
    issue_family: str | None,
    issue_kind: str | None,
    active_action: str | None,
    candidate_action: str | None,
    policy_action: str | None,
    path_like: str | None,
    include_existing: bool,
) -> list[dict[str, Any]]:
    family_sql = "AND item.issue_family = ?" if issue_family else ""
    kind_sql = "AND item.issue_kind = ?" if issue_kind else ""
    active_sql = "AND item.active_action = ?" if active_action else ""
    candidate_sql = "AND item.candidate_action = ?" if candidate_action else ""
    policy_sql = "AND item.policy_action = ?" if policy_action else ""
    path_sql = "AND item.relative_path LIKE ?" if path_like else ""
    existing_sql = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.ledger_item_id = item.id
                AND queued.agent_key = item.agent_key
          )
        """
    )
    params: list[Any] = [ledger_run_id, agent_key]
    if issue_family:
        params.append(issue_family)
    if issue_kind:
        params.append(issue_kind)
    if active_action:
        params.append(active_action)
    if candidate_action:
        params.append(candidate_action)
    if policy_action:
        params.append(policy_action)
    if path_like:
        params.append(path_like)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_ledger_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.agent_key = ?
          AND item.status = 'open'
          {family_sql}
          {kind_sql}
          {active_sql}
          {candidate_sql}
          {policy_sql}
          {path_sql}
          {existing_sql}
        ORDER BY item.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_rows(candidates: list[dict[str, Any]], limit: int, per_bucket: int) -> list[dict[str, Any]]:
    for row in candidates:
        row["queue_bucket"] = queue_bucket(row)
        row["priority_score"] = priority_score(row)
        row["suggested_decision"] = suggested_decision(row)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"]))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    if candidates and candidates[0].get("agent_key") == DYNAMIC_AGENT_KEY:
        bucket_order = DYNAMIC_BUCKET_ORDER.copy()
    elif candidates and candidates[0].get("agent_key") == GENDER_AGENT_KEY:
        bucket_order = GENDER_BUCKET_ORDER.copy()
    elif candidates and candidates[0].get("agent_key") == NICKNAME_SELECT_CSTRING_RESIDUAL_AGENT_KEY:
        bucket_order = NICKNAME_SELECT_CSTRING_RESIDUAL_BUCKET_ORDER.copy()
    else:
        bucket_order = [
            "token_sensitive",
            "short_dynamic_spanish_literal",
            "short_mojibake_or_script",
            "short_spanish_residual_literal",
            "short_dynamic_expression",
            "active_safe_candidate_autofix",
            "needs_human_conflict",
            "domain_titles_names",
            "domain_religion",
            "domain_culture",
            "domain_rules_tooltips",
            "domain_events_longform",
            "domain_interactions_activities",
            "package_dlc",
            "package_event_localization",
            "package_effects_l_spanish.yml",
            "package_triggers",
            "general_short_label",
        ]
    bucket_order = [bucket for bucket in bucket_order if grouped.get(bucket)]
    extra_buckets = sorted(bucket for bucket in grouped if bucket not in set(bucket_order))
    bucket_order.extend(extra_buckets)

    for index in range(per_bucket):
        for bucket in bucket_order:
            rows = grouped.get(bucket, [])
            if index >= len(rows):
                continue
            if len(selected) >= limit:
                break
            row = rows[index]
            selected.append(row)
            selected_ids.add(int(row["id"]))
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        remaining = [
            row
            for row in candidates
            if int(row["id"]) not in selected_ids
        ]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"], item["relative_path"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
            selected_ids.add(int(row["id"]))
    return selected


def insert_queue_run(
    conn,
    ledger_run_id: int,
    agent_key: str,
    issue_family: str | None,
    limit: int,
    per_bucket: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    run_id = conn.execute(
        """
        INSERT INTO ml_issue_review_queue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            issue_family,
            queue_strategy,
            limit_count,
            per_bucket,
            selected_count,
            open_count,
            reviewed_count,
            bucket_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            ledger_run_id,
            agent_key,
            issue_family,
            "stratified_issue_ledger",
            limit,
            per_bucket,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    ).lastrowid
    return int(run_id)


def insert_queue_items(conn, run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    payload = []
    for row in rows:
        payload.append(
            (
                run_id,
                ledger_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["issue_family"],
                row["issue_kind"],
                row["agent_key"],
                row["queue_bucket"],
                row["priority_score"],
                "pending",
                row["suggested_decision"],
                row.get("evidence_text"),
                row.get("evidence_json"),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO ml_issue_review_queue_items (
            run_id,
            ledger_run_id,
            ledger_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            issue_family,
            issue_kind,
            agent_key,
            queue_bucket,
            priority_score,
            review_status,
            suggested_decision,
            evidence_text,
            evidence_json,
            english_text,
            spanish_text,
            confirmed_text,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def decision_options_for_agent(agent_key: str) -> list[str]:
    if agent_key == DYNAMIC_AGENT_KEY:
        return [
            "needs_domain_context",
            "needs_repair",
            "needs_new_microagent",
            "manual_exception",
            "safe_short_label",
            "false_positive_reopen",
        ]
    if agent_key == GENDER_AGENT_KEY:
        return [
            "needs_domain_context",
            "needs_repair",
            "needs_new_microagent",
            "false_positive_reopen",
            "manual_exception",
            "safe_short_label",
        ]
    return [
        "safe_short_label",
        "needs_repair",
        "false_positive_reopen",
        "needs_domain_context",
        "needs_new_microagent",
        "manual_exception",
    ]


def review_guidance_for_agent(agent_key: str) -> list[str]:
    if agent_key == DYNAMIC_AGENT_KEY:
        return [
            "- Treat each row as a CK3 dynamic expression, not as a whole-segment verdict.",
            "- Mark `needs_domain_context` when the text may be correct but requires semantic/context review.",
            "- Mark `needs_repair` when visible PT-BR, Spanish residue, spacing, or expression wording is clearly wrong.",
            "- Mark `needs_new_microagent` when a narrower reusable specialist is needed, such as Select_CString pronoun logic or Custom('ES_*') grammar.",
            "- Mark `manual_exception` only for intentional game/UI exceptions that should remain locked.",
            "- Avoid approving as safe unless the expression, visible text, and token behavior are all clearly acceptable.",
            "- Do not apply output from this queue; it is evidence for the learning front.",
        ]
    if agent_key == GENDER_AGENT_KEY:
        return [
            "- Treat each row as a gender-token issue, not as a whole-segment verdict.",
            "- Mark `needs_repair` when the gender token clearly creates wrong PT-BR grammar or visible residue.",
            "- Mark `needs_new_microagent` when the case needs a narrower reusable specialist, such as Select_CString gender literals, pronoun swaps, or article/preposition helpers.",
            "- Mark `needs_domain_context` when the token may be valid but depends on CK3 referent/context.",
            "- Mark `false_positive_reopen` only when the current confirmed text is safe and the reopen is explainably wrong.",
            "- Do not apply output from this queue; it is evidence for the learning front.",
        ]
    return [
        "- Decide whether the reopened short label is actually safe, needs repair, or was a false-positive reopen.",
        "- Prefer marking reusable patterns over one-off segment decisions.",
        "- Do not apply output from this queue; it is evidence for the learning front.",
        "- For token-sensitive rows, do not approve without explicit token reasoning.",
    ]


def write_outputs(
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    ledger_run_id: int,
    agent_key: str,
    rows: list[dict[str, Any]],
    candidates_count: int,
    limit: int,
    per_bucket: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    package_counts = Counter(package_name(row.get("relative_path")) for row in rows)
    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "issue_family",
        "issue_kind",
        "token_impact",
        "token_status",
        "active_action",
        "candidate_action",
        "policy_action",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "ledger_item_id": row["id"],
                    **{name: row.get(name) for name in fieldnames if name not in {"queue_run_id", "ledger_item_id"}},
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_run_id": ledger_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": agent_key,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "token_impact": row["token_impact"],
                "token_status": row["token_status"],
                "active_action": row["active_action"],
                "candidate_action": row["candidate_action"],
                "policy_action": row["policy_action"],
                "evidence": parse_json(row.get("evidence_json"), {}),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        decision_options = decision_options_for_agent(agent_key)
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "decision": "",
                "decision_options": decision_options,
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent: {agent_key}",
        "",
        "Coverage:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(rows):,} ({percent(len(rows), candidates_count):.2f}%)",
        f"- Limit: {limit:,}",
        f"- Per bucket: {per_bucket:,}",
        "",
        "Buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Packages:",
        *[f"- {package}: {count:,}" for package, count in package_counts.most_common(20)],
        "",
        "Review guidance:",
        *review_guidance_for_agent(agent_key),
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in rows[:20]:
        lines.append(
            f"- ledger {row['id']} | segment {row['segment_id']} | {row['queue_bucket']} | "
            f"{row['relative_path']}::{row['source_key']} | {row['suggested_decision']}"
        )
        lines.append(f"  evidence: {short(row.get('evidence_text'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    agent_key: str = DEFAULT_AGENT_KEY,
    issue_family: str | None = None,
    issue_kind: str | None = None,
    active_action: str | None = None,
    candidate_action: str | None = None,
    policy_action: str | None = None,
    path_like: str | None = None,
    limit: int | None = None,
    per_bucket: int = DEFAULT_PER_BUCKET,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    selected_limit = limit or DEFAULT_LIMIT
    print("[issue_review_queue] Starting issue review queue")
    print(f"[issue_review_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_review_queue] Agent: {agent_key}")
    if issue_family:
        print(f"[issue_review_queue] Issue family: {issue_family}")
    if issue_kind:
        print(f"[issue_review_queue] Issue kind: {issue_kind}")
    if active_action:
        print(f"[issue_review_queue] Active action: {active_action}")
    if candidate_action:
        print(f"[issue_review_queue] Candidate action: {candidate_action}")
    if policy_action:
        print(f"[issue_review_queue] Policy action: {policy_action}")
    if path_like:
        print(f"[issue_review_queue] Path like: {path_like}")
    print(f"[issue_review_queue] Limit: {selected_limit}")
    print(f"[issue_review_queue] Per bucket: {per_bucket}")

    paths = report_paths(settings, agent_key)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ledger_run_id = latest_id(conn, "ml_issue_ledger_runs", "finished_at IS NOT NULL")
        if ledger_run_id is None:
            raise RuntimeError("No finished ml_issue_ledger_runs found. Run issue-ledger first.")
        candidates = fetch_candidates(
            conn,
            ledger_run_id=ledger_run_id,
            agent_key=agent_key,
            issue_family=issue_family,
            issue_kind=issue_kind,
            active_action=active_action,
            candidate_action=candidate_action,
            policy_action=policy_action,
            path_like=path_like,
            include_existing=include_existing,
        )
        selected = select_rows(candidates, limit=selected_limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            ledger_run_id=ledger_run_id,
            agent_key=agent_key,
            issue_family=issue_family,
            limit=selected_limit,
            per_bucket=per_bucket,
            selected=selected,
            paths=paths,
        )
        insert_queue_items(conn, queue_run_id, ledger_run_id, selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        ledger_run_id=ledger_run_id,
        agent_key=agent_key,
        rows=selected,
        candidates_count=len(candidates),
        limit=selected_limit,
        per_bucket=per_bucket,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print(f"[issue_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_review_queue] Candidates: {len(candidates)}")
    print(f"[issue_review_queue] Selected: {len(selected)}")
    print(f"[issue_review_queue] Report: {txt_path}")
    print(f"[issue_review_queue] CSV: {csv_path}")
    print(f"[issue_review_queue] JSONL: {jsonl_path}")
    print(f"[issue_review_queue] Decisions template: {decisions_template_path}")
    print("[issue_review_queue] Done")
    return {
        "queue_run_id": queue_run_id,
        "ledger_run_id": ledger_run_id,
        "agent_key": agent_key,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a stratified review queue from issue ledger items.")
    parser.add_argument("--agent-key", default=DEFAULT_AGENT_KEY)
    parser.add_argument("--issue-family", default=None)
    parser.add_argument("--issue-kind", default=None)
    parser.add_argument("--active-action", default=None)
    parser.add_argument("--candidate-action", default=None)
    parser.add_argument("--policy-action", default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        agent_key=args.agent_key,
        issue_family=args.issue_family,
        issue_kind=args.issue_kind,
        active_action=args.active_action,
        candidate_action=args.candidate_action,
        policy_action=args.policy_action,
        path_like=args.path_like,
        limit=args.limit,
        per_bucket=args.per_bucket,
        include_existing=args.include_existing,
    )
