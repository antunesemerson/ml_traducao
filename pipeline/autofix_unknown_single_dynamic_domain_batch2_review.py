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


TARGET_FAMILY = "autofix_unknown_microagent"

ALLOWED_DECISIONS = {
    "single_dynamic_domain_ready_false_reopen",
    "single_dynamic_domain_ready_lifecycle",
    "needs_single_dynamic_title_law_policy",
    "needs_single_dynamic_religion_policy",
    "needs_single_dynamic_culture_policy",
    "needs_single_dynamic_name_nickname_policy",
    "needs_single_dynamic_trait_epithet_policy",
    "needs_single_dynamic_artifact_activity_policy",
    "needs_single_dynamic_place_building_policy",
    "needs_single_dynamic_concept_domain_policy",
    "needs_single_dynamic_uncleared_expression_policy",
    "needs_single_dynamic_event_context_composer",
    "needs_single_dynamic_residual_repair",
    "needs_single_dynamic_mixed_domain_policy",
    "needs_new_microagent",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
CONCEPT_TOKEN_RE = re.compile(r"\[[A-Za-z0-9_]+(?:\|[^\]]+)?\]|\$[A-Za-z0-9_.:-]+\$", re.IGNORECASE)
UNCLEARED_DYNAMIC_RE = re.compile(r"\[[^\]]*\.(?:Get|Is|Has)[^\]]*\]|TaskContract\.|ROOT\.|CHARACTER\.|TARGET\.", re.IGNORECASE)
TITLE_LAW_RE = re.compile(
    r"vassal|courtier|vassal_contract|council|kurultai|tributary|suzerain|realm|government|law|succession|emperor|war|ransom",
    re.IGNORECASE,
)
RELIGION_RE = re.compile(r"faith|piety|clergy|doctrine|tenet|religion|converted|convert|new_faith|old_faith", re.IGNORECASE)
CULTURE_RE = re.compile(r"culture|tradition|innovation|ethos|heritage|Sajonia", re.IGNORECASE)
NAME_RE = re.compile(r"Get(?:FirstName|TitledFirstName|FullName|ShortUIName|Name)|nickname|dynasty|house", re.IGNORECASE)
TRAIT_RE = re.compile(r"trait|education_trait|prowess|personality|accolade|knight|descriptor", re.IGNORECASE)
ARTIFACT_ACTIVITY_RE = re.compile(r"artifact|court_artifact|activity|travel|tournament|legend|itinerary|map|hunt|falcon|animal|pet", re.IGNORECASE)
PLACE_BUILDING_RE = re.compile(r"domicile|building|estate|county|terrain|holding|barony|court|yurt|location|GetLocation", re.IGNORECASE)
EVENT_RE = re.compile(r"event_localization|\.desc|desc\.|opening|story_cycles|yearly|interaction|dialog|\.tt$|_tt$", re.IGNORECASE)
RESIDUAL_RE = re.compile(
    r"\b(?:Pres[eé]ntate|decisi[oó]n|seg[uú]n|conceder[aá]|educados|Probabilidad|menos probable|personajes|situados|Está opción|Ofrécete|ning[uú]n|autom[aá]ticamente)\b",
    re.IGNORECASE,
)


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


def collect_source_rows(path_value: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(path_value)):
        if row.get("dynamic_decision") != "needs_single_dynamic_domain_context":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


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


def fetch_family_counts(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id,
               COUNT(*) AS open_count,
               SUM(CASE WHEN issue_family = ? THEN 1 ELSE 0 END) AS target_count,
               SUM(CASE WHEN issue_family != ? THEN 1 ELSE 0 END) AS other_count
        FROM ml_issue_ledger_items
        WHERE run_id = 76
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (TARGET_FAMILY, TARGET_FAMILY, *segment_ids),
    ).fetchall()
    return {
        int(row["segment_id"]): (int(row["open_count"]), int(row["target_count"] or 0), int(row["other_count"] or 0))
        for row in rows
    }


def state_is_pending_confirmed(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def family_is_exact(family_counts: tuple[int, int, int] | None) -> bool:
    return family_counts == (1, 1, 0)


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Concept", CONCEPT_TOKEN_RE),
        ("UnclearedDynamic", UNCLEARED_DYNAMIC_RE),
        ("TitleLaw", TITLE_LAW_RE),
        ("Religion", RELIGION_RE),
        ("Culture", CULTURE_RE),
        ("Name", NAME_RE),
        ("Trait", TRAIT_RE),
        ("ArtifactActivity", ARTIFACT_ACTIVITY_RE),
        ("PlaceBuilding", PLACE_BUILDING_RE),
        ("CK3DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def classify(row: dict[str, Any], state: dict[str, Any] | None, family_counts: tuple[int, int, int] | None) -> tuple[str, str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])

    if row.get("dynamic_decision") != "needs_single_dynamic_domain_context":
        return "blocked_uncertain", "unexpected_source_branch", "source row is not needs_single_dynamic_domain_context"
    if not state_is_pending_confirmed(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "not eligible in selected segment-state run"
    if not family_is_exact(family_counts):
        return "blocked_uncertain", "not_single_autofix_family", "ledger no longer has exactly one autofix_unknown open family"
    if text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return "needs_single_dynamic_residual_repair", "broken_dynamic_token_boundary", "dynamic token boundary looks malformed"

    domain_hits = [
        ("title_law", TITLE_LAW_RE.search(haystack)),
        ("religion", RELIGION_RE.search(haystack)),
        ("culture", CULTURE_RE.search(haystack)),
        ("name", NAME_RE.search(haystack)),
        ("trait", TRAIT_RE.search(haystack)),
        ("artifact_activity", ARTIFACT_ACTIVITY_RE.search(haystack)),
        ("place_building", PLACE_BUILDING_RE.search(haystack)),
    ]
    hit_names = [name for name, hit in domain_hits if hit]

    if EVENT_RE.search(haystack) and (NAME_RE.search(text) or len(text) > 140):
        return "needs_single_dynamic_event_context_composer", "event_context_domain", "event text mixes domain tokens with contextual prose"
    if UNCLEARED_DYNAMIC_RE.search(text) and not CONCEPT_TOKEN_RE.fullmatch(text.strip()):
        if PLACE_BUILDING_RE.search(haystack) or TITLE_LAW_RE.search(haystack):
            return "needs_single_dynamic_uncleared_expression_policy", "uncleared_scope_getter_domain", "scope/getter expression remains the real driver"
    if len(set(hit_names)) >= 2:
        return "needs_single_dynamic_mixed_domain_policy", "mixed_domain_sensitive", "multiple sensitive domain types appear in one segment"
    if RESIDUAL_RE.search(text):
        return "needs_single_dynamic_residual_repair", "visible_spanish_residual", "visible Spanish residual remains, but no apply in this review"
    if TITLE_LAW_RE.search(haystack):
        return "needs_single_dynamic_title_law_policy", "title_law_government", "title/law/government domain needs dedicated policy"
    if RELIGION_RE.search(haystack):
        return "needs_single_dynamic_religion_policy", "religion_faith_doctrine", "religion or faith domain needs dedicated policy"
    if CULTURE_RE.search(haystack):
        return "needs_single_dynamic_culture_policy", "culture_tradition_innovation", "culture/tradition domain needs dedicated policy"
    if NAME_RE.search(haystack):
        return "needs_single_dynamic_name_nickname_policy", "name_character_dynamic", "name/person dynamic expression needs dedicated policy"
    if TRAIT_RE.search(haystack):
        return "needs_single_dynamic_trait_epithet_policy", "trait_epithet_descriptor", "trait or descriptor domain needs dedicated policy"
    if ARTIFACT_ACTIVITY_RE.search(haystack):
        return "needs_single_dynamic_artifact_activity_policy", "artifact_activity_travel", "artifact/activity domain needs dedicated policy"
    if PLACE_BUILDING_RE.search(haystack):
        return "needs_single_dynamic_place_building_policy", "place_building_holding", "place/building domain needs dedicated policy"
    if CONCEPT_TOKEN_RE.search(text):
        return "needs_single_dynamic_concept_domain_policy", "concept_domain_sensitive", "domain-sensitive concept expression needs dedicated policy"
    return "blocked_uncertain", "domain_uncertain", "domain route could not be classified safely"


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_counts: tuple[int, int, int] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state, family_counts)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_dynamic_decision": row["dynamic_decision"],
        "domain_decision": decision,
        "domain_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": decision in {"single_dynamic_domain_ready_false_reopen", "single_dynamic_domain_ready_lifecycle"},
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_unknown_single_dynamic_domain_batch2_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["domain_decision"] for row in rows)
    subpolicy_counts = Counter(row["domain_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])

    if ready_count >= 10:
        recommendation = "prepare_single_dynamic_domain_readonly_lifecycle"
    elif decision_counts["needs_single_dynamic_uncleared_expression_policy"] >= 15:
        recommendation = "return_to_expression_concept_scope_with_dedicated_prompt"
    else:
        needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_single_dynamic_")})
        if needs_counts and needs_counts.most_common(1)[0][1] >= 15:
            recommendation = f"prepare_specific_policy_or_microagent_for_{needs_counts.most_common(1)[0][0]}"
        else:
            recommendation = "fragmented_migrate_to_concept_expression_or_requirement_tooltip"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown single dynamic domain batch2 review",
        "",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_lifecycle_count: {ready_count}",
            f"future_apply_count: {apply_count}",
            f"recommendation: {recommendation}",
            "",
            "Safety: read-only review; no lifecycle, apply, segment-state, confirmations, production, reindex, training, source edits, or output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def validate_rows(rows: list[dict[str, Any]]) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_dynamic_decision",
        "domain_decision",
        "domain_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    seen: set[int] = set()
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        if row["segment_id"] in seen:
            raise SystemExit(f"duplicate segment_id: {row['segment_id']}")
        seen.add(row["segment_id"])
        if row["source_dynamic_decision"] != "needs_single_dynamic_domain_context":
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_dynamic_decision']}")
        if row["domain_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected domain decision for {row['segment_id']}: {row['domain_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {row['segment_id']}")
        if row["corrected_text"] and TOKEN_RE.findall(row["current_text"]) != TOKEN_RE.findall(row["corrected_text"]):
            raise SystemExit(f"token mismatch in corrected_text: {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.dynamic_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_counts = fetch_family_counts(conn, 76, segment_ids)

    reviewed = [
        decide(row, states.get(int(row["segment_id"])), family_counts.get(int(row["segment_id"])))
        for row in source_rows
    ]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"total_reviewed={len(reviewed)}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
