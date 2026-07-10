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


SOURCE_DECISION = "needs_dynamic_semantic_concept_expression_policy"
TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "semantic_review_router")

ALLOWED_DECISIONS = {
    "dynamic_semantic_concept_ready_false_reopen",
    "dynamic_semantic_concept_ready_lifecycle",
    "needs_dynamic_semantic_concept_plain_policy",
    "needs_dynamic_semantic_concept_requirement_tooltip_policy",
    "needs_dynamic_semantic_concept_effect_list_policy",
    "needs_dynamic_semantic_concept_custom_loc_policy",
    "needs_dynamic_semantic_concept_scope_getter_policy",
    "needs_dynamic_semantic_concept_script_value_policy",
    "needs_dynamic_semantic_concept_domain_context",
    "needs_dynamic_semantic_concept_event_context_composer",
    "needs_dynamic_semantic_concept_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
CONCEPT_RE = re.compile(r"\[[A-Za-z0-9_]+(?:\|[^\]]+)?\]|\$game_concept[^$]*\$|Concept\s*\(", re.IGNORECASE)
CUSTOM_LOC_RE = re.compile(r"Custom\(|custom_loc|GetCustom|Window\.Get|GetDesignateTooltip", re.IGNORECASE)
SCOPE_GETTER_RE = re.compile(
    r"\[[^\]]*(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|recipient|target|root)[^\]]*\]|"
    r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|Get[A-Za-z0-9_]+\(",
    re.IGNORECASE,
)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.IGNORECASE)
TOOLTIP_RE = re.compile(
    r"#tooltip|tooltip|_tt\b|_tt$|trigger|requirement|required|available|can_|cannot|"
    r"NO_CHANCE|CHANCE|WILL_GET|valid_|invalid_|unlock_tt|selection_tooltip|template_tt",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|^[-*]\s|#indent|#weak|#bold|#low|#high|effect|effects_l_", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"religion|faith|culture|title|law|succession|artifact|activity|trait|government|governor|"
    r"county|duchy|kingdom|empire|realm|house|dynasty|nickname|accolade|knight|court|"
    r"building|holding|contract|travel|tournament|scheme|memory|legend|holy_order|regiment|"
    r"tax|vassal|liege|claim|war|battle|prowess|piety|prestige|gold|herd|place|location|"
    r"weapon|blade|book|stewardship|councillor|task|commander|hunter|spouse|wedding|champion|prize|baron|camp",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|option|toast|dialogue|story|memory|memories|activity|travel|journey|"
    r"interaction|letter|request|petition|scheme|outcome|ongoing|flavor|narrative|episode|request",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:aumenta|consiguio|consiguió|ganaste|ganar|tendras|tendrás|lograste|acepta|"
    r"posesion|posesión|azar|conceder|reclamacion|reclamación|sera|será|mas|más|muy|"
    r"facil|fácil|revela|falsedad|ensenar|enseñar|provecho|poder|desheredar|"
    r"atacante|numero|número|elegir|enfoque|sufre|corte|bajar|perdiste|duelo|"
    r"pres[eé]ntate|cambiar|the|your|you|their|has|have|will|can|cannot)\b",
    re.IGNORECASE,
)
BAD_ENCODING_MARKERS = ("NÃ", "Ãƒ", "Â", "\ufffd")


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


def source_rows(residual_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(residual_jsonl):
        if row.get("residual_decision") != SOURCE_DECISION:
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


def has_bad_encoding(text: str) -> bool:
    return any(marker in text for marker in BAD_ENCODING_MARKERS)


def tokens_seen(text: str) -> list[str]:
    checks = [
        ("Concept", CONCEPT_RE),
        ("CustomLoc", CUSTOM_LOC_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("RequirementTooltip", TOOLTIP_RE),
        ("EffectList", EFFECT_LIST_RE),
        ("DomainContext", DOMAIN_RE),
        ("EventContext", EVENT_RE),
        ("Residual", RESIDUAL_RE),
    ]
    labels = [label for label, pattern in checks if pattern.search(text)]
    if CK3_TOKEN_RE.search(text) and "CK3DynamicToken" not in labels:
        labels.append("CK3DynamicToken")
    return labels


def ready_decision(state: dict[str, Any] | None, text: str, haystack: str) -> tuple[str, str, str] | None:
    if not token_boundaries_ok(text) or has_bad_encoding(text) or RESIDUAL_RE.search(text):
        return None
    if any(pattern.search(haystack) for pattern in (CUSTOM_LOC_RE, SCOPE_GETTER_RE, SCRIPT_VALUE_RE, TOOLTIP_RE, EFFECT_LIST_RE, DOMAIN_RE, EVENT_RE)):
        return None
    if len(text) > 120:
        return None
    if state and int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return (
            "dynamic_semantic_concept_ready_false_reopen",
            "concept_ready_false_reopen",
            "concept expression appears aligned for future false-reopen lifecycle",
        )
    return (
        "dynamic_semantic_concept_ready_lifecycle",
        "concept_ready_lifecycle",
        "short concept expression appears aligned for future lifecycle",
    )


def classify(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> tuple[str, str, str]:
    text = str(row.get("current_text") or "")
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "key", "current_text"))
    key_path = " ".join(str(row.get(key) or "") for key in ("relative_path", "key"))

    if not state_ready(state):
        return "blocked_uncertain", "not_pending_confirmed", "state guard failed in selected segment-state run"
    if not exact_family_shape(families):
        return "blocked_uncertain", "family_shape_guard", "open issue families are no longer exactly dynamic + semantic"
    if not text.strip() or not token_boundaries_ok(text):
        return "blocked_uncertain", "broken_or_missing_text", "missing text or malformed token boundary"
    if not CONCEPT_RE.search(text):
        return "needs_new_microagent", "concept_source_without_detected_concept", "source bucket no longer exposes a recognizable concept expression"

    if CUSTOM_LOC_RE.search(haystack):
        return "needs_dynamic_semantic_concept_custom_loc_policy", "concept_custom_loc", "concept expression is mixed with Custom(...) or helper custom"
    if SCOPE_GETTER_RE.search(text):
        return "needs_dynamic_semantic_concept_scope_getter_policy", "concept_scope_getter", "concept expression is mixed with getter/scope expression"
    if SCRIPT_VALUE_RE.search(text):
        return "needs_dynamic_semantic_concept_script_value_policy", "concept_script_value", "concept expression is mixed with numeric/script value"
    if TOOLTIP_RE.search(key_path):
        return "needs_dynamic_semantic_concept_requirement_tooltip_policy", "concept_requirement_tooltip", "concept expression is in requirement/tooltip surface"
    if EFFECT_LIST_RE.search(text):
        return "needs_dynamic_semantic_concept_effect_list_policy", "concept_effect_list", "concept expression is in effect list, multiline, or composed block"
    if DOMAIN_RE.search(haystack):
        return "needs_dynamic_semantic_concept_domain_context", "concept_domain_context", "concept expression depends on domain-sensitive vocabulary"
    if EVENT_RE.search(haystack) or len(text) > 180:
        return "needs_dynamic_semantic_concept_event_context_composer", "concept_event_context", "concept expression depends on event/contextual prose"
    if has_bad_encoding(text) or RESIDUAL_RE.search(text):
        return "needs_dynamic_semantic_concept_residual_repair", "concept_residual_repair", "visible residual remains inside concept expression"

    ready = ready_decision(state, text, haystack)
    if ready:
        return ready
    if len(text) <= 120:
        return "needs_dynamic_semantic_concept_plain_policy", "concept_plain", "concept in short plain prose still needs policy before lifecycle"
    return "needs_new_microagent", "concept_unclassified_pattern", "recurring concept expression pattern does not fit current sublanes"


def decide(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state, families)
    text = str(row.get("current_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": text,
        "source_residual_decision": row.get("residual_decision"),
        "concept_decision": decision,
        "concept_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(text),
        "requires_lifecycle_later": decision in {
            "dynamic_semantic_concept_ready_false_reopen",
            "dynamic_semantic_concept_ready_lifecycle",
        },
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_semantic_concept_expression_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def validate_results(results: list[dict[str, Any]], expected_total: int) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_residual_decision",
        "concept_decision",
        "concept_subpolicy",
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
        if row["source_residual_decision"] != SOURCE_DECISION:
            raise SystemExit(f"invalid source decision for segment_id={row['segment_id']}")
        if row["concept_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for segment_id={row['segment_id']}: {row['concept_decision']}")
        corrected = row.get("corrected_text") or ""
        if row["requires_apply_later"] and not corrected:
            raise SystemExit(f"apply candidate without corrected_text: segment_id={row['segment_id']}")
        if corrected and CK3_TOKEN_RE.findall(corrected) != CK3_TOKEN_RE.findall(row["current_text"]):
            raise SystemExit(f"CK3 token mismatch in corrected_text: segment_id={row['segment_id']}")


def recommendation(decision_counts: Counter[str], subpolicy_counts: Counter[str]) -> str:
    ready_count = (
        decision_counts["dynamic_semantic_concept_ready_false_reopen"]
        + decision_counts["dynamic_semantic_concept_ready_lifecycle"]
    )
    if ready_count >= 5:
        return "prepare_readonly_lifecycle_for_dynamic_semantic_concept_ready"
    if decision_counts["needs_dynamic_semantic_concept_residual_repair"] >= 5:
        return "prepare_concept_residual_split_before_apply"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_dynamic_semantic_concept_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 8:
            return f"register_specific_policy_microagent_for_{top_decision}"
    if subpolicy_counts:
        top_subpolicy, top_count = subpolicy_counts.most_common(1)[0]
        if top_count >= 8:
            return f"register_specific_policy_microagent_for_subpolicy_{top_subpolicy}"
    return "fragmented_migrate_to_custom_loc_or_global_diagnostic"


def write_reports(results: list[dict[str, Any]], jsonl_path: Path, txt_path: Path) -> None:
    decision_counts = Counter(row["concept_decision"] for row in results)
    subpolicy_counts = Counter(row["concept_subpolicy"] for row in results)
    ready_count = sum(1 for row in results if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in results if row["requires_apply_later"])
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("dynamic + semantic concept expression review\n")
        handle.write(f"total_reviewed: {len(results)}\n")
        handle.write(f"future_lifecycle_ready: {ready_count}\n")
        handle.write(f"future_apply_candidates: {apply_count}\n")
        handle.write("\nconcept_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nconcept_subpolicy_counts:\n")
        for subpolicy, count in subpolicy_counts.most_common():
            handle.write(f"- {subpolicy}: {count}\n")
        handle.write(f"\nrecommendation: {recommendation(decision_counts, subpolicy_counts)}\n")
        handle.write("\nprohibited_actions: none; no lifecycle, apply, segment-state, confirmations, reindex, training, source/output changes\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only concept-expression review for dynamic + semantic residual queue.")
    parser.add_argument("--residual-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(args.residual_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    families = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)
    results = [decide(row, states.get(int(row["segment_id"])), families.get(int(row["segment_id"]))) for row in source]
    validate_results(results, expected_total=len(source))

    jsonl_path, txt_path = output_paths()
    write_reports(results, jsonl_path, txt_path)
    decision_counts = Counter(row["concept_decision"] for row in results)
    print(f"jsonl: {jsonl_path}")
    print(f"txt: {txt_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"future_lifecycle_ready: {sum(1 for row in results if row['requires_lifecycle_later'])}")
    print(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}")
    print("concept_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
