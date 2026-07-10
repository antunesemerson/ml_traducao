from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TARGET_NAME_POLICY_DECISION = "needs_trait_or_epithet_policy"
ALLOWED_DECISIONS = {
    "trait_epithet_ready_false_reopen",
    "trait_epithet_style_watch_lifecycle",
    "needs_trait_lexical_policy",
    "needs_epithet_lexical_policy",
    "needs_article_gender_policy",
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
ARTICLE_GENDER_RE = re.compile(
    r"\b(?:O|A|Os|As|o|a|os|as)\s+"
    r"(?:Temer[aá]rio|Soldado|Fil[oó]sofo|Aspirante|Sombra|L[aâ]mina|"
    r"Festeiro|Torturador|Gigante|Flagelante|Bandido|Poeta|Saqueador)\b"
)
EPITHET_KEY_RE = re.compile(
    r"trait_track_|trait_education_|trait_blademaster|trait_reckless|trait_berserker",
    re.IGNORECASE,
)
LEXICAL_WATCH_RE = re.compile(
    r"tosses|glamoroso|Maus humores|definhando|erguer o pescoço|"
    r"poder é lei|cortar mais fundo|Besta e cavaleiro|tendões do arqueiro",
    re.IGNORECASE,
)
STYLE_SAFE_KEYS = {
    "trait_torturer_desc",
    "trait_whole_of_body_desc",
    "trait_beauty_good_3_desc",
    "trait_shrewd_desc",
    "trait_scholar_desc",
    "trait_irritable_desc",
    "trait_flagellant_desc",
    "trait_lovers_pox_desc",
    "trait_athletic_desc",
    "lifestyle_gardener_desc",
    "lifestyle_herbalist_desc",
    "trait_infirm_desc",
}


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
    key = as_text(row.get("key"))

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
            "visible residual marker blocks trait/epithet closure",
        )
    if ARTICLE_GENDER_RE.search(text):
        return (
            "needs_article_gender_policy",
            "epithet_article_gender",
            False,
            "visible article/gender on named epithet needs policy",
        )
    if EPITHET_KEY_RE.search(key):
        return (
            "needs_epithet_lexical_policy",
            "named_epithet_or_track_lexical_choice",
            False,
            "named epithet/track wording needs lexical policy",
        )
    if LEXICAL_WATCH_RE.search(text):
        return (
            "needs_trait_lexical_policy",
            "trait_description_lexical_choice",
            False,
            "trait description contains a lexical choice that needs policy",
        )
    if key in STYLE_SAFE_KEYS:
        return (
            "trait_epithet_style_watch_lifecycle",
            "acceptable_trait_description_style_watch",
            True,
            "trait description is acceptable PT-BR but should close under a style-watch lifecycle",
        )
    if key.startswith("trait_") or key.startswith("lifestyle_"):
        return (
            "needs_trait_lexical_policy",
            "trait_description_lexical_choice",
            False,
            "trait description still needs explicit lexical policy before closure",
        )
    return (
        "needs_semantic_review",
        "trait_epithet_unclear_semantic_review",
        False,
        "trait/epithet-sensitive text is too broad for automatic closure",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name-policy-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    name_policy_path = Path(args.name_policy_jsonl)
    rows = [
        row
        for row in read_jsonl(name_policy_path)
        if row.get("name_policy_decision") == TARGET_NAME_POLICY_DECISION
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
                "source_name_policy_decision": row.get("name_policy_decision", ""),
                "trait_epithet_decision": decision,
                "trait_epithet_subpolicy": subpolicy,
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
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_trait_epithet_policy_review.jsonl"
    txt_path = reports_dir / f"{stamp}_autofix_semantic_trait_epithet_policy_review.txt"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decisions = Counter(row["trait_epithet_decision"] for row in reviewed)
    subpolicies = Counter(row["trait_epithet_subpolicy"] for row in reviewed)
    ready_or_style_count = sum(
        count
        for decision, count in decisions.items()
        if decision in {"trait_epithet_ready_false_reopen", "trait_epithet_style_watch_lifecycle"}
    )

    lines = [
        "Autofix semantic trait/epithet policy review",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"name_policy_jsonl: {name_policy_path}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"reviewed: {len(reviewed)}",
        f"ready_or_style_lifecycle_count: {ready_or_style_count}",
        "apply_candidates_future: 0",
        "",
        "Counts by trait_epithet_decision:",
    ]
    for decision, count in decisions.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Top trait/epithet subpolicies:"])
    for subpolicy, count in subpolicies.most_common():
        lines.append(f"- {subpolicy}: {count}")
    lines.extend(["", "Recommendation:"])
    if ready_or_style_count >= 10:
        lines.append("- Prepare a narrow read-only lifecycle for ready/style-watch trait/epithet decisions.")
    else:
        lexical_counts = {
            decision: count
            for decision, count in decisions.items()
            if decision in {"needs_trait_lexical_policy", "needs_epithet_lexical_policy"}
        }
        if lexical_counts and max(lexical_counts.values()) >= 10:
            top_policy, top_count = Counter(lexical_counts).most_common(1)[0]
            lines.append(f"- Prepare a specific lexical microagent for {top_policy} ({top_count}).")
        else:
            lines.append("- Close this subtrail and return to the global bottleneck.")
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
    print(f"ready_or_style_lifecycle_count={ready_or_style_count}")
    print("trait_epithet_decisions=" + json.dumps(decisions, ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(subpolicies.most_common(), ensure_ascii=False))


if __name__ == "__main__":
    main()
