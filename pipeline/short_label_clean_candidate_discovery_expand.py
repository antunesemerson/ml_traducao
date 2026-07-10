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


DISCOVERY_KEY = "short_label_clean_candidate_discovery_expand"
COHORT_KEY = "semantic_review_router_plus_short_label_no_structural_overlap"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
PRIMARY_FAMILIES = {"semantic_review_router", "short_label_style_microagent"}
INPUTS = {
    "discovery_jsonl": "reports/20260624_014300_217906_short_label_clean_candidate_discovery.jsonl",
    "discovery_summary": "reports/20260624_014300_217906_short_label_clean_candidate_discovery_summary.json",
    "small_audit_jsonl": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit.jsonl",
    "small_audit_summary": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit_summary.json",
    "retarget_summary": "reports/20260624_004036_196562_candidate_discovery_cohort_retarget_summary.json",
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
NAME_TITLE_CULTURE_RE = re.compile(r"\b(?:GetName|GetHouse|GetCulture|GetFaith|dynasty|house|culture|religion|faith|title)\b", re.IGNORECASE)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_clean_candidate_discovery_expand"
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


def normalize_spacing(value: str) -> tuple[str, str, str, bool]:
    candidate = re.sub(r"[ \t]+([,.;:!?])", r"\1", value)
    candidate = re.sub(r"[ \t]+\)", ")", candidate)
    candidate = re.sub(r"\(\s+", "(", candidate)
    candidate = re.sub(r"\n[ \t]+(?=(?:\$BULLET|\$EFFECT_LIST_BULLET|#weak|#I|@warning_icon))", "\n", candidate)
    if candidate != value:
        return candidate, "candidate_short_label_spacing_punctuation", "mechanical spacing/punctuation cleanup in short label", False
    return value, "", "", False


def lexical_cleanup(value: str) -> tuple[str, str, str, bool]:
    candidate = re.sub(r"\bInterludio iranio\b", "Interlúdio iraniano", value, count=1)
    if candidate != value:
        return candidate, "candidate_short_label_minor_lexical_repair", "seed lexical repair pattern: Interludio iranio", True
    return value, "", "", False


def normalize_short_label_style(value: str) -> tuple[str, str, str, bool]:
    replacements = [
        (r"^IrÃ¡ afetar\b", "Afetará", "seed future-tense short-label normalization"),
        (r"^Irá afetar\b", "Afetará", "seed future-tense short-label normalization"),
        (r"^IrÃ¡ conceder\b", "Concederá", "future-tense short-label normalization"),
        (r"^Irá conceder\b", "Concederá", "future-tense short-label normalization"),
        (r"^IrÃ¡ ganhar\b", "Ganhará", "future-tense short-label normalization"),
        (r"^Irá ganhar\b", "Ganhará", "future-tense short-label normalization"),
        (r"^VocÃª buscarÃ¡\b", "Buscará", "seed concise UI short-label normalization"),
        (r"^Você buscará\b", "Buscará", "seed concise UI short-label normalization"),
        (r"^VocÃª ganharÃ¡\b", "Ganhará", "seed concise UI short-label normalization"),
        (r"^Você ganhará\b", "Ganhará", "seed concise UI short-label normalization"),
    ]
    for pattern, replacement, note in replacements:
        candidate = re.sub(pattern, replacement, value, count=1)
        if candidate != value:
            return candidate, "candidate_short_label_style_normalization", note, True
    return value, "", "", False


def article_cleanup(value: str) -> tuple[str, str, str, bool]:
    replacements = [
        (r"\bda \[activity\|lE\]\b", "em [activity|lE]", "minor article/preposition cleanup around protected concept"),
        (r"\bdo \[accolade_rank_short\|El\]\b", "de [accolade_rank_short|El]", "minor article cleanup around protected concept"),
    ]
    for pattern, replacement, note in replacements:
        candidate = re.sub(pattern, replacement, value, count=1)
        if candidate != value:
            return candidate, "candidate_short_label_article_cleanup", note, False
    return value, "", "", False


def apply_candidate_heuristics(value: str) -> tuple[str, str, str, bool]:
    for helper in [normalize_spacing, lexical_cleanup, normalize_short_label_style, article_cleanup]:
        candidate, candidate_type, note, seed_match = helper(value)
        if candidate_type:
            return candidate, candidate_type, note, seed_match
    return value, "", "", False


def load_and_validate_inputs(
    discovery_jsonl: str | None = None,
    discovery_summary: str | None = None,
    small_audit_jsonl: str | None = None,
    small_audit_summary: str | None = None,
    retarget_summary: str | None = None,
) -> tuple[set[int], set[int], dict[str, Any], dict[str, str]]:
    input_paths = dict(INPUTS)
    if discovery_jsonl:
        input_paths["discovery_jsonl"] = discovery_jsonl
    if discovery_summary:
        input_paths["discovery_summary"] = discovery_summary
    if small_audit_jsonl:
        input_paths["small_audit_jsonl"] = small_audit_jsonl
    if small_audit_summary:
        input_paths["small_audit_summary"] = small_audit_summary
    if retarget_summary:
        input_paths["retarget_summary"] = retarget_summary
    paths = {key: db.project_path(value) for key, value in input_paths.items()}
    for path in paths.values():
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    discovery_summary = read_json(paths["discovery_summary"])
    audit_summary = read_json(paths["small_audit_summary"])
    retarget_summary = read_json(paths["retarget_summary"])
    required_discovery = {
        "total_universe": 741,
        "total_reviewed": 600,
        "candidate_count": 6,
        "false_safe_risk_count": 0,
        "requires_lifecycle_later_count": 0,
    }
    for key, expected in required_discovery.items():
        if int(discovery_summary.get(key) or 0) != expected:
            raise SystemExit(f"discovery guard failed: {key}")
    required_audit = {
        "total_candidates_audited": 6,
        "safe_for_future_apply_batch_count": 6,
        "false_safe_risk_count": 0,
        "requires_lifecycle_later_count": 0,
        "token_integrity_ok_count": 6,
        "structure_integrity_ok_count": 6,
    }
    for key, expected in required_audit.items():
        if int(audit_summary.get(key) or 0) != expected:
            raise SystemExit(f"audit guard failed: {key}")
    if retarget_summary.get("recommended_cohort_key") != COHORT_KEY:
        raise SystemExit("retarget cohort guard failed")
    first_rows = read_jsonl(paths["discovery_jsonl"])
    previously_reviewed = {int(row["segment_id"]) for row in first_rows}
    accepted_seed_ids = {int(segment_id) for segment_id in read_json(paths["small_audit_summary"])["accepted_candidate_ids"]}
    safe_audit_rows = [row for row in read_jsonl(paths["small_audit_jsonl"]) if row.get("safe_for_future_apply_batch")]
    if {int(row["segment_id"]) for row in safe_audit_rows} != accepted_seed_ids:
        raise SystemExit("accepted seed JSONL guard failed")
    return previously_reviewed, accepted_seed_ids, audit_summary, {
        "discovery_jsonl": str(paths["discovery_jsonl"]),
        "discovery_summary": str(paths["discovery_summary"]),
        "small_audit_jsonl": str(paths["small_audit_jsonl"]),
        "small_audit_summary": str(paths["small_audit_summary"]),
        "retarget_summary": str(paths["retarget_summary"]),
    }


def fetch_universe(conn: sqlite3.Connection, run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
        SELECT
            l.segment_id,
            GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
            GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
            SUM(CASE WHEN l.issue_family IN ('semantic_review_router','short_label_style_microagent') THEN 1 ELSE 0 END) AS target_count,
            SUM(CASE WHEN l.issue_family NOT IN ('semantic_review_router','short_label_style_microagent') THEN 1 ELSE 0 END) AS structural_count
        FROM ml_issue_ledger_items l
        JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
        WHERE l.run_id = ?
          AND l.status = 'open'
          AND s.state_group = 'pending'
          AND s.is_closed = 0
          AND s.needs_output_apply = 0
          AND s.confirmed_matches_output = 1
        GROUP BY l.segment_id
        HAVING instr(all_families, 'semantic_review_router') > 0
           AND instr(all_families, 'short_label_style_microagent') > 0
           AND structural_count = 0
    )
    SELECT
        f.segment_id,
        f.all_families,
        f.all_agents,
        f.structural_count,
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
                "open_families": sorted(families & PRIMARY_FAMILIES),
                "all_families": sorted(families),
                "all_agents": sorted(part for part in str(row["all_agents"] or "").split(",") if part),
                "structural_guard_overlap": int(row["structural_count"] or 0) > 0,
                "original_text": str(row["old_text"] or ""),
                "current_output_text": str(row["current_output_text"] or ""),
            }
        )
    return rows


def classify(row: dict[str, Any], previously_reviewed: set[int], seed_ids: set[int]) -> dict[str, Any]:
    current = row["current_output_text"]
    candidate, candidate_type, note, seed_match = apply_candidate_heuristics(current)
    tokens_ok = token_integrity_ok(current, candidate)
    structure_ok = structure_integrity_ok(current, candidate)
    is_previous = int(row["segment_id"]) in previously_reviewed
    is_seed = int(row["segment_id"]) in seed_ids
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
    notes = "no mechanical short-label candidate generated"
    if row["structural_guard_overlap"]:
        decision = "blocked_by_structural_guard_overlap"
        notes = "structural overlap present"
    elif NAME_TITLE_CULTURE_RE.search(current):
        decision = "blocked_by_name_title_culture"
        notes = "name/title/culture-like getter guard present"
    elif candidate_type and candidate != current:
        if is_previous or is_seed:
            decision = "blocked_low_similarity_to_accepted_seed"
            notes = "previously reviewed or accepted seed; excluded from new candidate count"
        elif not seed_match:
            decision = "blocked_low_similarity_to_accepted_seed"
            notes = "candidate does not match accepted seed pattern"
        elif not tokens_ok or not structure_ok:
            decision = "blocked_by_token_integrity"
            notes = "candidate would alter protected token or structure inventory"
        else:
            decision = candidate_type
            final_type = candidate_type
            candidate_text = candidate
            would_change = True
            notes = note
    elif guards["dynamic_token_present"]:
        decision = "blocked_by_dynamic_token"
        notes = "dynamic token present with no safe seed-like short-label candidate"
    return {
        "segment_id": int(row["segment_id"]),
        "discovery_key": DISCOVERY_KEY,
        "cohort_key": COHORT_KEY,
        "previously_reviewed": is_previous,
        "matches_accepted_seed_pattern": bool(would_change and seed_match),
        "seed_pattern_reason": note if would_change and seed_match else "",
        "open_families": row["open_families"],
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


def build_summary(
    records: list[dict[str, Any]],
    total_universe: int,
    audit_summary: dict[str, Any],
    input_refs: dict[str, str],
) -> dict[str, Any]:
    decisions = Counter(row["decision"] for row in records)
    new_records = [row for row in records if not row["previously_reviewed"]]
    new_candidates = [row for row in new_records if row["would_change_output"]]
    total_potential = int(audit_summary["safe_for_future_apply_batch_count"]) + len(new_candidates)
    summary = {
        "schema_version": 1,
        "source": "short_label_clean_candidate_discovery_expand_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "input_refs": input_refs,
        "total_universe": total_universe,
        "previously_reviewed_count": sum(1 for row in records if row["previously_reviewed"]),
        "new_reviewed_count": len(new_records),
        "accepted_seed_count": int(audit_summary["safe_for_future_apply_batch_count"]),
        "seed_dominant_type": "candidate_short_label_style_normalization",
        "seed_false_safe_risk_count": int(audit_summary["false_safe_risk_count"]),
        "new_candidate_count": len(new_candidates),
        "candidate_short_label_style_normalization": decisions.get("candidate_short_label_style_normalization", 0),
        "candidate_short_label_ptbr_naturalness": decisions.get("candidate_short_label_ptbr_naturalness", 0),
        "candidate_short_label_spacing_punctuation": decisions.get("candidate_short_label_spacing_punctuation", 0),
        "candidate_short_label_article_cleanup": decisions.get("candidate_short_label_article_cleanup", 0),
        "candidate_short_label_minor_lexical_repair": decisions.get("candidate_short_label_minor_lexical_repair", 0),
        "matches_accepted_seed_pattern_count": sum(1 for row in records if row["matches_accepted_seed_pattern"]),
        "blocked_by_dynamic_token": decisions.get("blocked_by_dynamic_token", 0),
        "blocked_by_structural_guard_overlap": decisions.get("blocked_by_structural_guard_overlap", 0),
        "blocked_by_gender_or_perspective": decisions.get("blocked_by_gender_or_perspective", 0),
        "blocked_by_name_title_culture": decisions.get("blocked_by_name_title_culture", 0),
        "blocked_by_domain_context": decisions.get("blocked_by_domain_context", 0),
        "blocked_by_uncertain_semantics": decisions.get("blocked_by_uncertain_semantics", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_low_value_style_only": decisions.get("blocked_low_value_style_only", 0),
        "blocked_low_similarity_to_accepted_seed": decisions.get("blocked_low_similarity_to_accepted_seed", 0),
        "blocked_no_safe_candidate": decisions.get("blocked_no_safe_candidate", 0),
        "would_change_output_count": sum(1 for row in records if row["would_change_output"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "requires_human_review_count": sum(1 for row in records if row["requires_human_review"]),
        "sample_candidate_ids": [int(row["segment_id"]) for row in new_candidates[:80]],
        "total_potential_accepted_after_audit": total_potential,
        "expanded_audit_recommended": len(new_candidates) > 0 or total_potential >= 8,
        "specific_resolver_recommended_after_audit": total_potential >= 8,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "recommended_next_prompt": (
            "chat_exec_short_label_clean_candidate_expanded_audit_prompt.md"
            if len(new_candidates) > 0 or total_potential >= 8
            else "chat_exec_semantic_single_family_candidate_discovery_prompt.md"
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
    metric_keys = [
        "total_universe",
        "previously_reviewed_count",
        "new_reviewed_count",
        "accepted_seed_count",
        "new_candidate_count",
        "candidate_short_label_style_normalization",
        "candidate_short_label_ptbr_naturalness",
        "candidate_short_label_spacing_punctuation",
        "candidate_short_label_article_cleanup",
        "candidate_short_label_minor_lexical_repair",
        "matches_accepted_seed_pattern_count",
        "blocked_by_dynamic_token",
        "blocked_by_structural_guard_overlap",
        "blocked_by_gender_or_perspective",
        "blocked_by_name_title_culture",
        "blocked_by_domain_context",
        "blocked_by_uncertain_semantics",
        "blocked_by_token_integrity",
        "blocked_low_value_style_only",
        "blocked_low_similarity_to_accepted_seed",
        "blocked_no_safe_candidate",
        "would_change_output_count",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "requires_human_review_count",
    ]
    lines = [
        "short_label clean candidate discovery expand",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        f"sample_candidate_ids={summary['sample_candidate_ids']}",
        "",
        "analysis:",
        f"1. Segmentos novos revisados: {summary['new_reviewed_count']}.",
        f"2. Candidatos novos: {summary['new_candidate_count']}.",
        f"3. Padrao dos 6 aceitos se repetiu: {str(summary['matches_accepted_seed_pattern_count'] > 0).lower()}.",
        f"4. Lote total short-label grande o bastante para auditoria expandida: {str(summary['expanded_audit_recommended']).lower()} (potencial={summary['total_potential_accepted_after_audit']}).",
        f"5. Resolver especifico: {str(summary['specific_resolver_recommended_after_audit']).lower()} depois de auditoria expandida.",
        "6. Producao full agora: false.",
        "7. Network precisa atualizar: false.",
        "",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
        "apply_recommended_now=false",
        "production_full_recommended_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--discovery-jsonl", default="")
    parser.add_argument("--discovery-summary-json", default="")
    parser.add_argument("--small-audit-jsonl", default="")
    parser.add_argument("--small-audit-summary-json", default="")
    parser.add_argument("--retarget-summary-json", default="")
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    previously_reviewed, seed_ids, audit_summary, input_refs = load_and_validate_inputs(
        discovery_jsonl=args.discovery_jsonl or None,
        discovery_summary=args.discovery_summary_json or None,
        small_audit_jsonl=args.small_audit_jsonl or None,
        small_audit_summary=args.small_audit_summary_json or None,
        retarget_summary=args.retarget_summary_json or None,
    )
    with connect_readonly() as conn:
        universe = fetch_universe(conn, args.segment_state_run_id, args.ledger_run_id)
    universe.sort(key=lambda row: (int(row["segment_id"]) in previously_reviewed, int(row["segment_id"])))
    records = [classify(row, previously_reviewed, seed_ids) for row in universe]
    summary = build_summary(records, len(universe), audit_summary, input_refs)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_universe",
        "previously_reviewed_count",
        "new_reviewed_count",
        "accepted_seed_count",
        "new_candidate_count",
        "matches_accepted_seed_pattern_count",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
