from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "dynamic_acclaimed_knight_requirement_policy_review_v1"
MICROAGENT = "dynamic_acclaimed_knight_requirement_policy"
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
    "input_decision",
    "lexical_pattern",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_scope",
    "source_trait_lexical_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "requirement_pattern",
    "entity_pattern",
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
    "acclaimed_knight_requirement_ready_lifecycle",
    "acclaimed_knight_requirement_style_watch_lifecycle",
    "acclaimed_knight_requirement_repair_ready",
    "acclaimed_knight_requirement_spanish_residual_repair_ready",
    "acclaimed_knight_requirement_needs_concept_condition_policy",
    "acclaimed_knight_requirement_needs_activity_policy",
    "acclaimed_knight_requirement_needs_script_value_policy",
    "acclaimed_knight_requirement_needs_semantic_review",
    "acclaimed_knight_requirement_blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
BAD_ENCODING_RE = re.compile(r"[\u00c3\u00c2\ufffd]")
SPANISH_RE = re.compile(r"\b(?:combata|ocupe|t[úu]|car[áa]cter|caracter)\b", re.IGNORECASE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def path_group(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


def protected_tokens_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def tokens_preserved(current_text: str, candidate_text: str) -> bool:
    return protected_tokens_signature(current_text) == protected_tokens_signature(candidate_text)


def requirement_pattern(text: str) -> str:
    if "GetActivityType" in text:
        return "trait_list_with_activity_condition"
    if "GetScheme" in text:
        return "trait_list_with_scheme_condition"
    if re.search(r"GetMaA|culture|tradition|men_at_arms", text, re.IGNORECASE):
        return "trait_list_with_maa_or_culture"
    if re.search(r"Concept\s*\(|\[[^\]]*\|E\]", text):
        return "trait_list_with_concept_condition"
    if SPANISH_RE.search(text):
        return "spanish_residual_condition"
    if "GetTrait" in text:
        return "trait_list_with_acclaimed_knight"
    return "other"


def entity_pattern(text: str) -> str:
    if "[acclaimed_knight_possessive|" in text:
        return "acclaimed_knight_possessive"
    if "[acclaimed_knight|" in text:
        return "acclaimed_knight_subject"
    if re.search(r"\[Concept\([^]]*acclaimed_knight", text):
        return "acclaimed_knight_concept_alias"
    if "acclaimed_knight" in text:
        return "mixed_acclaimed_knight_condition"
    return "other"


def propose_correction(text: str) -> str:
    corrected = text
    corrected = re.sub(r"\btem um car[áa]cter\s+", "tem o traço ", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\btem um caracter\s+", "tem o traço ", corrected, flags=re.IGNORECASE)
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
    corrected = re.sub(r"\bpossui um tra[çc]o\s+", "tem o traço ", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bcar[áa]cter\b", "traço", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bcaracter\b", "traço", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bcaracter[íi]stica\b", "traço", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bcombata\b", "lute", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bocupe\b", "ocupa", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bt[úu]\b", "você", corrected, flags=re.IGNORECASE)
    return corrected


def safe_correction(current_text: str, corrected_text: str) -> bool:
    if current_text == corrected_text:
        return False
    if not tokens_preserved(current_text, corrected_text):
        return False
    if BAD_ENCODING_RE.search(corrected_text):
        return False
    if re.search(r"\w\?\w", corrected_text, flags=re.UNICODE):
        return False
    return True


def is_primary(row: dict[str, Any]) -> bool:
    return row.get("decision") == "trait_lexical_needs_acclaimed_knight_policy" or row.get(
        "lexical_pattern"
    ) == "acclaimed_knight_entity_policy"


def is_semantic_diagnostic(row: dict[str, Any]) -> bool:
    return row.get("decision") == "trait_lexical_needs_semantic_review" and row.get(
        "lexical_pattern"
    ) == "english_residual_trait_tooltip"


def classify(row: dict[str, Any], source_path: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    req_pattern = requirement_pattern(text)
    ent_pattern = entity_pattern(text)
    risk_flags: list[str] = []
    decision = "acclaimed_knight_requirement_blocked_uncertain"
    lifecycle = False
    requires_apply = False
    corrected_text = ""
    blocked = "blocked_uncertain"
    confidence = 0.72
    rationale = "requisito de cavaleiro aclamado precisa politica dedicada"

    if "ScriptValue" in text:
        decision = "acclaimed_knight_requirement_needs_script_value_policy"
        blocked = "script_value_policy"
        risk_flags.append("script_value")
        confidence = 0.82
        rationale = "ScriptValue domina o risco"
    elif req_pattern == "trait_list_with_activity_condition":
        decision = "acclaimed_knight_requirement_needs_activity_policy"
        blocked = "activity_policy"
        risk_flags.append("activity_condition")
        confidence = 0.8
        rationale = "GetActivityType domina o risco"
    elif req_pattern == "trait_list_with_concept_condition":
        decision = "acclaimed_knight_requirement_needs_concept_condition_policy"
        blocked = "concept_condition_policy"
        risk_flags.append("concept_condition")
        confidence = 0.8
        rationale = "condicao Concept domina o risco"
    else:
        candidate = propose_correction(text)
        if safe_correction(text, candidate):
            corrected_text = candidate
            requires_apply = True
            blocked = ""
            confidence = 0.88
            if req_pattern == "spanish_residual_condition":
                decision = "acclaimed_knight_requirement_spanish_residual_repair_ready"
                rationale = "reparo remove residual espanhol e preserva tokens"
            else:
                decision = "acclaimed_knight_requirement_repair_ready"
                rationale = "reparo padroniza formula de requisito preservando tokens"
        elif req_pattern == "trait_list_with_acclaimed_knight":
            decision = "acclaimed_knight_requirement_style_watch_lifecycle"
            lifecycle = True
            blocked = ""
            risk_flags.append("style_watch")
            confidence = 0.74
            rationale = "texto parece fechavel sem reparo seguro, mas merece watch"
        else:
            decision = "acclaimed_knight_requirement_needs_semantic_review"
            blocked = "semantic_review"
            risk_flags.append("semantic_review")
            confidence = 0.68
            rationale = "frase precisa decisao semantica maior"

    return {
        "source_scope": "acclaimed_knight_policy_primary",
        "source_trait_lexical_jsonl": source_path,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": as_text(row["relative_path"]),
        "source_key": as_text(row["source_key"]),
        "microagent": MICROAGENT,
        "requirement_pattern": req_pattern,
        "entity_pattern": ent_pattern,
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


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            missing = REQUIRED_INPUT_FIELDS - set(row)
            if missing:
                raise RuntimeError(f"Input row {line_number} missing fields: {sorted(missing)}")
            rows.append(row)
    return rows


def validate_outputs(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 20:
        raise RuntimeError(f"Expected 20 primary rows, got {len(rows)}")
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


def build_report(rows: list[dict[str, Any]], semantic_diagnostic: list[dict[str, Any]]) -> str:
    by_decision = Counter(row["decision"] for row in rows)
    by_requirement = Counter(row["requirement_pattern"] for row in rows)
    by_entity = Counter(row["entity_pattern"] for row in rows)
    by_input = Counter(row.get("input_decision", "") for row in semantic_diagnostic)
    by_path = Counter(path_group(row["relative_path"]) for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    blocked_count = sum(1 for row in rows if row["blocked_reason"])
    recommendations: list[str] = []
    if apply_count:
        recommendations.append("sugerir prompt de apply protegido para reparos seguros")
    if lifecycle_count:
        recommendations.append("sugerir ponte read-only separada para lifecycle candidates")
    if by_decision.get("acclaimed_knight_requirement_needs_concept_condition_policy", 0) or by_decision.get(
        "acclaimed_knight_requirement_needs_activity_policy", 0
    ) or by_decision.get("acclaimed_knight_requirement_needs_script_value_policy", 0):
        recommendations.append("quebrar restantes por concept/activity/script subpolicy")

    lines = [
        "Dynamic acclaimed knight requirement policy review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total processado principal: {len(rows)}",
        f"Total semantic review diagnostic: {len(semantic_diagnostic)}",
        "",
        "Contagem por decision:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por requirement_pattern:")
    for key, value in by_requirement.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por entity_pattern:")
    for key, value in by_entity.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por input decision diagnostic:")
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
            "- Exatamente 20 linhas principais na saida.",
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


def write_outputs(rows: list[dict[str, Any]], semantic_diagnostic: list[dict[str, Any]]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_dynamic_acclaimed_knight_requirement_policy_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_dynamic_acclaimed_knight_requirement_policy_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, semantic_diagnostic), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trait-lexical-jsonl", required=True)
    args = parser.parse_args()
    source_path = Path(args.trait_lexical_jsonl)
    input_rows = load_rows(source_path)
    primary = [row for row in input_rows if is_primary(row)]
    semantic_diagnostic = [row for row in input_rows if is_semantic_diagnostic(row)]
    output_rows = [classify(row, str(source_path)) for row in primary]
    validate_outputs(output_rows)
    out_jsonl, out_txt = write_outputs(output_rows, semantic_diagnostic)
    print(out_jsonl)
    print(out_txt)
    print("primary_processed", len(output_rows))
    print("semantic_diagnostic", len(semantic_diagnostic))
    print("decisions", dict(Counter(row["decision"] for row in output_rows)))
    print("patterns", dict(Counter(row["requirement_pattern"] for row in output_rows)))
    print("lifecycle", sum(1 for row in output_rows if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in output_rows if row["requires_apply_later"]))
    print("blocked", sum(1 for row in output_rows if row["blocked_reason"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
