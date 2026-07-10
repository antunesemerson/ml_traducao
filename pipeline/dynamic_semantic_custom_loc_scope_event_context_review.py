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


SOURCE_DECISION = "needs_custom_loc_scope_event_context_composer"
TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "semantic_review_router")

ALLOWED_DECISIONS = {
    "custom_loc_scope_event_ready_false_reopen",
    "custom_loc_scope_event_ready_lifecycle",
    "needs_custom_loc_scope_event_actor_target_policy",
    "needs_custom_loc_scope_event_local_player_policy",
    "needs_custom_loc_scope_event_recipient_policy",
    "needs_custom_loc_scope_event_name_dynasty_policy",
    "needs_custom_loc_scope_event_title_law_policy",
    "needs_custom_loc_scope_event_trait_modifier_policy",
    "needs_custom_loc_scope_event_requirement_tooltip_policy",
    "needs_custom_loc_scope_event_effect_list_policy",
    "needs_custom_loc_scope_event_domain_context",
    "needs_custom_loc_scope_event_residual_repair",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
CUSTOM_LOC_RE = re.compile(r"Custom\(|custom_loc|GetCustom|Window\.Get|GetDesignateTooltip", re.IGNORECASE)
ACTOR_TARGET_RE = re.compile(
    r"\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|target|root|from|scope|location|province|activity|host|first)\b",
    re.IGNORECASE,
)
LOCAL_PLAYER_RE = re.compile(r"\b(?:você|vocês|seu|sua|seus|suas|meu|minha|nossos|nossas|eu|me|mim)\b", re.IGNORECASE)
RECIPIENT_RE = re.compile(r"\b(?:recipient|addressee|destinat[aá]rio|para você|para ti|lhe|teu|tua)\b", re.IGNORECASE)
NAME_DYNASTY_RE = re.compile(
    r"GetName|GetFullName|GetFirstName|GetShortUIName|GetDynasty|GetHouse|Muhammad|Cicero|Kalila|Dimna|"
    r"nome|dinastia|casa|ep[ií]teto|entidade|profeta",
    re.IGNORECASE,
)
TITLE_LAW_RE = re.compile(
    r"GetTitle|GetPrimaryTitle|GetLaw|title|law|government|realm|succession|throne|holding|county|duchy|kingdom|empire|"
    r"t[ií]tulo|lei|governo|reino|cargo|vassalagem|rank",
    re.IGNORECASE,
)
TRAIT_MODIFIER_RE = re.compile(
    r"GetTrait|GetModifier|trait|modifier|accolade|knight|skill|propert|ArtifactAdverb|ArtifactBookContentQuality|"
    r"ArtifactWealth|ArtifactFlowerSpecies|HornedMythicalCreature|RegionalSkirmisher|RegionalMythicalCreature|"
    r"habilidade|propriedade|descritor",
    re.IGNORECASE,
)
TOOLTIP_RE = re.compile(
    r"#tooltip|tooltip|_tt\b|_tt$|trigger|requirement|required|available|can_|cannot|"
    r"NO_CHANCE|CHANCE|WILL_GET|valid_|invalid_|unlock_tt|selection_tooltip|template_tt",
    re.IGNORECASE,
)
EFFECT_LIST_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|^[-*]\s|#indent|#weak|#bold|#low|#high|effect|effects_l_", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"artifact|activity|journey|monument|book|anatomy|torture|horn|flower|mythical|skirmisher|creature|"
    r"court|terrain|holding|travel|tournament|faith|culture|trait|war|battle|religious|devoto|bibliofilo",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:consiguio|consiguió|ganaste|ganar|tendras|tendrás|lograste|acepta|posesion|"
    r"posesión|azar|conceder|reclamacion|reclamación|sera|será|mas|más|muy|facil|fácil|"
    r"the|your|you|their|has|have|will|can|cannot)\b",
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


def placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, needs_output_apply,
               confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders(segment_ids)})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, tuple[str, ...]]:
    if not segment_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT segment_id, issue_family
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders(segment_ids)})
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    by_segment: dict[int, set[str]] = {}
    for row in rows:
        by_segment.setdefault(int(row["segment_id"]), set()).add(str(row["issue_family"]))
    return {segment_id: tuple(sorted(families)) for segment_id, families in by_segment.items()}


def fetch_texts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, str]]:
    if not segment_ids:
        return {}
    source_rows = conn.execute(
        f"""
        SELECT id AS segment_id, source_key, old_text
        FROM source_segments
        WHERE id IN ({placeholders(segment_ids)})
        """,
        segment_ids,
    ).fetchall()
    output_rows = conn.execute(
        f"""
        SELECT segment_id, portuguese_text
        FROM output_segments
        WHERE segment_id IN ({placeholders(segment_ids)})
        """,
        segment_ids,
    ).fetchall()
    confirmation_rows = conn.execute(
        f"""
        SELECT segment_id, confirmed_text
        FROM segment_confirmations
        WHERE segment_id IN ({placeholders(segment_ids)})
        ORDER BY datetime(updated_at) DESC, id DESC
        """,
        segment_ids,
    ).fetchall()
    texts: dict[int, dict[str, str]] = {
        int(row["segment_id"]): {
            "source_key": str(row["source_key"] or ""),
            "old_text": str(row["old_text"] or ""),
            "confirmed_text": "",
            "output_text": "",
        }
        for row in source_rows
    }
    for row in output_rows:
        texts.setdefault(int(row["segment_id"]), {}).update({"output_text": str(row["portuguese_text"] or "")})
    for row in confirmation_rows:
        segment_id = int(row["segment_id"])
        if not texts.setdefault(segment_id, {}).get("confirmed_text"):
            texts[segment_id]["confirmed_text"] = str(row["confirmed_text"] or "")
    return texts


def source_rows(scope_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(scope_jsonl):
        if row.get("custom_loc_scope_decision") != SOURCE_DECISION:
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


def classify(
    row: dict[str, Any],
    state: dict[str, Any] | None,
    families: tuple[str, ...] | None,
    text_data: dict[str, str],
) -> tuple[str, str, str]:
    output_text = text_data.get("output_text") or str(row.get("current_text") or "")
    confirmed_text = text_data.get("confirmed_text") or output_text
    haystack = " ".join(str(value or "") for value in (row.get("relative_path"), row.get("key"), output_text, confirmed_text))
    key_path = " ".join(str(value or "") for value in (row.get("relative_path"), row.get("key")))

    if not state_ready(state):
        return "blocked_uncertain", "not_pending_confirmed", "state guard failed in selected segment-state run"
    if not exact_family_shape(families):
        return "blocked_uncertain", "family_shape_guard", "open issue families are no longer exactly dynamic + semantic"
    if not output_text.strip() or not token_boundaries_ok(output_text):
        return "blocked_uncertain", "broken_or_missing_text", "missing text or malformed token boundary"
    if not CUSTOM_LOC_RE.search(output_text):
        return "blocked_uncertain", "missing_custom_loc", "source bucket no longer exposes custom localization"
    if confirmed_text != output_text:
        return "blocked_uncertain", "confirmation_output_mismatch", "confirmed_text and output_text are not identical"

    if TOOLTIP_RE.search(key_path):
        return (
            "needs_custom_loc_scope_event_requirement_tooltip_policy",
            "event_requirement_tooltip",
            "event/custom-loc scope item is actually a requirement or tooltip surface",
        )
    if EFFECT_LIST_RE.search(output_text):
        return (
            "needs_custom_loc_scope_event_effect_list_policy",
            "event_effect_list",
            "event/custom-loc scope item contains multiline or effect-list structure",
        )
    if RECIPIENT_RE.search(haystack):
        return (
            "needs_custom_loc_scope_event_recipient_policy",
            "event_recipient",
            "translation depends on recipient/addressee perspective",
        )
    if LOCAL_PLAYER_RE.search(output_text):
        return (
            "needs_custom_loc_scope_event_local_player_policy",
            "event_local_player",
            "translation uses local-player or direct-address perspective",
        )
    if TITLE_LAW_RE.search(haystack):
        return (
            "needs_custom_loc_scope_event_title_law_policy",
            "event_title_law",
            "translation depends on title, law, government, rank, or realm context",
        )
    if NAME_DYNASTY_RE.search(haystack):
        return (
            "needs_custom_loc_scope_event_name_dynasty_policy",
            "event_name_dynasty",
            "translation depends on proper name, dynasty, house, epithet, or named entity",
        )
    if TRAIT_MODIFIER_RE.search(haystack):
        return (
            "needs_custom_loc_scope_event_trait_modifier_policy",
            "event_trait_modifier",
            "translation depends on trait, modifier, skill, property, or dynamic descriptor",
        )
    if ACTOR_TARGET_RE.search(output_text):
        return (
            "needs_custom_loc_scope_event_actor_target_policy",
            "event_actor_target",
            "translation depends on actor/target/root/scope perspective",
        )
    if has_bad_encoding(output_text) or RESIDUAL_RE.search(output_text):
        return (
            "needs_custom_loc_scope_event_residual_repair",
            "event_residual_repair",
            "visible residual remains, but this review does not apply repairs",
        )
    if DOMAIN_RE.search(haystack):
        return (
            "needs_custom_loc_scope_event_domain_context",
            "event_domain_context",
            "domain context is the main unresolved dependency",
        )
    if (
        state
        and int(state.get("needs_reopen") or 0) == 1
        and state.get("final_state") == "reopen_auto_confirmed_autofix"
        and confirmed_text == output_text
    ):
        return (
            "custom_loc_scope_event_ready_false_reopen",
            "event_ready_false_reopen",
            "custom loc/scope event text appears aligned for future false-reopen lifecycle",
        )
    if confirmed_text == output_text:
        return (
            "custom_loc_scope_event_ready_lifecycle",
            "event_ready_lifecycle",
            "custom loc/scope event text appears aligned for future lifecycle",
        )
    return "blocked_uncertain", "uncertain_event_context", "no safe event-context classification matched"


def decide(
    row: dict[str, Any],
    state: dict[str, Any] | None,
    families: tuple[str, ...] | None,
    text_data: dict[str, str],
) -> dict[str, Any]:
    output_text = text_data.get("output_text") or str(row.get("current_text") or "")
    confirmed_text = text_data.get("confirmed_text") or output_text
    decision, subpolicy, rationale = classify(row, state, families, text_data)
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": text_data.get("source_key") or row.get("key") or "",
        "old_text": text_data.get("old_text") or "",
        "confirmed_text": confirmed_text,
        "output_text": output_text,
        "families_open": list(families or ()),
        "source_custom_loc_scope_decision": row.get("custom_loc_scope_decision"),
        "event_context_decision": decision,
        "event_subpolicy": subpolicy,
        "requires_lifecycle_later": decision in {
            "custom_loc_scope_event_ready_false_reopen",
            "custom_loc_scope_event_ready_lifecycle",
        },
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_semantic_custom_loc_scope_event_context_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def validate_results(results: list[dict[str, Any]], expected_total: int) -> None:
    required = {
        "segment_id",
        "relative_path",
        "source_key",
        "old_text",
        "confirmed_text",
        "output_text",
        "families_open",
        "source_custom_loc_scope_decision",
        "event_context_decision",
        "event_subpolicy",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "rationale",
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
        if row["source_custom_loc_scope_decision"] != SOURCE_DECISION:
            raise SystemExit(f"invalid source decision for segment_id={row['segment_id']}")
        if row["event_context_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for segment_id={row['segment_id']}: {row['event_context_decision']}")
        if row["requires_apply_later"]:
            raise SystemExit(f"unexpected apply candidate: segment_id={row['segment_id']}")
        corrected = row.get("corrected_text") or ""
        if corrected and CK3_TOKEN_RE.findall(corrected) != CK3_TOKEN_RE.findall(row["output_text"]):
            raise SystemExit(f"CK3 token mismatch in corrected_text: segment_id={row['segment_id']}")


def recommendation(decision_counts: Counter[str]) -> str:
    ready_count = decision_counts["custom_loc_scope_event_ready_false_reopen"] + decision_counts["custom_loc_scope_event_ready_lifecycle"]
    if ready_count >= 5:
        return "prepare_readonly_lifecycle_for_custom_loc_scope_event_ready"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_custom_loc_scope_event_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 5:
            return f"prepare_specific_policy_or_composer_for_{top_decision}"
    return "fragmented_return_to_dynamic_semantic_global_diagnostic"


def write_reports(results: list[dict[str, Any]], jsonl_path: Path, txt_path: Path) -> None:
    decision_counts = Counter(row["event_context_decision"] for row in results)
    subpolicy_counts = Counter(row["event_subpolicy"] for row in results)
    ready_count = sum(1 for row in results if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in results if row["requires_apply_later"])
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("dynamic + semantic custom loc scope event context review\n")
        handle.write(f"total_reviewed: {len(results)}\n")
        handle.write(f"future_lifecycle_ready: {ready_count}\n")
        handle.write(f"future_apply_candidates: {apply_count}\n")
        handle.write("\nevent_context_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nevent_subpolicy_counts:\n")
        for subpolicy, count in subpolicy_counts.most_common():
            handle.write(f"- {subpolicy}: {count}\n")
        handle.write(f"\nrecommendation: {recommendation(decision_counts)}\n")
        handle.write("\nprohibited_actions: none; no lifecycle, apply, segment-state, confirmations, reindex, training, source/output changes\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only custom loc scope event-context review.")
    parser.add_argument("--scope-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(args.scope_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    families = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)
    texts = fetch_texts(conn, segment_ids)
    results = [
        decide(row, states.get(int(row["segment_id"])), families.get(int(row["segment_id"])), texts.get(int(row["segment_id"]), {}))
        for row in source
    ]
    validate_results(results, expected_total=len(source))

    jsonl_path, txt_path = output_paths()
    write_reports(results, jsonl_path, txt_path)
    decision_counts = Counter(row["event_context_decision"] for row in results)
    print(f"jsonl: {jsonl_path}")
    print(f"txt: {txt_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"future_lifecycle_ready: {sum(1 for row in results if row['requires_lifecycle_later'])}")
    print(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}")
    print("event_context_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
