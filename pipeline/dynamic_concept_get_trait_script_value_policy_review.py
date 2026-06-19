from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "dynamic_concept_get_trait_script_value_policy_review_v1"
MICROAGENT = "dynamic_concept_get_trait_script_value_policy"
PRIMARY_DECISION = "concept_expression_needs_get_trait_or_script_value_policy"
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "ledger_run_id",
    "segment_state_run_id",
    "relative_path",
    "source_key",
    "current_text",
    "english_text",
    "spanish_text",
    "concept_pattern",
    "concept_lane",
    "decision",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_scope",
    "source_concept_review_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "concept_pattern",
    "concept_lane",
    "call_signature",
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
    "concept_get_call_ready_lifecycle",
    "concept_get_call_style_watch_lifecycle",
    "needs_effect_requirement_policy",
    "needs_culture_tradition_policy",
    "needs_title_or_law_dynamic_policy",
    "needs_trait_requirement_policy",
    "needs_script_value_numeric_policy",
    "needs_activity_type_policy",
    "needs_domain_context",
    "needs_gender_or_custom_loc_policy",
    "needs_spanish_residual_repair",
    "needs_english_residual_repair",
    "needs_token_boundary_repair",
    "needs_semantic_rewrite",
    "blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
CALL_PATTERNS = [
    ("GetTrait", re.compile(r"\bGetTrait\b")),
    ("ScriptValue", re.compile(r"\bScriptValue\b")),
    ("GetActivityType", re.compile(r"\bGetActivityType\b")),
    ("GetLaw", re.compile(r"\bGetLaw\b")),
    ("GetTitleByKey", re.compile(r"\bGetTitleByKey\b")),
    ("GetCultureTradition", re.compile(r"\bGetCultureTradition\b")),
    ("GetScheme", re.compile(r"\bGetScheme\b")),
    ("GetMaA", re.compile(r"\bGetMaA\b")),
    ("GetBuilding", re.compile(r"\bGetBuilding\b")),
    ("GetPerk", re.compile(r"\bGetPerk\b")),
    ("Concept", re.compile(r"\bConcept\s*\(")),
    ("effect_list", re.compile(r"\$EFFECT_LIST_BULLET\$")),
    ("dollar_var", re.compile(r"\$[^$]+\$")),
    ("Custom", re.compile(r"\bCustom\s*\(")),
    ("Select_CString", re.compile(r"\bSelect_CString\s*\(")),
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
DOMAIN_RE = re.compile(r"(culture|religion|faith|law|title|government|casus|cb|war|innovation|tradition)", re.IGNORECASE)
EFFECT_RE = re.compile(r"\$EFFECT_LIST_BULLET\$|\\n|\n|qualquer uma destas|any of these|requirement|trigger|effect|tooltip", re.IGNORECASE)


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


def call_signature(text: str) -> str:
    parts = [name for name, pattern in CALL_PATTERNS if pattern.search(text)]
    return "+".join(parts) if parts else "none"


def dynamic_expansion_candidate(row: dict[str, Any]) -> bool:
    signature = as_text(row.get("token_signature"))
    text = as_text(row.get("current_text"))
    markers = {
        "get_trait",
        "script_value",
        "get_activity_type",
        "get_title_by_key",
        "get_law",
        "get_scheme",
        "get_maa",
        "get_culture_tradition",
    }
    return any(marker in signature for marker in markers) or bool(
        re.search(r"\b(?:GetCultureTradition|GetLaw|GetScheme|GetMaA|GetBuilding|GetPerk)\b", text)
    )


def classify(row: dict[str, Any], source_concept_review_jsonl: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    spanish = as_text(row["spanish_text"])
    relative_path = as_text(row["relative_path"])
    source_key = as_text(row["source_key"])
    signature = call_signature(text)
    preserve_tokens = tokens_preserved(text, english)
    risk_flags: list[str] = []
    decision = "blocked_uncertain"
    blocked = "blocked_uncertain"
    lifecycle = False
    confidence = 0.72
    rationale = "chamada dinamica com conceito precisa de politica estreita"
    haystack = f"{relative_path} {source_key} {text}"

    if not preserve_tokens:
        decision = "needs_token_boundary_repair"
        blocked = "token_boundary"
        risk_flags.append("token_boundary")
        confidence = 0.9
        rationale = "token ou markup de chamada dinamica precisa reparo"
    elif SPANISH_RE.search(text):
        decision = "needs_spanish_residual_repair"
        blocked = "spanish_residual"
        risk_flags.append("spanish_residual")
        confidence = 0.88
        rationale = "residual espanhol visivel"
    elif ENGLISH_RE.search(text):
        decision = "needs_english_residual_repair"
        blocked = "english_residual"
        risk_flags.append("english_residual")
        confidence = 0.84
        rationale = "residual ingles visivel"
    elif "Custom" in signature or "Select_CString" in signature:
        decision = "needs_gender_or_custom_loc_policy"
        blocked = "gender_or_custom_loc_policy"
        risk_flags.append("gender_or_custom_loc")
        confidence = 0.84
        rationale = "chamada de conceito mistura custom localization, genero dinamico ou Select_CString"
    elif EFFECT_RE.search(haystack):
        decision = "needs_effect_requirement_policy"
        blocked = "effect_requirement_policy"
        risk_flags.append("effect_requirement")
        confidence = 0.82
        rationale = "chamada aparece em requisito/lista de efeito/tooltip mecanico"
    elif "GetCultureTradition" in signature or re.search(r"culture|tradition|innovation", haystack, re.IGNORECASE):
        decision = "needs_culture_tradition_policy"
        blocked = "culture_tradition_policy"
        risk_flags.append("culture_tradition")
        confidence = 0.82
        rationale = "chamada depende de tradicao, inovacao ou cultura"
    elif "GetTitleByKey" in signature or "GetLaw" in signature or re.search(r"title|law|government|casus|cb", haystack, re.IGNORECASE):
        decision = "needs_title_or_law_dynamic_policy"
        blocked = "title_or_law_policy"
        risk_flags.append("title_or_law")
        confidence = 0.82
        rationale = "chamada depende de titulo, lei, governo ou dominio politico"
    elif "GetTrait" in signature:
        decision = "needs_trait_requirement_policy"
        blocked = "trait_requirement_policy"
        risk_flags.append("trait_requirement")
        confidence = 0.82
        rationale = "GetTrait em contexto de requisito/atributo precisa politica dedicada"
    elif "ScriptValue" in signature:
        decision = "needs_script_value_numeric_policy"
        blocked = "script_value_numeric_policy"
        risk_flags.append("script_value")
        confidence = 0.82
        rationale = "ScriptValue ou valor numerico dinamico precisa politica dedicada"
    elif "GetActivityType" in signature or re.search(r"activity|tour|tournament|debate|travel|examination", haystack, re.IGNORECASE):
        decision = "needs_activity_type_policy"
        blocked = "activity_type_policy"
        risk_flags.append("activity_type")
        confidence = 0.8
        rationale = "GetActivityType ou contexto de atividade precisa politica dedicada"
    elif DOMAIN_RE.search(haystack):
        decision = "needs_domain_context"
        blocked = "domain_context"
        risk_flags.append("domain_context")
        confidence = 0.78
        rationale = "dominio sensivel impede lifecycle direto"
    elif len(text) <= 100:
        decision = "concept_get_call_ready_lifecycle"
        blocked = ""
        lifecycle = True
        confidence = 0.78
        rationale = "texto curto com chamada preservada e sem residual visivel"
    else:
        decision = "concept_get_call_style_watch_lifecycle"
        blocked = ""
        lifecycle = True
        confidence = 0.74
        rationale = "texto parece fechavel, mas com estilo/contexto dinamico a observar"

    return {
        "source_scope": "concept_policy_primary",
        "source_concept_review_jsonl": source_concept_review_jsonl,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": relative_path,
        "source_key": source_key,
        "microagent": MICROAGENT,
        "concept_pattern": as_text(row["concept_pattern"]),
        "concept_lane": as_text(row["concept_lane"]),
        "call_signature": signature,
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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def validate_inputs(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        missing = REQUIRED_INPUT_FIELDS - set(row)
        if missing:
            raise RuntimeError(f"Input row {row.get('segment_id')} missing fields: {sorted(missing)}")


def validate_outputs(rows: list[dict[str, Any]]) -> None:
    primary = sum(1 for row in rows if row["source_scope"] == "concept_policy_primary")
    if primary < 26:
        raise RuntimeError(f"Expected at least 26 primary rows, got {primary}")
    if len(rows) > 80:
        raise RuntimeError(f"Expected at most 80 rows, got {len(rows)}")
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


def build_report(rows: list[dict[str, Any]], expansion_rows: list[dict[str, Any]]) -> str:
    by_decision = Counter(row["decision"] for row in rows)
    by_call = Counter(row["call_signature"] for row in rows)
    by_lane = Counter(row["concept_lane"] for row in rows)
    by_path = Counter(path_group(row["relative_path"]) for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    policy_counts = {
        "effect_requirement": by_decision.get("needs_effect_requirement_policy", 0),
        "culture": by_decision.get("needs_culture_tradition_policy", 0),
        "title_law": by_decision.get("needs_title_or_law_dynamic_policy", 0),
        "trait": by_decision.get("needs_trait_requirement_policy", 0),
        "script_value": by_decision.get("needs_script_value_numeric_policy", 0),
        "activity": by_decision.get("needs_activity_type_policy", 0),
    }
    ready_or_watch = by_decision.get("concept_get_call_ready_lifecycle", 0) + by_decision.get(
        "concept_get_call_style_watch_lifecycle", 0
    )
    recommendations: list[str] = []
    if ready_or_watch >= 10:
        recommendations.append("sugerir lifecycle read-only pequeno para ready/style_watch")
    if by_decision.get("needs_effect_requirement_policy", 0) >= max(policy_counts.values() or [0]):
        recommendations.append("needs_effect_requirement_policy domina; migrar para effect_list_or_multiline_dynamic")
    if by_decision.get("needs_trait_requirement_policy", 0) >= max(policy_counts.values() or [0]):
        recommendations.append("needs_trait_requirement_policy domina; criar microagente de requisito de trait")
    if by_decision.get("needs_culture_tradition_policy", 0) >= max(policy_counts.values() or [0]):
        recommendations.append("needs_culture_tradition_policy domina; preparar dominio cultural antes de lifecycle")
    if not recommendations:
        recommendations.append("continuar com policy estreita dominante antes de lifecycle")

    lines = [
        "Dynamic concept GetTrait/ScriptValue policy review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total processado principal: {sum(1 for row in rows if row['source_scope'] == 'concept_policy_primary')}",
        f"Total de expansao diagnostica: {len(expansion_rows)}",
        "",
        "Contagem por decision:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por call_signature:")
    for key, value in by_call.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por concept_lane:")
    for key, value in by_lane.most_common():
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
            f"Precisam policy effect/requirement: {policy_counts['effect_requirement']}",
            f"Precisam policy culture: {policy_counts['culture']}",
            f"Precisam policy title/law: {policy_counts['title_law']}",
            f"Precisam policy trait: {policy_counts['trait']}",
            f"Precisam policy script value: {policy_counts['script_value']}",
            f"Precisam policy activity: {policy_counts['activity']}",
        ]
    )
    for decision in ALLOWED_DECISIONS:
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
            "- Pelo menos 26 linhas principais na saida.",
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


def write_outputs(rows: list[dict[str, Any]], expansion_rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_concept_get_trait_script_value_policy_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_concept_get_trait_script_value_policy_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, expansion_rows), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concept-review-jsonl", required=True)
    parser.add_argument("--dynamic-review-jsonl", required=True)
    args = parser.parse_args()

    concept_path = Path(args.concept_review_jsonl)
    dynamic_path = Path(args.dynamic_review_jsonl)
    concept_rows = load_jsonl(concept_path)
    primary_rows = [row for row in concept_rows if row.get("decision") == PRIMARY_DECISION]
    validate_inputs(primary_rows)
    dynamic_rows = load_jsonl(dynamic_path)
    expansion_rows = [row for row in dynamic_rows if dynamic_expansion_candidate(row)]
    output_rows = [classify(row, str(concept_path)) for row in primary_rows]
    validate_outputs(output_rows)
    out_jsonl, out_txt = write_outputs(output_rows, expansion_rows)
    by_decision = Counter(row["decision"] for row in output_rows)
    by_call = Counter(row["call_signature"] for row in output_rows)
    print(out_jsonl)
    print(out_txt)
    print("primary_processed", len(output_rows))
    print("diagnostic_expansion", len(expansion_rows))
    print("decisions", dict(by_decision))
    print("call_signatures", dict(by_call))
    print("lifecycle", sum(1 for row in output_rows if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in output_rows if row["requires_apply_later"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
