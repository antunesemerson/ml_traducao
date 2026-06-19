from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RULE_VERSION = "spanish_residual_checkpoint9_safety_split_v1"
EXPECTED_ALLOWED = 50
BAD_MARKERS = ("Ãƒ", "Ã‚", "Ã¯Â¿Â½", "ï¿½", "Â¿", "Â¡")
QUESTION_IN_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]\?[A-Za-zÀ-ÿ]")

EXPLICIT_HOLDS: dict[int, tuple[str, str]] = {
    2318: ("needs_followup_residual_still_present", "known_remaining_sigues_sigue"),
    50570: ("needs_followup_residual_still_present", "known_remaining_proclamaste_proclamo"),
    156661: ("needs_followup_residual_still_present", "known_remaining_gobernadora_gobernador_inside_concept"),
    109269: ("needs_followup_residual_still_present", "known_remaining_conservaste_conservo"),
    229989: ("needs_followup_residual_still_present", "known_te_le_construction_suspicious"),
    229516: ("needs_context_composer", "known_concordance_fluency_issue_seus_opinioes"),
}

DOMAIN_POLICY_PATH_HINTS = (
    "nicknames_l_spanish.yml",
    "religion/",
    "ledger/ledger_filtersorts",
    "interactions_l_spanish.yml",
    "triggers/",
)
CONTEXT_PATH_HINTS = (
    "event_localization/",
    "activities/",
    "bookmark/",
    "dlc/ep3/",
    "dlc/bp2/",
)
SAFE_REASON_HINTS = (
    "año",
    "años",
    "ańo",
    "ańos",
    "lealtad",
    "vasallaje",
    "ganarás",
    "ganaste",
    "ganó",
    "ganará",
    "intentarás",
    "intentará",
    "apoyaste",
    "apoyó",
    "te opones",
    "se opone",
    "te opusiste",
    "se opuso",
    "eres",
    "es",
)

RESIDUAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("proclamaste", re.compile(r"\bproclamaste\b", re.I)),
    ("proclamo", re.compile(r"\bproclamó\b|\bproclam[oó]\b", re.I)),
    ("sigues", re.compile(r"\bsigues\b", re.I)),
    ("sigue", re.compile(r"\bsigue\b", re.I)),
    ("gobernador", re.compile(r"\bgobernador(?:a)?\b", re.I)),
    ("conservaste", re.compile(r"\bconservaste\b", re.I)),
    ("conservo", re.compile(r"\bconservó\b|\bconserv[oó]\b", re.I)),
    ("ganaste", re.compile(r"\bganaste\b", re.I)),
    ("gano", re.compile(r"\bganó\b|\bgan[oó]\b", re.I)),
    ("ganaras", re.compile(r"\bganarás\b|\bganará\b", re.I)),
    ("oposicion_es", re.compile(r"\bte opusiste\b|\bse opuso\b|\bte opones\b|\bse opone\b", re.I)),
    ("apoyo_es", re.compile(r"\bapoyaste\b|\bapoyó\b", re.I)),
    ("intentar_es", re.compile(r"\bintentarás\b|\bintentará\b", re.I)),
    ("prefiere_es", re.compile(r"\bprefieres\b|\bprefiere\b", re.I)),
    ("eres_es", re.compile(r"\beres\b", re.I)),
    ("lealtad", re.compile(r"\blealtad\b", re.I)),
    ("vasallaje", re.compile(r"\bvasallaje\b", re.I)),
    ("safe_phrase_es", re.compile(r"\bsana y salva\b|\bsano y salvo\b", re.I)),
    ("anos_es", re.compile(r"\baños?\b|\bańos?\b", re.I)),
    ("senhor_es", re.compile(r"\bseñor(?:a|ío)?\b|\bseńor(?:a|ío)?\b", re.I)),
    ("mi_senorio", re.compile(r"\bmi señorío\b|\bmi seńorío\b", re.I)),
    ("articles_es", re.compile(r"\bla\b|\bel\b|\bun\b|\buna\b", re.I)),
    ("le_pronoun", re.compile(r"\ble\b", re.I)),
]


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def report_paths() -> tuple[Path, Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_spanish_residual_checkpoint9_safety_split"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_suffix(".csv")


def load_rows(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Input is not valid UTF-8: {path}: {exc}") from exc
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def has_bad_marker(text: str) -> bool:
    return any(marker in text for marker in BAD_MARKERS) or bool(QUESTION_IN_WORD_RE.search(text))


def reasons_text(row: dict[str, Any]) -> str:
    try:
        data = json.loads(as_text(row.get("reasons_json")) or "[]")
    except json.JSONDecodeError:
        return as_text(row.get("reasons_json"))
    return " ".join(as_text(item) for item in data)


def residual_hits(text: str) -> list[str]:
    hits: list[str] = []
    for name, pattern in RESIDUAL_PATTERNS:
        if pattern.search(text):
            hits.append(name)
    return hits


def classify(row: dict[str, Any]) -> tuple[str, str, bool]:
    segment_id = int(row["segment_id"])
    corrected = as_text(row.get("corrected_text"))
    current = as_text(row.get("current_text"))
    relative_path = as_text(row.get("relative_path"))
    source_key = as_text(row.get("source_key"))
    reason_blob = reasons_text(row)

    if row.get("token_status") != "same_structural_tokens":
        return "blocked_uncertain", "token_status_not_same_structural_tokens", False
    if has_bad_marker(corrected):
        return "blocked_uncertain", "bad_encoding_or_question_inside_word", False
    if segment_id in EXPLICIT_HOLDS:
        decision, reason = EXPLICIT_HOLDS[segment_id]
        return decision, reason, False

    hits = residual_hits(corrected)
    if hits:
        article_only = set(hits).issubset({"articles_es", "le_pronoun"})
        if not article_only:
            return "needs_followup_residual_still_present", "residual_hits:" + ",".join(hits), False
        if "Select_CString" in corrected or "LocalPlayerString" in corrected or "Concept(" in corrected:
            return "needs_context_composer", "ambiguous_short_spanish_pronoun_or_article_in_dynamic_literal:" + ",".join(hits), False

    if "Concept(" in corrected and any(term in corrected for term in ("ES_", "Select_CString", "'te'", "'le'", "'seus'")):
        return "needs_context_composer", "dynamic_concept_or_pronoun_literal_requires_context", False
    if any(hint in relative_path for hint in DOMAIN_POLICY_PATH_HINTS):
        if any(hint in reason_blob for hint in SAFE_REASON_HINTS) and not any(token in corrected for token in ("'te'", "'le'", "'seus'", "Concept(")):
            return "safe_exact_residual_repair", "domain_file_but_exact_residual_literal_removed", True
        return "needs_domain_policy", "domain_policy_surface:" + relative_path, False
    if any(hint in relative_path for hint in CONTEXT_PATH_HINTS):
        simple_years_or_term = any(hint in reason_blob for hint in ("año", "años", "ańo", "ańos", "ganarás", "ganaste", "ganó", "ganará", "intentarás", "intentará"))
        if simple_years_or_term and not any(token in corrected for token in ("'te'", "'le'", "'seus'", "Concept(")):
            return "safe_exact_residual_repair", "context_file_but_exact_residual_literal_removed", True
        if source_key.endswith("_tt") or source_key.endswith("_tooltip"):
            return "needs_context_composer", "tooltip_in_contextual_file_requires_composer", False
        return "needs_context_composer", "event_or_activity_context_surface", False

    if any(hint in reason_blob for hint in SAFE_REASON_HINTS):
        return "safe_exact_residual_repair", "only_targeted_spanish_literal_removed", True
    if current != corrected:
        return "safe_exact_residual_repair", "no_remaining_residual_detected", True
    return "blocked_uncertain", "no_text_delta_or_unclear_repair_value", False


def write_reports(rows: list[dict[str, Any]], source_path: Path) -> tuple[Path, Path, Path, Counter]:
    txt_path, jsonl_path, csv_path = report_paths()
    reviewed: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("checkpoint_allowed") or 0) != 1:
            continue
        decision, reason, requires_apply = classify(row)
        reviewed.append(
            {
                "segment_id": int(row["segment_id"]),
                "relative_path": as_text(row.get("relative_path")),
                "source_key": as_text(row.get("source_key")),
                "decision": decision,
                "reason": reason,
                "current_text": as_text(row.get("current_text")),
                "corrected_text": as_text(row.get("corrected_text")),
                "token_status": as_text(row.get("token_status")),
                "requires_apply_later": bool(requires_apply),
                "repair_item_id": row.get("repair_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "subpolicy_name": as_text(row.get("subpolicy_name")),
            }
        )
    if len(reviewed) != EXPECTED_ALLOWED:
        raise RuntimeError(f"Expected {EXPECTED_ALLOWED} checkpoint_allowed rows, got {len(reviewed)}")

    counts = Counter(item["decision"] for item in reviewed)
    reasons = Counter(item["reason"] for item in reviewed)
    jsonl_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in reviewed) + "\n",
        encoding="utf-8",
    )
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "decision",
        "reason",
        "token_status",
        "requires_apply_later",
        "repair_item_id",
        "ledger_item_id",
        "current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(reviewed)

    safe = [item for item in reviewed if item["decision"] == "safe_exact_residual_repair"]
    held = [item for item in reviewed if item["decision"] != "safe_exact_residual_repair"]
    lines = [
        "Spanish residual checkpoint 9 safety split",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Checkpoint JSONL: {source_path}",
        "",
        "Counts:",
        f"- total_allowed_reviewed: {len(reviewed)}",
        f"- safe_exact_residual_repair: {counts['safe_exact_residual_repair']}",
        f"- needs_followup_residual_still_present: {counts['needs_followup_residual_still_present']}",
        f"- needs_context_composer: {counts['needs_context_composer']}",
        f"- needs_domain_policy: {counts['needs_domain_policy']}",
        f"- blocked_uncertain: {counts['blocked_uncertain']}",
        "",
        "Top reasons:",
    ]
    lines.extend(f"- {reason}: {count}" for reason, count in reasons.most_common(15))
    lines.extend(
        [
            "",
            "Safe IDs:",
            ", ".join(str(item["segment_id"]) for item in safe) or "none",
            "",
            "Held IDs:",
        ]
    )
    lines.extend(f"- {item['segment_id']}: {item['decision']} | {item['reason']} | {item['source_key']}" for item in held)
    lines.extend(["", "Safe examples:"])
    lines.extend(
        f"- {item['segment_id']} | {item['relative_path']}::{item['source_key']} | {item['corrected_text']}"
        for item in safe[:5]
    )
    lines.extend(["", "Held examples:"])
    lines.extend(
        f"- {item['segment_id']} | {item['decision']} | {item['reason']} | {item['corrected_text']}"
        for item in held[:5]
    )
    lines.extend(
        [
            "",
            "Safety:",
            "- Read-only split only.",
            "- No production run, no apply, no confirmations, no reindex, no training/model promotion.",
            "- No source/ or output/ writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, csv_path, counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-jsonl", required=True, type=Path)
    args = parser.parse_args()
    rows = load_rows(args.checkpoint_jsonl)
    txt_path, jsonl_path, csv_path, counts = write_reports(rows, args.checkpoint_jsonl)
    print("[spanish_residual_checkpoint9_safety_split] Completed")
    print(f"[spanish_residual_checkpoint9_safety_split] TXT: {txt_path}")
    print(f"[spanish_residual_checkpoint9_safety_split] JSONL: {jsonl_path}")
    print(f"[spanish_residual_checkpoint9_safety_split] CSV: {csv_path}")
    print(f"[spanish_residual_checkpoint9_safety_split] Counts: {json.dumps(dict(counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
