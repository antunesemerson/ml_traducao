from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from issue_event_surface_subcluster_report import key_surface
from local_quality_validator import validate_text


RULE_VERSION = "issue_event_surface_assisted_draft_v3"

SPANISH_RESIDUAL_RE = re.compile(
    r"\b("
    r"har[eé]|haremos|har[aá]n|cómo|tenemos|deprisa|dejad|dejadles|"
    r"contad|cont[aá]dmelo|vosotros|est[aá]is|hab[eé]is|"
    r"obligar|bruja|brujas|maldici[oó]n|mis|guardias|haced|hasta"
    r")\b",
    re.IGNORECASE,
)

TOKEN_GLUE_RE = re.compile(r"[A-Za-zÀ-ÿ]\[[A-Za-z0-9_]+\.(?:GetHerHim|GetHerselfHimself|GetHerHis|GetSheHe)")
REFERENCE_GLUE_RE = re.compile(r"\$[^$]+\$[A-Za-zÀ-ÿ\"“”#]")
ASCII_REFERENCE_GLUE_RE = re.compile(r"[A-Za-z]\[[A-Za-z0-9_]+\.")
GOVERNED_PREFIX_SEPARATOR_RE = re.compile(r"^(?:(?:\$[^$]+\$|\[[^\]]+\])\s*)+[:,]\s*\S")
KNIGHT_DUPLICATE_RE = re.compile(r"\bcavaleiro\b.*\[knight\|", re.IGNORECASE)
INFINITIVE_OPTION_RE = re.compile(r"^Não fazer aposta\.$", re.IGNORECASE)
ENGLISH_RESIDUAL_RE = re.compile(r"doo-dah", re.IGNORECASE)
ENGLISH_SENTENCE_RESIDUAL_RE = re.compile(r"\b(Fighting tactically|I keep most of my attacks|only leaning in)\b", re.IGNORECASE)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_number} is not an object: {path}")
        texts = payload.get("texts")
        if isinstance(texts, dict):
            for key in ("evidence_text", "confirmed_text", "english_text", "spanish_text"):
                if payload.get(key) in (None, "") and texts.get(key) not in (None, ""):
                    payload[key] = texts.get(key)
        rows.append(payload)
    return rows


def queue_text(row: dict[str, Any]) -> str:
    texts = row.get("texts")
    if isinstance(texts, dict):
        for key in ("evidence_text", "confirmed_text", "output_text"):
            value = texts.get(key)
            if value:
                return str(value)
    return str(row.get("evidence_text") or row.get("confirmed_text") or "")


def visible_text(text: str) -> str:
    text = text.replace("\\n", " ").replace("\\t", " ")
    visible = re.sub(r"\[[^\]]+\]", " ", text)
    visible = re.sub(r"\$[^$]+\$", " ", visible)
    visible = re.sub(r"#[^#\s]+", " ", visible)
    return re.sub(r"\s+", " ", visible).strip()


def has_governed_prefix_separator(text: str) -> bool:
    return bool(GOVERNED_PREFIX_SEPARATOR_RE.match(text.strip()))


def spanish_hits(text: str) -> list[str]:
    visible = visible_text(text)
    hits: list[str] = []
    for match in SPANISH_RESIDUAL_RE.finditer(visible):
        hits.append(match.group(1))
    if "Les haré saber cómo" in text:
        hits.append("les_hare_saber_como")
    return hits


def validator_reason(text: str) -> str:
    result = validate_text(text)
    issues = result.get("issues") or []
    if not issues:
        return ""
    return ",".join(f"{issue.get('code') or 'quality_issue'}:{issue.get('severity') or 'unknown'}" for issue in issues[:4])


def router_repair_reason(row: dict[str, Any], text: str) -> tuple[str, str] | None:
    source_key = str(row.get("source_key") or "")
    english_text = str(row.get("english_text") or "")
    visible = visible_text(text)
    lower = visible.lower()

    if (REFERENCE_GLUE_RE.search(text) or ASCII_REFERENCE_GLUE_RE.search(text)) and not has_governed_prefix_separator(text):
        return "", "localization_reference_glued_to_visible_text"
    if ENGLISH_RESIDUAL_RE.search(visible):
        return "", "english_residual_visible_chant"
    if ENGLISH_SENTENCE_RESIDUAL_RE.search(visible):
        return "", "english_residual_visible_sentence"
    if re.search(r"\bmilitary\b", visible, re.IGNORECASE):
        return "", "english_residual_military_visible_fragment"
    if visible.lstrip("\"'“”‘’").startswith("!"):
        return "", "leading_plain_bang_visible_fragment"
    if "eles estavam faltando" in lower:
        return "", "literal_english_missing_object_phrase"
    if "tamanho arrogância" in lower:
        return "Quanta arrogância...", "literal_size_arrogance_phrase"
    if source_key == "tour_grounds.recruited_my_courtier" and lower.startswith("seduziu "):
        return "", "semantic_voice_mismatch_passive_tempted_to_leave"
    if source_key == "DistantLandsIndia1_description" and "a listras marcantes" in lower:
        return "listras marcantes, corpo poderoso e olhar feroz", "article_fragment_agreement_error"
    if "embora eu apreciaria" in lower:
        return "", "conditional_mood_after_embora"
    if "os permite passar fome" in lower:
        return "Ninguém seguirá quem permite que passem fome.", "object_pronoun_case_allows_them_to_starve"
    if "verei que vocês aprendam" in lower:
        return "Pobres almas infelizes... Vou garantir que vocês aprendam a ler!", "unnatural_future_ensure_clause"
    if ("trusted me" in english_text.lower() or source_key == "trusted_friend_opinion") and "confio em mim" in lower:
        return "Confiou em mim.", "semantic_tense_mismatch_trusted_me"
    if "satisfeit?" in lower:
        return "Nada nunca vai satisfazer você?", "truncated_gendered_satisfeito_fragment"
    if "spymaster" in lower:
        return "Você deveria investigar isso, chefe de espiões.", "english_residual_spymaster_role"
    if "homens de topo" in lower:
        return "Precisa ser cuidado pelos meus melhores homens", "literal_top_men_fragment"
    if "espand normal" in lower:
        return "Você não pode escolher alharma normal como todo mundo?", "untranslated_espand_plant_fragment"
    if "preciosíssim" in lower:
        return "Você precisa entender. Você é importante demais para correr riscos.", "truncated_gendered_preciosissimo_fragment"
    if source_key.endswith(".outro") and " de para " in f" {lower} ":
        return "", "broken_preposition_sequence"
    if re.search(r"\bmis\b", lower):
        return "", "spanish_residual_mis_visible_fragment"
    if re.search(r"\bguardias\b", lower):
        return "", "spanish_residual_guardias_visible_fragment"
    if re.search(r"\bhaced\b", lower):
        return "", "spanish_residual_haced_visible_fragment"
    if "hasta que se va" in lower:
        return "", "spanish_residual_hasta_que_se_va_visible_fragment"
    if re.search(r"\bqualificad\b", lower):
        return "", "truncated_gendered_qualificado_fragment"
    if text.strip().startswith("!["):
        return "", "leading_bang_before_localization_reference"
    if re.search(r"\bm\s+\[[^\]]+\]", text, re.IGNORECASE):
        return "", "broken_possessive_before_localization_reference"
    return None


def context_composer_decision(row: dict[str, Any], text: str) -> tuple[str, str, str]:
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "").lower()
    surface = key_surface(source_key)
    visible = visible_text(text).strip()
    lower = visible.lower()

    repair = router_repair_reason(row, text)
    if repair:
        corrected_text, reason = repair
        return "needs_repair", corrected_text, reason

    if "debug" in relative_path or source_key.startswith("test."):
        return "needs_domain_context", "", "debug_or_test_context_surface"
    if surface not in {"title_key", "description_key", "flavor_key", "success_key", "failure_key"}:
        return "needs_domain_context", "", "context_composer_unsupported_key_surface"
    if not visible:
        return "needs_domain_context", "", "empty_context_fragment"
    if lower.startswith(":") and not has_governed_prefix_separator(text):
        return "needs_repair", "", "leading_colon_context_title_fragment"
    if re.match(r"^![A-Za-zÀ-ÿ]", visible):
        return "needs_repair", "", "leading_plain_exclamation_context_fragment"
    if visible.count('"') % 2 == 1 or text.count('"') % 2 == 1:
        return "needs_repair", "", "unbalanced_quote_context_fragment"
    if re.search(r"\b(de|para)\s+(?:de|para)\b", lower):
        return "needs_repair", "", "broken_preposition_sequence"
    if "um único, enorme, olho cru" in lower:
        return "needs_repair", "um único olho enorme e cru.", "adjective_comma_chain_literal_fragment"
    if "violência, gore e obscenidades" in lower:
        return "needs_repair", "", "english_loanword_gore_visible_fragment"
    if "um torrente de" in lower:
        corrected = re.sub(r"\bum\s+torrente\s+de\b", "uma torrente de", text, flags=re.IGNORECASE)
        return "needs_repair", corrected, "article_gender_torrente_fragment"
    if re.search(r"\bhonest\b", lower):
        return "needs_repair", "", "english_residual_honest_visible_fragment"
    if re.search(r"\bchei\s+de\b", lower):
        return "needs_repair", "", "truncated_gendered_cheio_fragment"
    if re.search(r"\bmuito\s+bo\s+em\b", lower):
        return "needs_repair", "", "truncated_gendered_bom_fragment"

    return "composition_ready", "", "clean_context_fragment_ready_for_composition"


def short_label_style_decision(row: dict[str, Any], text: str) -> tuple[str, str, str]:
    source_key = str(row.get("source_key") or "")
    visible = visible_text(text).strip()
    lower = visible.lower()

    repair = router_repair_reason(row, text)
    if repair:
        corrected_text, reason = repair
        return "needs_repair", corrected_text, reason

    if not visible:
        return "needs_domain_context", "", "empty_short_label_style_fragment"
    if source_key == "DistantLandsSteppe2_description" and "o manes" in lower:
        return "needs_repair", "crinas fluidas e membros fortes", "literal_manes_article_fragment"
    if source_key == "DistantLandsIndia2_description" and "o tamanho imponente e pele grossa" in lower:
        return "needs_repair", "tamanho imponente e pele grossa", "article_fragment_size_and_skin"
    if "toca uma corda" in lower:
        return "needs_repair", "Sua apresentação toca a fibra sensível", "literal_strikes_a_chord_fragment"
    if "dá pathos" in lower:
        return "needs_repair", "O clima dá dramaticidade às suas palavras", "english_loanword_pathos_fragment"
    if "após a interrogatório" in lower:
        return "needs_repair", "Água encontrada após o interrogatório", "article_gender_interrogatorio_fragment"
    if "seu sabotagem" in lower:
        return "needs_repair", "Sua sabotagem é bem-sucedida", "article_gender_sabotagem_fragment"

    return "safe_short_label", "", "clean_short_label_style_surface"


def effect_reward_phrase_decision(row: dict[str, Any], text: str) -> tuple[str, str, str]:
    source_key = str(row.get("source_key") or "")
    english_text = str(row.get("english_text") or "")
    visible = visible_text(text).strip()
    lower = visible.lower()

    repair = router_repair_reason(row, text)
    if repair:
        corrected_text, reason = repair
        return "needs_repair", corrected_text, reason

    if not visible:
        return "needs_domain_context", "", "empty_effect_reward_phrase"

    if source_key.startswith("artifact_monthly_") and "_lifestyle_xp_" in source_key:
        canonical_by_lifestyle = {
            "martial": "Experiência mensal de estilo de vida marcial",
            "stewardship": "Experiência mensal de estilo de vida em administração",
            "intrigue": "Experiência mensal de estilo de vida em intriga",
            "learning": "Experiência mensal de estilo de vida em aprendizado",
        }
        lifestyle = source_key.removeprefix("artifact_monthly_").split("_lifestyle_xp_", 1)[0]
        canonical = canonical_by_lifestyle.get(lifestyle)
        if not canonical:
            return "needs_domain_context", "", f"unknown_monthly_lifestyle_reward:{lifestyle}"
        if visible == canonical:
            return "safe_short_label", "", f"clean_monthly_{lifestyle}_lifestyle_reward"
        return "needs_repair", canonical, f"normalize_monthly_{lifestyle}_lifestyle_reward"

    if "xp de estilo de vida" in lower:
        return "needs_repair", "", "ui_xp_abbreviation_in_lifestyle_reward"
    if "experiência mensal de experiência de vida" in lower:
        return "needs_repair", "", "duplicated_experience_lifestyle_reward"
    if "monthly stewardship lifestyle xp" in english_text.lower() and "administração" not in lower:
        return "needs_repair", "Experiência mensal de estilo de vida em administração", "stewardship_lifestyle_not_administration"
    if "tesouraria" in lower:
        return "needs_repair", "Experiência mensal de estilo de vida em administração", "stewardship_lifestyle_literal_treasury"
    if "estilo de vida de aprendizagem" in lower:
        return "needs_repair", "Experiência mensal de estilo de vida em aprendizado", "learning_lifestyle_inconsistent_label"
    if "estilo de vida de aprendizado" in lower:
        return "needs_repair", "Experiência mensal de estilo de vida em aprendizado", "learning_lifestyle_preposition_label"
    if "estilo de vida de intriga" in lower:
        return "needs_repair", "Experiência mensal de estilo de vida em intriga", "intrigue_lifestyle_preposition_label"
    if "experiência mensal" in lower and "estilo de vida" in lower:
        return "safe_short_label", "", "clean_monthly_lifestyle_experience_reward"
    if "experiência" in lower or "prestígio" in lower or "piedade" in lower or "ouro" in lower:
        return "safe_short_label", "", "clean_effect_reward_phrase"

    return "needs_domain_context", "", f"effect_reward_phrase_needs_domain_review:{source_key}"


def classify(row: dict[str, Any], *, default_agent_key: str = "") -> tuple[str, str, str]:
    text = queue_text(row)
    source_key = str(row.get("source_key") or "")
    queue_bucket = str(row.get("queue_bucket") or "")
    agent_key = str(default_agent_key or row.get("agent_key") or "")

    if not text.strip():
        return "needs_domain_context", "", "missing_text_in_queue_export"

    if TOKEN_GLUE_RE.search(text):
        return (
            "needs_new_microagent",
            "",
            "dynamic_pronoun_token_glued_to_verb_requires_token_boundary_or_gender_microagent",
        )

    if KNIGHT_DUPLICATE_RE.search(text):
        return (
            "needs_repair",
            "Cada [knight|lE] arrogante",
            "literal_knight_duplicate_before_knight_token",
        )

    if INFINITIVE_OPTION_RE.search(text):
        return "needs_repair", "Não apostar.", "infinitive_option_surface_should_be_ptbr_command"

    hits = spanish_hits(text)
    if hits:
        corrected = ""
        if "les_hare_saber_como" in hits:
            corrected = "Vou mostrar a eles como #EMP eu#! me sinto!"
        return "needs_repair", corrected, "spanish_residual_visible_surface:" + ",".join(hits[:5])

    reason = validator_reason(text)
    if reason:
        return "needs_repair", "", "local_quality_validator:" + reason

    repair = router_repair_reason(row, text)
    if repair:
        corrected_text, reason = repair
        return "needs_repair", corrected_text, reason

    if agent_key == "micro_requirement_tooltip_surface":
        if ".tt" in source_key or "tooltip" in source_key or "requirement_or_tooltip_key" in queue_bucket:
            return "safe_short_label", "", "clean_requirement_or_tooltip_surface"
        return "safe_short_label", "", "clean_requirement_like_surface"

    if agent_key == "micro_event_dialogue_option":
        if key_surface(source_key) in {"title_key", "description_key", "flavor_key"}:
            return "needs_domain_context", "", "event_dialogue_queue_contains_contextual_key"
        return "safe_short_label", "", "clean_event_dialogue_or_option_surface"

    if agent_key == "micro_event_surface_router":
        if key_surface(source_key) == "debug_or_meta_key":
            return "needs_domain_context", "", "debug_or_meta_surface_requires_manual_scope"
        repair = router_repair_reason(row, text)
        if repair:
            corrected_text, reason = repair
            return "needs_repair", corrected_text, reason
        return "safe_short_label", "", "clean_event_router_surface"

    if agent_key == "micro_event_context_composer":
        return context_composer_decision(row, text)

    if agent_key == "micro_short_label_style":
        return short_label_style_decision(row, text)

    if agent_key == "micro_effect_reward_phrase":
        return effect_reward_phrase_decision(row, text)

    return "needs_domain_context", "", "unsupported_event_surface_agent_conservative"


def output_paths(settings: dict[str, Any], source_path: Path, reviewer: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reviewer = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in reviewer)
    base_name = source_path.stem.replace("_decisions_template", "")
    return (
        reports_dir / f"{stamp}_{base_name}_{safe_reviewer}_event_surface_reviewed.jsonl",
        reports_dir / f"{stamp}_{base_name}_{safe_reviewer}_event_surface_reviewed.txt",
    )


def main(
    *,
    queue_jsonl: str,
    reviewer: str = "codex_event_surface_review",
    agent_key: str = "",
) -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(queue_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = load_jsonl(source_path)
    decisions_path, report_path = output_paths(settings, source_path, reviewer)
    counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    samples: list[str] = []

    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            decision, corrected_text, reason = classify(row, default_agent_key=agent_key)
            counts[decision] += 1
            bucket = str(row.get("queue_bucket") or "unknown")
            bucket_counts[f"{decision}|{bucket}"] += 1
            payload = {
                "queue_run_id": row.get("queue_run_id"),
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "decision": decision,
                "corrected_text": corrected_text,
                "notes": f"{RULE_VERSION}; {reason}; source_key={row.get('source_key')}",
                "reviewer": reviewer,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if len(samples) < 120:
                samples.append(
                    f"- {decision} | item={row.get('queue_item_id')} segment={row.get('segment_id')} "
                    f"| {bucket} | {reason} | {short(queue_text(row), 140)}"
                )

    lines = [
        "Issue event surface assisted draft",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Source queue: {source_path}",
        f"Reviewer: {reviewer}",
        f"Agent key fallback: {agent_key or 'row/default'}",
        f"Rows: {len(rows):,}",
        f"Reviewed decisions: {decisions_path}",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Decision by bucket:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Samples:",
        *samples,
        "",
        "Safety note:",
        "- This draft creates learning evidence only.",
        "- It does not write source/output and does not create confirmations.",
        "- Safe decisions are scoped to event surface queues only.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_event_surface_assisted_draft] Draft generated")
    print(f"[issue_event_surface_assisted_draft] Rule version: {RULE_VERSION}")
    print(f"[issue_event_surface_assisted_draft] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_event_surface_assisted_draft] {key}: {value:,}")
    print(f"[issue_event_surface_assisted_draft] Decisions: {decisions_path}")
    print(f"[issue_event_surface_assisted_draft] Report: {report_path}")
    return {
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
        "rows": len(rows),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a conservative event-surface assisted decision draft.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default="codex_event_surface_review")
    parser.add_argument("--agent-key", default="")
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer, agent_key=args.agent_key)
