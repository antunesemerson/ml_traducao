from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "dynamic_ck3_expression_current_sublane_review_v1"
REQUIRED_FIELDS = {
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "issue_families",
    "dynamic_issue_kinds",
    "token_signature",
    "path_group",
    "text_length_bucket",
    "decision",
    "sublane",
    "lifecycle_candidate",
    "requires_apply_later",
    "corrected_text",
    "tokens_preserved",
    "confidence",
    "risk_flags",
    "blocked_reason",
    "rationale",
    "current_text",
    "english_text",
    "spanish_text",
}
ALLOWED_DECISIONS = {
    "dynamic_false_reopen_lifecycle_candidate",
    "needs_gender_dynamic_token_microagent",
    "needs_select_cstring_dynamic_composer",
    "needs_custom_loc_gender_context",
    "needs_concept_expression_policy",
    "needs_effect_list_dynamic_semantic_agent",
    "needs_get_trait_or_script_value_policy",
    "needs_dynamic_token_boundary_repair",
    "needs_spanish_residual_repair",
    "needs_english_residual_repair",
    "needs_semantic_context_composer",
    "needs_domain_context",
    "needs_new_microagent",
    "blocked_uncertain",
}
PRIORITY_COMBOS = [
    ("dynamic_ck3_expression_microagent",),
    ("dynamic_ck3_expression_microagent", "gender_token_microagent"),
    ("dynamic_ck3_expression_microagent", "short_label_style_microagent"),
    ("dynamic_ck3_expression_microagent", "semantic_review_router"),
    ("dynamic_ck3_expression_microagent", "spanish_residual_microagent"),
]


TOKEN_PATTERNS = [
    ("select_cstring", re.compile(r"Select_CString\s*\(")),
    ("custom_loc", re.compile(r"Custom\s*\(")),
    ("concept", re.compile(r"\bConcept\s*\(|\[[^\]]*\|E\]|\[[^\]]*concept[^\]]*\]", re.IGNORECASE)),
    ("script_value", re.compile(r"\bScriptValue\b")),
    ("get_trait", re.compile(r"\bGetTrait\b")),
    ("get_activity_type", re.compile(r"\bGetActivityType\b")),
    ("get_title_by_key", re.compile(r"\bGetTitleByKey\b")),
    ("local_player_string", re.compile(r"\bGetLocalPlayer[A-Za-z0-9_]*\b")),
    ("effect_list", re.compile(r"\$EFFECT_LIST_BULLET\$")),
    ("dollar_var", re.compile(r"\$[^$]+\$")),
    ("bracket_loc", re.compile(r"\[[^\]]+\]")),
    ("formatting", re.compile(r"#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!")),
]
SPANISH_RE = re.compile(
    r"\b(?:verdadero|verdadera|muchos|muchas|penalizaciones|migaja|amarillo|reino|condado|duque|"
    r"señor|senor|dinastia|guerra|cultura|fe|recuperacion|probabilidad|si pierdes|son)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(
    r"\b(?:will|must|cannot|should|kingdom|duchy|county|culture head|lose nothing|for each|"
    r"the\s+[A-Za-z]+|your\s+[A-Za-z]+|their\s+[A-Za-z]+)\b",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"(culture|religion|faith|title|nickname|law|war|casus|cb|struggle|dynasty|house|artifact|legend|lore|government)",
    re.IGNORECASE,
)
GENDER_CUSTOM_RE = re.compile(r"Custom\s*\(\s*['\"]ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)[^'\"]*['\"]", re.IGNORECASE)
TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def path_group(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


def visible_len(text: str) -> int:
    return len(text.replace("\\n", "\n").strip())


def text_length_bucket(text: str) -> str:
    length = visible_len(text)
    if length <= 60:
        return "short"
    if length <= 140:
        return "medium"
    if length <= 260:
        return "long"
    return "very_long"


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


def token_signature(text: str) -> str:
    parts = [name for name, pattern in TOKEN_PATTERNS if pattern.search(text)]
    return "+".join(parts) if parts else "none"


def combo_key(families: list[str]) -> str:
    return " + ".join(sorted(set(families)))


def combo_priority(families: list[str]) -> int:
    family_set = set(families)
    for idx, combo in enumerate(PRIORITY_COMBOS):
        if family_set == set(combo):
            return idx
    if "dynamic_ck3_expression_microagent" in family_set:
        return len(PRIORITY_COMBOS)
    return len(PRIORITY_COMBOS) + 1


def classify(row: dict[str, Any], ledger_run_id: int, state_run_id: int) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    spanish = as_text(row["spanish_text"])
    families = [part for part in as_text(row["issue_families"]).split(";") if part]
    dynamic_kinds = [part for part in as_text(row["dynamic_issue_kinds"]).split(";") if part]
    signature = token_signature(text)
    preserve_tokens = tokens_preserved(text, english)
    high_issue = int(row["high_issue_count"] or 0) > 0
    risk_flags: list[str] = []
    decision = "blocked_uncertain"
    sublane = "uncertain_dynamic_context"
    blocked = "blocked_uncertain"
    lifecycle = False
    confidence = 0.72
    rationale = "dynamic expression requires narrower review"

    family_set = set(families)
    text_haystack = f"{row['relative_path']} {row['source_key']} {text}"

    if not preserve_tokens:
        decision = "needs_dynamic_token_boundary_repair"
        sublane = "dynamic_token_boundary"
        blocked = "token_boundary"
        risk_flags.append("token_boundary")
        confidence = 0.9
        rationale = "dynamic token or markup boundary appears unsafe"
    elif "spanish_residual_microagent" in family_set or SPANISH_RE.search(text):
        decision = "needs_spanish_residual_repair"
        sublane = "dynamic_spanish_residual"
        blocked = "spanish_residual"
        risk_flags.append("spanish_residual")
        confidence = 0.88
        rationale = "visible Spanish residual in dynamic text"
    elif ENGLISH_RE.search(text):
        decision = "needs_english_residual_repair"
        sublane = "dynamic_english_residual"
        blocked = "english_residual"
        risk_flags.append("english_residual")
        confidence = 0.84
        rationale = "visible English residual in dynamic text"
    elif "gender_token_microagent" in family_set and GENDER_CUSTOM_RE.search(text):
        decision = "needs_custom_loc_gender_context"
        sublane = "custom_loc_gender_token_context"
        blocked = "gender_custom_loc"
        risk_flags.append("gender_custom_loc")
        confidence = 0.84
        rationale = "Custom ES gender localization needs target validation"
    elif "gender_token_microagent" in family_set:
        decision = "needs_gender_dynamic_token_microagent"
        sublane = "dynamic_gender_token_context"
        blocked = "gender_token_context"
        risk_flags.append("gender_token")
        confidence = 0.82
        rationale = "dynamic text is paired with gender token issue"
    elif "select_cstring" in signature:
        decision = "needs_select_cstring_dynamic_composer"
        sublane = "select_cstring_dynamic_composer"
        blocked = "select_cstring_context"
        risk_flags.append("select_cstring")
        confidence = 0.84
        rationale = "Select_CString branch semantics require composer"
    elif "concept" in signature:
        decision = "needs_concept_expression_policy"
        sublane = "concept_expression_policy"
        blocked = "concept_expression"
        risk_flags.append("concept_expression")
        confidence = 0.8
        rationale = "CK3 concept expression controls text semantics"
    elif "effect_list" in signature or "\\n" in text or "\n" in text:
        decision = "needs_effect_list_dynamic_semantic_agent"
        sublane = "effect_list_or_multiline_dynamic"
        blocked = "effect_list_dynamic"
        risk_flags.append("effect_list_or_multiline")
        confidence = 0.78
        rationale = "effect/list or multiline tooltip needs dynamic semantic agent"
    elif any(part in signature for part in ("script_value", "get_trait", "get_activity_type", "get_title_by_key")):
        decision = "needs_get_trait_or_script_value_policy"
        sublane = "get_trait_script_value_policy"
        blocked = "get_trait_or_script_value"
        risk_flags.append("get_trait_or_script_value")
        confidence = 0.82
        rationale = "Get*/ScriptValue expression needs narrow policy"
    elif DOMAIN_RE.search(text_haystack) or family_set & {
        "culture_semantic_microagent",
        "religion_semantic_microagent",
        "title_policy_microagent",
        "nickname_name_policy",
    }:
        decision = "needs_domain_context"
        sublane = "dynamic_domain_context"
        blocked = "domain_context"
        risk_flags.append("domain_sensitive")
        confidence = 0.78
        rationale = "dynamic text touches sensitive domain context"
    elif family_set == {"dynamic_ck3_expression_microagent"} and not high_issue and visible_len(text) <= 90:
        decision = "dynamic_false_reopen_lifecycle_candidate"
        sublane = "single_dynamic_short_false_reopen"
        blocked = ""
        lifecycle = True
        confidence = 0.76
        rationale = "short dynamic-only text has preserved tokens and no visible residual"
    elif "semantic_review_router" in family_set:
        decision = "needs_semantic_context_composer"
        sublane = "dynamic_semantic_context"
        blocked = "semantic_context"
        risk_flags.append("semantic_context")
        confidence = 0.76
        rationale = "semantic companion requires contextual validation"
    elif "short_label_style_microagent" in family_set and visible_len(text) <= 80:
        decision = "needs_new_microagent"
        sublane = "dynamic_short_label_surface"
        blocked = "dynamic_short_label_surface"
        risk_flags.append("short_label_dynamic")
        confidence = 0.74
        rationale = "short label dynamic surface should get a narrow checkpoint"
    else:
        decision = "blocked_uncertain"
        sublane = "uncertain_dynamic_context"
        blocked = "blocked_uncertain"
        risk_flags.append("insufficient_evidence")
        confidence = 0.55
        rationale = "insufficient evidence for safe dynamic sublane"

    return {
        "ledger_run_id": ledger_run_id,
        "segment_state_run_id": state_run_id,
        "segment_id": int(row["segment_id"]),
        "relative_path": as_text(row["relative_path"]),
        "source_key": as_text(row["source_key"]),
        "issue_families": families,
        "dynamic_issue_kinds": dynamic_kinds,
        "token_signature": signature,
        "path_group": path_group(as_text(row["relative_path"])),
        "text_length_bucket": text_length_bucket(text),
        "decision": decision,
        "sublane": sublane,
        "lifecycle_candidate": lifecycle,
        "requires_apply_later": False,
        "corrected_text": "",
        "tokens_preserved": preserve_tokens,
        "confidence": confidence,
        "risk_flags": risk_flags,
        "blocked_reason": blocked,
        "rationale": rationale,
        "current_text": text,
        "english_text": english,
        "spanish_text": spanish,
    }


def load_universe(conn, ledger_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_segments AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'dynamic_ck3_expression_microagent' THEN 1 ELSE 0 END) AS dynamic_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical')
                         THEN 1 ELSE 0 END) AS high_issue_count,
                GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
                GROUP_CONCAT(CASE WHEN issue_family = 'dynamic_ck3_expression_microagent' THEN issue_kind END, ';') AS dynamic_issue_kinds,
                GROUP_CONCAT(issue_family || ':' || issue_kind || ':' || COALESCE(issue_severity, ''), '; ') AS issue_tags
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
            state.needs_output_apply,
            state.confirmed_matches_output,
            issue_segments.open_issue_count,
            issue_segments.dynamic_issue_count,
            issue_segments.high_issue_count,
            issue_segments.issue_families,
            issue_segments.dynamic_issue_kinds,
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
        WHERE issue_segments.dynamic_issue_count > 0
          AND state.state_group = 'pending'
        ORDER BY state.relative_path, state.source_key, state.segment_id
        """,
        (ledger_run_id, state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def select_sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        families = [part for part in as_text(row["issue_families"]).split(",") if part]
        grouped[combo_priority(families)].append(row)
    for bucket_rows in grouped.values():
        bucket_rows.sort(
            key=lambda row: (
                -int(row["dynamic_issue_count"] or 0),
                path_group(as_text(row["relative_path"])),
                token_signature(as_text(row["current_text"])),
                int(row["segment_id"]),
            )
        )
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for priority in sorted(grouped):
        quota = max(1, limit // max(1, len(grouped)))
        for row in grouped[priority][:quota]:
            if len(selected) >= limit:
                break
            segment_id = int(row["segment_id"])
            if segment_id not in selected_ids:
                selected.append(row)
                selected_ids.add(segment_id)
    leftovers = [
        row
        for priority in sorted(grouped)
        for row in grouped[priority]
        if int(row["segment_id"]) not in selected_ids
    ]
    leftovers.sort(
        key=lambda row: (
            combo_priority([part for part in as_text(row["issue_families"]).split(",") if part]),
            path_group(as_text(row["relative_path"])),
            token_signature(as_text(row["current_text"])),
            int(row["segment_id"]),
        )
    )
    for row in leftovers:
        if len(selected) >= limit:
            break
        selected.append(row)
        selected_ids.add(int(row["segment_id"]))
    return selected


def validate_review(rows: list[dict[str, Any]], expected_count: int) -> None:
    if len(rows) != expected_count:
        raise RuntimeError(f"Expected {expected_count} reviewed rows, got {len(rows)}")
    for row in rows:
        missing = REQUIRED_FIELDS - set(row)
        if missing:
            raise RuntimeError(f"Output row {row.get('segment_id')} missing fields: {sorted(missing)}")
        if row["decision"] not in ALLOWED_DECISIONS:
            raise RuntimeError(f"Unexpected decision {row['decision']}")
        if not 0.0 <= float(row["confidence"]) <= 1.0:
            raise RuntimeError(f"Invalid confidence for segment {row['segment_id']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise RuntimeError(f"Missing corrected_text for apply candidate {row['segment_id']}")
        corrected = as_text(row["corrected_text"])
        if corrected:
            if any(marker in corrected for marker in ("Ã", "Â", "�")):
                raise RuntimeError(f"Encoding marker in correction for segment {row['segment_id']}")
            if re.search(r"\w\?\w", corrected, flags=re.UNICODE):
                raise RuntimeError(f"Question mark inside word in correction for segment {row['segment_id']}")
            if protected_tokens_signature(row["current_text"]) != protected_tokens_signature(corrected):
                raise RuntimeError(f"Token mismatch in correction for segment {row['segment_id']}")


def universe_stats(rows: list[dict[str, Any]]) -> dict[str, Counter]:
    stats: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        families = [part for part in as_text(row["issue_families"]).split(",") if part]
        dynamic_kinds = [part for part in as_text(row["dynamic_issue_kinds"]).split(";") if part]
        text = as_text(row["current_text"])
        stats["combo_families"][combo_key(families)] += 1
        for kind in dynamic_kinds:
            stats["dynamic_issue_kind"][kind] += 1
        stats["token_signature"][token_signature(text)] += 1
        stats["path_group"][path_group(as_text(row["relative_path"]))] += 1
        stats["text_length_bucket"][text_length_bucket(text)] += 1
        stats["high_issue_count"][str(int(row["high_issue_count"] or 0))] += 1
    return stats


def build_report(reviewed: list[dict[str, Any]], universe: list[dict[str, Any]], stats: dict[str, Counter]) -> str:
    by_decision = Counter(row["decision"] for row in reviewed)
    by_sublane = Counter(row["sublane"] for row in reviewed)
    by_combo = Counter(combo_key(row["issue_families"]) for row in reviewed)
    by_signature = Counter(row["token_signature"] for row in reviewed)
    by_path = Counter(row["path_group"] for row in reviewed)
    lifecycle_count = sum(1 for row in reviewed if row["lifecycle_candidate"])
    apply_count = sum(1 for row in reviewed if row["requires_apply_later"])
    blockers = Counter(row["blocked_reason"] for row in reviewed if row["blocked_reason"])
    dynamic_issue_total = sum(int(row["dynamic_issue_count"] or 0) for row in universe)

    if by_sublane:
        next_sublane = by_sublane.most_common(1)[0][0]
    else:
        next_sublane = "none"
    if lifecycle_count >= 30:
        lifecycle_reco = "existe lote lifecycle seguro estreito"
    else:
        lifecycle_reco = "nao ha lote lifecycle amplo seguro nesta amostra"
    if apply_count:
        apply_reco = "separar lote apply protegido"
    else:
        apply_reco = "sem lote apply nesta etapa"

    lines = [
        "Dynamic CK3 expression current sublane review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total elegivel no universo dynamic atual: {len(universe)}",
        f"Total de issues dynamic no universo: {dynamic_issue_total}",
        f"Total revisado: {len(reviewed)}",
        "",
        "Diagnostico do universo - combo de families:",
    ]
    for key, value in stats["combo_families"].most_common(20):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Diagnostico do universo - issue_kind dynamic:")
    for key, value in stats["dynamic_issue_kind"].most_common(20):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Diagnostico do universo - token signature:")
    for key, value in stats["token_signature"].most_common(20):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Diagnostico do universo - path group:")
    for key, value in stats["path_group"].most_common(20):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Diagnostico do universo - tamanho:")
    for key, value in stats["text_length_bucket"].most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Diagnostico do universo - high issue count:")
    for key, value in stats["high_issue_count"].most_common():
        lines.append(f"- {key}: {value}")

    lines.append("")
    lines.append("Contagem por decision:")
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por sublane:")
    for key, value in by_sublane.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por combo de families:")
    for key, value in by_combo.most_common(20):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por token signature:")
    for key, value in by_signature.most_common(20):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por pacote/path group:")
    for key, value in by_path.most_common(20):
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates futuros: {lifecycle_count}",
            f"Apply candidates futuros: {apply_count}",
            "",
            "Top 10 sublanes mais promissoras por volume:",
        ]
    )
    for key, value in by_sublane.most_common(10):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Top 10 riscos/bloqueios:")
    for key, value in blockers.most_common(10):
        lines.append(f"- {key}: {value}")

    example_specs = [
        ("Exemplos lifecycle:", lambda row: row["lifecycle_candidate"]),
        ("Exemplos genero dinamico:", lambda row: row["decision"] in {"needs_gender_dynamic_token_microagent", "needs_custom_loc_gender_context"}),
        ("Exemplos Select_CString:", lambda row: row["decision"] == "needs_select_cstring_dynamic_composer"),
        ("Exemplos effect/list:", lambda row: row["decision"] == "needs_effect_list_dynamic_semantic_agent"),
        ("Exemplos residual espanhol/ingles:", lambda row: row["decision"] in {"needs_spanish_residual_repair", "needs_english_residual_repair"}),
        ("Exemplos bloqueados incertos:", lambda row: row["decision"] == "blocked_uncertain"),
    ]
    for title, predicate in example_specs:
        examples = [row for row in reviewed if predicate(row)][:5]
        if not examples:
            continue
        lines.append("")
        lines.append(title)
        for row in examples:
            lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['current_text'][:120]}")

    lines.extend(
        [
            "",
            "Recomendacao objetiva:",
            f"- proximo prompt recomendado: {next_sublane}",
            f"- lifecycle: {lifecycle_reco}",
            f"- apply: {apply_reco}",
            "- melhor caminho: criar microagente novo para a sublane dominante antes de lifecycle generico.",
            "",
            "Validacoes finais:",
            "- JSONL UTF-8 valido.",
            "- Todos os campos obrigatorios presentes.",
            "- Nenhum corrected_text vazio quando requires_apply_later=true.",
            "- Nenhum reparo com token CK3 perdido/alterado.",
            "- Nenhum marcador ruim de encoding em corrected_text.",
            "- Nenhum ? dentro de palavra em corrected_text.",
            "- Nenhuma escrita em source/ ou output/.",
            "- Nenhum apply, production, confirmation, reindex, treino/model promotion, segment-state ou lifecycle executado.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(reviewed: list[dict[str, Any]], universe: list[dict[str, Any]], stats: dict[str, Counter]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_ck3_expression_current_sublane_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_ck3_expression_current_sublane_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(reviewed, universe, stats), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-run-id", type=int, default=76)
    parser.add_argument("--segment-state-run-id", type=int, default=368)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        universe = load_universe(conn, args.ledger_run_id, args.segment_state_run_id)
    sample = select_sample(universe, min(args.limit, len(universe)))
    reviewed = [classify(row, args.ledger_run_id, args.segment_state_run_id) for row in sample]
    validate_review(reviewed, min(args.limit, len(universe)))
    stats = universe_stats(universe)
    out_jsonl, out_txt = write_outputs(reviewed, universe, stats)
    print(out_jsonl)
    print(out_txt)
    print("eligible_dynamic_segments", len(universe))
    print("dynamic_issue_total", sum(int(row["dynamic_issue_count"] or 0) for row in universe))
    print("reviewed", len(reviewed))
    print("decisions", dict(Counter(row["decision"] for row in reviewed)))
    print("sublanes", dict(Counter(row["sublane"] for row in reviewed).most_common(10)))
    print("token_signatures", dict(Counter(row["token_signature"] for row in reviewed).most_common(10)))
    print("lifecycle", sum(1 for row in reviewed if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in reviewed if row["requires_apply_later"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
