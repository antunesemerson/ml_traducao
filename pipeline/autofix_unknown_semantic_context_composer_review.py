from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "autofix_unknown_semantic_context_composer_review_v1"
CONTEXT_DECISIONS = {"needs_plain_prose_context_composer", "needs_event_context_composer"}
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "ledger_run_id",
    "segment_state_run_id",
    "relative_path",
    "source_key",
    "decision",
    "surface_bucket",
    "suggested_subpolicy",
    "current_text",
    "english_text",
    "spanish_text",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_review_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "input_decision",
    "input_surface_bucket",
    "input_subpolicy",
    "composer_family",
    "composer_decision",
    "suggested_subpolicy",
    "lifecycle_candidate",
    "requires_apply_later",
    "corrected_text",
    "tokens_preserved",
    "confidence",
    "risk_flags",
    "issue_tags",
    "blocked_reason",
    "rationale",
    "current_text",
    "english_text",
    "spanish_text",
}
ALLOWED_COMPOSER_DECISIONS = {
    "composition_ready_plain_prose",
    "composition_ready_event_context",
    "composition_ready_modifier_or_trait_description",
    "composition_ready_memory_or_activity_text",
    "needs_domain_context",
    "needs_dynamic_expression_agent",
    "needs_event_context_composer",
    "needs_plain_prose_repair",
    "needs_event_context_repair",
    "needs_spanish_residual_repair",
    "needs_english_residual_repair",
    "needs_ptbr_fluency_repair",
    "needs_token_boundary_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
DYNAMIC_RE = re.compile(r"\b(?:Get[A-Za-z0-9_]*|Custom|Select_CString|ScriptValue)\b|\$[^$]+\$")
SPANISH_RE = re.compile(
    r"\b(?:verdadero|verdadera|muchos|muchas|penalizaciones|migaja|amarillo|reino|condado|duque|"
    r"señor|senor|dinastia|guerra|cultura|fe|bienvenido|recuperacion|probabilidad)\b",
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
    r"\besta liderando\b|\bate para sua familia\b|\beste personagem suporta silenciosamente\b)"
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def visible_len(text: str) -> int:
    return len(text.replace("\\n", "\n").strip())


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def package_name(relative_path: str) -> str:
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


def domain_sensitive(path: str, key: str, text: str) -> bool:
    haystack = f"{path} {key} {text}".lower()
    markers = (
        "culture",
        "religion",
        "faith",
        "title",
        "nickname",
        "law",
        "war",
        "casus",
        "cb",
        "struggle",
        "dynasty",
        "house",
        "government",
        "succession",
        "artifact",
        "legend",
        "lore",
        "historical",
        "court_position",
        "relation",
        "family",
        "scheme",
        "diarchy",
        "extravagance",
    )
    return any(marker in haystack for marker in markers)


def composer_family(path: str, key: str, text: str, input_decision: str) -> str:
    lower_path = path.lower()
    lower_key = key.lower()
    if DYNAMIC_RE.search(text) or lower_path.startswith("custom_localization") or lower_key.startswith(("loc_", "custom_", "get")):
        return "dynamic_or_tokenized"
    if "event" in lower_path or input_decision == "needs_event_context_composer":
        return "event_context"
    if any(marker in lower_path or marker in lower_key for marker in ("modifier", "trait", "_desc", "description", "regiment")):
        return "modifier_or_trait"
    if any(marker in lower_path or marker in lower_key for marker in ("memory", "memories", "activity", "activities", "travel", "journey", "hunt", "tour", "tournament")):
        return "memory_or_activity"
    if domain_sensitive(lower_path, lower_key, text):
        return "domain_specific"
    return "plain_prose"


def recurring_microagent(path: str, key: str, text: str) -> tuple[bool, str]:
    lower = f"{path} {key} {text}".lower()
    patterns = [
        ("board_game_dialogue_context", ("board_game", "chess", "game_dialogue")),
        ("diarchy_extravagance_description", ("diarchy", "extravagance")),
        ("historical_character_bio", ("historical", "bio", "bookmark")),
        ("artifact_description_lore", ("artifact", "lore")),
        ("journey_guide_description", ("journey", "guide", "travel")),
        ("major_decision_long_desc", ("major_decision", "decision")),
        ("memory_activity_context", ("memory", "activity", "activities")),
        ("modifier_trait_description_context", ("modifier", "trait")),
    ]
    for name, markers in patterns:
        if any(marker in lower for marker in markers):
            return True, name
    return False, ""


def classify(row: dict[str, Any], source_review_jsonl: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    spanish = as_text(row["spanish_text"])
    path = as_text(row["relative_path"])
    key = as_text(row["source_key"])
    input_decision = as_text(row["decision"])
    family = composer_family(path, key, text, input_decision)
    length = visible_len(text)
    words = word_count(text)
    multiline = "\n" in text.strip()
    quote_or_dialogue = any(mark in text for mark in ('"', "“", "”", "«", "»"))
    preserve_tokens = tokens_preserved(text, english)
    risk_flags: list[str] = []
    issue_tags: list[str] = []
    blocked = ""
    corrected_text = ""
    requires_apply = False
    lifecycle = False
    confidence = 0.72
    decision = "blocked_uncertain"
    subpolicy = family
    rationale = "insufficient evidence for safe composition"

    microagent, microagent_name = recurring_microagent(path, key, text)

    if not preserve_tokens:
        decision = "needs_token_boundary_repair"
        blocked = "token_boundary"
        risk_flags.append("token_boundary")
        rationale = "token or markup boundary needs repair before composition"
        confidence = 0.88
    elif DYNAMIC_RE.search(text):
        decision = "needs_dynamic_expression_agent"
        blocked = "dynamic_expression"
        risk_flags.append("dynamic_expression")
        subpolicy = "dynamic_or_tokenized_context_agent"
        rationale = "dynamic expression or CK3 command requires a dedicated semantic agent"
        confidence = 0.86
    elif SPANISH_RE.search(text):
        decision = "needs_spanish_residual_repair"
        blocked = "spanish_residual"
        risk_flags.append("spanish_residual")
        rationale = "visible Spanish residual remains in current text"
        confidence = 0.9
    elif ENGLISH_RE.search(text) and not any(term in text for term in ("Crusader Kings", "Creator Pack")):
        decision = "needs_english_residual_repair"
        blocked = "english_residual"
        risk_flags.append("english_residual")
        rationale = "visible English residual remains in current text"
        confidence = 0.84
    elif MOJIBAKE_RE.search(text) or PTBR_FLUENCY_RE.search(text.lower()):
        if input_decision == "needs_event_context_composer":
            decision = "needs_event_context_repair"
            blocked = "event_context_repair"
        else:
            decision = "needs_ptbr_fluency_repair"
            blocked = "ptbr_fluency"
        risk_flags.append("ptbr_fluency")
        rationale = "PT-BR text looks corrupted, literal, or awkward and needs repair"
        confidence = 0.78
    elif family == "domain_specific":
        if microagent:
            decision = "needs_new_microagent"
            blocked = "needs_new_microagent"
            subpolicy = microagent_name
            issue_tags.append(microagent_name)
            rationale = f"recurring domain family should be handled by {microagent_name}"
            confidence = 0.8
        else:
            decision = "needs_domain_context"
            blocked = "domain_context"
            risk_flags.append("domain_sensitive")
            rationale = "domain-sensitive text needs semantic context before lifecycle"
            confidence = 0.8
    elif family == "event_context":
        if quote_or_dialogue or re.search(r"\b(?:eu|você|voce|ele|ela|meu|minha|seu|sua|nosso|nossa)\b", text, re.IGNORECASE):
            decision = "needs_event_context_composer"
            blocked = "event_context"
            risk_flags.append("perspective_or_dialogue")
            rationale = "event text has dialogue, pronoun, or perspective risk"
            confidence = 0.8
        elif length <= 120 and not multiline:
            decision = "composition_ready_event_context"
            lifecycle = True
            subpolicy = "objective_event_context_ready"
            rationale = "event context phrase is objective and has no visible perspective risk"
            confidence = 0.76
        else:
            decision = "needs_event_context_composer"
            blocked = "event_context"
            risk_flags.append("event_context")
            rationale = "event text needs context validation before lifecycle"
            confidence = 0.76
    elif family == "modifier_or_trait":
        if microagent and length > 140:
            decision = "needs_new_microagent"
            blocked = "needs_new_microagent"
            subpolicy = microagent_name
            issue_tags.append(microagent_name)
            rationale = f"recurring modifier/trait context should be handled by {microagent_name}"
            confidence = 0.78
        elif length <= 140 and not multiline:
            decision = "composition_ready_modifier_or_trait_description"
            lifecycle = True
            subpolicy = "modifier_trait_description_context_ready"
            rationale = "modifier or trait description is objective and appears context-independent"
            confidence = 0.78
        else:
            decision = "blocked_uncertain"
            blocked = "plain_prose_context"
            risk_flags.append("long_modifier_or_trait")
            rationale = "long modifier or trait description needs context validation"
            confidence = 0.74
    elif family == "memory_or_activity":
        if microagent and length > 120:
            decision = "needs_new_microagent"
            blocked = "needs_new_microagent"
            subpolicy = microagent_name
            issue_tags.append(microagent_name)
            rationale = f"recurring memory/activity family should be handled by {microagent_name}"
            confidence = 0.78
        elif length <= 120 and not multiline:
            decision = "composition_ready_memory_or_activity_text"
            lifecycle = True
            subpolicy = "memory_activity_context_ready"
            rationale = "memory/activity text is short, objective, and context-independent"
            confidence = 0.76
        else:
            decision = "blocked_uncertain"
            blocked = "plain_prose_context"
            risk_flags.append("memory_activity_context")
            rationale = "memory/activity text needs context validation"
            confidence = 0.74
    elif family == "plain_prose":
        if length <= 140 and words <= 24 and not multiline:
            decision = "composition_ready_plain_prose"
            lifecycle = True
            subpolicy = "objective_plain_sentence_context_ready"
            rationale = "plain prose is objective, concise, and has no visible residual or domain risk"
            confidence = 0.78
        else:
            decision = "blocked_uncertain"
            blocked = "plain_prose_context"
            risk_flags.append("long_plain_prose")
            rationale = "plain prose is too long or multi-clause for safe lifecycle without context"
            confidence = 0.74

    output = {
        "source_review_jsonl": source_review_jsonl,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": path,
        "source_key": key,
        "input_decision": input_decision,
        "input_surface_bucket": as_text(row["surface_bucket"]),
        "input_subpolicy": as_text(row["suggested_subpolicy"]),
        "composer_family": family,
        "composer_decision": decision,
        "suggested_subpolicy": subpolicy,
        "lifecycle_candidate": lifecycle,
        "requires_apply_later": requires_apply,
        "corrected_text": corrected_text,
        "tokens_preserved": preserve_tokens,
        "confidence": confidence,
        "risk_flags": risk_flags,
        "issue_tags": issue_tags,
        "blocked_reason": "" if lifecycle else blocked,
        "rationale": rationale,
        "current_text": text,
        "english_text": english,
        "spanish_text": spanish,
    }
    return output


def load_review(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            missing = REQUIRED_INPUT_FIELDS - set(row)
            if missing:
                raise RuntimeError(f"Input row {line_number} missing fields: {sorted(missing)}")
            if row["decision"] in CONTEXT_DECISIONS:
                rows.append(row)
    return rows


def validate_outputs(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 115:
        raise RuntimeError(f"Expected 115 context composer rows, got {len(rows)}")
    for row in rows:
        missing = REQUIRED_OUTPUT_FIELDS - set(row)
        if missing:
            raise RuntimeError(f"Output row {row.get('segment_id')} missing fields: {sorted(missing)}")
        if row["composer_decision"] not in ALLOWED_COMPOSER_DECISIONS:
            raise RuntimeError(f"Unexpected composer decision {row['composer_decision']}")
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
            current_tokens = protected_tokens_signature(row["current_text"])
            corrected_tokens = protected_tokens_signature(corrected)
            if current_tokens != corrected_tokens:
                raise RuntimeError(f"Token mismatch in correction for segment {row['segment_id']}")


def build_report(rows: list[dict[str, Any]]) -> str:
    by_decision = Counter(row["composer_decision"] for row in rows)
    by_family = Counter(row["composer_family"] for row in rows)
    by_input = Counter(row["input_decision"] for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    new_microagent_count = sum(1 for row in rows if row["composer_decision"] == "needs_new_microagent")
    subpolicies = Counter(row["suggested_subpolicy"] for row in rows)
    blockers = Counter(row["blocked_reason"] for row in rows if row["blocked_reason"])
    ready_examples = [row for row in rows if row["composer_decision"].startswith("composition_ready_")][:8]
    domain_examples = [row for row in rows if row["composer_decision"] == "needs_domain_context"][:8]
    dynamic_examples = [row for row in rows if row["composer_decision"] == "needs_dynamic_expression_agent"][:8]
    microagent_examples = [row for row in rows if row["composer_decision"] == "needs_new_microagent"][:8]
    repair_examples = [row for row in rows if "_repair" in row["composer_decision"]][:8]

    recommendations: list[str] = []
    if lifecycle_count >= 40:
        recommendations.append("preparar proximo prompt de lifecycle read-only para composition_ready_*")
    else:
        recommendations.append("volume lifecycle ainda moderado; manter lifecycle estreito ou priorizar bloqueios dominantes")
    if apply_count:
        recommendations.append("separar reparos seguros em prompt de apply protegido")
    if new_microagent_count >= 10:
        recommendations.append("criar microagente dedicado para familias recorrentes")
    if lifecycle_count < 20 and new_microagent_count < 10:
        recommendations.append("considerar abandonar este veio e passar ao proximo gargalo")

    lines = [
        "Autofix unknown semantic context composer review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total processado: {len(rows)}",
        "",
        "Contagem por composer_decision:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por composer_family:")
    for key, value in by_family.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por input_decision:")
    for key, value in by_input.most_common():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates futuros: {lifecycle_count}",
            f"Apply candidates futuros: {apply_count}",
            f"Precisam novo microagente: {new_microagent_count}",
            "",
            "Top 15 subpoliticas/familias recorrentes:",
        ]
    )
    for key, value in subpolicies.most_common(15):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Top 15 riscos/bloqueios:")
    for key, value in blockers.most_common(15):
        lines.append(f"- {key}: {value}")

    def add_examples(title: str, examples: list[dict[str, Any]]) -> None:
        if not examples:
            return
        lines.append("")
        lines.append(title)
        for item in examples:
            lines.append(
                f"- {item['segment_id']} | {item['composer_decision']} | {item['source_key']} | {item['current_text'][:120]}"
            )

    add_examples("Top exemplos composition_ready_*:", ready_examples)
    add_examples("Top exemplos needs_domain_context:", domain_examples)
    add_examples("Top exemplos needs_dynamic_expression_agent:", dynamic_examples)
    add_examples("Top exemplos needs_new_microagent:", microagent_examples)
    add_examples("Top exemplos needs_*_repair:", repair_examples)
    lines.extend(
        [
            "",
            "Recomendacao objetiva:",
            *[f"- {item}" for item in recommendations],
            "",
            "Validacoes finais:",
            "- JSONL UTF-8 valido.",
            "- Exatamente 115 linhas na saida.",
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


def write_outputs(rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_autofix_unknown_semantic_context_composer_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_autofix_unknown_semantic_context_composer_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-jsonl", required=True)
    args = parser.parse_args()

    review_path = Path(args.review_jsonl)
    source_review_jsonl = str(review_path)
    input_rows = load_review(review_path)
    output_rows = [classify(row, source_review_jsonl) for row in input_rows]
    validate_outputs(output_rows)
    out_jsonl, out_txt = write_outputs(output_rows)
    by_decision = Counter(row["composer_decision"] for row in output_rows)
    by_family = Counter(row["composer_family"] for row in output_rows)
    lifecycle_count = sum(1 for row in output_rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in output_rows if row["requires_apply_later"])
    new_microagent_count = sum(1 for row in output_rows if row["composer_decision"] == "needs_new_microagent")
    print(out_jsonl)
    print(out_txt)
    print("processed", len(output_rows))
    print("composer_decisions", dict(by_decision))
    print("composer_families", dict(by_family))
    print("lifecycle", lifecycle_count)
    print("apply", apply_count)
    print("new_microagent", new_microagent_count)
    print("subpolicies", dict(Counter(row["suggested_subpolicy"] for row in output_rows).most_common(15)))
    print("blockers", dict(Counter(row["blocked_reason"] for row in output_rows if row["blocked_reason"]).most_common(15)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
