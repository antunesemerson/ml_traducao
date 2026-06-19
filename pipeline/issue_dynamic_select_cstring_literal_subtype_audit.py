from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_dynamic_select_cstring_literal_subtype_audit_v1"
DEFAULT_QUEUE_GLOB = "*_issue_dynamic_select_cstring_repair_queue.csv"

SECOND_TO_THIRD_PRETERITE_ENDINGS = (
    ("aste", ("ó", "Ã³", "ou", "o")),
    ("iste", ("ió", "iÃ³", "ció", "ciÃ³", "gió", "giÃ³", "jo", "zo", "iu", "eu", "u", "z")),
)

SPANISH_PTBR_HINTS = {
    "sufriste": "sofreu",
    "huiste": "fugiu",
    "exhibiste": "exibiu",
    "pasaste": "passou",
    "exploraste": "explorou",
    "conquistaste": "conquistou",
    "adoptaste": "adotou",
    "recordaste": "lembrou",
    "hiciste": "fez",
    "diste": "deu",
    "sacaste": "tirou",
    "cazaste": "caçou",
    "eclipsaste": "ofuscou",
    "contemplaste": "contemplou",
    "reclutaste": "recrutou",
    "liberaste": "libertou",
    "quedaste": "ficou",
    "restauraste": "restaurou",
    "esculpiste": "esculpiu",
    "dejaste": "deixou",
    "estudiaste": "estudou",
    "arrebataste": "arrebatou",
    "terminaste": "terminou",
    "participaste": "participou",
    "tuviste": "teve",
    "uniste": "uniu",
    "ganaste": "ganhou",
    "tiraste": "jogou",
    "mostraste": "mostrou",
    "obtuviste": "obteve",
}


def latest_queue_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(DEFAULT_QUEUE_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No dynamic Select_CString repair queue CSV found.")
    return candidates[0]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_select_cstring_literal_subtype_audit"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_microagent_queue").with_suffix(".jsonl"),
    )


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def normalize_literal(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def is_single_word(text: str) -> bool:
    return bool(re.fullmatch(r"[\wÀ-ÿ]+", text, flags=re.IGNORECASE))


def classify_subtype(left: str, right: str) -> tuple[str, str, str]:
    left_norm = normalize_literal(left)
    right_norm = normalize_literal(right)
    left_lower = left_norm.lower()
    right_lower = right_norm.lower()

    if left_lower in {"tu", "tú", "tus"} or right_lower in {"su", "sus"}:
        if left_lower in {"tus"} and right_lower == "sus":
            return "possessive_pronoun_plural", "safe_pattern_candidate", "Tus/tus -> Sus/sus local-player possessive shift"
        return "possessive_pronoun", "needs_more_evidence", "possessive pronoun shift"

    if left_lower == "te" and right_lower in {"le", "se"}:
        return "object_or_reflexive_pronoun", "needs_context", "te -> le/se depends on verb argument structure"

    if left_lower.startswith("te ") and right_lower.startswith("se "):
        return "reflexive_phrase", "microagent_candidate", "reflexive second-person phrase shifts to third-person phrase"

    if left_lower.startswith("me ") and right_lower.startswith("se "):
        return "first_person_to_third_person_phrase", "needs_context", "first-person local branch needs actor/context review"

    if re.search(r"\b(?:te|tu|tus|tú)\b", left_lower):
        return "mixed_second_person_phrase", "needs_context", "second-person marker appears inside a phrase"

    if re.search(r"\b(?:me|mi|mis|yo)\b", left_lower):
        return "mixed_first_person_phrase", "needs_context", "first-person marker appears inside a phrase"

    if "á" in left_lower or "ás" in left_lower or left_lower.endswith("rás"):
        return "future_or_conditional_person_shift", "needs_context", "future/conditional branch requires tense-specific PT-BR mapping"

    if is_single_word(left_lower) and is_single_word(right_lower):
        if left_lower in SPANISH_PTBR_HINTS:
            return "known_preterite_verb_shift", "microagent_candidate", f"known Spanish verb pair; PT-BR hint: {SPANISH_PTBR_HINTS[left_lower]}"
        for second_suffix, third_suffixes in SECOND_TO_THIRD_PRETERITE_ENDINGS:
            if left_lower.endswith(second_suffix) and any(right_lower.endswith(suffix) for suffix in third_suffixes):
                return "regular_preterite_verb_shift", "microagent_candidate", "single-word preterite second-person to third-person shift"
        return "single_word_other", "needs_more_evidence", "single-word literal pair not covered by current verb/pronoun heuristics"

    return "phrase_other", "needs_context", "phrase requires contextual rewrite or semantic review"


def suggested_microagent(subtype: str) -> str:
    mapping = {
        "known_preterite_verb_shift": "select_cstring_local_player_preterite_verb_rewrite",
        "regular_preterite_verb_shift": "select_cstring_local_player_preterite_verb_rewrite",
        "reflexive_phrase": "select_cstring_local_player_reflexive_phrase_rewrite",
        "possessive_pronoun_plural": "select_cstring_local_player_possessive_pronoun_rewrite",
        "possessive_pronoun": "select_cstring_local_player_possessive_pronoun_rewrite",
        "object_or_reflexive_pronoun": "select_cstring_local_player_pronoun_argument_review",
        "future_or_conditional_person_shift": "select_cstring_local_player_future_tense_review",
        "mixed_second_person_phrase": "select_cstring_local_player_phrase_context_review",
        "mixed_first_person_phrase": "select_cstring_local_player_phrase_context_review",
        "first_person_to_third_person_phrase": "select_cstring_local_player_phrase_context_review",
    }
    return mapping.get(subtype, "select_cstring_literal_payload_context_review")


def priority(row: dict[str, Any], subtype: str, maturity: str) -> float:
    score = float(row.get("priority_score") or 0)
    if maturity == "microagent_candidate":
        score += 300
    if subtype == "known_preterite_verb_shift":
        score += 150
    if subtype == "regular_preterite_verb_shift":
        score += 100
    if subtype == "reflexive_phrase":
        score += 80
    if subtype.startswith("possessive"):
        score += 60
    return round(score, 4)


def audit_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    audited: list[dict[str, Any]] = []
    for row in rows:
        left = normalize_literal(row.get("left_literal"))
        right = normalize_literal(row.get("right_literal"))
        subtype, maturity, reason = classify_subtype(left, right)
        payload = dict(row)
        payload.update(
            {
                "left_literal": left,
                "right_literal": right,
                "literal_subtype": subtype,
                "maturity": maturity,
                "audit_reason": reason,
                "suggested_microagent": suggested_microagent(subtype),
                "learning_only": 1,
                "apply_allowed": 0,
            }
        )
        payload["microagent_priority"] = priority(payload, subtype, maturity)
        audited.append(payload)
    return sorted(
        audited,
        key=lambda row: (
            str(row["maturity"]) != "microagent_candidate",
            -float(row["microagent_priority"]),
            row["suggested_microagent"],
            row["relative_path"],
            row["source_key"],
        ),
    )


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    microagent_queue_path: Path,
    queue_csv: Path,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "condition_family",
        "condition",
        "left_literal",
        "right_literal",
        "literal_subtype",
        "maturity",
        "suggested_microagent",
        "microagent_priority",
        "audit_reason",
        "apply_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    with microagent_queue_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            if row["maturity"] not in {"microagent_candidate", "safe_pattern_candidate"}:
                continue
            payload = {
                "queue_item_id": row["queue_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "condition": row["condition"],
                "left_literal": row["left_literal"],
                "right_literal": row["right_literal"],
                "literal_subtype": row["literal_subtype"],
                "suggested_microagent": row["suggested_microagent"],
                "decision": "",
                "decision_options": [
                    "microagent_positive_evidence",
                    "needs_context",
                    "manual_exception",
                    "false_positive",
                ],
                "ptbr_left_hint": SPANISH_PTBR_HINTS.get(str(row["left_literal"]).lower(), ""),
                "ptbr_right_hint": "",
                "notes": row["audit_reason"],
                "learning_only": 1,
                "apply_allowed": 0,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    subtype_counts = Counter(row["literal_subtype"] for row in rows)
    maturity_counts = Counter(row["maturity"] for row in rows)
    microagent_counts = Counter(row["suggested_microagent"] for row in rows)
    condition_counts = Counter(row["condition_family"] for row in rows)
    pair_counts = Counter((row["left_literal"].lower(), row["right_literal"].lower()) for row in rows)
    subtype_by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        subtype_by_agent[row["suggested_microagent"]][row["literal_subtype"]] += 1

    microagent_queue_count = sum(1 for row in rows if row["maturity"] in {"microagent_candidate", "safe_pattern_candidate"})
    lines = [
        "Issue dynamic Select_CString literal subtype audit",
        f"Rule version: {RULE_VERSION}",
        f"Source queue CSV: {queue_csv}",
        "",
        "Summary:",
        f"- Rows audited: {len(rows):,}",
        f"- Microagent queue candidates: {microagent_queue_count:,}",
        "- Apply allowed: 0",
        "",
        "Maturity:",
        *[f"- {key}: {value:,}" for key, value in maturity_counts.most_common()],
        "",
        "Literal subtypes:",
        *[f"- {key}: {value:,}" for key, value in subtype_counts.most_common()],
        "",
        "Suggested microagents:",
        *[f"- {key}: {value:,}" for key, value in microagent_counts.most_common()],
        "",
        "Condition families:",
        *[f"- {key}: {value:,}" for key, value in condition_counts.most_common()],
        "",
        "Top literal pairs:",
        *[f"- {left!r} -> {right!r}: {count:,}" for (left, right), count in pair_counts.most_common(30)],
        "",
        "Subtype by suggested microagent:",
    ]
    for agent, counts in sorted(subtype_by_agent.items()):
        lines.append(f"- {agent}: " + ", ".join(f"{key}={value}" for key, value in counts.most_common()))
    lines.extend(["", "Samples:"])
    for row in rows[:50]:
        lines.append(
            f"- {row['maturity']} | {row['literal_subtype']} | {row['suggested_microagent']} | "
            f"{row['relative_path']}::{row['source_key']} | {row['left_literal']!r} -> {row['right_literal']!r}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_csv: str | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    selected_queue_csv = Path(queue_csv) if queue_csv else latest_queue_csv(settings)
    rows = load_rows(selected_queue_csv)
    audited = audit_rows(rows)
    txt_path, csv_path, jsonl_path, microagent_queue_path = report_paths(settings)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        microagent_queue_path=microagent_queue_path,
        queue_csv=selected_queue_csv,
        rows=audited,
    )
    print("[issue_dynamic_select_cstring_literal_subtype_audit] Audit generated")
    print(f"[issue_dynamic_select_cstring_literal_subtype_audit] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_select_cstring_literal_subtype_audit] Rows: {len(audited):,}")
    print(f"[issue_dynamic_select_cstring_literal_subtype_audit] Report: {txt_path}")
    print(f"[issue_dynamic_select_cstring_literal_subtype_audit] CSV: {csv_path}")
    print(f"[issue_dynamic_select_cstring_literal_subtype_audit] JSONL: {jsonl_path}")
    print(f"[issue_dynamic_select_cstring_literal_subtype_audit] Microagent queue: {microagent_queue_path}")
    return {
        "rows": len(audited),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "microagent_queue_path": str(microagent_queue_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit dynamic Select_CString literal repair queue into reusable microagent subtypes.")
    parser.add_argument("--queue-csv", default=None)
    args = parser.parse_args()
    main(queue_csv=args.queue_csv)
