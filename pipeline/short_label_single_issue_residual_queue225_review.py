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


RULE_VERSION = "short_label_single_issue_residual_queue225_review_v1"
DEFAULT_QUEUE_JSONL = Path("reports/20260618_123445_851312_issue_short_label_single_issue_residual_review_queue.jsonl")
BAD_TEXT_RE = re.compile(r"Ãƒ|Ã‚|Ã¯Â¿Â½|ï¿½|Â¿|Â¡")
QUESTION_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\?(?=[A-Za-zÀ-ÖØ-öø-ÿ])")

BOLD_NO_REPAIRS = {
    284330: ("[CHARACTER.GetShortUIName|U] #bold não#! tem o nome régio de $NAME$", "lowercase_bold_no_visible_spanish_residual"),
    283461: ("[CHARACTER.GetShortUIName|V] #bold não#! possui [treasury|lE]", "lowercase_bold_no_visible_spanish_residual"),
    283532: ("#bold Não#! você não tem o [modifier|El]; $MODIFIER|V$", "uppercase_bold_no_visible_spanish_residual"),
    284058: ("#bold Não#! é [spouse|lE] de [liege.GetShortUIName]", "uppercase_bold_no_visible_spanish_residual"),
    284332: ("#bold Não#! você não tem o nome régio de $NAME$", "uppercase_bold_no_visible_spanish_residual"),
    283689: ("#bold Não#! não se permite ter mais cônjuges", "uppercase_bold_no_visible_spanish_residual"),
    283459: ("#bold Não#! você não tem [treasury|lE]", "uppercase_bold_no_visible_spanish_residual"),
}

SPANISH_REPAIRS = {
    2303: ("#N #bold Redução importante#!#! da sua #V probabilidade de sucesso#!", "mechanical_spanish_terms_in_short_modifier"),
    2306: ("#P #bold Aumento importante#!#! da sua #V probabilidade de sucesso#!", "mechanical_spanish_terms_in_short_modifier"),
    144513: ("A [disaster|lE] chegou à fase de #bold Recuperação#!", "single_spanish_noun_in_short_label"),
    2140: ('"Olhos indiscretos devem ser — #EMP serão cegados!#!"', "obvious_dialogue_spanish_verb_repair"),
    79796: ('"Bem, deveríamos nomear vocês #EMP imediatamente...#!"', "obvious_dialogue_spanish_adverb_repair"),
    69453: ('"Fora da minha vista, #emp imediatamente!#!"', "obvious_dialogue_spanish_adverb_repair"),
    78556: ('"A #EMP mim#! me agradariam tais bênçãos".', "obvious_dialogue_spanish_pronoun_repair"),
    43207: ('"Como #EMP ousais#!"', "obvious_dialogue_spanish_phrase_repair"),
}

SEMANTIC_REPAIRS = {
    245660: (
        "#BOLD Resultado:#! Ganha o [single_combat_fight|lE] contra [sc_loser.GetFirstNameNoTooltip] por sua pontuação de [single_combat_injury_chance_injury_risk|lE]",
        "markup_and_spanish_verb_need_guarded_repair",
    ),
}

SAFE_NOOP_IDS = {
    4214,
    4215,
    4216,
    4428,
    122499,
    156162,
    156163,
    156285,
    26769376,
    46216,
    70395,
}

QUOTE_PUNCTUATION_IDS = {
    99699,
    99698,
    99700,
    157570,
    115641,
    77598,
    31141,
    44648,
    44647,
    61003,
}

DOMAIN_CONTEXT_IDS = {68467, 73932}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def report_paths() -> tuple[Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_single_issue_residual_queue225_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def has_bad_text(value: str) -> bool:
    return bool(BAD_TEXT_RE.search(value))


def question_inside_word(value: str) -> bool:
    return bool(QUESTION_INSIDE_WORD_RE.search(value))


def make_decision(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    text = as_text(row.get("evidence_text") or row.get("confirmed_text"))
    decision = "blocked_uncertain"
    subpolicy = "domain_context_required"
    reason = "conservative_default_requires_manual_context"
    corrected = ""
    requires_apply = False
    lifecycle = False
    notes = f"{RULE_VERSION}; suggested_decision={as_text(row.get('suggested_decision'))}"

    if segment_id in BOLD_NO_REPAIRS:
        corrected, reason = BOLD_NO_REPAIRS[segment_id]
        decision = "safe_markup_no_repair"
        subpolicy = "bold_no_ptbr_microrepair"
        requires_apply = True
    elif segment_id in SPANISH_REPAIRS:
        corrected, reason = SPANISH_REPAIRS[segment_id]
        decision = "safe_spanish_residual_repair"
        subpolicy = "dialogue_spanish_residual_microrepair"
        requires_apply = True
    elif segment_id in SEMANTIC_REPAIRS:
        corrected, reason = SEMANTIC_REPAIRS[segment_id]
        decision = "semantic_repair_needed"
        subpolicy = "semantic_short_label_repair"
        requires_apply = True
    elif segment_id in SAFE_NOOP_IDS:
        decision = "safe_markup_no_repair"
        subpolicy = "bold_no_ptbr_microrepair"
        reason = "reviewed_markup_or_mojibake_bucket_is_already_ptbr_safe"
        lifecycle = True
    elif segment_id in QUOTE_PUNCTUATION_IDS:
        decision = "quote_punctuation_context_required"
        subpolicy = "quote_punctuation_context_required"
        reason = "quote_boundary_or_truncated_dialogue_requires_neighbor_context"
    elif segment_id in DOMAIN_CONTEXT_IDS:
        decision = "needs_domain_context"
        subpolicy = "domain_context_required"
        reason = "domain_or_title_term_inside_quoted_surface_requires_policy"
    elif row.get("queue_bucket") == "dialogue_or_quote_surface":
        decision = "dialogue_context_required"
        subpolicy = "dialogue_context_required"
        reason = "dialogue_or_character_voice_requires_context"
    elif row.get("queue_bucket") == "mojibake_visible":
        decision = "safe_markup_no_repair"
        subpolicy = "mojibake_short_label_microrepair"
        reason = "no_real_mojibake_in_utf8_jsonl_console_rendering_only"
        lifecycle = True

    if requires_apply and structural_tokens(text) != structural_tokens(corrected):
        decision = "blocked_uncertain"
        subpolicy = "domain_context_required"
        reason = "candidate_repair_changes_ck3_structural_tokens"
        corrected = ""
        requires_apply = False
        lifecycle = False

    return {
        "segment_id": segment_id,
        "relative_path": as_text(row.get("relative_path")),
        "source_key": as_text(row.get("source_key")),
        "queue_bucket": as_text(row.get("queue_bucket")),
        "decision": decision,
        "subpolicy": subpolicy,
        "reason": reason,
        "current_text": text,
        "corrected_text": corrected,
        "requires_apply_later": requires_apply,
        "candidate_lifecycle_later": lifecycle,
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


def write_outputs(decisions: list[dict[str, Any]], *, queue_run_id: int, queue_jsonl: Path) -> tuple[Path, Path]:
    jsonl_path, txt_path = report_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    decision_counts = Counter(row["decision"] for row in decisions)
    subpolicy_counts = Counter(row["subpolicy"] for row in decisions)
    apply_rows = [row for row in decisions if row["requires_apply_later"]]
    lifecycle_rows = [row for row in decisions if row["candidate_lifecycle_later"]]
    retained_rows = [row for row in decisions if not row["requires_apply_later"] and not row["candidate_lifecycle_later"]]

    lines = [
        "Short Label Single-Issue Residual Queue 225 Review",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Queue JSONL: {queue_jsonl}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total revisado: {len(decisions)}",
        "",
        "Contagem por decisão:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in decision_counts.most_common())
    lines.append("")
    lines.append("Contagem por subpolítica:")
    lines.extend(f"- {key}: {value}" for key, value in subpolicy_counts.most_common())
    lines.extend(
        [
            "",
            f"Candidatos a apply futuro: {len(apply_rows)}",
            f"IDs aplicáveis: {', '.join(str(row['segment_id']) for row in apply_rows) or 'none'}",
            "",
            f"Candidatos a lifecycle futuro: {len(lifecycle_rows)}",
            f"IDs lifecycle/no-op: {', '.join(str(row['segment_id']) for row in lifecycle_rows) or 'none'}",
            "",
            f"Retidos: {len(retained_rows)}",
            f"IDs retidos: {', '.join(str(row['segment_id']) for row in retained_rows) or 'none'}",
            "",
            "Top 10 reparos seguros:",
        ]
    )
    for row in apply_rows[:10]:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['corrected_text']}")
    lines.append("")
    lines.append("Top 10 retidos com motivo:")
    for row in retained_rows[:10]:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['reason']}")
    lines.extend(
        [
            "",
            "Recomendação de próximo passo:",
            "- Separar bold_no_ptbr_microrepair em apply protegido por allowlist.",
            "- Manter diálogos/aspas em compositor contextual, exceto os poucos resíduos espanhóis locais já marcados como aplicáveis.",
            "- Tratar semantic_short_label_repair em lote separado por ser reparo não puramente literal.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def run(*, queue_run_id: int, queue_jsonl: Path) -> tuple[list[dict[str, Any]], tuple[Path, Path]]:
    rows = [json.loads(line) for line in queue_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [row for row in rows if int(row.get("segment_id") or 0)]
    if queue_run_id != 225:
        raise RuntimeError(f"Unexpected queue run id: {queue_run_id}")
    if len(selected) != 64:
        raise RuntimeError(f"Expected 64 rows, got {len(selected)}")
    decisions = [make_decision(row) for row in selected]
    errors = validate(decisions)
    if errors:
        raise RuntimeError("; ".join(errors))
    return decisions, write_outputs(decisions, queue_run_id=queue_run_id, queue_jsonl=queue_jsonl)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-run-id", type=int, default=225)
    parser.add_argument("--queue-jsonl", type=Path, default=DEFAULT_QUEUE_JSONL)
    args = parser.parse_args()
    try:
        decisions, paths = run(queue_run_id=args.queue_run_id, queue_jsonl=args.queue_jsonl)
    except Exception as exc:
        print(f"[short_label_single_issue_residual_queue225_review] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    decision_counts = Counter(row["decision"] for row in decisions)
    subpolicy_counts = Counter(row["subpolicy"] for row in decisions)
    print("[short_label_single_issue_residual_queue225_review] Completed")
    print(f"[short_label_single_issue_residual_queue225_review] Rows: {len(decisions)}")
    for key, value in decision_counts.most_common():
        print(f"[short_label_single_issue_residual_queue225_review] decision {key}: {value}")
    for key, value in subpolicy_counts.most_common():
        print(f"[short_label_single_issue_residual_queue225_review] subpolicy {key}: {value}")
    print(f"[short_label_single_issue_residual_queue225_review] JSONL: {paths[0]}")
    print(f"[short_label_single_issue_residual_queue225_review] Report: {paths[1]}")


if __name__ == "__main__":
    main()
