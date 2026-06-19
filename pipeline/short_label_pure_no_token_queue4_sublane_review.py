from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from apply_segment_state_updates import structural_tokens


RULE_VERSION = "short_label_pure_no_token_queue4_sublane_review_v1"
DEFAULT_QUEUE_JSONL = Path("reports/20260618_114327_273442_issue_short_label_pure_no_token_review_queue.jsonl")
DEFAULT_ASSISTED_JSONL = Path("reports/20260618_114335_819974_issue_short_label_pure_no_token_assisted_review.jsonl")
BAD_TEXT_RE = re.compile(r"Ãƒ|Ã‚|Ã¯Â¿Â½|ï¿½|Â¿|Â¡")
QUESTION_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\?(?=[A-Za-zÀ-ÖØ-öø-ÿ])")

ENGLISH_CORRECTIONS = {
    142696: "Personagens e condados têm fés históricas.",
    143364: "Chance de Frankokratia",
    172777: "Viagens e Torneios",
    172782: "Pupilos e Guardiões",
}

SEMANTIC_CORRECTIONS = {
    44323: "Um último blefe pode decidir isso...",
    45187: "Você precisa entender. Você é preciosíssimo demais para correr riscos.",
    53543: "O desaparecimento passa despercebido",
    63863: "Você não pode pegar espólio normal como todo mundo?",
}

SPANISH_CORRECTIONS = {
    22893: "pirata",
    22899: "pirata",
}

EVENT_OPTION_READY_IDS = {
    12704,
    27747,
    27749,
    27751,
    27157,
    27158,
    27160,
    27161,
    162632,
    41690,
    47071,
    47163,
    47164,
    47371,
    51975,
    53656,
}

EVENT_CONTEXT_IDS = {
    32707,
    32976,
    27748,
    27750,
    40147,
    43673,
    45187,
    47167,
    47168,
    63863,
}

DOMAIN_KEY_RE = re.compile(r"(sword_name|artifact|title|nickname|faith|religion|culture|camp_purpose)", re.I)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def report_paths() -> tuple[Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_pure_no_token_queue4_sublane_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def has_bad_text(value: str) -> bool:
    return bool(BAD_TEXT_RE.search(value))


def question_inside_word(value: str) -> bool:
    return bool(QUESTION_INSIDE_WORD_RE.search(value))


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ']+", text)


def is_modifier_description(row: dict[str, Any], text: str) -> bool:
    key = as_text(row.get("source_key")).lower()
    if "modifier_desc" not in key:
        return False
    if '"' in text or "!" in text or "?" in text:
        return False
    return len(words(text)) <= 14


def is_nominal_label(row: dict[str, Any], text: str) -> bool:
    key = as_text(row.get("source_key")).lower()
    if any(mark in text for mark in ('"', ".", "!", "?", ":", ";", ",")):
        return False
    if len(words(text)) > 5:
        return False
    if DOMAIN_KEY_RE.search(key):
        return False
    if key.endswith("_name") or "_name_" in key:
        return False
    return len(words(text)) >= 1


def make_decision(row: dict[str, Any], assisted: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    text = as_text(row.get("evidence_text") or assisted.get("evidence_text"))
    source_key = as_text(row.get("source_key"))
    reason = as_text(assisted.get("reason"))
    decision = "blocked_uncertain"
    subpolicy = "domain_policy_required"
    out_reason = "conservative_default_requires_human_review"
    corrected = ""
    requires_apply = False
    lifecycle_candidate = False
    checkpoint_candidate = False
    notes = f"{RULE_VERSION}; assisted_reason={reason}"

    if segment_id in ENGLISH_CORRECTIONS:
        decision = "english_residual_repair_needed"
        subpolicy = "english_residual_short_label_repair"
        out_reason = "visible_english_residual_with_obvious_ptbr_repair"
        corrected = ENGLISH_CORRECTIONS[segment_id]
        requires_apply = True
    elif segment_id in SPANISH_CORRECTIONS:
        decision = "spanish_residual_repair_needed"
        subpolicy = "spanish_residual_short_label_repair"
        out_reason = "visible_spanish_residual_with_obvious_ptbr_repair"
        corrected = SPANISH_CORRECTIONS[segment_id]
        requires_apply = True
    elif segment_id in SEMANTIC_CORRECTIONS:
        decision = "semantic_error_needs_repair"
        subpolicy = "semantic_repair_required"
        out_reason = "visible_ptbr_semantic_or_grammar_error_with_obvious_repair"
        corrected = SEMANTIC_CORRECTIONS[segment_id]
        requires_apply = True
    elif reason == "quoted_dialogue_or_fragment_requires_context":
        decision = "event_quote_or_dialogue_needs_context"
        subpolicy = "event_quote_dialogue_context_required"
        out_reason = "quoted_dialogue_or_voice_dependent_fragment"
    elif segment_id in EVENT_CONTEXT_IDS:
        decision = "event_quote_or_dialogue_needs_context"
        subpolicy = "event_quote_dialogue_context_required"
        out_reason = "event_option_or_dialogue_surface_requires_voice_context"
    elif segment_id in EVENT_OPTION_READY_IDS:
        decision = "event_option_ready"
        subpolicy = "event_option_short_phrase_guard"
        out_reason = "short_event_option_or_tooltip_phrase_without_token_or_quote_dependency"
        lifecycle_candidate = True
        checkpoint_candidate = True
    elif reason == "custom_localization_fragment_requires_context":
        if DOMAIN_KEY_RE.search(source_key):
            decision = "domain_policy_needed"
            subpolicy = "domain_policy_required"
            out_reason = "custom_localization_domain_name_or_term_requires_policy"
        else:
            decision = "custom_localization_fragment_needs_context"
            subpolicy = "custom_localization_fragment_context_required"
            out_reason = "custom_localization_fragment_requires_external_grammar"
    elif reason == "long_or_clause_like_no_token_text":
        decision = "long_sentence_needs_context"
        subpolicy = "long_sentence_context_required"
        out_reason = "long_clause_or_death_reason_requires_context"
    elif is_modifier_description(row, text):
        decision = "safe_modifier_description"
        subpolicy = "modifier_description_guard"
        out_reason = "short_modifier_description_without_token_or_external_context_dependency"
        lifecycle_candidate = True
        checkpoint_candidate = True
    elif is_nominal_label(row, text):
        decision = "safe_nominal_label"
        subpolicy = "pure_no_token_nominal_label_guard"
        out_reason = "short_noun_phrase_without_tokens_or_context_dependency"
        lifecycle_candidate = True
        checkpoint_candidate = True
    elif reason == "sentence_or_dialogue_surface_requires_context":
        decision = "long_sentence_needs_context"
        subpolicy = "long_sentence_context_required"
        out_reason = "sentence_surface_requires_context_before_lifecycle"

    if requires_apply and structural_tokens(text) != structural_tokens(corrected):
        decision = "blocked_uncertain"
        subpolicy = "domain_policy_required"
        out_reason = "correction_would_change_ck3_structural_tokens"
        corrected = ""
        requires_apply = False
        lifecycle_candidate = False
        checkpoint_candidate = False

    return {
        "segment_id": segment_id,
        "relative_path": as_text(row.get("relative_path")),
        "source_key": source_key,
        "decision": decision,
        "subpolicy": subpolicy,
        "reason": out_reason,
        "current_text": text,
        "corrected_text": corrected,
        "requires_apply_later": requires_apply,
        "candidate_lifecycle_later": lifecycle_candidate,
        "candidate_checkpoint_later": checkpoint_candidate,
        "notes": notes,
    }


def validate(decisions: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for row in decisions:
        corrected = as_text(row.get("corrected_text"))
        if row.get("requires_apply_later") and not corrected:
            errors.append(f"missing_corrected_text:{row['segment_id']}")
        if corrected and has_bad_text(corrected):
            errors.append(f"bad_text_marker:{row['segment_id']}")
        if corrected and question_inside_word(corrected):
            errors.append(f"question_inside_word:{row['segment_id']}")
        if corrected and structural_tokens(row["current_text"]) != structural_tokens(corrected):
            errors.append(f"structural_tokens_changed:{row['segment_id']}")
    return errors


def write_outputs(decisions: list[dict[str, Any]], *, queue_run_id: int, queue_jsonl: Path, assisted_jsonl: Path) -> tuple[Path, Path]:
    decisions_path, report_path = report_paths()
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    decision_counts = Counter(row["decision"] for row in decisions)
    subpolicy_counts = Counter(row["subpolicy"] for row in decisions)
    lifecycle_count = sum(1 for row in decisions if row["candidate_lifecycle_later"])
    checkpoint_count = sum(1 for row in decisions if row["candidate_checkpoint_later"])
    apply_count = sum(1 for row in decisions if row["requires_apply_later"])
    safe_rows = [row for row in decisions if row["candidate_checkpoint_later"]][:10]
    retained_rows = [row for row in decisions if not row["candidate_checkpoint_later"] and not row["requires_apply_later"]][:10]

    lines = [
        "Short Label Pure No-Token Queue 4 Sublane Review",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Queue JSONL: {queue_jsonl}",
        f"Assisted JSONL: {assisted_jsonl}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total revisado: {len(decisions)}",
        "",
        "Contagem por decision:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in decision_counts.most_common())
    lines.append("")
    lines.append("Contagem por subpolicy:")
    lines.extend(f"- {key}: {value}" for key, value in subpolicy_counts.most_common())
    lines.extend(
        [
            "",
            "Candidatos a lifecycle/checkpoint futuros:",
            f"- lifecycle: {lifecycle_count}",
            f"- checkpoint: {checkpoint_count}",
            "",
            "Candidatos a apply futuro:",
            f"- total: {apply_count}",
        ]
    )
    for key in ("english_residual_repair_needed", "spanish_residual_repair_needed", "semantic_error_needs_repair"):
        lines.append(f"- {key}: {decision_counts.get(key, 0)}")

    lines.append("")
    lines.append("Top 10 exemplos seguros:")
    for row in safe_rows:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['current_text']}")

    lines.append("")
    lines.append("Top 10 exemplos retidos:")
    for row in retained_rows:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['reason']} | {row['current_text']}")

    lines.extend(
        [
            "",
            "Recomendação de próximo microagente/checkpoint:",
            "- Priorizar modifier_description_guard como checkpoint read-only/lifecycle, pois concentra descrições curtas de modificador sem tokens e com baixo acoplamento contextual.",
            "- Em paralelo, separar english_residual_short_label_repair e semantic_repair_required em apply futuro protegido por allowlist, sem misturar com lifecycle.",
            "- Manter diálogos, custom localization e domínios sensíveis em filas de contexto/composição.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return decisions_path, report_path


def run(*, queue_run_id: int, queue_jsonl: Path, assisted_jsonl: Path) -> tuple[list[dict[str, Any]], tuple[Path, Path]]:
    queue_rows = [row for row in load_jsonl(queue_jsonl) if int(row.get("queue_run_id") or 0) == queue_run_id]
    assisted_rows = [row for row in load_jsonl(assisted_jsonl) if int(row.get("queue_run_id") or 0) == queue_run_id]
    assisted_by_id = {int(row["segment_id"]): row for row in assisted_rows}
    if len(queue_rows) != 240:
        raise RuntimeError(f"Expected 240 queue rows for run {queue_run_id}, got {len(queue_rows)}")
    if set(assisted_by_id) != {int(row["segment_id"]) for row in queue_rows}:
        raise RuntimeError("Queue and assisted review segment ids do not match exactly")
    decisions = [make_decision(row, assisted_by_id[int(row["segment_id"])]) for row in queue_rows]
    errors = validate(decisions)
    if errors:
        raise RuntimeError("; ".join(errors))
    return decisions, write_outputs(decisions, queue_run_id=queue_run_id, queue_jsonl=queue_jsonl, assisted_jsonl=assisted_jsonl)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-run-id", type=int, default=4)
    parser.add_argument("--queue-jsonl", type=Path, default=DEFAULT_QUEUE_JSONL)
    parser.add_argument("--assisted-jsonl", type=Path, default=DEFAULT_ASSISTED_JSONL)
    args = parser.parse_args()
    try:
        decisions, paths = run(queue_run_id=args.queue_run_id, queue_jsonl=args.queue_jsonl, assisted_jsonl=args.assisted_jsonl)
    except Exception as exc:
        print(f"[short_label_pure_no_token_queue4_sublane_review] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    decision_counts = Counter(row["decision"] for row in decisions)
    subpolicy_counts = Counter(row["subpolicy"] for row in decisions)
    print("[short_label_pure_no_token_queue4_sublane_review] Completed")
    print(f"[short_label_pure_no_token_queue4_sublane_review] Rows: {len(decisions)}")
    for key, value in decision_counts.most_common():
        print(f"[short_label_pure_no_token_queue4_sublane_review] decision {key}: {value}")
    for key, value in subpolicy_counts.most_common():
        print(f"[short_label_pure_no_token_queue4_sublane_review] subpolicy {key}: {value}")
    print(f"[short_label_pure_no_token_queue4_sublane_review] JSONL: {paths[0]}")
    print(f"[short_label_pure_no_token_queue4_sublane_review] Report: {paths[1]}")


if __name__ == "__main__":
    main()
