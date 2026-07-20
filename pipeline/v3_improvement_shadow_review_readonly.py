from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "v3_improvement_shadow_review_readonly_v1"
TOKEN_RE = re.compile(
    r"\[[^\[\]\r\n]+\]|\$[^$\r\n]+\$|#[A-Za-z0-9_]+(?:\s+[^#!\r\n]+)?#!|\\n"
)


REVIEWS: dict[int, dict[str, Any]] = {
    243902: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Texto natural e fiel; tokens preservados."},
    101849: {"decision": "corrected_text_possible", "pattern": "literal_fluency_agreement", "reason": "A construção final é pouco natural e apresenta concordância ruim.", "suggested_text": "A dor torna difícil cavalgar, e certamente sentirei o esforço nessas condições."},
    44026: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Formulação natural e semanticamente equivalente."},
    131848: {"decision": "corrected_text_possible", "pattern": "literal_fluency", "reason": "Calque excessivamente literal em 'contemplando a liberação'.", "suggested_text": "Você mantém sua águia ao seu lado por mais algum tempo, pensando em libertá-la mais adiante."},
    133458: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Texto claro, natural e adequado ao contexto."},
    44395: {"decision": "approve_stable_output", "pattern": "false_reopen_acceptable_style", "reason": "Há alternativa estilística possível, mas não há erro que justifique reabertura."},
    135177: {"decision": "corrected_text_possible", "pattern": "literal_redundancy", "reason": "A expressão 'caçada ao homem de uma presa humana' é redundante e pouco natural.", "suggested_text": "Esta taça de caveira é um troféu de uma caçada bem-sucedida a uma presa humana."},
    44367: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Texto natural e fiel."},
    113488: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Pergunta retórica natural em PT-BR."},
    56392: {"decision": "corrected_text_possible", "pattern": "literal_false_friend_style", "reason": "'Excitou a imaginação' soa inadequado neste contexto; 'despertou' é mais natural.", "suggested_text": "Este personagem despertou a imaginação de seus súditos com promessas de vitórias militares."},
    34922: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Texto claro e semanticamente correto."},
    44394: {"decision": "approve_stable_output", "pattern": "false_reopen_acceptable_style", "reason": "A formulação é aceitável e não contém erro operacional."},
    162872: {"decision": "needs_context", "pattern": "missing_dynamic_pronoun_token", "reason": "O token [religious_leader.GetHerHis] do inglês não aparece no PT-BR; a reconstrução exige contexto sintático."},
    123366: {"decision": "corrected_text_possible", "pattern": "semantic_role_mistranslation", "reason": "Footman foi traduzido como camponês, alterando o papel do personagem.", "suggested_text": "No início, pensei que o simples soldado de infantaria fosse um tolo por entrar no campo de treinamento."},
    120100: {"decision": "corrected_text_possible", "pattern": "gender_perspective_neutralization", "reason": "'Temê-lo' fixa gênero sem necessidade; a versão neutra preserva a perspectiva.", "suggested_text": "Ninguém pode provar seu envolvimento, mas todos suspeitam o bastante para temer você."},
    122296: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Enumeração natural e fiel."},
    43901: {"decision": "corrected_text_possible", "pattern": "literal_redundancy", "reason": "'De qualquer maneira' repete a função de 'no entanto'.", "suggested_text": "No entanto, nenhum dos vencedores é um pretendente elegível..."},
    65271: {"decision": "needs_context", "pattern": "historical_glossary_term", "reason": "'Armadilha de pé' não é terminologia natural; a escolha final exige validação do objeto histórico."},
    156833: {"decision": "approve_stable_output", "pattern": "safe_pronoun_elision", "reason": "A elisão do possessivo dinâmico é gramatical e segura em português."},
    243978: {"decision": "needs_context", "pattern": "missing_action_token", "reason": "O token $sway_action$ foi substituído por literal; é preciso validar como a ação renderiza na interface."},
    105469: {"decision": "corrected_text_possible", "pattern": "missing_space_before_token", "reason": "Falta espaço antes do token de personagem.", "suggested_text": "\\\"Deixe [sad_kid.GetHerHim] em paz!\\\""},
    61003: {"decision": "approve_stable_output", "pattern": "intentional_fragment_boundary", "reason": "A fala é um fragmento composto e acompanha a fronteira do source."},
    64785: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Tradução direta, natural e correta."},
    287686: {"decision": "approve_stable_output", "pattern": "false_reopen_token_preserved", "reason": "Termo e token preservados corretamente."},
    33332: {"decision": "approve_stable_output", "pattern": "false_reopen_acceptable_style", "reason": "Registro informal aceitável para a fala."},
    40283: {"decision": "corrected_text_possible", "pattern": "missing_space_before_token", "reason": "Falta espaço antes do token de personagem.", "suggested_text": "\\\"Expulsem [rival.GetHerHim] daqui!\\\""},
    162563: {"decision": "approve_stable_output", "pattern": "dynamic_fragment_preserved", "reason": "Fragmento dinâmico e pontuação preservados."},
    287880: {"decision": "approve_stable_output", "pattern": "false_reopen_token_preserved", "reason": "Texto natural com token preservado."},
    61043: {"decision": "corrected_text_possible", "pattern": "token_signature_downgrade", "reason": "GetFullNameNoTooltip foi trocado por GetFullName, podendo introduzir tooltip indevido.", "suggested_text": "\\\"Seu tolo, eu sou [ROOT.GetCharacter.GetFullNameNoTooltip]!\\\""},
    79299: {"decision": "corrected_text_possible", "pattern": "literal_pronoun_redundancy", "reason": "'A vocês' é desnecessário e pouco natural nesta fala.", "suggested_text": "\\\"Agradeço por esta contribuição.\\\""},
    243987: {"decision": "approve_stable_output", "pattern": "false_reopen_token_preserved", "reason": "Texto e token corretos."},
    243883: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Expressão idiomática natural em PT-BR."},
    156285: {"decision": "approve_stable_output", "pattern": "known_bold_nao_microrepair", "reason": "A correção #bold no#! para #bold não#! é válida e deve ser reconhecida como melhoria."},
    63436: {"decision": "approve_stable_output", "pattern": "false_reopen_natural_pt", "reason": "Texto claro e fiel."},
    156512: {"decision": "approve_stable_output", "pattern": "ui_infinitive_style", "reason": "Infinitivo é adequado ao rótulo de interação."},
    131919: {"decision": "approve_stable_output", "pattern": "intentional_fragment_boundary", "reason": "Fragmento curto natural e coerente com o source."},
    155995: {"decision": "approve_stable_output", "pattern": "safe_pronoun_elision", "reason": "A omissão do pronome evita gênero desnecessário sem perda semântica."},
    158599: {"decision": "needs_context", "pattern": "player_gender_perspective", "reason": "'O entretenha' pode fixar gênero do jogador; requer regra de perspectiva antes de corrigir."},
    76003: {"decision": "needs_context", "pattern": "untranslated_domain_term", "reason": "'Levies' permaneceu em inglês; a correção depende da terminologia oficial adotada pelo projeto."},
    61020: {"decision": "corrected_text_possible", "pattern": "singular_player_perspective", "reason": "ROOT.Char é singular, mas o texto usa 'vocês não esperavam'.", "suggested_text": "\\\"Olá, [ROOT.Char.GetFirstName]. Aposto que você não esperava me ver novamente."},
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def token_signature(value: str | None) -> list[str]:
    return TOKEN_RE.findall(value or "")


def latest_queue() -> Path:
    reports = db.project_path(db.load_settings()["reports_dir"])
    matches = sorted(reports.glob("*_v3_improvement_shadow_queue_readonly.jsonl"))
    if not matches:
        raise RuntimeError("No V3 shadow queue JSONL was found.")
    return matches[-1]


def output_paths() -> dict[str, Path]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_v3_improvement_shadow_review_readonly"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }


def load_records(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = {int(row["segment_id"]) for row in rows}
    expected = set(REVIEWS)
    if ids != expected:
        raise RuntimeError(f"Review/queue ID mismatch: missing={sorted(ids - expected)}, extra={sorted(expected - ids)}")
    if len(rows) != len(ids):
        raise RuntimeError("The source queue contains duplicate segment IDs.")
    return rows


def reviewed_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed: list[dict[str, Any]] = []
    for row in rows:
        review = dict(REVIEWS[int(row["segment_id"])])
        suggestion = review.get("suggested_text")
        old_tokens = token_signature(row.get("output_text"))
        spanish_tokens = token_signature(row.get("spanish_text"))
        new_tokens = token_signature(suggestion) if suggestion is not None else old_tokens
        row = dict(row)
        row.update(review)
        row.update(
            {
                "review_rule_version": RULE_VERSION,
                "reviewed_at": datetime.now().isoformat(timespec="seconds"),
                "current_token_signature": old_tokens,
                "spanish_token_signature": spanish_tokens,
                "suggested_token_signature": new_tokens,
                "token_signature_exact": old_tokens == new_tokens,
                "spanish_token_signature_exact": spanish_tokens == new_tokens,
                "candidate_generation_allowed": False,
                "apply_allowed": False,
                "source_changed": False,
                "output_changed": False,
            }
        )
        reviewed.append(row)
    return reviewed


def write_reports(paths: dict[str, Path], queue_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["decision"] for row in rows)
    patterns = Counter(row["pattern"] for row in rows)
    families = Counter(row["primary_family"] for row in rows)
    corrected = [row for row in rows if row["decision"] == "corrected_text_possible"]
    deterministic_corrections = [
        row
        for row in corrected
        if row["token_signature_exact"] and row["spanish_token_signature_exact"]
    ]
    token_authority_reviews = [
        row
        for row in corrected
        if not row["token_signature_exact"] or not row["spanish_token_signature_exact"]
    ]
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "source_queue": str(queue_path),
        "record_count": len(rows),
        "decision_counts": dict(decisions),
        "family_counts": dict(families),
        "pattern_counts": dict(patterns),
        "corrected_text_token_signature_exact": sum(bool(row["token_signature_exact"]) for row in corrected),
        "corrected_text_token_signature_changed": sum(not bool(row["token_signature_exact"]) for row in corrected),
        "deterministic_correction_review_count": len(deterministic_corrections),
        "token_authority_review_count": len(token_authority_reviews),
        "token_authority_review_segment_ids": [int(row["segment_id"]) for row in token_authority_reviews],
        "candidate_generation": 0,
        "apply": 0,
        "source_changed": False,
        "output_changed": False,
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# V3 improvement shadow review",
        "",
        f"- Source queue: `{queue_path.name}`",
        f"- Records reviewed: `{len(rows)}`",
        f"- Stable output approved: `{decisions.get('approve_stable_output', 0)}`",
        f"- Corrected text possible: `{decisions.get('corrected_text_possible', 0)}`",
        f"- Deterministic correction review: `{len(deterministic_corrections)}`",
        f"- Cross-language token authority review: `{len(token_authority_reviews)}`",
        f"- Needs context: `{decisions.get('needs_context', 0)}`",
        "- Candidate generation: `0`",
        "- Apply: `0`",
        "",
        "## Architectural reading",
        "",
        "- Stable, high-score output must not be reopened solely because a legacy family still routes it to autofix.",
        "- Whitespace next to dynamic tokens and exact getter variants are deterministic specialist checks.",
        "- Pronoun/perspective, historical terminology and omitted runtime actions remain context-sensitive.",
        "- Literal fluency problems are valid learning examples, but require preview before any protected apply.",
        "",
        "## Pattern counts",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in patterns.most_common())
    lines.extend(["", "## Reviewed items", ""])
    for row in rows:
        lines.extend(
            [
                f"### {row['segment_id']} - {row['decision']}",
                "",
                f"- Family: `{row['primary_family']}`",
                f"- Path/key: `{row['relative_path']} :: {row['source_key']}`",
                f"- Pattern: `{row['pattern']}`",
                f"- Reason: {row['reason']}",
                f"- Current PT-BR: {row.get('output_text') or ''}",
            ]
        )
        if row.get("suggested_text") is not None:
            lines.extend(
                [
                    f"- Suggested PT-BR: {row['suggested_text']}",
                    f"- Token signature exact: `{str(bool(row['token_signature_exact'])).lower()}`",
                    f"- Spanish token signature exact: `{str(bool(row['spanish_token_signature_exact'])).lower()}`",
                ]
            )
        lines.append("")
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Review the V3 improvement shadow queue without changing operational state.")
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args()
    queue = args.queue.resolve() if args.queue else latest_queue()
    rows = reviewed_records(load_records(queue))
    paths = output_paths()
    summary = write_reports(paths, queue, rows)
    print("[v3-shadow-review] Read-only review completed")
    print(f"[v3-shadow-review] Records: {summary['record_count']}")
    print(f"[v3-shadow-review] Decisions: {summary['decision_counts']}")
    print(f"[v3-shadow-review] Markdown: {paths['markdown']}")
    print(f"[v3-shadow-review] JSONL: {paths['jsonl']}")
    print(f"[v3-shadow-review] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
