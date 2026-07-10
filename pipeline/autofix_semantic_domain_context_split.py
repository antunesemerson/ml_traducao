from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


REVIEW_SOURCE_BUCKET = "review_needs_domain_context"
DYNAMIC_SOURCE_BUCKET = "dynamic_ck3_domain_entity_context"
ALLOWED_DECISIONS = {
    "domain_ready_ck3_entity_false_reopen",
    "domain_ready_title_or_law_false_reopen",
    "domain_ready_named_concept_false_reopen",
    "needs_title_policy",
    "needs_law_policy",
    "needs_culture_policy",
    "needs_religion_policy",
    "needs_name_or_nickname_policy",
    "needs_artifact_or_activity_policy",
    "needs_concept_expression_policy",
    "needs_dynamic_expression_agent",
    "needs_context_composer",
    "needs_semantic_review",
    "needs_residual_repair",
    "blocked_uncertain",
}

CONCEPT_RE = re.compile(
    r"\[[A-Za-z0-9_.'()|]+\|[A-Za-z]*E\]|\$game_concept[^$]+\$|\bConcept\s*\(",
    re.IGNORECASE,
)
DYNAMIC_RE = re.compile(
    r"\[[^\]]*\bGet[A-Za-z_]*[^\]]*\]|\bSelect_CString\s*\(|\bCustom\s*\(|\bScriptValue\b|\bGetTrait\b"
)
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|caballero|"
    r"probabilidad)\b",
    re.IGNORECASE,
)
TOKEN_BOUNDARY_RE = re.compile(r"\[[^\]]*$|^[^\[]*\]")

RELIGION_RE = re.compile(
    r"faith|religion|holy_order|piety|clero|pag[aã]os|fé|religião|doutrina|tenet|devoted|holy_warrior",
    re.IGNORECASE,
)
CULTURE_RE = re.compile(r"culture|heritage|ethos|innovation|tradition|cultura|tradição", re.IGNORECASE)
LAW_RE = re.compile(
    r"law|succession|gender_law|authority|war|wars_|cb_|casus|revoke|tribut|reparaç|claim|claims",
    re.IGNORECASE,
)
TITLE_RE = re.compile(
    r"title|titles|tier|rank|empire|duchy|county|reino|títulos?|ducado|condado|liege|vassal|de_jure",
    re.IGNORECASE,
)
ARTIFACT_ACTIVITY_RE = re.compile(
    r"artifact|activity|activities/|journey|roaming|tournament|accolade|acclaimed_knight|knights",
    re.IGNORECASE,
)
NAME_NICK_RE = re.compile(
    r"trait_|lifestyle_|nick|nickname|dynasty|house|name|GetName|GetShortUIName|"
    r"GetTitleAsName|personagem|Sombra|Lâmina|Filósofo|Temerário|Festeiro|Soldado",
    re.IGNORECASE,
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at line {line_number}: {exc}") from exc
    return rows


def fetch_state_rows(conn, state_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            state_group,
            needs_output_apply,
            confirmed_matches_output,
            needs_reopen,
            final_state,
            is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (state_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def clean_state(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
    )


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, bool, str]:
    text = as_text(row.get("current_text"))
    relative_path = as_text(row.get("relative_path"))
    key = as_text(row.get("key"))
    haystack = f"{relative_path} {key} {text}"

    if not clean_state(state):
        return (
            "blocked_uncertain",
            "state_guard_not_clean",
            False,
            "segment is not pending/aligned in the requested segment-state run",
        )
    if text.count("[") != text.count("]") or text.count("$") % 2 or TOKEN_BOUNDARY_RE.search(text):
        return (
            "needs_residual_repair",
            "token_boundary_or_markup",
            False,
            "token or markup boundary is not safely balanced",
        )
    if RESIDUAL_RE.search(text):
        return (
            "needs_residual_repair",
            "visible_language_or_fluency_residual",
            False,
            "visible residual marker or terminology issue blocks domain-ready closure",
        )
    if CONCEPT_RE.search(text):
        return (
            "needs_concept_expression_policy",
            "named_concept_or_bracket_expression",
            False,
            "CK3 concept expression needs dedicated concept policy",
        )
    if DYNAMIC_RE.search(text):
        if RELIGION_RE.search(haystack):
            return (
                "needs_religion_policy",
                "religion_dynamic_entity",
                False,
                "religion/faith dynamic entity needs domain policy",
            )
        if LAW_RE.search(haystack):
            return (
                "needs_law_policy",
                "war_claim_or_rule_dynamic_entity",
                False,
                "war/claim/rule dynamic entity needs law policy",
            )
        if TITLE_RE.search(haystack):
            return (
                "needs_title_policy",
                "title_rank_or_liege_dynamic_entity",
                False,
                "title/rank/liege dynamic entity needs title policy",
            )
        return (
            "needs_dynamic_expression_agent",
            "dynamic_expression_outside_domain_policy",
            False,
            "dynamic expression is not covered by a simple domain policy",
        )
    if RELIGION_RE.search(haystack):
        return (
            "needs_religion_policy",
            "faith_religion_or_clergy",
            False,
            "faith/religion/clergy terminology needs a dedicated policy",
        )
    if CULTURE_RE.search(haystack):
        return (
            "needs_culture_policy",
            "culture_tradition_or_ethos",
            False,
            "culture/tradition terminology needs a dedicated policy",
        )
    if LAW_RE.search(haystack):
        return (
            "needs_law_policy",
            "war_claim_or_rule",
            False,
            "war/claim/rule terminology needs a dedicated policy",
        )
    if TITLE_RE.search(haystack):
        return (
            "needs_title_policy",
            "landed_title_rank_or_feudal_term",
            False,
            "title/rank/feudal terminology needs a dedicated policy",
        )
    if ARTIFACT_ACTIVITY_RE.search(haystack):
        return (
            "needs_artifact_or_activity_policy",
            "accolade_activity_or_knight_term",
            False,
            "accolade/activity/knight terminology needs a dedicated policy",
        )
    if NAME_NICK_RE.search(haystack):
        return (
            "needs_name_or_nickname_policy",
            "trait_name_nickname_or_character_entity",
            False,
            "trait/name/nickname entity needs a naming policy before closure",
        )
    return (
        "needs_semantic_review",
        "domain_context_unclear_semantic_review",
        False,
        "domain-sensitive text is too broad for an automatic ready decision",
    )


def load_domain_rows(review_path: Path, dynamic_path: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    review_rows = read_jsonl(review_path)
    dynamic_rows = read_jsonl(dynamic_path)
    selected: list[dict[str, Any]] = []
    raw_counts: Counter[str] = Counter()

    for row in review_rows:
        if row.get("decision") == "needs_domain_context":
            raw_counts[REVIEW_SOURCE_BUCKET] += 1
            item = dict(row)
            item["source_bucket"] = REVIEW_SOURCE_BUCKET
            selected.append(item)
    for row in dynamic_rows:
        if row.get("dynamic_decision") == "needs_domain_context":
            raw_counts[DYNAMIC_SOURCE_BUCKET] += 1
            item = dict(row)
            item["source_bucket"] = DYNAMIC_SOURCE_BUCKET
            selected.append(item)

    deduped: dict[int, dict[str, Any]] = {}
    for row in selected:
        segment_id = int(row["segment_id"])
        if segment_id in deduped:
            deduped[segment_id]["source_bucket"] = (
                deduped[segment_id]["source_bucket"] + "+" + row["source_bucket"]
            )
        else:
            deduped[segment_id] = row
    return list(deduped.values()), raw_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-jsonl", required=True)
    parser.add_argument("--dynamic-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    review_path = Path(args.review_jsonl)
    dynamic_path = Path(args.dynamic_jsonl)
    rows, raw_counts = load_domain_rows(review_path, dynamic_path)
    segment_ids = [int(row["segment_id"]) for row in rows]

    conn = db.connect()
    state_rows = fetch_state_rows(conn, args.segment_state_run_id, segment_ids)
    conn.close()

    reviewed: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        decision, subpolicy, lifecycle_later, notes = classify(row, state_rows.get(segment_id))
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"Internal classifier emitted invalid decision: {decision}")
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row.get("key", ""),
                "relative_path": row.get("relative_path", ""),
                "current_text": row.get("current_text", ""),
                "source_bucket": row.get("source_bucket", ""),
                "domain_decision": decision,
                "domain_subpolicy": subpolicy,
                "requires_lifecycle_later": lifecycle_later,
                "requires_apply_later": False,
                "corrected_text": "",
                "notes": notes,
            }
        )

    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_domain_context_split.jsonl"
    txt_path = reports_dir / f"{stamp}_autofix_semantic_domain_context_split.txt"

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decisions = Counter(row["domain_decision"] for row in reviewed)
    subpolicies = Counter(row["domain_subpolicy"] for row in reviewed)
    ready_count = sum(count for decision, count in decisions.items() if decision.startswith("domain_ready_"))
    raw_total = sum(raw_counts.values())
    duplicate_count = raw_total - len(reviewed)

    lines = [
        "Autofix semantic domain context split",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"review_jsonl: {review_path}",
        f"dynamic_jsonl: {dynamic_path}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        "",
        "Raw source counts:",
    ]
    for source, count in raw_counts.items():
        lines.append(f"- {source}: {count}")
    lines.extend(
        [
            f"raw_total: {raw_total}",
            f"deduplicated_reviewed: {len(reviewed)}",
            f"duplicates_removed: {duplicate_count}",
            f"domain_ready_count: {ready_count}",
            "apply_candidates_future: 0",
            "",
            "Counts by domain_decision:",
        ]
    )
    for decision, count in decisions.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Top domain subpolicies:"])
    for subpolicy, count in subpolicies.most_common():
        lines.append(f"- {subpolicy}: {count}")
    lines.extend(["", "Recommendation:"])
    if ready_count >= 10:
        lines.append("- Prepare a narrow read-only lifecycle for domain_ready_* false reopens.")
    else:
        policy_counts = {
            decision: count
            for decision, count in decisions.items()
            if decision.startswith("needs_") and decision.endswith("_policy")
        }
        if policy_counts and max(policy_counts.values()) >= 10:
            top_policy, top_count = Counter(policy_counts).most_common(1)[0]
            lines.append(f"- Prepare a specific microagent/policy for {top_policy} ({top_count}).")
        else:
            lines.append("- Domain cases are pulverized; return to semantic_review_router + autofix_unknown_microagent with a new batch.")
    lines.extend(
        [
            "",
            "Safety:",
            "- read-only split only",
            "- no apply",
            "- no lifecycle",
            "- no segment-state run",
            "- no confirmations",
            "- no source/output writes",
            "",
            f"JSONL: {jsonl_path}",
            f"TXT: {txt_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")
    print(f"raw_counts={json.dumps(raw_counts, ensure_ascii=False, sort_keys=True)}")
    print(f"raw_total={raw_total}")
    print(f"deduplicated_reviewed={len(reviewed)}")
    print(f"duplicates_removed={duplicate_count}")
    print(f"domain_ready_count={ready_count}")
    print("domain_decisions=" + json.dumps(decisions, ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(subpolicies.most_common(), ensure_ascii=False))


if __name__ == "__main__":
    main()
