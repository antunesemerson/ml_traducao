from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "short_label_semantic_pair_current_review_v1"
TARGET_ISSUES = {
    ("semantic_review_router", "needs_human_or_semantic_conflict"),
    ("short_label_style_microagent", "short_or_compact_label_reopened"),
}
BUCKET_ORDER = [
    "quote_or_dialogue_fragment",
    "compact_punctuated_option",
    "custom_localization_nominal",
    "compact_label",
    "short_phrase",
    "longer_context_needed",
    "load_tip_or_long_hint",
    "event_context_short_surface",
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
    "lifecycle_ready_quote_fragment",
    "lifecycle_ready_compact_option",
    "lifecycle_ready_nominal_label",
    "lifecycle_ready_short_phrase",
    "needs_context_composer",
    "needs_domain_context",
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
SPANISH_RE = re.compile(
    r"\b(?:verdadero|verdadera|muchos|muchas|penalizaciones|migaja|bien|amarillo|que aproveche|más fuerte|reino)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(
    r"\b(?:will|must|cannot|should|the |kingdom|duchy|county|culture head|lose nothing|for each)\b",
    re.IGNORECASE,
)
GET_DYNAMIC_RE = re.compile(r"\b(?:Get[A-Za-z0-9_]*|Custom|Select_CString|ScriptValue)\b")


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
    # CK3 localization often translates only visible prose; require current tokens to be structurally closed.
    if current_text.count("[") != current_text.count("]"):
        return False
    if current_text.count("#") % 2 != 0:
        return False
    if current_text.count("$") % 2 != 0:
        return False
    if not english_tokens:
        return True
    return len(current_tokens) >= min(len(english_tokens), 1)


def surface_bucket(row: dict[str, Any]) -> str:
    text = as_text(row["current_text"])
    key = as_text(row["source_key"])
    path = as_text(row["relative_path"])
    stripped = text.strip()
    words = word_count(stripped)
    length = visible_len(stripped)
    if path.startswith("load_tips") or key.startswith("load_tip") or "load_tip" in key:
        return "load_tip_or_long_hint"
    if path.startswith("custom_localization") or key.startswith(("loc_", "custom_", "Get")):
        return "custom_localization_nominal"
    if any(mark in stripped for mark in ('"', "“", "”", "«", "»")):
        return "quote_or_dialogue_fragment"
    if key.startswith(("event_", "bp", "ep", "fp", "travel_", "court.", "murder_", "child_", "board_games.")) or "event" in path:
        if length <= 90:
            return "event_context_short_surface"
        return "longer_context_needed"
    if re.search(r"[.!?…]$", stripped) and words <= 7:
        return "compact_punctuated_option"
    if words <= 4 and length <= 40:
        return "compact_label"
    if length <= 80:
        return "short_phrase"
    return "longer_context_needed"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    spanish = as_text(row["spanish_text"])
    key = as_text(row["source_key"])
    path = as_text(row["relative_path"])
    bucket = as_text(row["surface_bucket"])
    tags: list[str] = []
    blocked = ""
    confidence = 0.78
    subpolicy = bucket
    rationale = ""
    decision = "blocked_uncertain"
    lifecycle = False
    preserve_tokens = tokens_preserved(text, english)
    length = visible_len(text)
    words = word_count(text)

    sensitive = any(
        marker in path or marker in key.lower()
        for marker in (
            "religion",
            "faith",
            "culture",
            "title",
            "nickname",
            "law",
            "war",
            "struggle",
            "dynasty",
            "house",
            "custom_localization",
        )
    )
    dynamic_semantic = bool(GET_DYNAMIC_RE.search(text))
    multiline = "\n" in text.strip()
    quote = bucket == "quote_or_dialogue_fragment"

    if not preserve_tokens:
        decision = "token_boundary_repair_needed"
        tags.append("token_boundary")
        rationale = "token/markup possivelmente incompleto ou desalinhado"
        blocked = "token_boundary"
        confidence = 0.88
    elif SPANISH_RE.search(text):
        decision = "spanish_residual_repair_needed"
        tags.append("spanish_residual")
        rationale = "residual espanhol visível no texto atual"
        blocked = "spanish_residual"
        confidence = 0.9
    elif ENGLISH_RE.search(text) and not any(term in text for term in ("Crusader Kings", "Creator Pack")):
        decision = "english_residual_repair_needed"
        tags.append("english_residual")
        rationale = "possível residual inglês em superfície curta"
        blocked = "english_residual"
        confidence = 0.82
    elif any(fragment in text for fragment in (" de o ", " a o ", " para o a ", " pode não ", "não pode não")):
        decision = "ptbr_fluency_repair_needed"
        tags.append("ptbr_fluency")
        rationale = "fluência PT-BR parece literal ou truncada"
        blocked = "ptbr_fluency"
        confidence = 0.74
    elif sensitive or dynamic_semantic:
        if bucket in {"custom_localization_nominal", "compact_label"} and words <= 5 and not dynamic_semantic:
            decision = "lifecycle_ready_nominal_label"
            lifecycle = True
            subpolicy = "nominal_label_pair_false_reopen"
            rationale = "label nominal curto e sem dependência dinâmica visível"
            confidence = 0.82
        else:
            decision = "needs_domain_context"
            tags.append("domain_sensitive")
            if dynamic_semantic:
                tags.append("dynamic_context")
            rationale = "depende de domínio ou expressão dinâmica para validar sentido"
            blocked = "domain_or_dynamic_context"
            confidence = 0.76
    elif bucket == "compact_punctuated_option" and words <= 7 and length <= 60 and not multiline:
        decision = "lifecycle_ready_compact_option"
        lifecycle = True
        subpolicy = "compact_option_pair_false_reopen"
        rationale = "opção compacta natural em PT-BR, sem resíduo visível"
        confidence = 0.86
    elif bucket == "compact_label" and words <= 5 and length <= 45:
        decision = "lifecycle_ready_nominal_label"
        lifecycle = True
        subpolicy = "compact_nominal_label_pair_false_reopen"
        rationale = "label compacto natural em PT-BR"
        confidence = 0.84
    elif bucket == "short_phrase" and length <= 80 and not multiline and not quote:
        decision = "lifecycle_ready_short_phrase"
        lifecycle = True
        subpolicy = "short_phrase_pair_false_reopen"
        rationale = "frase curta natural em PT-BR, sem resíduo visível"
        confidence = 0.82
    elif quote and length <= 90 and not multiline:
        decision = "lifecycle_ready_quote_fragment"
        lifecycle = True
        subpolicy = "quote_fragment_pair_false_reopen"
        rationale = "fragmento curto de fala parece natural e sem resíduo visível"
        confidence = 0.8
    elif bucket in {"load_tip_or_long_hint", "event_context_short_surface", "longer_context_needed", "quote_or_dialogue_fragment"}:
        if "board_games." in key or "load_tips" in path:
            decision = "needs_new_microagent"
            tags.append("reusable_surface_family")
            subpolicy = "board_or_load_tip_short_context_microagent"
            rationale = "família reutilizável pede microagente estreito antes de fechar"
            blocked = "needs_new_microagent"
            confidence = 0.78
        else:
            decision = "needs_context_composer"
            tags.append("context_required")
            subpolicy = "short_context_composer"
            rationale = "superfície curta/contextual precisa de compositor antes de fechar"
            blocked = "needs_context_composer"
            confidence = 0.72
    else:
        decision = "blocked_uncertain"
        tags.append("insufficient_evidence")
        rationale = "evidência insuficiente para lifecycle seguro"
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
                SUM(CASE WHEN issue_family = 'semantic_review_router'
                          AND issue_kind = 'needs_human_or_semantic_conflict'
                         THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family = 'short_label_style_microagent'
                          AND issue_kind = 'short_or_compact_label_reopened'
                         THEN 1 ELSE 0 END) AS short_label_count,
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
        WHERE issue_segments.issue_count = 2
          AND issue_segments.semantic_count = 1
          AND issue_segments.short_label_count = 1
          AND issue_segments.high_issue_count = 0
          AND state.state_group = 'pending'
          AND COALESCE(state.needs_output_apply, 0) = 0
          AND COALESCE(state.confirmed_matches_output, 0) = 1
          AND COALESCE(state.review_state, '') NOT IN ('human_locked', 'human_confirmed')
          AND COALESCE(state.confirmation_level, '') NOT IN ('human_locked', 'human_confirmed')
          AND COALESCE(state.locked, 0) = 0
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
        bucket_rows.sort(key=lambda row: (-min(visible_len(as_text(row["current_text"])), 240), package_name(row["relative_path"]), row["source_key"], row["segment_id"]))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    base_quota = max(1, limit // max(1, len(BUCKET_ORDER)))
    for bucket in BUCKET_ORDER:
        for row in grouped.get(bucket, [])[:base_quota]:
            if int(row["segment_id"]) not in selected_ids and len(selected) < limit:
                selected.append(row)
                selected_ids.add(int(row["segment_id"]))
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
    leftovers.sort(key=lambda row: (BUCKET_ORDER.index(row["surface_bucket"]) if row["surface_bucket"] in BUCKET_ORDER else 99, package_name(row["relative_path"]), row["source_key"], row["segment_id"]))
    for row in leftovers:
        if len(selected) >= limit:
            break
        selected.append(row)
        selected_ids.add(int(row["segment_id"]))
    return selected[:limit]


def load_excluded_segment_ids(paths: list[str]) -> set[int]:
    excluded: set[int] = set()
    for raw_path in paths:
        path = Path(raw_path)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                excluded.add(int(row["segment_id"]))
    return excluded


def write_outputs(
    reviewed: list[dict[str, Any]],
    eligible_total: int,
    out_prefix: str,
    *,
    excluded_prior_reviewed_count: int = 0,
    excluded_relative_path_prefix_count: int = 0,
    eligible_after_exclusion: int | None = None,
    exclude_reviewed_jsonls: list[str] | None = None,
    exclude_relative_path_prefixes: list[str] | None = None,
) -> tuple[Path, Path]:
    out_jsonl = Path(f"reports/{out_prefix}_short_label_semantic_pair_current_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{out_prefix}_short_label_semantic_pair_current_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in reviewed) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(
        build_report(
            reviewed,
            eligible_total,
            excluded_prior_reviewed_count=excluded_prior_reviewed_count,
            excluded_relative_path_prefix_count=excluded_relative_path_prefix_count,
            eligible_after_exclusion=eligible_after_exclusion,
            exclude_reviewed_jsonls=exclude_reviewed_jsonls or [],
            exclude_relative_path_prefixes=exclude_relative_path_prefixes or [],
        ),
        encoding="utf-8",
    )
    return out_jsonl, out_txt


def build_report(
    reviewed: list[dict[str, Any]],
    eligible_total: int,
    *,
    excluded_prior_reviewed_count: int = 0,
    excluded_relative_path_prefix_count: int = 0,
    eligible_after_exclusion: int | None = None,
    exclude_reviewed_jsonls: list[str] | None = None,
    exclude_relative_path_prefixes: list[str] | None = None,
) -> str:
    by_decision = Counter(item["decision"] for item in reviewed)
    by_bucket_decision = Counter(f"{item['surface_bucket']} + {item['decision']}" for item in reviewed)
    by_package_decision = Counter(f"{package_name(item['relative_path'])} + {item['decision']}" for item in reviewed)
    lifecycle_count = sum(1 for item in reviewed if item["lifecycle_candidate"])
    apply_count = sum(1 for item in reviewed if item["requires_apply_later"])
    patterns = Counter(item["suggested_subpolicy"] for item in reviewed)
    blockers = Counter(item["blocked_reason"] for item in reviewed if item["blocked_reason"])
    lifecycle_examples = [item for item in reviewed if item["lifecycle_candidate"]][:8]
    repair_examples = [item for item in reviewed if item["decision"].endswith("_repair_needed") or item["decision"] == "needs_repair"][:8]
    context_examples = [
        item
        for item in reviewed
        if item["decision"] in {"needs_context_composer", "needs_domain_context", "needs_new_microagent"}
    ][:8]

    if lifecycle_count >= 70:
        recommendation = "preparar lifecycle batch 5 read-only estreito para os lifecycle_ready_* com guards de ledger exato e tokens."
    else:
        recommendation = "migrar para autofix_unknown + semantic ou criar compositor dos bloqueios dominantes antes de lifecycle amplo."
    if apply_count:
        recommendation += " Separar reparos em prompt de apply protegido."

    lines = [
        "Short label + semantic pair current review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total elegível no bloco: {eligible_total}",
        f"Excluded prior reviewed count: {excluded_prior_reviewed_count}",
        f"Excluded relative_path prefix count: {excluded_relative_path_prefix_count}",
        f"Total elegivel apos exclusao: {eligible_after_exclusion if eligible_after_exclusion is not None else eligible_total}",
        f"Total revisado: {len(reviewed)}",
        "",
        "Contagem por decisão:",
    ]
    if exclude_reviewed_jsonls:
        lines.insert(-2, "JSONLs excluidos:")
        for path in reversed(exclude_reviewed_jsonls):
            lines.insert(-2, f"- {path}")
        lines.insert(-2, "")
    if exclude_relative_path_prefixes:
        lines.insert(-2, "relative_path prefixes excluidos:")
        for prefix in reversed(exclude_relative_path_prefixes):
            lines.insert(-2, f"- {prefix}")
        lines.insert(-2, "")

    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por surface bucket + decisão:")
    for key, value in by_bucket_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por pacote + decisão:")
    for key, value in by_package_decision.most_common(30):
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates: {lifecycle_count}",
            f"Apply candidates: {apply_count}",
            "",
            "Top 10 padrões aproveitáveis:",
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
            "Recomendação objetiva:",
            f"- {recommendation}",
            "",
            "Validações finais:",
            "- JSONL UTF-8 válido.",
            "- Nenhum apply, production, confirmation, reindex, treino/model promotion, segment-state ou lifecycle executado.",
            "- Nenhuma escrita em source/ ou output/.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-run-id", type=int, default=70)
    parser.add_argument("--segment-state-run-id", type=int, default=353)
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--exclude-reviewed-jsonl", action="append", default=[])
    parser.add_argument("--exclude-relative-path-prefix", action="append", default=[])
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        eligible = load_eligible(conn, args.ledger_run_id, args.segment_state_run_id)

    excluded_ids = load_excluded_segment_ids(args.exclude_reviewed_jsonl)
    eligible_after_jsonl_exclusion = [row for row in eligible if int(row["segment_id"]) not in excluded_ids]
    excluded_prior_reviewed_count = len(eligible) - len(eligible_after_jsonl_exclusion)
    excluded_prefixes = tuple(args.exclude_relative_path_prefix)
    eligible_after_exclusion = [
        row
        for row in eligible_after_jsonl_exclusion
        if not excluded_prefixes or not as_text(row["relative_path"]).startswith(excluded_prefixes)
    ]
    excluded_relative_path_prefix_count = len(eligible_after_jsonl_exclusion) - len(eligible_after_exclusion)

    sample = stratified_sample(eligible_after_exclusion, args.limit)
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
    out_jsonl, out_txt = write_outputs(
        reviewed,
        len(eligible),
        stamp,
        excluded_prior_reviewed_count=excluded_prior_reviewed_count,
        excluded_relative_path_prefix_count=excluded_relative_path_prefix_count,
        eligible_after_exclusion=len(eligible_after_exclusion),
        exclude_reviewed_jsonls=args.exclude_reviewed_jsonl,
        exclude_relative_path_prefixes=args.exclude_relative_path_prefix,
    )
    print(out_jsonl)
    print(out_txt)
    print("eligible_total", len(eligible))
    print("excluded_prior_reviewed_count", excluded_prior_reviewed_count)
    print("excluded_relative_path_prefix_count", excluded_relative_path_prefix_count)
    print("eligible_after_exclusion", len(eligible_after_exclusion))
    print("reviewed", len(reviewed))
    print("decisions", dict(Counter(item["decision"] for item in reviewed)))
    print("lifecycle", sum(1 for item in reviewed if item["lifecycle_candidate"]))
    print("apply", sum(1 for item in reviewed if item["requires_apply_later"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
