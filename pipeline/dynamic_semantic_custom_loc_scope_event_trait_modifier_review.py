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


SOURCE_DECISION = "needs_custom_loc_scope_event_trait_modifier_policy"
TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "semantic_review_router")

ALLOWED_DECISIONS = {
    "needs_custom_loc_event_trait_policy",
    "needs_custom_loc_event_modifier_policy",
    "needs_custom_loc_event_accolade_policy",
    "needs_custom_loc_event_character_state_policy",
    "needs_custom_loc_event_descriptor_lexical_policy",
    "needs_custom_loc_event_scope_actor_target_policy",
    "needs_custom_loc_event_local_player_policy",
    "needs_custom_loc_event_residual_repair",
    "custom_loc_event_trait_modifier_ready_false_reopen",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
TRAIT_RE = re.compile(r"GetTrait|trait|education|personality|congenit|commander", re.IGNORECASE)
MODIFIER_RE = re.compile(r"GetModifier|modifier|bonus|malus|opinion modifier|character modifier|state modifier", re.IGNORECASE)
ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|title of honor|knight", re.IGNORECASE)
CHARACTER_STATE_RE = re.compile(
    r"alive|dead|prisoner|sick|travel|host|guest|participant|ally|enemy|consort|vassal|"
    r"vivo|morto|prisioneiro|doente|viajando|anfitri|convidado|participante|aliado|inimigo|consorte|vassalo",
    re.IGNORECASE,
)
DESCRIPTOR_RE = re.compile(
    r"ArtifactAdverb|ArtifactBookContentQuality|ArtifactWealth|HornedMythicalCreature|"
    r"quality|wealth|adverb|mythical|creature|qualidade|riqueza|mitol[oó]gico|descritor|adjetivo|ep[ií]teto",
    re.IGNORECASE,
)
ACTOR_TARGET_RE = re.compile(r"\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|target|root|from|scope|recipient)\b", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"\b(?:você|vocês|seu|sua|seus|suas|meu|minha|eu|me|mim)\b", re.IGNORECASE)
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


def source_rows(event_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(event_jsonl):
        if row.get("event_context_decision") != SOURCE_DECISION:
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


def classify(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> tuple[str, str, str]:
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or output_text)
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))

    if not state_ready(state):
        return "blocked_uncertain", "not_pending_confirmed", "state guard failed in selected segment-state run"
    if not exact_family_shape(families):
        return "blocked_uncertain", "family_shape_guard", "open issue families are no longer exactly dynamic + semantic"
    if not output_text.strip() or not token_boundaries_ok(output_text):
        return "blocked_uncertain", "broken_or_missing_text", "missing text or malformed token boundary"
    if has_bad_encoding(output_text) or RESIDUAL_RE.search(output_text):
        return "needs_custom_loc_event_residual_repair", "trait_modifier_residual_repair", "visible residual remains, but this review does not apply repairs"
    if LOCAL_PLAYER_RE.search(output_text):
        return "needs_custom_loc_event_local_player_policy", "trait_modifier_local_player", "item depends on local player, direct address, possessive, or pronoun perspective"
    if TRAIT_RE.search(haystack):
        return "needs_custom_loc_event_trait_policy", "trait_custom_loc_event", "item depends on CK3 trait or trait localization"
    if MODIFIER_RE.search(haystack):
        return "needs_custom_loc_event_modifier_policy", "modifier_custom_loc_event", "item depends on modifier or bonus/malus localization"
    if ACCOLADE_RE.search(haystack):
        return "needs_custom_loc_event_accolade_policy", "accolade_custom_loc_event", "item depends on accolade or acclaimed knight terminology"
    if CHARACTER_STATE_RE.search(haystack):
        return "needs_custom_loc_event_character_state_policy", "character_state_custom_loc_event", "item depends on character state or event role"
    if DESCRIPTOR_RE.search(haystack):
        return "needs_custom_loc_event_descriptor_lexical_policy", "descriptor_lexical_custom_loc_event", "item depends on short dynamic descriptor/adjective/quality wording"
    if ACTOR_TARGET_RE.search(output_text):
        return "needs_custom_loc_event_scope_actor_target_policy", "scope_actor_target_custom_loc_event", "item still depends mainly on actor/target/root/scope"
    if confirmed_text == output_text and int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return "custom_loc_event_trait_modifier_ready_false_reopen", "trait_modifier_ready_false_reopen", "confirmed/output is aligned and looks like a governable false reopen"
    return "blocked_uncertain", "uncertain_trait_modifier", "insufficient local context for a safe narrower classification"


def decide(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> dict[str, Any]:
    decision, subpolicy, rationale = classify(row, state, families)
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row.get("source_key") or "",
        "old_text": row.get("old_text") or "",
        "confirmed_text": row.get("confirmed_text") or "",
        "output_text": row.get("output_text") or "",
        "families_open": list(families or ()),
        "source_event_context_decision": row.get("event_context_decision"),
        "trait_modifier_decision": decision,
        "trait_modifier_subpolicy": subpolicy,
        "requires_lifecycle_later": decision == "custom_loc_event_trait_modifier_ready_false_reopen",
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_semantic_custom_loc_scope_event_trait_modifier_review"
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
        "source_event_context_decision",
        "trait_modifier_decision",
        "trait_modifier_subpolicy",
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
        if row["source_event_context_decision"] != SOURCE_DECISION:
            raise SystemExit(f"invalid source decision for segment_id={row['segment_id']}")
        if row["trait_modifier_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for segment_id={row['segment_id']}: {row['trait_modifier_decision']}")
        if row["requires_apply_later"]:
            raise SystemExit(f"unexpected apply candidate: segment_id={row['segment_id']}")
        if row.get("corrected_text"):
            raise SystemExit(f"corrected_text must stay empty in this review: segment_id={row['segment_id']}")


def recommendation(decision_counts: Counter[str]) -> str:
    if decision_counts["custom_loc_event_trait_modifier_ready_false_reopen"] >= 5:
        return "prepare_readonly_lifecycle_for_trait_modifier_false_reopen"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_custom_loc_event_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 4:
            return f"record_policy_and_close_branch_for_{top_decision}"
    return "branch_classified_return_to_custom_loc_scope_or_global_diagnostic"


def write_reports(results: list[dict[str, Any]], jsonl_path: Path, txt_path: Path) -> None:
    decision_counts = Counter(row["trait_modifier_decision"] for row in results)
    subpolicy_counts = Counter(row["trait_modifier_subpolicy"] for row in results)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("dynamic + semantic custom loc event trait/modifier review\n")
        handle.write(f"total_reviewed: {len(results)}\n")
        handle.write(f"future_lifecycle_ready: {sum(1 for row in results if row['requires_lifecycle_later'])}\n")
        handle.write(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}\n")
        handle.write("\ntrait_modifier_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\ntrait_modifier_subpolicy_counts:\n")
        for subpolicy, count in subpolicy_counts.most_common():
            handle.write(f"- {subpolicy}: {count}\n")
        handle.write(f"\nrecommendation: {recommendation(decision_counts)}\n")
        handle.write("\nprohibited_actions: none; no lifecycle, apply, segment-state, confirmations, reindex, training, source/output changes\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only custom loc event trait/modifier review.")
    parser.add_argument("--event-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(args.event_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    families = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)
    results = [decide(row, states.get(int(row["segment_id"])), families.get(int(row["segment_id"]))) for row in source]
    validate_results(results, expected_total=len(source))

    jsonl_path, txt_path = output_paths()
    write_reports(results, jsonl_path, txt_path)
    decision_counts = Counter(row["trait_modifier_decision"] for row in results)
    print(f"jsonl: {jsonl_path}")
    print(f"txt: {txt_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"future_lifecycle_ready: {sum(1 for row in results if row['requires_lifecycle_later'])}")
    print(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}")
    print("trait_modifier_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
