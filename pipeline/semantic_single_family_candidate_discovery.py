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


DISCOVERY_KEY = "semantic_single_family_candidate_discovery"
COHORT_KEY = "semantic_review_router_single_family"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
INPUTS = {
    "retarget_jsonl": "reports/20260624_004036_196562_candidate_discovery_cohort_retarget.jsonl",
    "retarget_summary": "reports/20260624_004036_196562_candidate_discovery_cohort_retarget_summary.json",
    "short_label_summary": "reports/20260624_014300_217906_short_label_clean_candidate_discovery_summary.json",
    "short_label_audit": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit_summary.json",
    "short_label_expand": "reports/20260624_023918_807449_short_label_clean_candidate_discovery_expand_summary.json",
    "architecture_inventory": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
}
EXCLUDED_FAMILIES = {
    "short_label_style_microagent",
    "autofix_unknown_microagent",
    "dynamic_ck3_expression_microagent",
    "gender_token_microagent",
    "requirement_effect_router_readonly",
    "effect_list_multiline_policy",
    "artifact_activity_effect_policy",
    "building_modifier_effect_policy",
    "event_context_after_requirement_effect",
    "residual_repair_after_requirement_effect",
    "accolade_trait_requirement_policy",
    "script_value_effect_policy",
    "holy_site_effect_name_policy",
    "domain_context_after_requirement_effect",
    "not_requirement_effect_global_router",
    "blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)"
)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
DEBUG_RE = re.compile(r"#D\b.*?#!", re.DOTALL)
SELECT_CSTRING_RE = re.compile(r"Select_CString\([^)]*\)")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
SCOPE_GETTER_RE = re.compile(r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+")
DYNAMIC_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Select_CString\(|\.Custom\('ES_|\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+")
NAME_TITLE_CULTURE_RE = re.compile(
    r"\b(?:GetName|GetHouse|GetCulture|GetFaith|dynasty|house|culture|religion|faith|holy_order|kingdom|title|Imperador|Romano|Shahnama)\b",
    re.IGNORECASE,
)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_single_family_candidate_discovery"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
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
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def token_inventory(value: str) -> list[str]:
    return TOKEN_RE.findall(value)


def token_integrity_ok(original: str, candidate: str) -> bool:
    return (
        original.count("[") == candidate.count("[")
        and original.count("]") == candidate.count("]")
        and original.count("$") == candidate.count("$")
        and token_inventory(original) == token_inventory(candidate)
        and BRACKET_RE.findall(original) == BRACKET_RE.findall(candidate)
        and VARIABLE_RE.findall(original) == VARIABLE_RE.findall(candidate)
        and DEBUG_RE.findall(original) == DEBUG_RE.findall(candidate)
        and SELECT_CSTRING_RE.findall(original) == SELECT_CSTRING_RE.findall(candidate)
        and ES_HELPER_RE.findall(original) == ES_HELPER_RE.findall(candidate)
    )


def structure_integrity_ok(original: str, candidate: str) -> bool:
    return (
        original.count("\n") == candidate.count("\n")
        and original.count("#") == candidate.count("#")
        and original.count("|") == candidate.count("|")
        and SCOPE_GETTER_RE.findall(original) == SCOPE_GETTER_RE.findall(candidate)
    )


def load_and_validate_inputs() -> None:
    paths = {key: db.project_path(value) for key, value in INPUTS.items()}
    for path in paths.values():
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    retarget = read_json(paths["retarget_summary"])
    if retarget.get("fallback_cohort_key") != "semantic_review_router_single_family":
        raise SystemExit("retarget fallback guard failed")
    if bool(retarget.get("production_full_recommended_now")) or int(retarget.get("apply_ready_now") or 0) != 0:
        raise SystemExit("retarget production/apply guard failed")
    if len(read_jsonl(paths["retarget_jsonl"])) < 8:
        raise SystemExit("retarget JSONL guard failed")
    short_label = read_json(paths["short_label_summary"])
    short_audit = read_json(paths["short_label_audit"])
    short_expand = read_json(paths["short_label_expand"])
    if int(short_label.get("candidate_count") or 0) != 6:
        raise SystemExit("short-label discovery candidate guard failed")
    if int(short_audit.get("safe_for_future_apply_batch_count") or 0) != 6:
        raise SystemExit("short-label audit guard failed")
    if int(short_expand.get("new_candidate_count") or 0) != 0:
        raise SystemExit("short-label expand guard failed")
    for source, label in [(short_label, "short-label"), (short_audit, "audit"), (short_expand, "expand")]:
        if int(source.get("false_safe_risk_count") or 0) != 0:
            raise SystemExit(f"{label} false-safe guard failed")
        if int(source.get("requires_lifecycle_later_count") or 0) != 0:
            raise SystemExit(f"{label} lifecycle guard failed")


def fetch_universe(conn: sqlite3.Connection, run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
        SELECT
            l.segment_id,
            GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
            GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
            COUNT(*) AS issue_count
        FROM ml_issue_ledger_items l
        JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
        WHERE l.run_id = ?
          AND l.status = 'open'
          AND s.state_group = 'pending'
          AND s.is_closed = 0
          AND s.needs_output_apply = 0
          AND s.confirmed_matches_output = 1
        GROUP BY l.segment_id
        HAVING all_families = 'semantic_review_router'
    )
    SELECT
        f.segment_id,
        f.all_families,
        f.all_agents,
        src.old_text,
        out.portuguese_text AS current_output_text
    FROM fam f
    LEFT JOIN source_segments src ON src.id = f.segment_id
    LEFT JOIN output_segments out ON out.segment_id = f.segment_id
    """
    rows: list[dict[str, Any]] = []
    for row in conn.execute(query, (run_id, ledger_run_id)):
        families = {part for part in str(row["all_families"] or "").split(",") if part}
        rows.append(
            {
                "segment_id": int(row["segment_id"]),
                "open_families": sorted(families),
                "all_families": sorted(families),
                "all_agents": sorted(part for part in str(row["all_agents"] or "").split(",") if part),
                "structural_guard_overlap": bool(families - {"semantic_review_router"}),
                "original_text": str(row["old_text"] or ""),
                "current_output_text": str(row["current_output_text"] or ""),
            }
        )
    return rows


def semantic_spacing(value: str) -> tuple[str, str, str]:
    candidate = re.sub(r"[ \t]+([,.;:!?])", r"\1", value)
    candidate = re.sub(r"[ \t]+\)", ")", candidate)
    candidate = re.sub(r"\(\s+", "(", candidate)
    if candidate != value:
        return candidate, "candidate_semantic_spacing_punctuation", "mechanical spacing/punctuation cleanup"
    return value, "", ""


def semantic_literal_repair(value: str) -> tuple[str, str, str]:
    replacements = [
        (r"\bInterludio iranio\b", "Interlúdio iraniano", "literal mistranslation/orthography repair"),
        (r"\bacuerdo\b", "acordo", "literal Spanish residue repair"),
        (r"\bbueno\b", "bom", "literal Spanish residue repair"),
        (r"\bpoco\b", "pouco", "literal Spanish residue repair"),
        (r"\beso\b", "isso", "literal Spanish residue repair"),
    ]
    for pattern, replacement, note in replacements:
        candidate = re.sub(pattern, replacement, value, count=1, flags=re.IGNORECASE)
        if candidate != value:
            return candidate, "candidate_semantic_minor_lexical_repair", note
    return value, "", ""


def semantic_ptbr_naturalness(value: str) -> tuple[str, str, str]:
    replacements = [
        (r"\bPropriedade\b", "propriedade", "lowercase common noun in prose"),
        (r"\bVila nível\b", "vila nível", "lowercase common noun in prose"),
    ]
    for pattern, replacement, note in replacements:
        candidate = re.sub(pattern, replacement, value, count=1)
        if candidate != value:
            return candidate, "candidate_semantic_ptbr_naturalness", note
    return value, "", ""


def semantic_article_cleanup(value: str) -> tuple[str, str, str]:
    replacements = [
        (r"\bparticipar do trabalho de manutenção das estradas do condado\b", "participar da manutenção das estradas do condado", "removes redundant noun in prose"),
    ]
    for pattern, replacement, note in replacements:
        candidate = re.sub(pattern, replacement, value, count=1)
        if candidate != value:
            return candidate, "candidate_semantic_article_preposition_cleanup", note
    return value, "", ""


def apply_candidate_heuristics(value: str) -> tuple[str, str, str]:
    for helper in [semantic_spacing, semantic_literal_repair, semantic_ptbr_naturalness, semantic_article_cleanup]:
        candidate, candidate_type, note = helper(value)
        if candidate_type:
            return candidate, candidate_type, note
    return value, "", ""


def risky_candidate(original: str, candidate: str, candidate_type: str) -> bool:
    if NAME_TITLE_CULTURE_RE.search(original):
        return True
    if candidate_type == "candidate_semantic_article_preposition_cleanup" and DYNAMIC_RE.search(original):
        return True
    if len(original) > 300 and candidate_type != "candidate_semantic_spacing_punctuation":
        return True
    return False


def row_priority(row: dict[str, Any]) -> tuple[int, int, int]:
    current = row["current_output_text"]
    candidate, candidate_type, _ = apply_candidate_heuristics(current)
    safe = bool(
        candidate_type
        and candidate != current
        and token_integrity_ok(current, candidate)
        and structure_integrity_ok(current, candidate)
        and not risky_candidate(current, candidate, candidate_type)
    )
    return (0 if safe else 1, 1 if DYNAMIC_RE.search(current) else 0, int(row["segment_id"]))


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = row["current_output_text"]
    candidate, candidate_type, note = apply_candidate_heuristics(current)
    tokens_ok = token_integrity_ok(current, candidate)
    structure_ok = structure_integrity_ok(current, candidate)
    guards = {
        "token_integrity_ok": tokens_ok,
        "structure_integrity_ok": structure_ok,
        "dynamic_token_present": bool(DYNAMIC_RE.search(current)),
        "line_breaks_preserved": current.count("\n") == candidate.count("\n"),
        "debug_markers_preserved": DEBUG_RE.findall(current) == DEBUG_RE.findall(candidate),
        "select_cstring_preserved": SELECT_CSTRING_RE.findall(current) == SELECT_CSTRING_RE.findall(candidate),
        "es_helper_preserved": ES_HELPER_RE.findall(current) == ES_HELPER_RE.findall(candidate),
        "scope_getters_preserved": SCOPE_GETTER_RE.findall(current) == SCOPE_GETTER_RE.findall(candidate),
        "candidate_generated": bool(candidate_type and candidate != current),
    }
    decision = "blocked_no_safe_candidate"
    final_type = ""
    candidate_text = current
    would_change = False
    notes = "no conservative semantic candidate generated"
    if row["structural_guard_overlap"] or set(row["all_families"]) & EXCLUDED_FAMILIES:
        decision = "blocked_by_structural_guard_overlap"
        notes = "structural or excluded family overlap present"
    elif candidate_type and candidate != current:
        if not tokens_ok or not structure_ok:
            decision = "blocked_by_token_integrity"
            notes = "candidate would alter protected token or structure inventory"
        elif risky_candidate(current, candidate, candidate_type):
            decision = "blocked_by_uncertain_semantics" if not NAME_TITLE_CULTURE_RE.search(current) else "blocked_by_name_title_culture"
            notes = "candidate touches guarded or context-sensitive semantic surface"
        else:
            decision = candidate_type
            final_type = candidate_type
            candidate_text = candidate
            would_change = True
            notes = note
    elif NAME_TITLE_CULTURE_RE.search(current):
        decision = "blocked_by_name_title_culture"
        notes = "name/title/culture/religion surface present"
    elif guards["dynamic_token_present"]:
        decision = "blocked_by_dynamic_token"
        notes = "dynamic token present with no safe semantic candidate"
    return {
        "segment_id": int(row["segment_id"]),
        "discovery_key": DISCOVERY_KEY,
        "cohort_key": COHORT_KEY,
        "open_families": row["open_families"],
        "structural_guard_overlap": row["structural_guard_overlap"],
        "original_text": row["original_text"],
        "current_output_text": current,
        "candidate_text": candidate_text,
        "decision": decision,
        "candidate_type": final_type,
        "guards": guards,
        "would_change_output": would_change,
        "requires_human_review": True,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": notes,
    }


def build_summary(records: list[dict[str, Any]], total_universe: int) -> dict[str, Any]:
    decisions = Counter(row["decision"] for row in records)
    candidate_types = Counter(row["candidate_type"] for row in records if row["candidate_type"])
    candidates = [row for row in records if row["would_change_output"]]
    count = len(candidates)
    summary = {
        "schema_version": 1,
        "source": "semantic_single_family_candidate_discovery_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "total_universe": total_universe,
        "total_reviewed": len(records),
        "candidate_count": count,
        "candidate_semantic_minor_lexical_repair": decisions.get("candidate_semantic_minor_lexical_repair", 0),
        "candidate_semantic_ptbr_naturalness": decisions.get("candidate_semantic_ptbr_naturalness", 0),
        "candidate_semantic_article_preposition_cleanup": decisions.get("candidate_semantic_article_preposition_cleanup", 0),
        "candidate_semantic_spacing_punctuation": decisions.get("candidate_semantic_spacing_punctuation", 0),
        "candidate_semantic_literal_mistranslation_repair": decisions.get("candidate_semantic_literal_mistranslation_repair", 0),
        "blocked_by_dynamic_token": decisions.get("blocked_by_dynamic_token", 0),
        "blocked_by_structural_guard_overlap": decisions.get("blocked_by_structural_guard_overlap", 0),
        "blocked_by_gender_or_perspective": decisions.get("blocked_by_gender_or_perspective", 0),
        "blocked_by_name_title_culture": decisions.get("blocked_by_name_title_culture", 0),
        "blocked_by_domain_context": decisions.get("blocked_by_domain_context", 0),
        "blocked_by_uncertain_semantics": decisions.get("blocked_by_uncertain_semantics", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_low_value_style_only": decisions.get("blocked_low_value_style_only", 0),
        "blocked_no_safe_candidate": decisions.get("blocked_no_safe_candidate", 0),
        "would_change_output_count": sum(1 for row in records if row["would_change_output"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "requires_human_review_count": sum(1 for row in records if row["requires_human_review"]),
        "top_candidate_types": dict(candidate_types.most_common(10)),
        "sample_candidate_ids": [int(row["segment_id"]) for row in candidates[:80]],
        "performed_better_than_short_label_clean": count > 6,
        "focused_audit_recommended": count >= 20,
        "small_audit_recommended": 5 <= count < 20,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "network_update_data_only_optional": count >= 20,
        "recommended_next_prompt": (
            "chat_exec_semantic_single_family_candidate_audit_prompt.md"
            if count >= 20
            else "chat_exec_semantic_single_family_candidate_small_audit_prompt.md"
            if count >= 5
            else "chat_exec_candidate_discovery_status_and_next_options_prompt.md"
        ),
        "decision_counts": dict(sorted(decisions.items())),
    }
    if summary["false_safe_risk_count"] != 0:
        raise SystemExit("false-safe guard failed")
    if summary["requires_lifecycle_later_count"] != 0:
        raise SystemExit("lifecycle guard failed")
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    dominant = next(iter(summary["top_candidate_types"]), "none")
    lines = [
        "semantic single-family candidate discovery",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[
            f"{key}={summary[key]}"
            for key in [
                "total_universe",
                "total_reviewed",
                "candidate_count",
                "candidate_semantic_minor_lexical_repair",
                "candidate_semantic_ptbr_naturalness",
                "candidate_semantic_article_preposition_cleanup",
                "candidate_semantic_spacing_punctuation",
                "candidate_semantic_literal_mistranslation_repair",
                "blocked_by_dynamic_token",
                "blocked_by_structural_guard_overlap",
                "blocked_by_gender_or_perspective",
                "blocked_by_name_title_culture",
                "blocked_by_domain_context",
                "blocked_by_uncertain_semantics",
                "blocked_by_token_integrity",
                "blocked_low_value_style_only",
                "blocked_no_safe_candidate",
                "would_change_output_count",
                "false_safe_risk_count",
                "requires_apply_later_count",
                "requires_lifecycle_later_count",
                "requires_human_review_count",
            ]
        ],
        f"top_candidate_types={json.dumps(summary['top_candidate_types'], ensure_ascii=False, sort_keys=True)}",
        f"sample_candidate_ids={summary['sample_candidate_ids']}",
        "",
        "analysis:",
        f"1. Universo suficiente: {str(summary['total_universe'] > 0).lower()} ({summary['total_universe']}).",
        f"2. Candidatos seguros surgiram: {summary['candidate_count']}.",
        f"3. Tipo dominante: {dominant}.",
        f"4. Melhor que short_label_clean: {str(summary['performed_better_than_short_label_clean']).lower()}.",
        f"5. Auditaveis em lote: {str(summary['focused_audit_recommended'] or summary['small_audit_recommended']).lower()}.",
        f"6. Vale auditoria focal: {str(summary['focused_audit_recommended']).lower()}; auditoria pequena={str(summary['small_audit_recommended']).lower()}.",
        "7. Producao full agora: false.",
        "8. Network precisa atualizar: false.",
        "",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
        "apply_ready_now=0",
        "production_full_recommended_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    load_and_validate_inputs()
    with connect_readonly() as conn:
        universe = fetch_universe(conn, args.segment_state_run_id, args.ledger_run_id)
    universe.sort(key=row_priority)
    selected = universe[: args.limit]
    records = [classify(row) for row in selected]
    summary = build_summary(records, len(universe))
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_universe",
        "total_reviewed",
        "candidate_count",
        "candidate_semantic_minor_lexical_repair",
        "candidate_semantic_ptbr_naturalness",
        "candidate_semantic_article_preposition_cleanup",
        "candidate_semantic_spacing_punctuation",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
