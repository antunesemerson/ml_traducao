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


DISCOVERY_KEY = "semantic_short_label_autofix_candidate_discovery_expand"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
TARGET_FAMILIES = {
    "semantic_review_router",
    "short_label_style_microagent",
    "autofix_unknown_microagent",
}
INPUTS = {
    "prior_discovery_jsonl": "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery.jsonl",
    "prior_discovery_summary": "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery_summary.json",
    "audit_jsonl": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit.jsonl",
    "audit_summary": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit_summary.json",
    "status_summary": "reports/20260623_225655_764509_resolution_phase_status_and_next_strategy_summary.json",
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

SPANISH_LITERAL_REPAIRS = [
    (r"muchos m(?:\u00c3\u00a1|á)s", "muitos mais", "seed literal Spanish residue: muchos mas -> muitos mais"),
    (r"\bacuerdo\b", "acordo", "seed literal Spanish residue: acuerdo -> acordo"),
    (r"\bbueno\b", "bom", "seed literal Spanish residue: bueno -> bom"),
    (r"\bbuenos\b", "bons", "seed literal Spanish residue: buenos -> bons"),
    (r"\bbuena\b", "boa", "literal Spanish residue: buena -> boa"),
    (r"\bbuenas\b", "boas", "literal Spanish residue: buenas -> boas"),
    (r"\beso\b", "isso", "seed literal Spanish residue: eso -> isso"),
    (r"\bpoco\b", "pouco", "seed literal Spanish residue: poco -> pouco"),
    (r"\bpoca\b", "pouca", "literal Spanish residue: poca -> pouca"),
    (r"\bpocos\b", "poucos", "literal Spanish residue: pocos -> poucos"),
    (r"\bpocas\b", "poucas", "literal Spanish residue: pocas -> poucas"),
    (r"\bmucho\b", "muito", "literal Spanish residue: mucho -> muito"),
    (r"\bmucha\b", "muita", "literal Spanish residue: mucha -> muita"),
    (r"\bmuchos\b", "muitos", "literal Spanish residue: muchos -> muitos"),
    (r"\bmuchas\b", "muitas", "literal Spanish residue: muchas -> muitas"),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_autofix_candidate_discovery_expand"
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


def has_dynamic(value: str) -> bool:
    return bool(
        BRACKET_RE.search(value)
        or VARIABLE_RE.search(value)
        or SCOPE_GETTER_RE.search(value)
        or "Select_CString" in value
        or ".Custom('ES_" in value
    )


def has_domain_guard_family(families: list[str]) -> bool:
    guarded = {
        "title_policy_microagent",
        "nickname_name_policy",
        "culture_semantic_microagent",
        "religion_semantic_microagent",
    }
    return any(family in guarded for family in families)


def has_gender_family(families: list[str]) -> bool:
    return any(family in {"gender_token_microagent", "gender_local_player_policy"} for family in families)


def candidate_from_literal_spanish(value: str) -> tuple[str, str, str, bool]:
    for pattern_text, replacement, reason in SPANISH_LITERAL_REPAIRS:
        pattern = re.compile(rf"(?<![A-Za-z_]){pattern_text}(?![A-Za-z_])", re.IGNORECASE)
        if pattern.search(value):
            candidate = pattern.sub(replacement, value, count=1)
            return candidate, "candidate_semantic_minor_lexical_repair", reason, True
    return value, "", "", False


def candidate_from_spacing(value: str) -> tuple[str, str, str, bool]:
    candidate = re.sub(r"\s+([,.;:!?])", r"\1", value)
    candidate = re.sub(r"\s+\)", ")", candidate)
    if candidate != value:
        return candidate, "candidate_spacing_punctuation_cleanup", "seed spacing/punctuation cleanup", True
    return value, "", "", False


def apply_candidate_heuristics(value: str) -> tuple[str, str, str, bool]:
    literal_candidate = candidate_from_literal_spanish(value)
    if literal_candidate[1]:
        return literal_candidate
    return candidate_from_spacing(value)


def risky_candidate(original: str, candidate: str) -> bool:
    lowered = candidate.lower()
    if re.search(r"\b(estudios|nivel)\b", lowered):
        return True
    if re.search(r"#emp\s+muito#!\s+muito\b", lowered):
        return True
    if candidate.count("#EMP") != original.count("#EMP"):
        return True
    return False


def cohort_key(families: set[str]) -> str:
    if {"autofix_unknown_microagent", "semantic_review_router"} <= families:
        return "autofix_unknown_microagent+semantic_review_router"
    if {"semantic_review_router", "short_label_style_microagent"} <= families:
        return "semantic_review_router+short_label_style_microagent"
    if families == {"autofix_unknown_microagent"}:
        return "autofix_unknown_microagent"
    if families == {"short_label_style_microagent"}:
        return "short_label_style_microagent"
    if families == {"semantic_review_router"}:
        return "semantic_review_router"
    return "+".join(sorted(families & TARGET_FAMILIES)) or "other"


def fetch_universe(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int, excluded_ids: set[int]) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
        SELECT
            l.segment_id,
            GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
            GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
            SUM(CASE WHEN l.issue_family IN ('semantic_review_router','short_label_style_microagent','autofix_unknown_microagent') THEN 1 ELSE 0 END) AS target_issue_count,
            SUM(CASE WHEN l.issue_family NOT IN ('semantic_review_router','short_label_style_microagent','autofix_unknown_microagent') THEN 1 ELSE 0 END) AS structural_issue_count
        FROM ml_issue_ledger_items l
        JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
        WHERE l.run_id = ?
          AND l.status = 'open'
          AND s.state_group = 'pending'
          AND s.is_closed = 0
          AND s.needs_output_apply = 0
          AND s.confirmed_matches_output = 1
        GROUP BY l.segment_id
        HAVING target_issue_count > 0
    )
    SELECT
        f.segment_id,
        f.all_families,
        f.all_agents,
        f.structural_issue_count,
        s.relative_path,
        s.source_key,
        src.old_text,
        out.portuguese_text AS current_output_text
    FROM fam f
    JOIN segment_state_items s ON s.segment_id = f.segment_id AND s.run_id = ?
    LEFT JOIN source_segments src ON src.id = f.segment_id
    LEFT JOIN output_segments out ON out.segment_id = f.segment_id
    """
    rows: list[dict[str, Any]] = []
    for row in conn.execute(query, (segment_state_run_id, ledger_run_id, segment_state_run_id)):
        segment_id = int(row["segment_id"])
        if segment_id in excluded_ids:
            continue
        all_families = {part for part in str(row["all_families"] or "").split(",") if part}
        target_families = all_families & TARGET_FAMILIES
        rows.append(
            {
                "segment_id": segment_id,
                "all_families": sorted(all_families),
                "open_families": sorted(target_families),
                "all_agents": sorted(part for part in str(row["all_agents"] or "").split(",") if part),
                "cohort_key": cohort_key(target_families),
                "structural_guard_overlap": int(row["structural_issue_count"] or 0) > 0,
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "original_text": str(row["old_text"] or ""),
                "current_output_text": str(row["current_output_text"] or ""),
            }
        )
    return rows


def candidate_priority(row: dict[str, Any]) -> tuple[int, int, int, int]:
    current = str(row.get("current_output_text") or "")
    candidate, candidate_type, _, seed_match = apply_candidate_heuristics(current)
    tokens_ok = token_integrity_ok(current, candidate)
    structure_ok = structure_integrity_ok(current, candidate)
    safe_candidate = bool(candidate_type and candidate != current and seed_match and tokens_ok and structure_ok)
    cohort_rank = {
        "autofix_unknown_microagent+semantic_review_router": 0,
        "autofix_unknown_microagent": 1,
        "semantic_review_router": 2,
        "semantic_review_router+short_label_style_microagent": 3,
        "short_label_style_microagent": 4,
    }.get(str(row["cohort_key"]), 9)
    dynamic_rank = 1 if has_dynamic(current) else 0
    return (0 if safe_candidate else 1, cohort_rank, dynamic_rank, int(row["segment_id"]))


def classify_record(row: dict[str, Any]) -> dict[str, Any]:
    current = str(row["current_output_text"])
    candidate, candidate_type, seed_reason, seed_match = apply_candidate_heuristics(current)
    tokens_ok = token_integrity_ok(current, candidate)
    structure_ok = structure_integrity_ok(current, candidate)
    guards = {
        "token_integrity_ok": tokens_ok,
        "structure_integrity_ok": structure_ok,
        "dynamic_token_present": has_dynamic(current),
        "line_breaks_preserved": current.count("\n") == candidate.count("\n"),
        "debug_markers_preserved": DEBUG_RE.findall(current) == DEBUG_RE.findall(candidate),
        "select_cstring_preserved": SELECT_CSTRING_RE.findall(current) == SELECT_CSTRING_RE.findall(candidate),
        "es_helper_preserved": ES_HELPER_RE.findall(current) == ES_HELPER_RE.findall(candidate),
        "scope_getters_preserved": SCOPE_GETTER_RE.findall(current) == SCOPE_GETTER_RE.findall(candidate),
        "structural_guard_overlap": bool(row["structural_guard_overlap"]),
        "candidate_generated": bool(candidate_type and candidate != current),
    }
    decision = "blocked_no_safe_candidate"
    final_type = ""
    candidate_text = current
    would_change = False
    notes = "no seed-like low-risk candidate generated"
    if bool(row["structural_guard_overlap"]):
        decision = "blocked_by_structural_guard_overlap"
        notes = "segment has non-target structural family"
    elif has_gender_family(row["all_families"]):
        decision = "blocked_by_gender_or_perspective"
        notes = "gender/perspective family present"
    elif has_domain_guard_family(row["all_families"]):
        decision = "blocked_by_name_title_culture"
        notes = "name/title/culture/religion family present"
    elif candidate_type and candidate != current:
        if not seed_match:
            decision = "blocked_low_similarity_to_accepted_seed"
            notes = "candidate does not match accepted seed pattern"
        elif not tokens_ok or not structure_ok:
            decision = "blocked_by_token_integrity"
            notes = "candidate would alter token or structural inventory"
        elif risky_candidate(current, candidate):
            decision = "blocked_by_uncertain_semantics"
            notes = "candidate leaves partial Spanish residue or awkward duplicated wording"
        else:
            decision = candidate_type
            final_type = candidate_type
            candidate_text = candidate
            would_change = True
            notes = seed_reason
    elif has_dynamic(current):
        decision = "blocked_by_dynamic_token"
        notes = "dynamic token present and no seed-like lexical candidate generated"

    return {
        "segment_id": int(row["segment_id"]),
        "discovery_key": DISCOVERY_KEY,
        "open_families": row["open_families"],
        "cohort_key": row["cohort_key"],
        "structural_guard_overlap": bool(row["structural_guard_overlap"]),
        "matches_accepted_seed_pattern": bool(would_change and seed_match),
        "seed_pattern_reason": seed_reason if would_change else "",
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


def load_and_validate_inputs(segment_state_run_id: int, ledger_run_id: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")
    paths = {key: db.project_path(value) for key, value in INPUTS.items()}
    for path in paths.values():
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    prior = read_json(paths["prior_discovery_summary"])
    audit = read_json(paths["audit_summary"])
    status = read_json(paths["status_summary"])
    required_prior = {
        "total_universe": 9544,
        "total_reviewed": 240,
        "candidate_count": 8,
        "candidate_semantic_minor_lexical_repair": 6,
        "candidate_spacing_punctuation_cleanup": 2,
        "false_safe_risk_count": 0,
    }
    for key, expected in required_prior.items():
        if int(prior.get(key) or 0) != expected:
            raise SystemExit(f"prior discovery guard failed: {key}")
    required_audit = {
        "total_candidates_audited": 8,
        "safe_for_future_apply_batch_count": 8,
        "false_safe_risk_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "token_integrity_ok_count": 8,
        "structure_integrity_ok_count": 8,
    }
    for key, expected in required_audit.items():
        if int(audit.get(key) or 0) != expected:
            raise SystemExit(f"audit summary guard failed: {key}")
    if audit.get("dominant_safe_candidate_type") != "candidate_semantic_minor_lexical_repair":
        raise SystemExit("audit dominant type guard failed")
    if not bool(status.get("architecture_closed")) or int(status.get("true_unknown_count") or 0) != 0:
        raise SystemExit("status architecture guard failed")
    audit_rows = read_jsonl(paths["audit_jsonl"])
    safe_rows = [row for row in audit_rows if row.get("safe_for_future_apply_batch")]
    if len(safe_rows) != 8:
        raise SystemExit("safe seed rows guard failed")
    return prior, audit, safe_rows


def build_summary(records: list[dict[str, Any]], total_universe: int, audit_summary: dict[str, Any]) -> dict[str, Any]:
    decisions = Counter(row["decision"] for row in records)
    cohorts = Counter(row["cohort_key"] for row in records)
    candidate_records = [row for row in records if row["would_change_output"]]
    dominant = Counter(row["candidate_type"] for row in candidate_records).most_common(1)
    candidate_count = len(candidate_records)
    summary = {
        "schema_version": 1,
        "source": "semantic_short_label_autofix_candidate_discovery_expand_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "total_universe": total_universe,
        "total_reviewed": len(records),
        "accepted_seed_count": int(audit_summary["safe_for_future_apply_batch_count"]),
        "seed_dominant_type": audit_summary["dominant_safe_candidate_type"],
        "seed_false_safe_risk_count": int(audit_summary["false_safe_risk_count"]),
        "candidate_count": candidate_count,
        "candidate_semantic_minor_lexical_repair": decisions.get("candidate_semantic_minor_lexical_repair", 0),
        "candidate_spacing_punctuation_cleanup": decisions.get("candidate_spacing_punctuation_cleanup", 0),
        "candidate_case_or_article_cleanup": decisions.get("candidate_case_or_article_cleanup", 0),
        "candidate_ptbr_naturalness_review": decisions.get("candidate_ptbr_naturalness_review", 0),
        "candidate_short_label_style_normalization": decisions.get("candidate_short_label_style_normalization", 0),
        "matches_accepted_seed_pattern_count": sum(1 for row in records if row["matches_accepted_seed_pattern"]),
        "blocked_by_dynamic_token": decisions.get("blocked_by_dynamic_token", 0),
        "blocked_by_structural_guard_overlap": decisions.get("blocked_by_structural_guard_overlap", 0),
        "blocked_by_domain_context": decisions.get("blocked_by_domain_context", 0),
        "blocked_by_gender_or_perspective": decisions.get("blocked_by_gender_or_perspective", 0),
        "blocked_by_name_title_culture": decisions.get("blocked_by_name_title_culture", 0),
        "blocked_by_uncertain_semantics": decisions.get("blocked_by_uncertain_semantics", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_low_similarity_to_accepted_seed": decisions.get("blocked_low_similarity_to_accepted_seed", 0),
        "blocked_no_safe_candidate": decisions.get("blocked_no_safe_candidate", 0),
        "would_change_output_count": sum(1 for row in records if row["would_change_output"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "requires_human_review_count": sum(1 for row in records if row["requires_human_review"]),
        "top_cohorts": dict(cohorts.most_common(10)),
        "sample_candidate_ids": [int(row["segment_id"]) for row in candidate_records[:80]],
        "dominant_candidate_type": dominant[0][0] if dominant else "",
        "expanded_audit_recommended": candidate_count >= 10,
        "specific_resolver_recommended": candidate_count >= 30,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "network_update_data_only_optional": candidate_count >= 30,
        "recommended_next_prompt": (
            "chat_exec_semantic_short_label_autofix_candidate_expanded_audit_prompt.md"
            if candidate_count >= 10
            else "chat_exec_candidate_discovery_cohort_retarget_prompt.md"
        ),
        "decision_counts": dict(sorted(decisions.items())),
    }
    if summary["false_safe_risk_count"] != 0:
        raise SystemExit("false safe risk guard failed")
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
        "total_reviewed",
        "accepted_seed_count",
        "candidate_count",
        "candidate_semantic_minor_lexical_repair",
        "candidate_spacing_punctuation_cleanup",
        "candidate_case_or_article_cleanup",
        "candidate_ptbr_naturalness_review",
        "candidate_short_label_style_normalization",
        "matches_accepted_seed_pattern_count",
        "blocked_by_dynamic_token",
        "blocked_by_structural_guard_overlap",
        "blocked_by_domain_context",
        "blocked_by_gender_or_perspective",
        "blocked_by_name_title_culture",
        "blocked_by_uncertain_semantics",
        "blocked_by_token_integrity",
        "blocked_low_similarity_to_accepted_seed",
        "blocked_no_safe_candidate",
        "would_change_output_count",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "requires_human_review_count",
    ]
    grew_materially = summary["candidate_count"] > 8
    dominant_continued = summary["dominant_candidate_type"] == "candidate_semantic_minor_lexical_repair"
    risk = "semantica incerta em residuos compostos e grande massa sem padrao seed seguro"
    lines = [
        "semantic + short_label + autofix candidate discovery expand",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        f"seed_dominant_type={summary['seed_dominant_type']}",
        f"seed_false_safe_risk_count={summary['seed_false_safe_risk_count']}",
        f"top_cohorts={json.dumps(summary['top_cohorts'], ensure_ascii=False, sort_keys=True)}",
        f"sample_candidate_ids={summary['sample_candidate_ids']}",
        "",
        "analysis:",
        f"1. A expansao aumentou materialmente os candidatos: {str(grew_materially).lower()} ({summary['candidate_count']} vs 8).",
        f"2. O tipo dominante continuou semantic_minor_lexical_repair: {str(dominant_continued).lower()}.",
        f"3. Candidatos que seguem o padrao aceito: {summary['matches_accepted_seed_pattern_count']}.",
        f"4. Risco principal: {risk}.",
        f"5. Vale auditar lote maior agora: {str(summary['expanded_audit_recommended']).lower()}.",
        f"6. Vale criar resolver especifico agora: {str(summary['specific_resolver_recommended']).lower()}; antes disso, auditoria expandida.",
        "7. Vale rodar producao full agora: false.",
        f"8. Network precisa atualizar: {str(summary['network_update_recommended']).lower()}; data-only opcional={str(summary['network_update_data_only_optional']).lower()}.",
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
    parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()

    _, audit_summary, safe_rows = load_and_validate_inputs(args.segment_state_run_id, args.ledger_run_id)
    excluded_ids = {int(row["segment_id"]) for row in safe_rows}
    with connect_readonly() as conn:
        universe = fetch_universe(conn, args.segment_state_run_id, args.ledger_run_id, excluded_ids)
    universe.sort(key=candidate_priority)
    selected = universe[: args.limit]
    records = [classify_record(row) for row in selected]
    summary = build_summary(records, len(universe), audit_summary)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_universe",
        "total_reviewed",
        "accepted_seed_count",
        "candidate_count",
        "candidate_semantic_minor_lexical_repair",
        "candidate_spacing_punctuation_cleanup",
        "matches_accepted_seed_pattern_count",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
