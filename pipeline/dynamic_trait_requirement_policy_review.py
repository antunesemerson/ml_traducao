from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "dynamic_trait_requirement_policy_review_v1"
MICROAGENT = "dynamic_trait_requirement_policy"
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "ledger_run_id",
    "segment_state_run_id",
    "relative_path",
    "source_key",
    "current_text",
    "english_text",
    "spanish_text",
    "call_signature",
    "effect_pattern",
    "effect_lane",
    "decision",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_effect_review_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "trait_requirement_pattern",
    "trait_requirement_lane",
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
    "trait_requirement_ready_lifecycle",
    "trait_requirement_style_watch_lifecycle",
    "trait_requirement_needs_lexical_policy",
    "trait_requirement_needs_trait_xp_policy",
    "trait_requirement_needs_acclaimed_knight_policy",
    "trait_requirement_needs_concept_condition_policy",
    "trait_requirement_needs_script_value_policy",
    "trait_requirement_needs_activity_type_policy",
    "trait_requirement_needs_spanish_residual_repair",
    "trait_requirement_needs_english_residual_repair",
    "trait_requirement_needs_token_boundary_repair",
    "trait_requirement_needs_semantic_rewrite",
    "trait_requirement_blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
SPANISH_RE = re.compile(
    r"\b(?:car[Ãá]cter|caracter|traco|trazo|t[úu]|combata|ocupe|verdadero|verdadera|reino|condado|duque|señor|senor|son)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(
    r"\b(?:will|must|cannot|should|trait|knight|acclaimed|for each|the\s+[A-Za-z]+|your\s+[A-Za-z]+)\b",
    re.IGNORECASE,
)
LEXICAL_RE = re.compile(r"\b(?:caracteristica|característica|traco|traço|carater|caráter|caractere|caracter)\b", re.IGNORECASE)


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


def trait_pattern(row: dict[str, Any]) -> str:
    text = as_text(row["current_text"])
    key = as_text(row["source_key"]).lower()
    signature = as_text(row["call_signature"])
    haystack = f"{key} {text}".lower()
    if "ScriptValue" in signature:
        return "trait_plus_script_value"
    if "GetActivityType" in signature:
        return "trait_plus_activity_type"
    if "GetScheme" in signature:
        return "trait_plus_scheme"
    if "GetMaA" in signature:
        return "trait_plus_maa"
    if "xp" in haystack and ("all" in haystack or "todos" in haystack):
        return "all_knights_trait_xp"
    if "xp" in haystack:
        return "trait_xp_bonus"
    if "acclaimed" in haystack or "accolade" in haystack or "attribute.unlock" in haystack:
        return "any_of_these_trait_list"
    if "Concept" in signature and signature.count("GetTrait") >= 1:
        return "trait_plus_concept_condition"
    if signature == "GetTrait" or "GetTrait" in signature:
        return "any_of_these_trait_list"
    return "other"


def trait_lane(row: dict[str, Any], pattern: str) -> str:
    text = as_text(row["current_text"]).lower()
    key = as_text(row["source_key"]).lower()
    haystack = f"{key} {text}"
    if "acclaimed" in haystack or "accolade" in haystack or "attribute.unlock" in haystack:
        return "acclaimed_knight_trait_unlock"
    if "tournament" in haystack or "tournoi" in haystack or "contest" in haystack:
        return "tournament_trait_xp"
    if "councillor" in haystack or "council" in haystack or "task" in haystack:
        return "councillor_task_trait_xp"
    if pattern in {"trait_xp_bonus", "all_knights_trait_xp", "single_knight_trait_xp"}:
        return "tournament_trait_xp"
    if pattern == "trait_plus_concept_condition":
        return "trait_condition_unlock"
    if pattern == "trait_plus_activity_type":
        return "trait_activity_bonus"
    if SPANISH_RE.search(as_text(row["current_text"])) or ENGLISH_RE.search(as_text(row["current_text"])):
        return "residual_repair"
    if pattern == "any_of_these_trait_list":
        return "trait_requirement_list"
    return "other"


def is_candidate(row: dict[str, Any]) -> bool:
    return row.get("decision") == "needs_trait_requirement_policy" or row.get("effect_lane") == "trait_requirement"


def classify(row: dict[str, Any], source_effect_review_jsonl: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    signature = as_text(row["call_signature"])
    preserve_tokens = tokens_preserved(text, english)
    pattern = trait_pattern(row)
    lane = trait_lane(row, pattern)
    risk_flags: list[str] = []
    decision = "trait_requirement_blocked_uncertain"
    blocked = "blocked_uncertain"
    lifecycle = False
    confidence = 0.72
    rationale = "requisito com GetTrait precisa politica estreita"

    if not preserve_tokens:
        decision = "trait_requirement_needs_token_boundary_repair"
        blocked = "token_boundary"
        risk_flags.append("token_boundary")
        confidence = 0.9
        rationale = "token ou markup em requisito de trait precisa reparo"
    elif SPANISH_RE.search(text):
        decision = "trait_requirement_needs_spanish_residual_repair"
        blocked = "spanish_residual"
        risk_flags.append("spanish_residual")
        confidence = 0.88
        rationale = "residual espanhol visivel em requisito de trait"
    elif ENGLISH_RE.search(text):
        decision = "trait_requirement_needs_english_residual_repair"
        blocked = "english_residual"
        risk_flags.append("english_residual")
        confidence = 0.84
        rationale = "residual ingles visivel em requisito de trait"
    elif LEXICAL_RE.search(text):
        decision = "trait_requirement_needs_lexical_policy"
        blocked = "lexical_policy"
        risk_flags.append("lexical_trait_term")
        confidence = 0.8
        rationale = "escolha lexical para trait/caracteristica precisa politica dedicada"
    elif "ScriptValue" in signature:
        decision = "trait_requirement_needs_script_value_policy"
        blocked = "script_value_policy"
        risk_flags.append("script_value")
        confidence = 0.82
        rationale = "ScriptValue domina o risco do requisito de trait"
    elif "GetActivityType" in signature:
        decision = "trait_requirement_needs_activity_type_policy"
        blocked = "activity_type_policy"
        risk_flags.append("activity_type")
        confidence = 0.8
        rationale = "GetActivityType domina o risco do requisito de trait"
    elif lane == "acclaimed_knight_trait_unlock":
        decision = "trait_requirement_needs_acclaimed_knight_policy"
        blocked = "acclaimed_knight_policy"
        risk_flags.append("acclaimed_knight")
        confidence = 0.84
        rationale = "requisito de cavaleiro aclamado/accolade precisa politica dedicada"
    elif pattern == "trait_plus_concept_condition":
        decision = "trait_requirement_needs_concept_condition_policy"
        blocked = "concept_condition_policy"
        risk_flags.append("concept_condition")
        confidence = 0.8
        rationale = "condicao Concept junto de GetTrait precisa politica dedicada"
    elif pattern in {"trait_xp_bonus", "all_knights_trait_xp", "single_knight_trait_xp"}:
        decision = "trait_requirement_needs_trait_xp_policy"
        blocked = "trait_xp_policy"
        risk_flags.append("trait_xp")
        confidence = 0.8
        rationale = "experiencia/bonus numerico de trait precisa politica dedicada"
    elif lane == "trait_requirement_list":
        decision = "trait_requirement_style_watch_lifecycle"
        lifecycle = True
        blocked = ""
        risk_flags.append("style_watch")
        confidence = 0.76
        rationale = "lista de requisitos de trait parece estruturalmente correta, mas merece watch"
    elif len(text) <= 100:
        decision = "trait_requirement_ready_lifecycle"
        lifecycle = True
        blocked = ""
        confidence = 0.78
        rationale = "requisito curto com GetTrait preservado e sem residual visivel"
    else:
        decision = "trait_requirement_blocked_uncertain"
        blocked = "blocked_uncertain"
        risk_flags.append("insufficient_evidence")
        confidence = 0.58
        rationale = "evidencia insuficiente para fechar requisito de trait"

    return {
        "source_effect_review_jsonl": source_effect_review_jsonl,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": as_text(row["relative_path"]),
        "source_key": as_text(row["source_key"]),
        "microagent": MICROAGENT,
        "trait_requirement_pattern": pattern,
        "trait_requirement_lane": lane,
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
        "spanish_text": as_text(row["spanish_text"]),
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


def build_report(rows: list[dict[str, Any]], expected_count: int) -> str:
    by_decision = Counter(row["decision"] for row in rows)
    by_pattern = Counter(row["trait_requirement_pattern"] for row in rows)
    by_lane = Counter(row["trait_requirement_lane"] for row in rows)
    by_call = Counter(row["call_signature"] for row in rows)
    by_path = Counter(path_group(row["relative_path"]) for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    policy_counts = {
        "lexical": by_decision.get("trait_requirement_needs_lexical_policy", 0),
        "trait_xp": by_decision.get("trait_requirement_needs_trait_xp_policy", 0),
        "acclaimed_knight": by_decision.get("trait_requirement_needs_acclaimed_knight_policy", 0),
        "concept_condition": by_decision.get("trait_requirement_needs_concept_condition_policy", 0),
        "script": by_decision.get("trait_requirement_needs_script_value_policy", 0),
        "activity": by_decision.get("trait_requirement_needs_activity_type_policy", 0),
        "residual": by_decision.get("trait_requirement_needs_spanish_residual_repair", 0)
        + by_decision.get("trait_requirement_needs_english_residual_repair", 0),
    }
    ready_or_watch = by_decision.get("trait_requirement_ready_lifecycle", 0) + by_decision.get(
        "trait_requirement_style_watch_lifecycle", 0
    )
    dominant_policy = max(policy_counts.items(), key=lambda item: item[1]) if policy_counts else ("none", 0)
    recommendations: list[str] = []
    if ready_or_watch >= 15:
        recommendations.append("sugerir lifecycle read-only para ready/style_watch")
    if dominant_policy[1] > 0:
        recommendations.append(f"policy dominante: {dominant_policy[0]}; sugerir proximo microagente estreito")
    if apply_count:
        recommendations.append("separar reparos seguros em apply protegido")
    if len(by_lane) > 3:
        recommendations.append("grupo ainda misto; quebrar por trait_requirement_lane")
    if not recommendations:
        recommendations.append("manter como diagnostico ate surgir lote lifecycle ou policy dominante maior")

    lines = [
        "Dynamic trait requirement policy review",
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
    lines.append("Contagem por trait_requirement_pattern:")
    for key, value in by_pattern.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por trait_requirement_lane:")
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
            f"Precisam lexical/trait_xp/acclaimed/concept/script/activity/residual: {json.dumps(policy_counts, sort_keys=True)}",
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


def write_outputs(rows: list[dict[str, Any]], expected_count: int) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_trait_requirement_policy_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_trait_requirement_policy_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, expected_count), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effect-review-jsonl", required=True)
    args = parser.parse_args()
    review_path = Path(args.effect_review_jsonl)
    input_rows = load_candidates(review_path)
    output_rows = [classify(row, str(review_path)) for row in input_rows]
    validate_outputs(output_rows)
    out_jsonl, out_txt = write_outputs(output_rows, expected_count=31)
    by_decision = Counter(row["decision"] for row in output_rows)
    by_lane = Counter(row["trait_requirement_lane"] for row in output_rows)
    print(out_jsonl)
    print(out_txt)
    print("processed", len(output_rows))
    print("decisions", dict(by_decision))
    print("lanes", dict(by_lane))
    print("lifecycle", sum(1 for row in output_rows if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in output_rows if row["requires_apply_later"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
