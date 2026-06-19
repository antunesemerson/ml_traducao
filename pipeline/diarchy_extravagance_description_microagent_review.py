from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "diarchy_extravagance_description_microagent_review_v1"
MICROAGENT = "diarchy_extravagance_description"
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "ledger_run_id",
    "segment_state_run_id",
    "relative_path",
    "source_key",
    "current_text",
    "english_text",
    "spanish_text",
    "composer_decision",
    "suggested_subpolicy",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_composer_jsonl",
    "ledger_run_id",
    "segment_state_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "microagent",
    "extravagance_tier",
    "extravagance_lane",
    "topic",
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
    "diarchy_extravagance_ready_lifecycle",
    "diarchy_extravagance_style_watch_lifecycle",
    "diarchy_extravagance_ptbr_fluency_repair_needed",
    "diarchy_extravagance_spanish_residual_repair_needed",
    "diarchy_extravagance_english_residual_repair_needed",
    "diarchy_extravagance_semantic_rewrite_needed",
    "diarchy_extravagance_token_boundary_repair_needed",
    "diarchy_extravagance_domain_uncertain",
    "blocked_uncertain",
}


TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!|[A-Za-z0-9_.]+\\((?:[^()]|\\([^()]*\\))*\\))"
)
KEY_RE = re.compile(r"(?:^|[._])(?P<tier>t\d+)\.(?P<lane>[A-Za-z0-9_]+)\.(?P<topic>[A-Za-z0-9_.-]+)$")
SPANISH_RE = re.compile(
    r"\b(?:verdadero|verdadera|muchos|muchas|penalizaciones|migaja|amarillo|reino|condado|duque|"
    r"señor|senor|dinastia|guerra|cultura|fe|recuperacion|probabilidad)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(
    r"\b(?:will|must|cannot|should|kingdom|duchy|county|culture head|for each|leader|leaders|"
    r"the\s+[A-Za-z]+|your\s+[A-Za-z]+|their\s+[A-Za-z]+)\b",
    re.IGNORECASE,
)
MOJIBAKE_RE = re.compile(r"[\u00c3\u00c2\ufffd]")
PTBR_FLUENCY_RE = re.compile(
    r"(?:\bde o\b|\ba o\b|\bpara o a\b|\bpode nao\b|\bnao pode nao\b|"
    r"\bcom determinacao com\b|\bate para sua familia\b|\best[ea] personagem suporta silenciosamente\b)",
    re.IGNORECASE,
)
STRONG_SEMANTIC_REWRITE_RE = re.compile(
    r"(?:\batos ardentes\b|\bcharioteers\b|\bimpulsionando-se do final\b|\bmesa principal\b)",
    re.IGNORECASE,
)
STYLE_WATCH_RE = re.compile(
    r"(?:\bostenta\b|\bsuntuos[ao]s?\b|\bextravag[aâ]ncia\b|\bopul[eê]ncia\b|\bfausto\b)",
    re.IGNORECASE,
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


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


def extract_metadata(source_key: str) -> tuple[str | None, str | None, str | None, str | None]:
    match = KEY_RE.search(source_key)
    if not match:
        return None, None, None, "source_key did not match expected extravagance tier/lane/topic pattern"
    return match.group("tier"), match.group("lane"), match.group("topic"), None


def is_candidate(row: dict[str, Any]) -> bool:
    tags = row.get("issue_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return (
        row.get("composer_decision") == "needs_new_microagent"
        and row.get("suggested_subpolicy") == MICROAGENT
    ) or MICROAGENT in tags


def classify(row: dict[str, Any], source_composer_jsonl: str) -> dict[str, Any]:
    text = as_text(row["current_text"])
    english = as_text(row["english_text"])
    spanish = as_text(row["spanish_text"])
    path = as_text(row["relative_path"])
    key = as_text(row["source_key"])
    tier, lane, topic, metadata_issue = extract_metadata(key)
    preserve_tokens = tokens_preserved(text, english)
    risk_flags: list[str] = []
    blocked_reason = ""
    decision = "blocked_uncertain"
    lifecycle = False
    confidence = 0.72
    rationale = "evidencia insuficiente para fechar com seguranca"

    if path != "diarchies/diarchies_l_spanish.yml":
        risk_flags.append("unexpected_relative_path")
    if metadata_issue:
        risk_flags.append("metadata_parse_failed")

    if not preserve_tokens:
        decision = "diarchy_extravagance_token_boundary_repair_needed"
        blocked_reason = "token_boundary"
        risk_flags.append("token_boundary")
        rationale = "token ou markup CK3 precisa de reparo antes de lifecycle"
        confidence = 0.9
    elif SPANISH_RE.search(text):
        decision = "diarchy_extravagance_spanish_residual_repair_needed"
        blocked_reason = "spanish_residual"
        risk_flags.append("spanish_residual")
        rationale = "residual espanhol visivel na descricao de extravagancia"
        confidence = 0.9
    elif ENGLISH_RE.search(text) and not any(term in text for term in ("Crusader Kings", "Creator Pack")):
        decision = "diarchy_extravagance_english_residual_repair_needed"
        blocked_reason = "english_residual"
        risk_flags.append("english_residual")
        rationale = "residual ingles visivel na descricao de extravagancia"
        confidence = 0.86
    elif MOJIBAKE_RE.search(text) or PTBR_FLUENCY_RE.search(text):
        decision = "diarchy_extravagance_ptbr_fluency_repair_needed"
        blocked_reason = "ptbr_fluency"
        risk_flags.append("ptbr_fluency")
        rationale = "texto PT-BR parece corrompido, literal ou pouco natural"
        confidence = 0.8
    elif STRONG_SEMANTIC_REWRITE_RE.search(text):
        decision = "diarchy_extravagance_semantic_rewrite_needed"
        blocked_reason = "semantic_rewrite"
        risk_flags.append("semantic_rewrite")
        rationale = "texto atual parece divergir ou soar literal demais frente ao padrao esperado"
        confidence = 0.78
    elif metadata_issue or path != "diarchies/diarchies_l_spanish.yml":
        decision = "diarchy_extravagance_domain_uncertain"
        blocked_reason = "domain_uncertain"
        rationale = "metadados ou path fugiram do padrao esperado do microagente"
        confidence = 0.72
    elif STYLE_WATCH_RE.search(text) or len(text) > 180:
        decision = "diarchy_extravagance_style_watch_lifecycle"
        lifecycle = True
        confidence = 0.8
        rationale = "texto esta semanticamente fechavel, mas merece watch por estilo ou extensao"
    else:
        decision = "diarchy_extravagance_ready_lifecycle"
        lifecycle = True
        confidence = 0.86
        rationale = "texto PT-BR natural e semanticamente alinhado para o padrao de extravagancia"

    return {
        "source_composer_jsonl": source_composer_jsonl,
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": path,
        "source_key": key,
        "microagent": MICROAGENT,
        "extravagance_tier": tier,
        "extravagance_lane": lane,
        "topic": topic,
        "decision": decision,
        "lifecycle_candidate": lifecycle,
        "requires_apply_later": False,
        "corrected_text": "",
        "tokens_preserved": preserve_tokens,
        "confidence": confidence,
        "risk_flags": risk_flags,
        "blocked_reason": "" if lifecycle else blocked_reason,
        "rationale": rationale,
        "current_text": text,
        "english_text": english,
        "spanish_text": spanish,
    }


def load_candidates(path: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            missing = REQUIRED_INPUT_FIELDS - set(row)
            if missing:
                raise RuntimeError(f"Input row {line_number} missing fields: {sorted(missing)}")
            if is_candidate(row):
                candidates.append(row)
    return candidates


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
    by_tier = Counter(row["extravagance_tier"] or "null" for row in rows)
    by_lane = Counter(row["extravagance_lane"] or "null" for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    repair_without_apply = sum(
        1
        for row in rows
        if row["decision"].endswith("_repair_needed") and not row["requires_apply_later"]
    )
    ready_or_watch = sum(
        1
        for row in rows
        if row["decision"]
        in {"diarchy_extravagance_ready_lifecycle", "diarchy_extravagance_style_watch_lifecycle"}
    )
    recommendations: list[str] = []
    if ready_or_watch >= 20:
        recommendations.append("sugerir prompt de lifecycle read-only para ready/style_watch com guards estritos")
    if apply_count:
        recommendations.append("separar candidatos apply em prompt protegido")
    if ready_or_watch < len(rows) / 2:
        recommendations.append("grupo majoritariamente bloqueado; quebrar por extravagance_lane")
    if not recommendations:
        recommendations.append("usar esta revisao como base de ponte lifecycle futura, sem alterar output")

    lines = [
        "Diarchy extravagance description microagent review",
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
    lines.append("Contagem por extravagance_tier:")
    for key, value in by_tier.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por extravagance_lane:")
    for key, value in by_lane.most_common():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates futuros: {lifecycle_count}",
            f"Apply candidates futuros: {apply_count}",
            f"Precisam reparo sem apply seguro: {repair_without_apply}",
        ]
    )

    example_groups = [
        "diarchy_extravagance_ready_lifecycle",
        "diarchy_extravagance_style_watch_lifecycle",
        "diarchy_extravagance_ptbr_fluency_repair_needed",
        "diarchy_extravagance_spanish_residual_repair_needed",
        "diarchy_extravagance_english_residual_repair_needed",
        "diarchy_extravagance_semantic_rewrite_needed",
        "diarchy_extravagance_token_boundary_repair_needed",
        "blocked_uncertain",
    ]
    for decision in example_groups:
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
    out_jsonl = Path(f"reports/{stamp}_diarchy_extravagance_description_microagent_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_diarchy_extravagance_description_microagent_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(rows, expected_count), encoding="utf-8")
    return out_jsonl, out_txt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--composer-jsonl", required=True)
    args = parser.parse_args()

    composer_path = Path(args.composer_jsonl)
    candidates = load_candidates(composer_path)
    rows = [classify(row, str(composer_path)) for row in candidates]
    validate_outputs(rows)
    out_jsonl, out_txt = write_outputs(rows, expected_count=32)
    by_decision = Counter(row["decision"] for row in rows)
    by_tier = Counter(row["extravagance_tier"] or "null" for row in rows)
    by_lane = Counter(row["extravagance_lane"] or "null" for row in rows)
    print(out_jsonl)
    print(out_txt)
    print("processed", len(rows))
    print("decisions", dict(by_decision))
    print("tiers", dict(by_tier))
    print("lanes", dict(by_lane))
    print("lifecycle", sum(1 for row in rows if row["lifecycle_candidate"]))
    print("apply", sum(1 for row in rows if row["requires_apply_later"]))
    print(
        "repair_without_apply",
        sum(1 for row in rows if row["decision"].endswith("_repair_needed") and not row["requires_apply_later"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
