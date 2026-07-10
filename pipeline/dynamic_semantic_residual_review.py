from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE_DECISION = "needs_dynamic_semantic_residual_repair"
TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "semantic_review_router")

ALLOWED_DECISIONS = {
    "dynamic_semantic_safe_spanish_residual_repair",
    "dynamic_semantic_safe_english_residual_repair",
    "dynamic_semantic_safe_ptbr_fluency_repair",
    "needs_dynamic_semantic_custom_loc_policy",
    "needs_dynamic_semantic_concept_expression_policy",
    "needs_dynamic_semantic_requirement_tooltip_policy",
    "needs_dynamic_semantic_effect_list_policy",
    "needs_dynamic_semantic_domain_context",
    "needs_dynamic_semantic_event_context_composer",
    "needs_dynamic_semantic_gender_or_custom_loc_policy",
    "needs_dynamic_semantic_semantic_review",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
CONCEPT_RE = re.compile(r"\[[A-Za-z0-9_]+(?:\|[^\]]+)?\]|\$game_concept[^$]*\$|Concept\s*\(", re.IGNORECASE)
CUSTOM_LOC_RE = re.compile(r"Custom\(|custom_loc|GetCustom|Window\.Get|GetDesignateTooltip", re.IGNORECASE)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)\b|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)",
    re.IGNORECASE,
)
TOOLTIP_RE = re.compile(
    r"#tooltip|tooltip|_tt\b|_tt$|trigger|requirement|required|available|can_|cannot|"
    r"NO_CHANCE|CHANCE|WILL_GET|valid_|invalid_|unlock_tt|selection_tooltip",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|^[-*]\s|#indent|#weak|#bold|#low|#high|effect|effects_l_", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"religion|faith|culture|title|law|succession|artifact|activity|trait|government|governor|"
    r"county|duchy|kingdom|empire|realm|house|dynasty|nickname|accolade|knight|court|"
    r"building|holding|contract|travel|tournament|scheme|memory|legend|holy_order|regiment|"
    r"tax|vassal|liege|claim|war|battle|prowess|piety|prestige|gold|herd|place|location|"
    r"weapon|blade|book|stewardship|councillor|task|commander|hunter|hunter",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|option|toast|dialogue|story|memory|memories|activity|travel|journey|"
    r"interaction|letter|request|petition|scheme|outcome|ongoing|flavor|narrative|episode",
    re.IGNORECASE,
)
SCOPE_GETTER_RE = re.compile(
    r"\[[^\]]*(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|recipient|target|root)[^\]]*\]|"
    r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|Get[A-Za-z0-9_]+\(",
    re.IGNORECASE,
)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.IGNORECASE)
SPANISH_RE = re.compile(
    r"\b(?:aumenta|consiguio|consiguió|ganaste|ganar|tendras|tendrás|lograste|acepta|"
    r"posesion|posesión|azar|conceder|reclamacion|reclamación|sera|será|mas|más|muy|"
    r"facil|fácil|revela|falsedad|ensenar|enseñar|provecho|poder|desheredar|"
    r"atacante|numero|número|elegir|enfoque|sufre|corte|bajar|perdiste|duelo|"
    r"pres[eé]ntate|cambiar)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(r"\b(?:the|your|you|their|has|have|will|can|cannot|must|should|if|when)\b", re.IGNORECASE)
PTBR_FLUENCY_RE = re.compile(r"\bem o\b|\bem a\b|\bde o\b|\bde a\b|\ba fatura\b|\bmuito mais chances\b", re.IGNORECASE)
BAD_ENCODING_MARKERS = ("NÃ", "Ãƒ", "Â", "\ufffd")
WORD_QUESTION_RE = re.compile(r"\w\?\w")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, needs_output_apply,
               confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, tuple[str, ...]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, issue_family
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    by_segment: dict[int, set[str]] = {}
    for row in rows:
        by_segment.setdefault(int(row["segment_id"]), set()).add(str(row["issue_family"]))
    return {segment_id: tuple(sorted(families)) for segment_id, families in by_segment.items()}


def source_rows(combo_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(combo_jsonl):
        if row.get("dynamic_semantic_decision") != SOURCE_DECISION:
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate source segment_id: {segment_id}")
        seen.add(segment_id)
        rows.append(row)
    return rows


def state_ready(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state["state_group"] == "pending"
        and int(state["is_closed"] or 0) == 0
        and int(state["needs_output_apply"] or 0) == 0
        and int(state["confirmed_matches_output"] or 0) == 1
    )


def exact_family_shape(families: tuple[str, ...] | None) -> bool:
    return families == tuple(sorted(TARGET_FAMILIES))


def token_boundaries_ok(text: str) -> bool:
    return text.count("[") == text.count("]") and text.count("$") % 2 == 0


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    checks = [
        ("CustomLoc", CUSTOM_LOC_RE),
        ("GenderHelper", GENDER_RE),
        ("Concept", CONCEPT_RE),
        ("RequirementTooltip", TOOLTIP_RE),
        ("EffectList", EFFECT_LIST_RE),
        ("DomainContext", DOMAIN_RE),
        ("EventContext", EVENT_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("SpanishResidual", SPANISH_RE),
        ("EnglishResidual", ENGLISH_RE),
        ("PTBRFluency", PTBR_FLUENCY_RE),
    ]
    for label, pattern in checks:
        if pattern.search(text) and label not in labels:
            labels.append(label)
    if CK3_TOKEN_RE.search(text) and "CK3DynamicToken" not in labels:
        labels.append("CK3DynamicToken")
    return labels


def has_bad_encoding(text: str) -> bool:
    return any(marker in text for marker in BAD_ENCODING_MARKERS)


def dependency_patterns_match(haystack: str) -> bool:
    return any(
        pattern.search(haystack)
        for pattern in (
            CUSTOM_LOC_RE,
            GENDER_RE,
            CONCEPT_RE,
            TOOLTIP_RE,
            EFFECT_LIST_RE,
            DOMAIN_RE,
            EVENT_RE,
            SCOPE_GETTER_RE,
            SCRIPT_VALUE_RE,
        )
    )


def conservative_safe_repair(text: str, haystack: str) -> tuple[str, str, str] | None:
    if dependency_patterns_match(haystack):
        return None
    if not token_boundaries_ok(text):
        return None
    corrected = text
    corrected = re.sub(r"\bem o\b", "no", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bem a\b", "na", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bde o\b", "do", corrected, flags=re.IGNORECASE)
    corrected = re.sub(r"\bde a\b", "da", corrected, flags=re.IGNORECASE)
    if corrected == text:
        return None
    if CK3_TOKEN_RE.findall(corrected) != CK3_TOKEN_RE.findall(text):
        return None
    if has_bad_encoding(corrected) or WORD_QUESTION_RE.search(corrected):
        return None
    if SPANISH_RE.search(text):
        return (
            "dynamic_semantic_safe_spanish_residual_repair",
            "spanish_residual_short_safe",
            corrected,
        )
    if ENGLISH_RE.search(text):
        return (
            "dynamic_semantic_safe_english_residual_repair",
            "english_residual_short_safe",
            corrected,
        )
    return (
        "dynamic_semantic_safe_ptbr_fluency_repair",
        "ptbr_fluency_short_safe",
        corrected,
    )


def classify(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> tuple[str, str, str, str]:
    text = str(row.get("current_text") or "")
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "key", "current_text"))
    key_path = " ".join(str(row.get(key) or "") for key in ("relative_path", "key"))

    if not state_ready(state):
        return "blocked_uncertain", "not_pending_confirmed", "", "state guard failed in selected segment-state run"
    if not exact_family_shape(families):
        return "blocked_uncertain", "family_shape_guard", "", "open issue families are no longer exactly dynamic + semantic"
    if not text.strip() or not token_boundaries_ok(text):
        return "blocked_uncertain", "broken_or_missing_text", "", "missing text or malformed token boundary"

    safe = conservative_safe_repair(text, haystack)
    if safe:
        decision, subpolicy, corrected_text = safe
        return decision, subpolicy, corrected_text, "short mechanical residual repair candidate"

    if GENDER_RE.search(haystack) and CUSTOM_LOC_RE.search(haystack):
        return (
            "needs_dynamic_semantic_gender_or_custom_loc_policy",
            "gender_custom_loc_residual",
            "",
            "residual is coupled to gender/custom localization helper",
        )
    if CUSTOM_LOC_RE.search(haystack):
        return "needs_dynamic_semantic_custom_loc_policy", "custom_loc_residual", "", "residual is coupled to Custom(...) or helper custom"
    if GENDER_RE.search(haystack):
        return (
            "needs_dynamic_semantic_gender_or_custom_loc_policy",
            "gender_helper_residual",
            "",
            "residual is coupled to gender helper",
        )
    if CONCEPT_RE.search(haystack):
        return (
            "needs_dynamic_semantic_concept_expression_policy",
            "concept_expression_residual",
            "",
            "residual is coupled to concept link or expression",
        )
    if TOOLTIP_RE.search(key_path):
        return (
            "needs_dynamic_semantic_requirement_tooltip_policy",
            "requirement_tooltip_residual",
            "",
            "residual is coupled to requirement/tooltip surface",
        )
    if EFFECT_LIST_RE.search(text):
        return (
            "needs_dynamic_semantic_effect_list_policy",
            "effect_list_residual",
            "",
            "residual is coupled to effect list, multiline, or formatted block",
        )
    if DOMAIN_RE.search(haystack):
        return "needs_dynamic_semantic_domain_context", "domain_context_residual", "", "residual depends on domain-sensitive vocabulary"
    if EVENT_RE.search(haystack):
        return (
            "needs_dynamic_semantic_event_context_composer",
            "event_context_residual",
            "",
            "residual depends on event/dialogue/perspective context",
        )
    if SCOPE_GETTER_RE.search(text) or SCRIPT_VALUE_RE.search(text):
        return (
            "needs_dynamic_semantic_semantic_review",
            "dynamic_expression_semantic_residual",
            "",
            "residual depends on dynamic expression semantics without a permitted narrower bucket",
        )
    if has_bad_encoding(text) or WORD_QUESTION_RE.search(text) or SPANISH_RE.search(text) or ENGLISH_RE.search(text):
        return (
            "needs_dynamic_semantic_semantic_review",
            "semantic_residual",
            "",
            "visible residual exists, but no safe mechanical repair passed all guards",
        )
    return "blocked_uncertain", "uncertain_residual", "", "no safe mechanical repair pattern matched"


def decide(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> dict[str, Any]:
    decision, subpolicy, corrected_text, notes = classify(row, state, families)
    text = str(row.get("current_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": text,
        "source_dynamic_semantic_decision": row.get("dynamic_semantic_decision"),
        "residual_decision": decision,
        "residual_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(text),
        "requires_lifecycle_later": False,
        "requires_apply_later": decision.startswith("dynamic_semantic_safe_"),
        "corrected_text": corrected_text,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_semantic_residual_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def validate_results(results: list[dict[str, Any]], expected_total: int) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_dynamic_semantic_decision",
        "residual_decision",
        "residual_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    if len(results) != expected_total:
        raise SystemExit(f"review total mismatch: expected {expected_total}, got {len(results)}")
    ids = [row["segment_id"] for row in results]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id in results")
    for row in results:
        missing = required.difference(row)
        if missing:
            raise SystemExit(f"missing fields for segment_id={row.get('segment_id')}: {sorted(missing)}")
        decision = row["residual_decision"]
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for segment_id={row['segment_id']}: {decision}")
        if row["source_dynamic_semantic_decision"] != SOURCE_DECISION:
            raise SystemExit(f"invalid source decision for segment_id={row['segment_id']}")
        corrected = row.get("corrected_text") or ""
        if row["requires_apply_later"] and not corrected:
            raise SystemExit(f"apply candidate without corrected_text: segment_id={row['segment_id']}")
        if corrected:
            if has_bad_encoding(corrected):
                raise SystemExit(f"bad encoding marker in corrected_text: segment_id={row['segment_id']}")
            if WORD_QUESTION_RE.search(corrected):
                raise SystemExit(f"word question marker in corrected_text: segment_id={row['segment_id']}")
            if CK3_TOKEN_RE.findall(corrected) != CK3_TOKEN_RE.findall(row["current_text"]):
                raise SystemExit(f"CK3 token mismatch in corrected_text: segment_id={row['segment_id']}")


def recommendation(decision_counts: Counter[str], subpolicy_counts: Counter[str]) -> str:
    safe_count = sum(count for decision, count in decision_counts.items() if decision.startswith("dynamic_semantic_safe_"))
    if safe_count >= 5:
        return "prepare_protected_apply_for_safe_dynamic_semantic_residual_repairs"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_dynamic_semantic_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 15:
            return f"prepare_specific_policy_microagent_for_{top_decision}"
    if subpolicy_counts:
        top_subpolicy, top_count = subpolicy_counts.most_common(1)[0]
        if top_count >= 15:
            return f"prepare_specific_policy_microagent_for_subpolicy_{top_subpolicy}"
    return "do_not_apply_now_fragmented_residuals_return_to_global_diagnostic"


def write_reports(results: list[dict[str, Any]], jsonl_path: Path, txt_path: Path) -> None:
    decision_counts = Counter(row["residual_decision"] for row in results)
    subpolicy_counts = Counter(row["residual_subpolicy"] for row in results)
    safe_count = sum(count for decision, count in decision_counts.items() if decision.startswith("dynamic_semantic_safe_"))
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("dynamic + semantic residual review\n")
        handle.write(f"total_reviewed: {len(results)}\n")
        handle.write(f"future_apply_candidates: {safe_count}\n")
        handle.write("\nresidual_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nresidual_subpolicy_counts:\n")
        for subpolicy, count in subpolicy_counts.most_common():
            handle.write(f"- {subpolicy}: {count}\n")
        handle.write(f"\nrecommendation: {recommendation(decision_counts, subpolicy_counts)}\n")
        handle.write("\nprohibited_actions: none; no lifecycle, apply, segment-state, confirmations, reindex, training, source/output changes\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only residual review for dynamic + semantic queue.")
    parser.add_argument("--combo-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(args.combo_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    families = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)
    results = [decide(row, states.get(int(row["segment_id"])), families.get(int(row["segment_id"]))) for row in source]
    validate_results(results, expected_total=len(source))

    jsonl_path, txt_path = output_paths()
    write_reports(results, jsonl_path, txt_path)
    decision_counts = Counter(row["residual_decision"] for row in results)
    safe_count = sum(count for decision, count in decision_counts.items() if decision.startswith("dynamic_semantic_safe_"))
    print(f"jsonl: {jsonl_path}")
    print(f"txt: {txt_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"future_apply_candidates: {safe_count}")
    print("residual_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
