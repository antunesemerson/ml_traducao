from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TARGET_DOMAIN_DECISION = "needs_name_or_nickname_policy"
ALLOWED_DECISIONS = {
    "name_policy_ready_character_name_false_reopen",
    "nickname_policy_ready_false_reopen",
    "trait_or_epithet_name_ready_false_reopen",
    "needs_nickname_gender_article_policy",
    "needs_character_name_policy",
    "needs_trait_or_epithet_policy",
    "needs_dynasty_house_name_policy",
    "needs_title_or_rank_name_policy",
    "needs_context_composer",
    "needs_semantic_review",
    "needs_residual_repair",
    "blocked_uncertain",
}

RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|caballero|"
    r"probabilidad)\b",
    re.IGNORECASE,
)
DYNAMIC_GENDER_RE = re.compile(
    r"Select_CString\s*\(|Custom\s*\(|ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|"
    r"Get(?:WomenMen|WomanMan|HerHis|SheHe|HerselfHimself|HerHim)\b",
    re.IGNORECASE,
)
DYNASTY_HOUSE_RE = re.compile(r"dynasty|house|dinastia|casa|fam[ií]lia", re.IGNORECASE)
TITLE_RANK_RE = re.compile(r"title|rank|tier|king|duke|count|comandante|soldado|mestre", re.IGNORECASE)
CHARACTER_NAME_RE = re.compile(r"GetName|GetShortUIName|GetFirstName|personagem", re.IGNORECASE)
TRAIT_PATH_RE = re.compile(r"traits_l_|trait_|lifestyle_", re.IGNORECASE)
EPITHET_ARTICLE_RE = re.compile(
    r"\b(?:O|A|Os|As|o|a|os|as)\s+"
    r"(?:Festeiro|Temer[aá]rio|Soldado|Fil[oó]sofo|Aspirante|Sombra|L[aâ]mina|"
    r"Torturador|Gigante|Flagelante|Bandido|Poeta|Saqueador|Jardineiro|Herborista)\b"
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


def state_is_clean(state: dict[str, Any] | None) -> bool:
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

    if not state_is_clean(state):
        return (
            "blocked_uncertain",
            "state_guard_not_clean",
            False,
            "segment is not pending/aligned in the requested segment-state run",
        )
    if text.count("[") != text.count("]") or text.count("$") % 2:
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
            "visible residual marker blocks name/nickname policy closure",
        )
    if DYNAMIC_GENDER_RE.search(text):
        return (
            "needs_nickname_gender_article_policy",
            "dynamic_gender_article",
            False,
            "dynamic gender/custom-loc token needs a dedicated article policy",
        )
    if DYNASTY_HOUSE_RE.search(haystack):
        return (
            "needs_dynasty_house_name_policy",
            "dynasty_house_family_name",
            False,
            "dynasty/house/family naming requires its own policy",
        )
    if EPITHET_ARTICLE_RE.search(text):
        return (
            "needs_nickname_gender_article_policy",
            "epithet_article_gender",
            False,
            "named trait/epithet uses visible article or gendered form",
        )
    if TITLE_RANK_RE.search(haystack) and re.search(r"\b(?:O|A|o|a)\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ]", text):
        return (
            "needs_title_or_rank_name_policy",
            "rank_or_title_like_epithet",
            False,
            "named epithet appears mixed with rank/title wording",
        )
    if TRAIT_PATH_RE.search(haystack):
        return (
            "needs_trait_or_epithet_policy",
            "trait_description_or_epithet_lexical_choice",
            False,
            "trait/epithet wording needs an explicit lexical policy before closure",
        )
    if CHARACTER_NAME_RE.search(haystack):
        return (
            "needs_character_name_policy",
            "character_entity_name_policy",
            False,
            "character/person naming needs a narrow name policy",
        )
    return (
        "needs_semantic_review",
        "name_policy_unclear_semantic_review",
        False,
        "name/nickname-sensitive text is too broad for automatic ready closure",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    domain_path = Path(args.domain_jsonl)
    rows = [
        row
        for row in read_jsonl(domain_path)
        if row.get("domain_decision") == TARGET_DOMAIN_DECISION
    ]
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
                "source_domain_decision": row.get("domain_decision", ""),
                "name_policy_decision": decision,
                "name_subpolicy": subpolicy,
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
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_name_nickname_policy_review.jsonl"
    txt_path = reports_dir / f"{stamp}_autofix_semantic_name_nickname_policy_review.txt"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decisions = Counter(row["name_policy_decision"] for row in reviewed)
    subpolicies = Counter(row["name_subpolicy"] for row in reviewed)
    ready_count = sum(count for decision, count in decisions.items() if "_ready_" in decision)

    lines = [
        "Autofix semantic name/nickname policy review",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"domain_jsonl: {domain_path}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"reviewed: {len(reviewed)}",
        f"ready_false_reopen_count: {ready_count}",
        "apply_candidates_future: 0",
        "",
        "Counts by name_policy_decision:",
    ]
    for decision, count in decisions.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Top name subpolicies:"])
    for subpolicy, count in subpolicies.most_common():
        lines.append(f"- {subpolicy}: {count}")
    lines.extend(["", "Recommendation:"])
    if ready_count >= 10:
        lines.append("- Prepare a narrow read-only lifecycle for *_ready_*_false_reopen decisions.")
    else:
        needs_counts = {
            decision: count
            for decision, count in decisions.items()
            if decision.startswith("needs_")
        }
        if needs_counts and max(needs_counts.values()) >= 10:
            top_policy, top_count = Counter(needs_counts).most_common(1)[0]
            lines.append(f"- Prepare a specific microagent/policy for {top_policy} ({top_count}).")
        else:
            lines.append("- Name/nickname cases are pulverized; return to the next global bottleneck.")
    lines.extend(
        [
            "",
            "Safety:",
            "- read-only review only",
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
    print(f"reviewed={len(reviewed)}")
    print(f"ready_false_reopen_count={ready_count}")
    print("name_policy_decisions=" + json.dumps(decisions, ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(subpolicies.most_common(), ensure_ascii=False))


if __name__ == "__main__":
    main()
