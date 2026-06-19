from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "dynamic_trait_requirement_lexical_policy_review_v1"
MICROAGENT = "dynamic_trait_requirement_lexical_policy"
INPUT_DECISIONS = {
    "trait_requirement_needs_lexical_policy",
    "trait_requirement_needs_spanish_residual_repair",
    "trait_requirement_needs_english_residual_repair",
}
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "ledger_run_id",
    "segment_state_run_id",
    "relative_path",
    "source_key",
    "current_text",
    "english_text",
    "spanish_text",
    "decision",
    "trait_requirement_pattern",
    "trait_requirement_lane",
    "call_signature",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_trait_review_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "input_decision",
    "lexical_pattern",
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
    "trait_lexical_ready_lifecycle",
    "trait_lexical_style_watch_lifecycle",
    "trait_lexical_repair_ready",
    "trait_spanish_residual_repair_ready",
    "trait_english_residual_repair_ready",
    "trait_lexical_needs_script_value_policy",
    "trait_lexical_needs_acclaimed_knight_policy",
    "trait_lexical_needs_semantic_review",
    "trait_lexical_blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
BAD_ENCODING_RE = re.compile(r"[\u00c3\u00c2\ufffd]")
SPANISH_RESIDUAL_RE = re.compile(r"\b(?:combata|ocupe|t[úu]|car[áa]cter|carÃ¡cter|caracter)\b", re.IGNORECASE)
ENGLISH_RESIDUAL_RE = re.compile(r"\b(?:trait|experience|xp|knight|acclaimed|for each)\b", re.IGNORECASE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def path_group(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


def protected_tokens_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def tokens_preserved(current_text: str, candidate_text: str) -> bool:
    return protected_tokens_signature(current_text) == protected_tokens_signature(candidate_text)


def lexical_pattern(text: str, input_decision: str) -> str:
    lower = text.lower()
    if "scriptvalue" in text:
        return "script_value_blocks_lexical_repair"
    if "[acclaimed_knight" in text or "cavaleiro aclamado" in lower:
        return "acclaimed_knight_entity_policy"
    if re.search(r"tem (?:a|uma) caracter", lower):
        return "tem_caracteristica_to_tem_traco"
    if re.search(r"car[áa]cter|caracter|característica|caracteristica", lower):
        return "caracter_to_traco"
    if input_decision == "trait_requirement_needs_spanish_residual_repair":
        return "spanish_residual_trait_tooltip"
    if input_decision == "trait_requirement_needs_english_residual_repair":
        return "english_residual_trait_tooltip"
    return "other_trait_lexical"


def propose_correction(text: str, pattern: str, input_decision: str) -> str:
    corrected = text
    if pattern == "tem_caracteristica_to_tem_traco":
        corrected = re.sub(
            r"\btem (?:a|uma) caracter[íi]stica de\s+",
            "tem o traço ",
            corrected,
            flags=re.IGNORECASE,
        )
        corrected = re.sub(
            r"\btem (?:a|uma) caracter[íi]stica\s+",
            "tem o traço ",
            corrected,
            flags=re.IGNORECASE,
        )
    elif pattern == "caracter_to_traco":
        corrected = re.sub(r"\bcar[áa]cter\b", "traço", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bcaracter\b", "traço", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bcaracter[íi]stica\b", "traço", corrected, flags=re.IGNORECASE)
    elif input_decision == "trait_requirement_needs_spanish_residual_repair":
        corrected = re.sub(r"\bcombata\b", "lute", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bocupe\b", "ocupa", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bt[úu]\b", "você", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bcar[áa]cter\b", "traço", corrected, flags=re.IGNORECASE)
    elif input_decision == "trait_requirement_needs_english_residual_repair":
        corrected = re.sub(r"\btrait\b", "traço", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bexperience\b", "experiência", corrected, flags=re.IGNORECASE)
        corrected = re.sub(r"\bfor each\b", "para cada", corrected, flags=re.IGNORECASE)
    return corrected


def is_safe_correction(current_text: str, corrected_text: str) -> bool:
    if corrected_text == current_text:
        return False
    if not tokens_preserved(current_text, corrected_text):
        return False
    if BAD_ENCODING_RE.search(corrected_text):
        return False
    if re.search(r"\w\?\w", corrected_text, flags=re.UNICODE):
        return False
    return True


def classify(row: dict[str, Any], source_path: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    input_decision = as_text(row["decision"])
    pattern = lexical_pattern(text, input_decision)
    risk_flags: list[str] = []
    decision = "trait_lexical_blocked_uncertain"
    lifecycle = False
    requires_apply = False
    corrected_text = ""
    blocked = "blocked_uncertain"
    confidence = 0.72
    rationale = "politica lexical de trait/accolade precisa revisao"

    if pattern == "script_value_blocks_lexical_repair":
        decision = "trait_lexical_needs_script_value_policy"
        blocked = "script_value_policy"
        risk_flags.append("script_value")
        confidence = 0.82
        rationale = "ScriptValue impede reparo lexical isolado seguro"
    elif pattern == "acclaimed_knight_entity_policy":
        decision = "trait_lexical_needs_acclaimed_knight_policy"
        blocked = "acclaimed_knight_policy"
        risk_flags.append("acclaimed_knight")
        confidence = 0.82
        rationale = "entidade cavaleiro aclamado domina o risco"
    else:
        candidate = propose_correction(text, pattern, input_decision)
        if is_safe_correction(text, candidate):
            corrected_text = candidate
            requires_apply = True
            blocked = ""
            confidence = 0.88
            if input_decision == "trait_requirement_needs_spanish_residual_repair":
                decision = "trait_spanish_residual_repair_ready"
                rationale = "reparo remove residual espanhol e preserva tokens"
            elif input_decision == "trait_requirement_needs_english_residual_repair":
                decision = "trait_english_residual_repair_ready"
                rationale = "reparo remove residual ingles e preserva tokens"
            else:
                decision = "trait_lexical_repair_ready"
                rationale = "reparo lexical troca trait/caracteristica por traço preservando tokens"
        elif input_decision == "trait_requirement_needs_lexical_policy" and not SPANISH_RESIDUAL_RE.search(text) and not ENGLISH_RESIDUAL_RE.search(text):
            decision = "trait_lexical_style_watch_lifecycle"
            lifecycle = True
            blocked = ""
            confidence = 0.74
            rationale = "texto parece fechavel sem reparo seguro necessario, mas merece watch lexical"
        else:
            decision = "trait_lexical_needs_semantic_review"
            blocked = "semantic_review_needed"
            risk_flags.append("unsafe_or_no_correction")
            confidence = 0.68
            rationale = "reparo exigiria decisao semantica maior ou nao preservou guardrails"

    return {
        "source_trait_review_jsonl": source_path,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": as_text(row["relative_path"]),
        "source_key": as_text(row["source_key"]),
        "microagent": MICROAGENT,
        "input_decision": input_decision,
        "lexical_pattern": pattern,
        "decision": decision,
        "lifecycle_candidate": lifecycle,
        "requires_apply_later": requires_apply,
        "corrected_text": corrected_text,
        "tokens_preserved": tokens_preserved(text, corrected_text or text),
        "confidence": confidence,
        "risk_flags": risk_flags,
        "blocked_reason": blocked,
        "rationale": rationale,
        "current_text": text,
        "english_text": as_text(row["english_text"]),
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
            if row.get("decision") in INPUT_DECISIONS:
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
            if not tokens_preserved(row["current_text"], corrected):
                raise RuntimeError(f"Token mismatch in correction for segment {row['segment_id']}")
            if BAD_ENCODING_RE.search(corrected):
                raise RuntimeError(f"Encoding marker in correction for segment {row['segment_id']}")
            if re.search(r"\w\?\w", corrected, flags=re.UNICODE):
                raise RuntimeError(f"Question mark inside word in correction for segment {row['segment_id']}")


def build_report(rows: list[dict[str, Any]], expected_count: int) -> str:
    by_decision = Counter(row["decision"] for row in rows)
    by_pattern = Counter(row["lexical_pattern"] for row in rows)
    by_input = Counter(row["input_decision"] for row in rows)
    by_path = Counter(path_group(row["relative_path"]) for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    blocked_count = sum(1 for row in rows if row["blocked_reason"])
    recommendations: list[str] = []
    if apply_count:
        recommendations.append("sugerir prompt de apply protegido para reparos lexical/residual seguros")
    if lifecycle_count:
        recommendations.append("sugerir ponte read-only separada para lifecycle candidates")
    if by_decision.get("trait_lexical_needs_script_value_policy", 0) or by_decision.get(
        "trait_lexical_needs_acclaimed_knight_policy", 0
    ):
        recommendations.append("voltar para ScriptValue/acclaimed knight policies nos bloqueios restantes")

    lines = [
        "Dynamic trait requirement lexical policy review",
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
    lines.append("Contagem por lexical_pattern:")
    for key, value in by_pattern.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por input decision:")
    for key, value in by_input.most_common():
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
            f"Bloqueados: {blocked_count}",
        ]
    )
    for decision in ALLOWED_DECISIONS:
        examples = [row for row in rows if row["decision"] == decision][:8]
        if not examples:
            continue
        lines.append("")
        lines.append(f"Exemplos {decision}:")
        for row in examples:
            suffix = f" -> {row['corrected_text'][:120]}" if row["corrected_text"] else ""
            lines.append(f"- {row['segment_id']} | {row['source_key']} | {row['current_text'][:100]}{suffix}")
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
            "- Todos os corrected_text preservam exatamente tokens CK3/markup do texto atual.",
            "- Nenhum marcador ruim de encoding em corrected_text.",
            "- Nenhum ? dentro de palavra em corrected_text.",
            "- Nenhuma escrita em source/ ou output/.",
            "- Nenhum apply, production, confirmation, reindex, treino/model promotion, segment-state ou lifecycle executado.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(rows: list[dict[str, Any]], expected_count: int) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_trait_requirement_lexical_policy_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_trait_requirement_lexical_policy_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, expected_count), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trait-review-jsonl", required=True)
    args = parser.parse_args()
    review_path = Path(args.trait_review_jsonl)
    input_rows = load_candidates(review_path)
    output_rows = [classify(row, str(review_path)) for row in input_rows]
    validate_outputs(output_rows)
    out_jsonl, out_txt = write_outputs(output_rows, expected_count=25)
    print(out_jsonl)
    print(out_txt)
    print("processed", len(output_rows))
    print("decisions", dict(Counter(row["decision"] for row in output_rows)))
    print("patterns", dict(Counter(row["lexical_pattern"] for row in output_rows)))
    print("lifecycle", sum(1 for row in output_rows if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in output_rows if row["requires_apply_later"]))
    print("blocked", sum(1 for row in output_rows if row["blocked_reason"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
