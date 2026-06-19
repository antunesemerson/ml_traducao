from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "dynamic_effect_requirement_policy_review_v1"
MICROAGENT = "dynamic_effect_requirement_policy"
REQUIRED_OUTPUT_FIELDS = {
    "source_scopes",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "effect_pattern",
    "effect_lane",
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
    "effect_requirement_ready_lifecycle",
    "effect_requirement_style_watch_lifecycle",
    "needs_trait_requirement_policy",
    "needs_culture_or_innovation_requirement_policy",
    "needs_activity_requirement_policy",
    "needs_script_value_numeric_policy",
    "needs_title_law_or_government_requirement_policy",
    "needs_dynamic_concept_requirement_policy",
    "needs_select_cstring_or_gender_policy",
    "needs_token_boundary_repair",
    "needs_spanish_residual_repair",
    "needs_english_residual_repair",
    "needs_semantic_rewrite",
    "needs_domain_context",
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
    ("Concept", re.compile(r"\bConcept\s*\(|\[[^\]]*\|E\]")),
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


def call_signature(row: dict[str, Any]) -> str:
    existing = as_text(row.get("call_signature"))
    if existing:
        return existing
    text = as_text(row.get("current_text"))
    parts = [name for name, pattern in CALL_PATTERNS if pattern.search(text)]
    return "+".join(parts) if parts else as_text(row.get("token_signature")) or "none"


def effect_pattern(row: dict[str, Any], signature: str) -> str:
    text = as_text(row["current_text"])
    haystack = f"{row['relative_path']} {row['source_key']} {text}"
    if re.search(r"qualquer uma destas|any of these", haystack, re.IGNORECASE):
        return "any_of_these_requirement_list"
    if "effect_list" in signature and re.search(r"unlock|desbloque", haystack, re.IGNORECASE):
        return "effect_list_unlock"
    if "effect_list" in signature:
        return "effect_list_bonus"
    if "GetActivityType" in signature or re.search(r"activity|tour|tournament|debate|travel|examination", haystack, re.IGNORECASE):
        return "activity_requirement_tooltip"
    if "ScriptValue" in signature:
        return "script_value_numeric_effect"
    if "GetTrait" in signature:
        return "trait_requirement_list"
    if "GetCultureTradition" in signature or re.search(r"culture|innovation|tradition", haystack, re.IGNORECASE):
        return "culture_innovation_requirement"
    if "GetTitleByKey" in signature or "GetLaw" in signature or re.search(r"title|law|government", haystack, re.IGNORECASE):
        return "title_law_requirement"
    if "\\n" in text or "\n" in text:
        return "multiline_dynamic_description"
    return "other"


def effect_lane(row: dict[str, Any], signature: str, pattern: str) -> str:
    haystack = f"{row['relative_path']} {row['source_key']} {row['current_text']}"
    if "Select_CString" in signature or "Custom" in signature:
        return "select_cstring_or_gender"
    if "GetTrait" in signature:
        return "trait_requirement"
    if pattern == "culture_innovation_requirement":
        return "culture_or_innovation_requirement"
    if pattern == "activity_requirement_tooltip":
        return "activity_requirement"
    if "ScriptValue" in signature:
        return "script_value_numeric"
    if pattern == "title_law_requirement":
        return "title_law_government"
    if "Concept" in signature or re.search(r"\[[^\]]*\|E\]", haystack):
        return "concept_requirement"
    if SPANISH_RE.search(as_text(row["current_text"])) or ENGLISH_RE.search(as_text(row["current_text"])):
        return "residual_repair"
    if pattern in {"effect_list_bonus", "effect_list_unlock", "any_of_these_requirement_list"}:
        return "semantic_context"
    return "other"


def classify(row: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    signature = call_signature(row)
    pattern = effect_pattern(row, signature)
    lane = effect_lane(row, signature, pattern)
    preserve_tokens = tokens_preserved(text, english)
    risk_flags: list[str] = []
    decision = "blocked_uncertain"
    blocked = "blocked_uncertain"
    lifecycle = False
    confidence = 0.72
    rationale = "requisito/efeito dinamico precisa politica estreita"

    if not preserve_tokens:
        decision = "needs_token_boundary_repair"
        blocked = "token_boundary"
        risk_flags.append("token_boundary")
        confidence = 0.9
        rationale = "token ou markup em requisito/efeito precisa reparo"
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
    elif lane == "select_cstring_or_gender":
        decision = "needs_select_cstring_or_gender_policy"
        blocked = "select_cstring_or_gender"
        risk_flags.append("select_cstring_or_gender")
        confidence = 0.84
        rationale = "lista dinamica mistura genero, Custom ou Select_CString"
    elif lane == "trait_requirement":
        decision = "needs_trait_requirement_policy"
        blocked = "trait_requirement"
        risk_flags.append("trait_requirement")
        confidence = 0.84
        rationale = "requisito baseado em GetTrait precisa politica dedicada"
    elif lane == "culture_or_innovation_requirement":
        decision = "needs_culture_or_innovation_requirement_policy"
        blocked = "culture_or_innovation_requirement"
        risk_flags.append("culture_or_innovation")
        confidence = 0.84
        rationale = "requisito cultural/inovacao precisa politica dedicada"
    elif lane == "activity_requirement":
        decision = "needs_activity_requirement_policy"
        blocked = "activity_requirement"
        risk_flags.append("activity_requirement")
        confidence = 0.82
        rationale = "requisito de atividade precisa politica dedicada"
    elif lane == "script_value_numeric":
        decision = "needs_script_value_numeric_policy"
        blocked = "script_value_numeric"
        risk_flags.append("script_value")
        confidence = 0.82
        rationale = "valor numerico dinamico precisa politica dedicada"
    elif lane == "title_law_government":
        decision = "needs_title_law_or_government_requirement_policy"
        blocked = "title_law_government"
        risk_flags.append("title_law_government")
        confidence = 0.82
        rationale = "titulo/lei/governo em requisito dinamico precisa politica dedicada"
    elif lane == "concept_requirement":
        decision = "needs_dynamic_concept_requirement_policy"
        blocked = "dynamic_concept_requirement"
        risk_flags.append("concept_requirement")
        confidence = 0.8
        rationale = "conceito CK3 controla requisito/efeito"
    elif pattern in {"effect_list_bonus", "effect_list_unlock", "any_of_these_requirement_list"}:
        decision = "effect_requirement_style_watch_lifecycle"
        lifecycle = True
        blocked = ""
        risk_flags.append("style_watch")
        confidence = 0.74
        rationale = "lista de efeito parece estruturalmente correta, mas merece watch de estilo"
    elif len(text) <= 100:
        decision = "effect_requirement_ready_lifecycle"
        lifecycle = True
        blocked = ""
        confidence = 0.76
        rationale = "requisito curto com tokens preservados e sem residual visivel"
    else:
        decision = "needs_domain_context"
        blocked = "domain_or_semantic_context"
        risk_flags.append("semantic_context")
        confidence = 0.7
        rationale = "requisito/efeito dinamico precisa contexto semantico ou dominio"

    return {
        "source_scopes": scopes,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": as_text(row["relative_path"]),
        "source_key": as_text(row["source_key"]),
        "microagent": MICROAGENT,
        "effect_pattern": pattern,
        "effect_lane": lane,
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
        "english_text": as_text(row["english_text"]),
        "spanish_text": as_text(row["spanish_text"]),
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def dynamic_candidate(row: dict[str, Any]) -> bool:
    return row.get("decision") == "needs_effect_list_dynamic_semantic_agent" or row.get("sublane") == "effect_list_or_multiline_dynamic"


def concept_get_candidate(row: dict[str, Any]) -> bool:
    return row.get("decision") == "needs_effect_requirement_policy"


def expansion_candidate(row: dict[str, Any]) -> bool:
    sig = as_text(row.get("token_signature"))
    return any(marker in sig for marker in ("effect_list", "get_trait", "get_activity_type", "script_value", "dollar_var", "concept"))


def validate_input_row(row: dict[str, Any]) -> None:
    required_any = {"token_signature", "call_signature"}
    required = {
        "segment_id",
        "ledger_run_id",
        "segment_state_run_id",
        "relative_path",
        "source_key",
        "current_text",
        "english_text",
        "spanish_text",
    }
    missing = required - set(row)
    if missing:
        raise RuntimeError(f"Input row {row.get('segment_id')} missing fields: {sorted(missing)}")
    if not (required_any & set(row)):
        raise RuntimeError(f"Input row {row.get('segment_id')} missing token_signature/call_signature")


def validate_outputs(rows: list[dict[str, Any]], expected_distinct: int) -> None:
    if len(rows) != expected_distinct:
        raise RuntimeError(f"Expected {expected_distinct} distinct rows, got {len(rows)}")
    if len(rows) > 100:
        raise RuntimeError(f"Expected at most 100 rows, got {len(rows)}")
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


def build_report(rows: list[dict[str, Any]], raw_count: int, expansion_count: int) -> str:
    by_decision = Counter(row["decision"] for row in rows)
    by_pattern = Counter(row["effect_pattern"] for row in rows)
    by_lane = Counter(row["effect_lane"] for row in rows)
    by_call = Counter(row["call_signature"] for row in rows)
    by_path = Counter(path_group(row["relative_path"]) for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    policy_counts = {
        "trait": by_decision.get("needs_trait_requirement_policy", 0),
        "culture": by_decision.get("needs_culture_or_innovation_requirement_policy", 0),
        "activity": by_decision.get("needs_activity_requirement_policy", 0),
        "script": by_decision.get("needs_script_value_numeric_policy", 0),
        "title": by_decision.get("needs_title_law_or_government_requirement_policy", 0),
        "gender": by_decision.get("needs_select_cstring_or_gender_policy", 0),
        "residual": by_decision.get("needs_spanish_residual_repair", 0) + by_decision.get("needs_english_residual_repair", 0),
    }
    ready_or_watch = by_decision.get("effect_requirement_ready_lifecycle", 0) + by_decision.get(
        "effect_requirement_style_watch_lifecycle", 0
    )
    dominant_policy = max(policy_counts.items(), key=lambda item: item[1]) if policy_counts else ("none", 0)
    recommendations: list[str] = []
    if ready_or_watch >= 15:
        recommendations.append("sugerir lifecycle read-only para ready/style_watch")
    if dominant_policy[1] > 0:
        recommendations.append(f"policy dominante: {dominant_policy[0]}; sugerir proximo microagente estreito")
    if apply_count:
        recommendations.append("separar reparos seguros em apply protegido")
    if len({row["effect_lane"] for row in rows}) > 4:
        recommendations.append("bloco ainda misto; quebrar por effect_lane")

    lines = [
        "Dynamic effect requirement policy review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total bruto de linhas filtradas: {raw_count}",
        f"Total distinto por segment_id: {len(rows)}",
        f"Total de expansao diagnostica: {expansion_count}",
        "",
        "Contagem por decision:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por effect_pattern:")
    for key, value in by_pattern.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por effect_lane:")
    for key, value in by_lane.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por call_signature:")
    for key, value in by_call.most_common():
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
            f"Precisam trait/culture/activity/script/title/gender/residual: {json.dumps(policy_counts, sort_keys=True)}",
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
            "- Total de linhas igual ao total distinto.",
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


def write_outputs(rows: list[dict[str, Any]], raw_count: int, expansion_count: int) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_effect_requirement_policy_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_effect_requirement_policy_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, raw_count, expansion_count), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-review-jsonl", required=True)
    parser.add_argument("--concept-get-review-jsonl", required=True)
    args = parser.parse_args()

    dynamic_rows = load_jsonl(Path(args.dynamic_review_jsonl))
    concept_rows = load_jsonl(Path(args.concept_get_review_jsonl))
    filtered: list[tuple[dict[str, Any], str]] = []
    filtered.extend((row, "dynamic_effect_list") for row in dynamic_rows if dynamic_candidate(row))
    filtered.extend((row, "concept_get_effect_requirement") for row in concept_rows if concept_get_candidate(row))
    for row, _ in filtered:
        validate_input_row(row)

    by_segment: dict[int, dict[str, Any]] = {}
    scopes_by_segment: dict[int, list[str]] = defaultdict(list)
    for row, scope in filtered:
        segment_id = int(row["segment_id"])
        if segment_id not in by_segment:
            by_segment[segment_id] = row
        if scope not in scopes_by_segment[segment_id]:
            scopes_by_segment[segment_id].append(scope)

    reviewed = [
        classify(by_segment[segment_id], scopes_by_segment[segment_id])
        for segment_id in sorted(by_segment)
    ]
    expansion_count = sum(1 for row in dynamic_rows if expansion_candidate(row))
    validate_outputs(reviewed, len(by_segment))
    out_jsonl, out_txt = write_outputs(reviewed, len(filtered), expansion_count)
    print(out_jsonl)
    print(out_txt)
    print("raw_filtered", len(filtered))
    print("distinct_segments", len(reviewed))
    print("diagnostic_expansion", expansion_count)
    print("decisions", dict(Counter(row["decision"] for row in reviewed)))
    print("effect_lanes", dict(Counter(row["effect_lane"] for row in reviewed)))
    print("lifecycle", sum(1 for row in reviewed if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in reviewed if row["requires_apply_later"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
