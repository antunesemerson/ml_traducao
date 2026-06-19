from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "autofix_unknown_semantic_pair_current_review_v1"
TARGET_ISSUES = {
    ("autofix_unknown_microagent", "needs_autofix_unclassified"),
    ("semantic_review_router", "needs_human_or_semantic_conflict"),
}
BUCKET_ORDER = [
    "compact_or_short_phrase",
    "medium_plain_sentence",
    "long_context_needed",
    "event_context_surface",
    "modifier_or_trait_description",
    "memory_or_activity_text",
    "artifact_or_lore_text",
    "custom_localization_or_dynamic_fragment",
    "domain_sensitive",
    "residual_or_fluency_suspect",
]
REQUIRED_FIELDS = {
    "ledger_run_id",
    "segment_state_run_id",
    "ledger_item_ids",
    "segment_id",
    "relative_path",
    "source_key",
    "surface_bucket",
    "issue_families",
    "decision",
    "confidence",
    "lifecycle_candidate",
    "requires_apply_later",
    "suggested_subpolicy",
    "issue_tags",
    "rationale",
    "current_text",
    "english_text",
    "spanish_text",
    "corrected_text",
    "tokens_preserved",
    "blocked_reason",
}
ALLOWED_DECISIONS = {
    "lifecycle_ready_plain_sentence",
    "lifecycle_ready_modifier_description",
    "lifecycle_ready_memory_activity_text",
    "needs_event_context_composer",
    "needs_plain_prose_context_composer",
    "needs_domain_context",
    "needs_dynamic_expression_agent",
    "needs_new_microagent",
    "needs_repair",
    "spanish_residual_repair_needed",
    "english_residual_repair_needed",
    "ptbr_fluency_repair_needed",
    "token_boundary_repair_needed",
    "blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
DYNAMIC_RE = re.compile(r"\b(?:Get[A-Za-z0-9_]*|Custom|Select_CString|ScriptValue)\b")
SPANISH_RE = re.compile(
    r"\b(?:verdadero|verdadera|muchos|muchas|penalizaciones|migaja|amarillo|reino|condado|duque|"
    r"señor|senor|dinastia|familia|guerra|cultura|fe|bienvenido|recuperacion|probabilidad)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(
    r"\b(?:will|must|cannot|should|kingdom|duchy|county|culture head|lose nothing|for each|"
    r"charioteers|leader|leaders|the\s+[A-Za-z]+|your\s+[A-Za-z]+|their\s+[A-Za-z]+)\b",
    re.IGNORECASE,
)
MOJIBAKE_RE = re.compile(r"[\u00c3\u00c2\ufffd]")
PTBR_FLUENCY_RE = re.compile(
    r"(?:\bde o\b|\ba o\b|\bpara o a\b|\bpode nao\b|\bnao pode nao\b|\bcom determinacao com\b|"
    r"\besta liderando\b|\bate para sua familia\b)"
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def package_name(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


def visible_len(text: str) -> int:
    return len(text.replace("\\n", "\n").strip())


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def protected_tokens_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def tokens_preserved(current_text: str, english_text: str) -> bool:
    current_tokens = protected_tokens_signature(current_text)
    english_tokens = protected_tokens_signature(english_text)
    if current_text.count("[") != current_text.count("]"):
        return False
    if current_text.count("$") % 2 != 0:
        return False
    if current_text.count("#") % 2 != 0:
        return False
    if not english_tokens:
        return True
    return current_tokens == english_tokens or len(current_tokens) >= min(len(english_tokens), 1)


def has_domain_sensitive_path_or_key(path: str, key: str) -> bool:
    haystack = f"{path} {key}".lower()
    markers = (
        "culture",
        "religion",
        "faith",
        "title",
        "nickname",
        "law",
        "war",
        "struggle",
        "dynasty",
        "house",
        "government",
        "council",
        "succession",
        "artifact",
        "legend",
        "lore",
        "court_position",
        "relation",
        "family",
        "scheme",
    )
    return any(marker in haystack for marker in markers)


def surface_bucket(row: dict[str, Any]) -> str:
    text = as_text(row["current_text"]).strip()
    key = as_text(row["source_key"])
    path = as_text(row["relative_path"])
    lower_key = key.lower()
    lower_path = path.lower()
    length = visible_len(text)

    if DYNAMIC_RE.search(text) or lower_path.startswith("custom_localization") or lower_key.startswith(("loc_", "custom_", "get")):
        return "custom_localization_or_dynamic_fragment"
    if SPANISH_RE.search(text) or ENGLISH_RE.search(text) or MOJIBAKE_RE.search(text) or PTBR_FLUENCY_RE.search(text.lower()):
        return "residual_or_fluency_suspect"
    if "event" in lower_path or re.search(r"\b(?:desc|option|toast|flavor|letter)\b", lower_key) and "." in key:
        return "event_context_surface"
    if any(marker in lower_path or marker in lower_key for marker in ("modifier", "trait", "_desc", "description")):
        return "modifier_or_trait_description"
    if any(marker in lower_path or marker in lower_key for marker in ("memory", "memories", "activity", "activities")):
        return "memory_or_activity_text"
    if any(marker in lower_path or marker in lower_key for marker in ("artifact", "legend", "lore")):
        return "artifact_or_lore_text"
    if has_domain_sensitive_path_or_key(lower_path, lower_key):
        return "domain_sensitive"
    if length <= 90:
        return "compact_or_short_phrase"
    if length <= 180:
        return "medium_plain_sentence"
    return "long_context_needed"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    key = as_text(row["source_key"])
    path = as_text(row["relative_path"])
    bucket = as_text(row["surface_bucket"])
    tags: list[str] = []
    blocked = ""
    confidence = 0.72
    subpolicy = bucket
    rationale = ""
    decision = "blocked_uncertain"
    lifecycle = False
    preserve_tokens = tokens_preserved(text, english)
    length = visible_len(text)
    words = word_count(text)
    multiline = "\n" in text.strip()
    quote_or_dialogue = any(mark in text for mark in ('"', "“", "”", "«", "»"))
    dynamic = bool(DYNAMIC_RE.search(text))
    domain_sensitive = has_domain_sensitive_path_or_key(path.lower(), key.lower())

    if not preserve_tokens:
        decision = "token_boundary_repair_needed"
        tags.append("token_boundary")
        rationale = "token or markup boundary needs mechanical review"
        blocked = "token_boundary"
        confidence = 0.88
    elif dynamic:
        decision = "needs_dynamic_expression_agent"
        tags.append("dynamic_expression")
        rationale = "dynamic CK3 expression requires semantic interpretation"
        blocked = "dynamic_expression"
        confidence = 0.86
    elif SPANISH_RE.search(text):
        decision = "spanish_residual_repair_needed"
        tags.append("spanish_residual")
        rationale = "visible Spanish residual in current PT-BR text"
        blocked = "spanish_residual"
        confidence = 0.9
    elif ENGLISH_RE.search(text) and not any(term in text for term in ("Crusader Kings", "Creator Pack")):
        decision = "english_residual_repair_needed"
        tags.append("english_residual")
        rationale = "visible English residual or untranslated wording"
        blocked = "english_residual"
        confidence = 0.84
    elif MOJIBAKE_RE.search(text) or PTBR_FLUENCY_RE.search(text.lower()):
        decision = "ptbr_fluency_repair_needed"
        tags.append("ptbr_fluency")
        rationale = "PT-BR text looks literal, corrupted, or awkward"
        blocked = "ptbr_fluency"
        confidence = 0.78
    elif bucket == "custom_localization_or_dynamic_fragment":
        decision = "needs_dynamic_expression_agent"
        tags.append("custom_localization")
        rationale = "custom localization or dynamic fragment needs a narrow agent"
        blocked = "dynamic_or_custom_localization"
        confidence = 0.82
    elif bucket == "event_context_surface" or quote_or_dialogue:
        decision = "needs_event_context_composer"
        tags.append("event_context")
        rationale = "event or dialogue surface needs perspective/context validation"
        blocked = "event_context"
        confidence = 0.78
    elif bucket == "artifact_or_lore_text" or domain_sensitive:
        decision = "needs_domain_context"
        tags.append("domain_sensitive")
        rationale = "domain-sensitive lore, title, culture, relation, war, law, or artifact context"
        blocked = "domain_context"
        confidence = 0.8
    elif bucket == "modifier_or_trait_description" and length <= 140 and not multiline and words <= 20:
        decision = "lifecycle_ready_modifier_description"
        lifecycle = True
        subpolicy = "generic_modifier_or_trait_description_false_reopen"
        rationale = "generic modifier/trait description appears natural and context-independent"
        confidence = 0.78
    elif bucket == "memory_or_activity_text" and length <= 120 and not multiline and words <= 18:
        decision = "lifecycle_ready_memory_activity_text"
        lifecycle = True
        subpolicy = "short_memory_activity_text_false_reopen"
        rationale = "short memory/activity text appears natural and context-independent"
        confidence = 0.76
    elif bucket == "compact_or_short_phrase" and length <= 90 and not multiline and words <= 14:
        decision = "lifecycle_ready_plain_sentence"
        lifecycle = True
        subpolicy = "compact_plain_sentence_false_reopen"
        rationale = "short plain PT-BR phrase with no visible residual or sensitive dependency"
        confidence = 0.78
    elif bucket == "medium_plain_sentence" and length <= 140 and not multiline and words <= 22:
        decision = "needs_plain_prose_context_composer"
        tags.append("plain_prose_context")
        rationale = "plain prose may be good, but medium text needs context before closure"
        blocked = "plain_prose_context"
        confidence = 0.72
    elif bucket == "long_context_needed" or multiline or length > 140:
        decision = "needs_plain_prose_context_composer"
        tags.append("long_context")
        rationale = "long or multi-clause prose needs context validation"
        blocked = "long_plain_prose_context"
        confidence = 0.74
    else:
        decision = "blocked_uncertain"
        tags.append("insufficient_evidence")
        rationale = "insufficient evidence for safe lifecycle classification"
        blocked = "blocked_uncertain"
        confidence = 0.55

    return {
        "decision": decision,
        "confidence": confidence,
        "lifecycle_candidate": lifecycle,
        "requires_apply_later": False,
        "suggested_subpolicy": subpolicy,
        "issue_tags": tags,
        "rationale": rationale,
        "corrected_text": "",
        "tokens_preserved": preserve_tokens,
        "blocked_reason": "" if lifecycle else blocked,
    }


def load_eligible(conn, ledger_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_segments AS (
            SELECT
                segment_id,
                COUNT(*) AS issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent'
                          AND issue_kind = 'needs_autofix_unclassified'
                         THEN 1 ELSE 0 END) AS autofix_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router'
                          AND issue_kind = 'needs_human_or_semantic_conflict'
                         THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical')
                         THEN 1 ELSE 0 END) AS high_issue_count,
                GROUP_CONCAT(id) AS ledger_item_ids,
                GROUP_CONCAT(issue_family, ';') AS issue_families,
                GROUP_CONCAT(issue_family || ':' || issue_kind || ':' || issue_severity, '; ') AS issue_tags
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.state_group,
            state.final_state,
            state.review_state,
            state.confirmation_level,
            state.locked,
            state.needs_output_apply,
            state.confirmed_matches_output,
            COALESCE(confirmation.locked, 0) AS confirmation_locked,
            issue_segments.ledger_item_ids,
            issue_segments.issue_families,
            issue_segments.issue_tags,
            source.english_text,
            source.spanish_text,
            output.portuguese_text AS current_text
        FROM issue_segments
        JOIN segment_state_items state
          ON state.segment_id = issue_segments.segment_id
         AND state.run_id = ?
        JOIN source_segments source
          ON source.id = state.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
        WHERE issue_segments.issue_count = 2
          AND issue_segments.autofix_count = 1
          AND issue_segments.semantic_count = 1
          AND issue_segments.high_issue_count = 0
          AND state.state_group = 'pending'
          AND COALESCE(state.needs_output_apply, 0) = 0
          AND COALESCE(state.confirmed_matches_output, 0) = 1
          AND COALESCE(state.review_state, '') NOT IN ('human_locked', 'human_confirmed')
          AND COALESCE(state.confirmation_level, '') NOT IN ('human_locked', 'human_confirmed')
          AND COALESCE(state.locked, 0) = 0
          AND COALESCE(confirmation.locked, 0) = 0
        ORDER BY state.relative_path, state.source_key, state.segment_id
        """,
        (ledger_run_id, state_run_id),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["surface_bucket"] = surface_bucket(item)
        result.append(item)
    return result


def stratified_sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["surface_bucket"]].append(row)
    for bucket_rows in grouped.values():
        bucket_rows.sort(
            key=lambda row: (
                -min(visible_len(as_text(row["current_text"])), 260),
                package_name(as_text(row["relative_path"])),
                as_text(row["source_key"]),
                int(row["segment_id"]),
            )
        )

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    base_quota = max(1, limit // max(1, len(BUCKET_ORDER)))
    for bucket in BUCKET_ORDER:
        for row in grouped.get(bucket, [])[:base_quota]:
            segment_id = int(row["segment_id"])
            if segment_id not in selected_ids and len(selected) < limit:
                selected.append(row)
                selected_ids.add(segment_id)
    leftovers = [
        row
        for bucket in BUCKET_ORDER
        for row in grouped.get(bucket, [])[base_quota:]
        if int(row["segment_id"]) not in selected_ids
    ]
    leftovers.extend(
        row
        for bucket, bucket_rows in grouped.items()
        if bucket not in BUCKET_ORDER
        for row in bucket_rows
        if int(row["segment_id"]) not in selected_ids
    )
    leftovers.sort(
        key=lambda row: (
            BUCKET_ORDER.index(row["surface_bucket"]) if row["surface_bucket"] in BUCKET_ORDER else 99,
            package_name(as_text(row["relative_path"])),
            as_text(row["source_key"]),
            int(row["segment_id"]),
        )
    )
    for row in leftovers:
        if len(selected) >= limit:
            break
        selected.append(row)
        selected_ids.add(int(row["segment_id"]))
    return selected[:limit]


def build_report(reviewed: list[dict[str, Any]], eligible_total: int) -> str:
    by_decision = Counter(item["decision"] for item in reviewed)
    by_bucket_decision = Counter(f"{item['surface_bucket']} + {item['decision']}" for item in reviewed)
    by_package_decision = Counter(f"{package_name(item['relative_path'])} + {item['decision']}" for item in reviewed)
    lifecycle_count = sum(1 for item in reviewed if item["lifecycle_candidate"])
    apply_count = sum(1 for item in reviewed if item["requires_apply_later"])
    patterns = Counter(item["suggested_subpolicy"] for item in reviewed)
    blockers = Counter(item["blocked_reason"] for item in reviewed if item["blocked_reason"])
    lifecycle_examples = [item for item in reviewed if item["lifecycle_candidate"]][:8]
    repair_examples = [
        item
        for item in reviewed
        if item["decision"].endswith("_repair_needed") or item["decision"] == "needs_repair"
    ][:8]
    context_examples = [
        item
        for item in reviewed
        if item["decision"]
        in {
            "needs_event_context_composer",
            "needs_plain_prose_context_composer",
            "needs_domain_context",
            "needs_dynamic_expression_agent",
            "needs_new_microagent",
        }
    ][:8]

    if lifecycle_count >= 70:
        recommendation = "Prepare a governed lifecycle bridge for the safe lifecycle_ready_* lanes."
    elif blockers.get("event_context", 0) + blockers.get("plain_prose_context", 0) + blockers.get("long_plain_prose_context", 0) >= 70:
        recommendation = "Prioritize a context composer for event/plain prose surfaces before lifecycle."
    elif blockers.get("domain_context", 0) >= 70:
        recommendation = "Create domain-specific microagents before attempting broad lifecycle closure."
    else:
        recommendation = "Continue diagnostics with a second stratified batch or target the dominant blocker lane."

    lines = [
        "Autofix unknown + semantic pair current review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total elegivel no bloco: {eligible_total}",
        f"Total revisado: {len(reviewed)}",
        "",
        "Contagem por decisao:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por surface bucket + decisao:")
    for key, value in by_bucket_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por pacote + decisao:")
    for key, value in by_package_decision.most_common(30):
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates: {lifecycle_count}",
            f"Apply candidates: {apply_count}",
            "",
            "Top 10 padroes aproveitaveis:",
        ]
    )
    for key, value in patterns.most_common(10):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Top 10 bloqueios:")
    for key, value in blockers.most_common(10):
        lines.append(f"- {key}: {value}")
    if lifecycle_examples:
        lines.append("")
        lines.append("Exemplos de lifecycle seguros:")
        for item in lifecycle_examples:
            lines.append(f"- {item['segment_id']} | {item['decision']} | {item['source_key']} | {item['current_text'][:120]}")
    if repair_examples:
        lines.append("")
        lines.append("Exemplos de reparos:")
        for item in repair_examples:
            lines.append(f"- {item['segment_id']} | {item['decision']} | {item['source_key']} | {item['current_text'][:120]}")
    if context_examples:
        lines.append("")
        lines.append("Exemplos de contexto/microagente:")
        for item in context_examples:
            lines.append(f"- {item['segment_id']} | {item['decision']} | {item['source_key']} | {item['current_text'][:120]}")
    lines.extend(
        [
            "",
            "Recomendacao objetiva:",
            f"- {recommendation}",
            "",
            "Validacoes finais:",
            "- JSONL UTF-8 valido.",
            "- Nenhum apply, production, confirmation, reindex, treino/model promotion, segment-state ou lifecycle executado.",
            "- Nenhuma escrita em source/ ou output/.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(reviewed: list[dict[str, Any]], eligible_total: int, out_prefix: str) -> tuple[Path, Path]:
    out_jsonl = Path(f"reports/{out_prefix}_autofix_unknown_semantic_pair_current_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{out_prefix}_autofix_unknown_semantic_pair_current_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in reviewed) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(reviewed, eligible_total), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-run-id", type=int, default=76)
    parser.add_argument("--segment-state-run-id", type=int, default=366)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        eligible = load_eligible(conn, args.ledger_run_id, args.segment_state_run_id)

    sample = stratified_sample(eligible, args.limit)
    reviewed: list[dict[str, Any]] = []
    for row in sample:
        classification = classify(row)
        item = {
            "ledger_run_id": args.ledger_run_id,
            "segment_state_run_id": args.segment_state_run_id,
            "ledger_item_ids": [int(value) for value in as_text(row["ledger_item_ids"]).split(",") if value],
            "segment_id": int(row["segment_id"]),
            "relative_path": as_text(row["relative_path"]),
            "source_key": as_text(row["source_key"]),
            "surface_bucket": as_text(row["surface_bucket"]),
            "issue_families": as_text(row["issue_families"]).split(";"),
            "current_text": as_text(row["current_text"]),
            "english_text": as_text(row["english_text"]),
            "spanish_text": as_text(row["spanish_text"]),
            **classification,
        }
        if set(item) != REQUIRED_FIELDS:
            raise RuntimeError(f"Output field mismatch for segment {item['segment_id']}: {set(item) ^ REQUIRED_FIELDS}")
        if not 0.0 <= float(item["confidence"]) <= 1.0:
            raise RuntimeError(f"Invalid confidence for segment {item['segment_id']}")
        if item["requires_apply_later"] and not item["corrected_text"]:
            raise RuntimeError(f"Missing corrected_text for apply candidate {item['segment_id']}")
        if item["decision"] not in ALLOWED_DECISIONS:
            raise RuntimeError(f"Unexpected decision {item['decision']}")
        reviewed.append(item)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl, out_txt = write_outputs(reviewed, len(eligible), stamp)
    print(out_jsonl)
    print(out_txt)
    print("eligible_total", len(eligible))
    print("reviewed", len(reviewed))
    print("decisions", dict(Counter(item["decision"] for item in reviewed)))
    print("lifecycle", sum(1 for item in reviewed if item["lifecycle_candidate"]))
    print("apply", sum(1 for item in reviewed if item["requires_apply_later"]))
    print("patterns", dict(Counter(item["suggested_subpolicy"] for item in reviewed).most_common(10)))
    print("blockers", dict(Counter(item["blocked_reason"] for item in reviewed if item["blocked_reason"]).most_common(10)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
