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


TARGET_SOURCE_DECISION = "needs_concept_domain_policy"

TITLE_LAW_RE = re.compile(
    r"title|rank|law|succession|government|governance|crown|authority|war|tyranny|tyrannical|"
    r"liege|vassal|faction|councillor|council|revocation|retraction|hostage|debt|claim|realm|"
    r"titulo|título|lei|sucess|governo|guerra|tirania|vassalo|suserano",
    re.IGNORECASE,
)
CULTURE_RE = re.compile(
    r"culture|tradition|innovation|heritage|ethos|language|cultural|cultura|tradi[cç][aã]o|inova[cç][aã]o|heran[cç]a",
    re.IGNORECASE,
)
RELIGION_RE = re.compile(
    r"religion|faith|piety|doctrine|tenet|clergy|zeal|cynic|devot|holy|pilgrim|sin|virtue|"
    r"religião|f[eé]|piedade|doutrina|clero|sagrado",
    re.IGNORECASE,
)
NAME_DYNASTY_RE = re.compile(
    r"historical_characters|character|dynasty|house|nickname|name|epithet|personagem|dinastia|casa|apelido|nome",
    re.IGNORECASE,
)
ARTIFACT_ACTIVITY_RE = re.compile(
    r"artifact|court_artifact|activity|activities|travel|journey|tour|tournament|accolade|knight|legend|inspiration|"
    r"artefato|atividade|viagem|torneio|lenda|inspira[cç][aã]o",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc$|desc\.|option|toast|story_cycle|scheme|interaction|memory|memories|"
    r"evento|mem[oó]ria|esquema|intera[cç][aã]o",
    re.IGNORECASE,
)
DYNAMIC_RE = re.compile(
    r"Select_CString|Custom\(|ScriptValue|GetScriptValue|GetTrait|Get[A-Za-z0-9_]*|ROOT\.|CHARACTER\.|TARGET\.|SCOPE\.",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals)\b|"
    r"\b(una|uno|verdadero|verdadera|fuerza|probabilidad|improbable|mientras|alrededor)\b",
    re.IGNORECASE,
)
CUSTOM_GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)|Custom\(|custom_loc",
    re.IGNORECASE,
)
CONCEPT_TOKEN_RE = re.compile(r"\[[^\]|]+[|][^\]]+\]|\$[^$]*concept[^$]*\$", re.IGNORECASE)


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
        SELECT
            segment_id,
            final_state,
            state_group,
            needs_reopen,
            needs_output_apply,
            confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def ready_state(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
    )


def false_reopen_state(state: dict[str, Any] | None) -> bool:
    return bool(
        ready_state(state)
        and state
        and state.get("final_state") == "reopen_auto_confirmed_autofix"
        and int(state.get("needs_reopen") or 0) == 1
    )


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    domain_hits = {
        "title_law": bool(TITLE_LAW_RE.search(haystack)),
        "culture": bool(CULTURE_RE.search(haystack)),
        "religion": bool(RELIGION_RE.search(haystack)),
        "name_dynasty": bool(NAME_DYNASTY_RE.search(haystack)),
        "artifact_activity": bool(ARTIFACT_ACTIVITY_RE.search(haystack)),
    }
    hit_count = sum(1 for value in domain_hits.values() if value)
    has_concept = bool(CONCEPT_TOKEN_RE.search(text))
    clean_ready = (
        ready_state(state)
        and has_concept
        and text.count("[") == text.count("]")
        and text.count("$") % 2 == 0
        and not CUSTOM_GENDER_RE.search(haystack)
        and not RESIDUAL_RE.search(text)
        and not EVENT_RE.search(haystack)
    )

    if false_reopen_state(state) and clean_ready and hit_count == 0:
        decision = "concept_domain_ready_false_reopen"
        subpolicy = "domain_named_concept_false_reopen"
    elif clean_ready and hit_count == 0:
        decision = "concept_domain_ready_named_concept_lifecycle"
        subpolicy = "domain_named_concept_lifecycle"
    elif CUSTOM_GENDER_RE.search(haystack) or DYNAMIC_RE.search(haystack):
        decision = "needs_dynamic_expression_agent"
        subpolicy = "domain_concept_with_uncleared_dynamic_expression"
    elif hit_count > 1:
        decision = "needs_concept_mixed_domain_semantic_review"
        subpolicy = "mixed_domain_concept"
    elif domain_hits["religion"]:
        decision = "needs_concept_religion_policy"
        subpolicy = "religion_or_doctrine_concept"
    elif domain_hits["culture"]:
        decision = "needs_concept_culture_policy"
        subpolicy = "culture_or_tradition_concept"
    elif domain_hits["name_dynasty"]:
        decision = "needs_concept_name_or_dynasty_policy"
        subpolicy = "name_dynasty_or_character_concept"
    elif domain_hits["artifact_activity"]:
        decision = "needs_concept_artifact_or_activity_policy"
        subpolicy = "artifact_activity_or_legend_concept"
    elif domain_hits["title_law"]:
        decision = "needs_concept_title_or_law_policy"
        subpolicy = "title_law_government_or_realm_concept"
    elif EVENT_RE.search(haystack):
        decision = "needs_event_context_composer"
        subpolicy = "domain_concept_with_event_context"
    elif RESIDUAL_RE.search(text):
        decision = "needs_residual_repair"
        subpolicy = "domain_concept_with_visible_residual"
    else:
        decision = "blocked_uncertain"
        subpolicy = "unclassified_domain_concept"

    requires_lifecycle = decision.startswith("concept_domain_ready_")
    return {
        "concept_domain_decision": decision,
        "concept_domain_subpolicy": subpolicy,
        "requires_lifecycle_later": requires_lifecycle,
        "requires_apply_later": False,
        "notes": (
            "domain concept appears aligned and suitable for future narrow lifecycle"
            if requires_lifecycle
            else f"route to {decision} before lifecycle closure"
        ),
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_semantic_concept_domain_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["concept_domain_decision"] for row in rows)
    subpolicy_counts = Counter(row["concept_domain_subpolicy"] for row in rows)
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("concept_domain_ready_"))
    policy_concentrations = [
        (key, count)
        for key, count in subpolicy_counts.most_common()
        if key.startswith(("religion_", "culture_", "name_", "artifact_", "title_", "mixed_"))
    ]
    recommendation = (
        "prepare_narrow_concept_domain_lifecycle"
        if ready_total >= 10
        else f"prepare_specific_policy:{policy_concentrations[0][0]}"
        if policy_concentrations and policy_concentrations[0][1] >= 20
        else "migrate_to_needs_concept_effect_list_policy_or_next_bottleneck"
    )
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Autofix + semantic concept domain policy review",
        f"reviewed: {len(rows):,}",
        "",
        "Decision counts:",
    ]
    if decision_counts:
        for key, count in decision_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Top domain subpolicies:"])
    if subpolicy_counts:
        for key, count in subpolicy_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", f"concept_domain_ready_total: {ready_total:,}", f"Recommendation: {recommendation}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concept-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    concept_rows = [
        row
        for row in read_jsonl(db.project_path(args.concept_jsonl))
        if row.get("concept_decision") == TARGET_SOURCE_DECISION
    ]
    segment_ids = [int(row["segment_id"]) for row in concept_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    reviewed: list[dict[str, Any]] = []
    for row in concept_rows:
        segment_id = int(row["segment_id"])
        decision = decide(row, states.get(segment_id))
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_concept_decision": row["concept_decision"],
                **decision,
            }
        )
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready_total = sum(count for key, count in decision_counts.items() if key.startswith("concept_domain_ready_"))
    print(f"reviewed={len(reviewed)}")
    print(f"concept_domain_ready={ready_total}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("top_subpolicies=" + json.dumps(dict(subpolicy_counts.most_common(10)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
