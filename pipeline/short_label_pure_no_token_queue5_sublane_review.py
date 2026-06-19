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


RULE_VERSION = "short_label_pure_no_token_queue5_sublane_review_v1"
DEFAULT_QUEUE_JSONL = Path("reports/20260618_140650_791781_issue_short_label_pure_no_token_review_queue.jsonl")
EXPECTED_QUEUE_RUN_ID = 5
EXPECTED_ROWS = 240

BAD_TEXT_RE = re.compile(r"ÃƒÆ’|Ãƒâ€š|ÃƒÂ¯Ã‚Â¿Ã‚Â½|Ã¯Â¿Â½|Ã‚Â¿|Ã‚Â¡")
QUESTION_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\?(?=[A-Za-zÀ-ÖØ-öø-ÿ])")

ENGLISH_REPAIRS = {
    32707: "Você deveria investigar isso, Mestre-espião.",
    33934: "Espero que um dia você possa me perdoar, por nós dois.",
    143231: "em uma colina perto da cidade de Hastings,",
    143232: "no alto dos próprios Penhascos Brancos de Dover,",
    143235: "fora das próprias muralhas de Londres,",
    143236: "perto da antiga capital saxã de Winchester,",
    143238: "perto da antiga cidade de Lincoln,",
}

SEMANTIC_REPAIRS = {
    101489: "Você se safa mentindo",
    159947: "Vocês dois ganham umas moedas",
}

DOMAIN_IDS = {
    22750,
    22751,
    22752,
    22776,
    26069,
    26071,
    26073,
    26074,
    26075,
    26076,
    26077,
    26078,
    26079,
    26080,
    16521,
    19997,
    20003,
    20011,
    20057,
    20099,
    20101,
    20103,
    20105,
    20109,
    20131,
    20139,
    20147,
    20149,
    20163,
    20169,
    20178,
    20180,
    20236,
    20268,
    20643,
    142870,
    142872,
}

CUSTOM_FRAGMENT_IDS = {
    21911,
    22420,
    22422,
    22423,
    22428,
    22508,
    23554,
    23562,
    23566,
    23570,
    23662,
    23664,
    23681,
    23687,
    25130,
    25201,
}

SAFE_NOMINAL_IDS = {
    101829,
    101901,
    101909,
    32976,
    4015,
    4410,
    159685,
    159712,
    159715,
    159727,
    159766,
    159904,
}

SAFE_MODIFIER_IDS = {
    98736,
    101691,
    101706,
    1471,
    3281,
    3474,
    4441,
    4443,
    4479,
    4644,
    4645,
    4667,
    33216,
    33223,
    33244,
    159662,
    159668,
    159708,
    159709,
    159717,
    159723,
    159900,
}

EVENT_OPTION_IDS = {
    99195,
    99479,
    99555,
    99658,
    101011,
    101494,
    31205,
    32707,
    34475,
    158959,
    159707,
    159710,
    159735,
    159737,
    159740,
    159745,
    159746,
    159850,
    159997,
    160001,
    3355,
    3938,
    3939,
    4013,
    4014,
    4390,
    4408,
    4523,
    4535,
    4711,
    4712,
    157197,
    157214,
    157221,
    157298,
    157308,
    157310,
    157388,
    157741,
    157742,
    157745,
    157928,
    157996,
    158008,
    158031,
    20959,
    20975,
    21034,
    21035,
    21071,
    21082,
    21083,
    21085,
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def load_rows(queue_jsonl: Path, queue_run_id: int) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in queue_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"Expected {EXPECTED_ROWS} rows, got {len(rows)}")
    bad_run = sorted({int(row.get("queue_run_id") or 0) for row in rows if int(row.get("queue_run_id") or 0) != queue_run_id})
    if bad_run:
        raise RuntimeError(f"Unexpected queue_run_id values: {bad_run}")
    return rows


def has_dialogue_surface(text: str) -> bool:
    stripped = text.strip()
    return (
        '"' in stripped
        or '\\"' in stripped
        or stripped.startswith("!")
        or "..." in stripped
        or stripped.endswith("!")
    )


def is_death_reason(row: dict[str, Any]) -> bool:
    return as_text(row.get("relative_path")).endswith("death_reasons_l_spanish.yml")


def is_trait_description(row: dict[str, Any]) -> bool:
    return as_text(row.get("relative_path")).endswith("traits_l_spanish.yml")


def classify(row: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    text = as_text(row["evidence_text"])
    corrected = ""
    notes = ""

    if segment_id in ENGLISH_REPAIRS:
        corrected = ENGLISH_REPAIRS[segment_id]
        decision = "english_residual_repair_needed"
        subpolicy = "english_residual_short_label_repair"
        reason = "visible_english_residual_with_safe_local_repair"
        requires_apply = True
        lifecycle = checkpoint = False
    elif segment_id in SEMANTIC_REPAIRS:
        corrected = SEMANTIC_REPAIRS[segment_id]
        decision = "semantic_error_needs_repair"
        subpolicy = "semantic_repair_required"
        reason = "local_ptbr_grammar_or_word_choice_error_with_safe_repair"
        requires_apply = True
        lifecycle = checkpoint = False
    elif segment_id in DOMAIN_IDS:
        decision = "domain_policy_needed"
        subpolicy = "domain_policy_required"
        reason = "domain_sensitive_culture_religion_name_or_game_rule_term"
        requires_apply = lifecycle = checkpoint = False
    elif segment_id in CUSTOM_FRAGMENT_IDS or as_text(row.get("package")) == "custom_localization":
        decision = "custom_localization_fragment_needs_context"
        subpolicy = "custom_localization_fragment_context_required"
        reason = "custom_localization_fragment_requires_external_grammar_context"
        requires_apply = lifecycle = checkpoint = False
    elif is_death_reason(row):
        decision = "safe_death_reason_fragment"
        subpolicy = "death_reason_fragment_guard"
        reason = "death_reason_fragment_is_natural_ptbr_without_tokens"
        requires_apply = False
        lifecycle = checkpoint = True
    elif is_trait_description(row):
        decision = "needs_new_microagent"
        subpolicy = "new_microagent_required"
        reason = "repeated_trait_description_pattern_needs_trait_description_microagent"
        requires_apply = lifecycle = checkpoint = False
        notes = "trait_description_esta_personagem_pattern"
    elif segment_id in SAFE_NOMINAL_IDS:
        decision = "safe_nominal_label"
        subpolicy = "pure_no_token_nominal_label_guard"
        reason = "short_nominal_label_without_tokens_or_context_dependency"
        requires_apply = False
        lifecycle = checkpoint = True
    elif segment_id in SAFE_MODIFIER_IDS:
        decision = "safe_modifier_description"
        subpolicy = "modifier_description_guard"
        reason = "short_modifier_or_result_description_is_natural_ptbr"
        requires_apply = False
        lifecycle = checkpoint = True
    elif has_dialogue_surface(text):
        decision = "event_quote_or_dialogue_needs_context"
        subpolicy = "event_quote_dialogue_context_required"
        reason = "quoted_or_exclamatory_event_voice_requires_context"
        requires_apply = lifecycle = checkpoint = False
    elif segment_id in EVENT_OPTION_IDS:
        decision = "event_option_ready"
        subpolicy = "event_option_short_phrase_guard"
        reason = "short_event_option_surface_is_natural_ptbr"
        requires_apply = False
        lifecycle = checkpoint = True
    elif int(row.get("word_count") or 0) >= 9 or text.endswith("."):
        decision = "long_sentence_needs_context"
        subpolicy = "long_sentence_context_required"
        reason = "sentence_or_clause_like_surface_requires_neighbor_context"
        requires_apply = lifecycle = checkpoint = False
    else:
        decision = "blocked_uncertain"
        subpolicy = "new_microagent_required"
        reason = "insufficient_pattern_confidence_for_safe_sublane"
        requires_apply = lifecycle = checkpoint = False

    if requires_apply and structural_tokens(text) != structural_tokens(corrected):
        decision = "blocked_uncertain"
        subpolicy = "new_microagent_required"
        reason = "repair_would_change_structural_tokens"
        corrected = ""
        requires_apply = lifecycle = checkpoint = False

    return {
        "segment_id": segment_id,
        "relative_path": as_text(row["relative_path"]),
        "source_key": as_text(row["source_key"]),
        "decision": decision,
        "subpolicy": subpolicy,
        "reason": reason,
        "current_text": text,
        "corrected_text": corrected,
        "requires_apply_later": requires_apply,
        "candidate_lifecycle_later": lifecycle,
        "candidate_checkpoint_later": checkpoint,
        "notes": notes,
    }


def validate(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        segment_id = row["segment_id"]
        corrected = as_text(row["corrected_text"])
        if row["requires_apply_later"] and not corrected:
            raise RuntimeError(f"Apply candidate missing corrected_text: {segment_id}")
        if corrected and BAD_TEXT_RE.search(corrected):
            raise RuntimeError(f"Bad marker in corrected_text: {segment_id}")
        if corrected and QUESTION_INSIDE_WORD_RE.search(corrected):
            raise RuntimeError(f"Question mark inside word in corrected_text: {segment_id}")
        if corrected and structural_tokens(row["current_text"]) != structural_tokens(corrected):
            raise RuntimeError(f"Structural tokens changed: {segment_id}")


def report_paths() -> tuple[Path, Path]:
    reports_dir = Path("reports")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_pure_no_token_queue5_sublane_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_outputs(rows: list[dict[str, Any]], *, queue_jsonl: Path, queue_run_id: int) -> tuple[Path, Path]:
    jsonl_path, txt_path = report_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    decision_counts = Counter(row["decision"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy"] for row in rows)
    lifecycle_rows = [
        row
        for row in rows
        if row["decision"]
        in {
            "safe_nominal_label",
            "safe_modifier_description",
            "event_option_ready",
            "safe_trait_or_modifier_label",
            "safe_death_reason_fragment",
        }
        and row["candidate_lifecycle_later"]
        and row["candidate_checkpoint_later"]
    ]
    apply_rows = [
        row
        for row in rows
        if row["decision"]
        in {"english_residual_repair_needed", "spanish_residual_repair_needed", "semantic_error_needs_repair"}
        and row["requires_apply_later"]
    ]
    new_microagent_rows = [row for row in rows if row["decision"] == "needs_new_microagent"]
    retained_rows = [row for row in rows if not row["requires_apply_later"] and not row["candidate_lifecycle_later"]]

    lines = [
        "Short Label Pure No-Token Queue 5 Sublane Review",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Queue JSONL: {queue_jsonl}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Total revisado: {len(rows)}",
        "",
        "Contagem por decisão:",
        *[f"- {key}: {value}" for key, value in decision_counts.most_common()],
        "",
        "Contagem por subpolítica:",
        *[f"- {key}: {value}" for key, value in subpolicy_counts.most_common()],
        "",
        f"Candidatos a lifecycle/checkpoint futuro: {len(lifecycle_rows)}",
        "IDs lifecycle/checkpoint: " + ", ".join(str(row["segment_id"]) for row in lifecycle_rows),
        "",
        f"Candidatos a apply futuro: {len(apply_rows)}",
        "IDs apply: " + ", ".join(str(row["segment_id"]) for row in apply_rows),
        "",
        f"Padrões que sugerem novo microagente: {len(new_microagent_rows)}",
        "",
        "Top 10 exemplos seguros:",
    ]
    for row in lifecycle_rows[:10]:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['current_text']}")
    lines.append("")
    lines.append("Top 10 exemplos retidos:")
    for row in retained_rows[:10]:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['reason']}")
    lines.append("")
    lines.append("Top 10 padrões que sugerem novo microagente:")
    for row in new_microagent_rows[:10]:
        lines.append(f"- {row['segment_id']} | {row['source_key']} | {row['notes'] or row['reason']}")
    lines.extend(
        [
            "",
            "Recomendação de próximo microagente/checkpoint:",
            "- Criar microagente/checkpoint de trait_description_esta_personagem para descrições de traços sem tokens.",
            "- Separar lifecycle seguro para death_reason_fragment_guard, modifier_description_guard e event_option_short_phrase_guard.",
            "- Tratar resíduos em inglês desta fila com apply protegido por allowlist e token-check.",
            "- Manter cultura/religião/nomes/game-rules em fila de domain_policy_required.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-run-id", type=int, default=EXPECTED_QUEUE_RUN_ID)
    parser.add_argument("--queue-jsonl", type=Path, default=DEFAULT_QUEUE_JSONL)
    args = parser.parse_args()
    try:
        source_rows = load_rows(args.queue_jsonl, args.queue_run_id)
        reviewed_rows = [classify(row) for row in source_rows]
        validate(reviewed_rows)
        jsonl_path, txt_path = write_outputs(reviewed_rows, queue_jsonl=args.queue_jsonl, queue_run_id=args.queue_run_id)
    except Exception as exc:
        print(f"[short_label_pure_no_token_queue5_sublane_review] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[short_label_pure_no_token_queue5_sublane_review] Completed")
    print(f"[short_label_pure_no_token_queue5_sublane_review] Queue run id: {args.queue_run_id}")
    print(f"[short_label_pure_no_token_queue5_sublane_review] Rows: {len(reviewed_rows)}")
    print(f"[short_label_pure_no_token_queue5_sublane_review] JSONL: {jsonl_path}")
    print(f"[short_label_pure_no_token_queue5_sublane_review] Report: {txt_path}")


if __name__ == "__main__":
    main()
