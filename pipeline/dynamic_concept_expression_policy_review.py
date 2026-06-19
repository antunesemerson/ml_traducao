from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "dynamic_concept_expression_policy_review_v1"
MICROAGENT = "dynamic_concept_expression_policy"
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "ledger_run_id",
    "segment_state_run_id",
    "relative_path",
    "source_key",
    "issue_families",
    "dynamic_issue_kinds",
    "token_signature",
    "current_text",
    "english_text",
    "spanish_text",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_dynamic_review_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "concept_pattern",
    "concept_lane",
    "decision",
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
    "concept_expression_ready_lifecycle",
    "concept_expression_style_watch_lifecycle",
    "concept_expression_needs_domain_policy",
    "concept_expression_needs_effect_list_policy",
    "concept_expression_needs_get_trait_or_script_value_policy",
    "concept_expression_needs_gender_or_custom_loc_policy",
    "concept_expression_needs_token_boundary_repair",
    "concept_expression_needs_spanish_residual_repair",
    "concept_expression_needs_english_residual_repair",
    "concept_expression_needs_semantic_rewrite",
    "concept_expression_blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
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
    r"(culture|tradition|innovation|religion|faith|law|government|title|casus|cb|war|artifact|legend|lore)",
    re.IGNORECASE,
)
GET_TRAIT_SCRIPT_RE = re.compile(
    r"\b(?:GetTrait|ScriptValue|GetActivityType|GetLaw|GetTitleByKey|GetCultureTradition|GetMaA)\b"
)
GENDER_CUSTOM_RE = re.compile(
    r"Custom\s*\(\s*['\"]ES_|Select_CString\s*\(|gender_token_microagent",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(r"\$EFFECT_LIST_BULLET\$|\\n|\n|requirement|trigger|effect|tooltip", re.IGNORECASE)
MOJIBAKE_RE = re.compile(r"[\u00c3\u00c2\ufffd]")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def path_group(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


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


def concept_pattern(text: str, token_signature: str) -> str:
    has_concept_func = bool(re.search(r"\bConcept\s*\(", text))
    has_bracket_concept = "concept" in token_signature or bool(re.search(r"\[[^\]]*\|E\]", text))
    if "effect_list" in token_signature or "$EFFECT_LIST_BULLET$" in text:
        return "concept_with_effect_list"
    if "get_trait" in token_signature or "GetTrait" in text:
        return "concept_with_get_trait"
    if "GetCultureTradition" in text:
        return "concept_with_culture_tradition"
    if "script_value" in token_signature or "ScriptValue" in text:
        return "concept_with_script_value"
    if "custom_loc" in token_signature or "Custom(" in text:
        return "concept_with_custom_loc"
    if "select_cstring" in token_signature or "Select_CString" in text:
        return "concept_with_select_cstring"
    if has_concept_func:
        return "Concept_function"
    if has_bracket_concept:
        return "bracket_concept"
    return "mixed_concept_dynamic"


def concept_lane(relative_path: str, source_key: str, text: str) -> str:
    haystack = f"{relative_path} {source_key} {text}"
    lower = haystack.lower()
    if EFFECT_LIST_RE.search(haystack):
        return "effect_or_requirement_tooltip"
    if re.search(r"culture|tradition|innovation|religion|faith|law", lower):
        return "culture_or_law_domain"
    if re.search(r"activity|activities|event|\.desc|\.option", lower):
        return "activity_or_event_context"
    if re.search(r"trait|modifier|perk", lower):
        return "trait_or_modifier_context"
    if re.search(r"title|government|realm|vassal|liege", lower):
        return "title_or_government_context"
    if "concept" in lower:
        return "plain_concept_sentence"
    return "other"


def is_candidate(row: dict[str, Any]) -> bool:
    return row.get("decision") == "needs_concept_expression_policy" or row.get("sublane") == "concept_expression_policy"


def classify(row: dict[str, Any], source_path: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    spanish = as_text(row["spanish_text"])
    relative_path = as_text(row["relative_path"])
    source_key = as_text(row["source_key"])
    families = row.get("issue_families") or []
    if isinstance(families, str):
        families = [part for part in families.split(";") if part]
    token_sig = as_text(row["token_signature"])
    pattern = concept_pattern(text, token_sig)
    lane = concept_lane(relative_path, source_key, text)
    preserve_tokens = tokens_preserved(text, english)
    family_set = set(families)
    risk_flags: list[str] = []
    decision = "concept_expression_blocked_uncertain"
    lifecycle = False
    blocked = "blocked_uncertain"
    confidence = 0.72
    rationale = "conceito dinamico precisa de politica estreita"
    haystack = f"{relative_path} {source_key} {text}"

    if not preserve_tokens:
        decision = "concept_expression_needs_token_boundary_repair"
        blocked = "token_boundary"
        risk_flags.append("token_boundary")
        confidence = 0.9
        rationale = "fronteira de token/markup de conceito precisa reparo"
    elif "spanish_residual_microagent" in family_set or SPANISH_RE.search(text):
        decision = "concept_expression_needs_spanish_residual_repair"
        blocked = "spanish_residual"
        risk_flags.append("spanish_residual")
        confidence = 0.88
        rationale = "residual espanhol visivel em expressao de conceito"
    elif ENGLISH_RE.search(text):
        decision = "concept_expression_needs_english_residual_repair"
        blocked = "english_residual"
        risk_flags.append("english_residual")
        confidence = 0.84
        rationale = "residual ingles visivel em expressao de conceito"
    elif MOJIBAKE_RE.search(text):
        decision = "concept_expression_needs_semantic_rewrite"
        blocked = "mojibake_or_semantic_rewrite"
        risk_flags.append("mojibake")
        confidence = 0.78
        rationale = "texto atual tem marcador de encoding ou precisa reescrita semantica"
    elif GENDER_CUSTOM_RE.search(haystack) or family_set & {"gender_token_microagent"}:
        decision = "concept_expression_needs_gender_or_custom_loc_policy"
        blocked = "gender_or_custom_loc_policy"
        risk_flags.append("gender_or_custom_loc")
        confidence = 0.84
        rationale = "conceito esta misturado com genero dinamico/custom localization"
    elif GET_TRAIT_SCRIPT_RE.search(haystack) or pattern in {
        "concept_with_get_trait",
        "concept_with_script_value",
        "concept_with_culture_tradition",
    }:
        decision = "concept_expression_needs_get_trait_or_script_value_policy"
        blocked = "get_trait_or_script_value_policy"
        risk_flags.append("get_trait_or_script_value")
        confidence = 0.82
        rationale = "conceito esta misturado com Get*/ScriptValue e precisa politica dedicada"
    elif lane == "effect_or_requirement_tooltip" or pattern == "concept_with_effect_list":
        decision = "concept_expression_needs_effect_list_policy"
        blocked = "effect_list_policy"
        risk_flags.append("effect_or_requirement_tooltip")
        confidence = 0.8
        rationale = "conceito aparece em lista de efeito/requisito ou tooltip mecanico"
    elif DOMAIN_RE.search(haystack) or family_set & {
        "culture_semantic_microagent",
        "religion_semantic_microagent",
        "title_policy_microagent",
        "nickname_name_policy",
    }:
        decision = "concept_expression_needs_domain_policy"
        blocked = "domain_policy"
        risk_flags.append("domain_sensitive")
        confidence = 0.8
        rationale = "conceito envolve dominio sensivel antes de lifecycle"
    elif family_set - {"dynamic_ck3_expression_microagent"}:
        decision = "concept_expression_blocked_uncertain"
        blocked = "companion_issue_context"
        risk_flags.append("companion_issue_context")
        confidence = 0.62
        rationale = "ha issue companheira que impede lifecycle conceitual seguro"
    elif lane == "plain_concept_sentence" and pattern in {"Concept_function", "bracket_concept"}:
        decision = "concept_expression_ready_lifecycle"
        lifecycle = True
        blocked = ""
        confidence = 0.82
        rationale = "frase de conceito simples, tokens preservados e sem residuo visivel"
    else:
        decision = "concept_expression_style_watch_lifecycle"
        lifecycle = True
        blocked = ""
        confidence = 0.76
        rationale = "expressao de conceito parece fechavel, mas merece watch de estilo/contexto leve"

    return {
        "source_dynamic_review_jsonl": source_path,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": relative_path,
        "source_key": source_key,
        "microagent": MICROAGENT,
        "concept_pattern": pattern,
        "concept_lane": lane,
        "decision": decision,
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


def load_candidates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            missing = REQUIRED_INPUT_FIELDS - set(row)
            if missing:
                raise RuntimeError(f"Input row {line_number} missing fields: {sorted(missing)}")
            if is_candidate(row):
                rows.append(row)
    return rows


def validate_outputs(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        missing = REQUIRED_OUTPUT_FIELDS - set(row)
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


def combo_key(row: dict[str, Any]) -> str:
    families = row.get("issue_families") or []
    if isinstance(families, str):
        families = [part for part in families.split(";") if part]
    return " + ".join(sorted(families))


def build_report(rows: list[dict[str, Any]], input_rows: list[dict[str, Any]], expected_count: int) -> str:
    by_decision = Counter(row["decision"] for row in rows)
    by_pattern = Counter(row["concept_pattern"] for row in rows)
    by_lane = Counter(row["concept_lane"] for row in rows)
    by_combo = Counter(combo_key(row) for row in input_rows)
    by_path = Counter(path_group(row["relative_path"]) for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    policy_count = sum(
        1
        for row in rows
        if row["decision"]
        in {
            "concept_expression_needs_domain_policy",
            "concept_expression_needs_effect_list_policy",
            "concept_expression_needs_get_trait_or_script_value_policy",
            "concept_expression_needs_gender_or_custom_loc_policy",
        }
    )
    ready_or_watch = sum(
        1
        for row in rows
        if row["decision"] in {"concept_expression_ready_lifecycle", "concept_expression_style_watch_lifecycle"}
    )
    effect_like = by_decision.get("concept_expression_needs_effect_list_policy", 0)
    domain_like = by_decision.get("concept_expression_needs_domain_policy", 0)

    recommendations: list[str] = []
    if ready_or_watch >= 20:
        recommendations.append("sugerir prompt de lifecycle read-only para ready/style_watch")
    if apply_count:
        recommendations.append("separar reparos seguros em prompt de apply protegido")
    if effect_like > len(rows) / 2:
        recommendations.append("maioria caiu em effect/list; mover para effect_list_or_multiline_dynamic")
    if domain_like > len(rows) / 2:
        recommendations.append("maioria caiu em dominio; preparar dominio/policy antes de lifecycle")
    if not recommendations:
        recommendations.append("priorizar a policy dominante antes de lifecycle generico")

    lines = [
        "Dynamic concept expression policy review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total processado: {len(rows)}",
        f"Total esperado: {expected_count}",
        f"Divergencia explicada: {'none' if len(rows) == expected_count else f'expected {expected_count}, got {len(rows)}'}",
        "",
        "Contagem por decision:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por concept_pattern:")
    for key, value in by_pattern.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por concept_lane:")
    for key, value in by_lane.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por combo de issue families:")
    for key, value in by_combo.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por path group/pacote:")
    for key, value in by_path.most_common():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates futuros: {lifecycle_count}",
            f"Apply candidates futuros: {apply_count}",
            f"Precisam politica dominio/effect/get_trait/genero: {policy_count}",
        ]
    )
    example_decisions = [
        "concept_expression_ready_lifecycle",
        "concept_expression_style_watch_lifecycle",
        "concept_expression_needs_domain_policy",
        "concept_expression_needs_effect_list_policy",
        "concept_expression_needs_get_trait_or_script_value_policy",
        "concept_expression_needs_gender_or_custom_loc_policy",
        "concept_expression_needs_token_boundary_repair",
        "concept_expression_needs_spanish_residual_repair",
        "concept_expression_needs_english_residual_repair",
        "concept_expression_needs_semantic_rewrite",
        "concept_expression_blocked_uncertain",
    ]
    for decision in example_decisions:
        examples = [row for row in rows if row["decision"] == decision][:8]
        if not examples:
            continue
        lines.append("")
        lines.append(f"Exemplos {decision}:")
        for row in examples:
            lines.append(f"- {row['segment_id']} | {row['source_key']} | {row['current_text'][:140]}")
    lines.extend(
        [
            "",
            "Recomendacao objetiva:",
            *[f"- {item}" for item in recommendations],
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


def write_outputs(rows: list[dict[str, Any]], input_rows: list[dict[str, Any]], expected_count: int) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_concept_expression_policy_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_concept_expression_policy_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, input_rows, expected_count), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-review-jsonl", required=True)
    args = parser.parse_args()
    review_path = Path(args.dynamic_review_jsonl)
    input_rows = load_candidates(review_path)
    output_rows = [classify(row, str(review_path)) for row in input_rows]
    validate_outputs(output_rows)
    out_jsonl, out_txt = write_outputs(output_rows, input_rows, expected_count=45)
    by_decision = Counter(row["decision"] for row in output_rows)
    by_pattern = Counter(row["concept_pattern"] for row in output_rows)
    by_lane = Counter(row["concept_lane"] for row in output_rows)
    print(out_jsonl)
    print(out_txt)
    print("processed", len(output_rows))
    print("decisions", dict(by_decision))
    print("patterns", dict(by_pattern))
    print("lanes", dict(by_lane))
    print("lifecycle", sum(1 for row in output_rows if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in output_rows if row["requires_apply_later"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
