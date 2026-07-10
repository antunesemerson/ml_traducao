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


DISCOVERY_KEY = "semantic_short_label_autofix_candidate_discovery"
TARGET_FAMILIES = {
    "semantic_review_router",
    "short_label_style_microagent",
    "autofix_unknown_microagent",
}
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
REQUIRED_INPUTS = [
    "reports/20260623_213737_409531_resolver_wave2_consolidated_diagnostic_summary.json",
    "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic.jsonl",
    "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
    "reports/20260621_144729_685653_global_pending_architecture_review_after_maturation.jsonl",
    "reports/20260621_144729_685653_global_pending_architecture_review_after_maturation.txt",
]

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^]]+\)|\.Custom\('ES_[A-Za-z0-9_]+'\)"
)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
DEBUG_RE = re.compile(r"#D\b.*?#!", re.DOTALL)
SCOPE_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|Get[A-Za-z0-9_]+")
SPANISH_REPAIRS = [
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])muchos más(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "muitos mais"),
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])mucho(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "muito"),
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])poco(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "pouco"),
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])acuerdo(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "acordo"),
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])bueno(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "bom"),
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])buenos(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "bons"),
    (re.compile(r"(?<![A-Za-zÀ-ÿ_])eso(?![A-Za-zÀ-ÿ_])", re.IGNORECASE), "isso"),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_autofix_candidate_discovery"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_required_inputs() -> None:
    for rel_path in REQUIRED_INPUTS:
        path = db.project_path(rel_path)
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    wave2 = read_json(db.project_path(REQUIRED_INPUTS[0]))
    if int(wave2.get("wave1_plus_wave2_reviewed") or 0) != 716:
        raise SystemExit("wave2 reviewed guard failed")
    if int(wave2.get("wave1_plus_wave2_suggestion_candidates") or 0) != 0:
        raise SystemExit("wave2 candidate guard failed")
    if int(wave2.get("wave1_plus_wave2_false_safe_risk_count") or 0) != 0:
        raise SystemExit("wave2 false-safe guard failed")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(value)


def token_integrity_ok(original: str, candidate: str) -> bool:
    return (
        original.count("[") == original.count("]")
        and candidate.count("[") == candidate.count("]")
        and original.count("$") % 2 == 0
        and candidate.count("$") % 2 == 0
        and tokens(original) == tokens(candidate)
        and BRACKET_RE.findall(original) == BRACKET_RE.findall(candidate)
        and VARIABLE_RE.findall(original) == VARIABLE_RE.findall(candidate)
        and DEBUG_RE.findall(original) == DEBUG_RE.findall(candidate)
        and original.count("\n") == candidate.count("\n")
    )


def has_dynamic(value: str) -> bool:
    return bool(BRACKET_RE.search(value) or VARIABLE_RE.search(value) or SCOPE_RE.search(value) or "Select_CString" in value or ".Custom('ES_" in value)


def apply_candidate_heuristics(value: str) -> tuple[str, str, str]:
    for pattern, replacement in SPANISH_REPAIRS:
        if pattern.search(value):
            return pattern.sub(replacement, value, count=1), "candidate_semantic_minor_lexical_repair", "literal Spanish lexical residue normalized for human review"
    candidate = re.sub(r"\s+([,.;:!?])", r"\1", value)
    candidate = re.sub(r"\s+\)", ")", candidate)
    if candidate != value:
        return candidate, "candidate_spacing_punctuation_cleanup", "spacing before punctuation/parenthesis normalized"
    return value, "", ""


def risky_partial_spanish_candidate(value: str) -> bool:
    if re.search(r"(?i)\b(estudios|nivel)\b", value):
        return True
    if re.search(r"(?i)#EMP\s+muito#!\s+muito\b", value):
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


def candidate_priority(row: dict[str, Any]) -> tuple[int, int, int]:
    current = str(row.get("current_output_text") or "")
    candidate, candidate_type, _ = apply_candidate_heuristics(current)
    safe_candidate = bool(candidate_type and candidate != current and token_integrity_ok(current, candidate))
    structural_overlap = bool(row["structural_guard_overlap"])
    cohort_rank = {
        "autofix_unknown_microagent+semantic_review_router": 0,
        "semantic_review_router+short_label_style_microagent": 1,
        "autofix_unknown_microagent": 2,
        "short_label_style_microagent": 3,
        "semantic_review_router": 4,
    }.get(str(row["cohort_key"]), 9)
    return (0 if safe_candidate and not structural_overlap else 1, cohort_rank, int(row["segment_id"]))


def fetch_universe(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
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
    rows = []
    for row in conn.execute(query, (segment_state_run_id, ledger_run_id, segment_state_run_id)):
        all_families = {part for part in str(row["all_families"] or "").split(",") if part}
        target_families = all_families & TARGET_FAMILIES
        record = {
            "segment_id": int(row["segment_id"]),
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
        rows.append(record)
    return rows


def classify_record(row: dict[str, Any]) -> dict[str, Any]:
    current = str(row["current_output_text"])
    candidate, candidate_type, notes = apply_candidate_heuristics(current)
    guards = {
        "token_integrity_ok": token_integrity_ok(current, candidate),
        "dynamic_token_present": has_dynamic(current),
        "line_breaks_preserved": current.count("\n") == candidate.count("\n"),
        "debug_markers_preserved": DEBUG_RE.findall(current) == DEBUG_RE.findall(candidate),
        "structural_guard_overlap": bool(row["structural_guard_overlap"]),
        "candidate_generated": bool(candidate_type and candidate != current),
    }
    decision = "blocked_no_safe_candidate"
    final_type = ""
    candidate_text = current
    would_change = False
    false_safe_risk = False
    if guards["structural_guard_overlap"]:
        decision = "blocked_by_structural_guard_overlap"
        notes = "segment also has non-target structural family; kept out of candidate lane"
    elif candidate_type and candidate != current:
        if not guards["token_integrity_ok"]:
            decision = "blocked_by_token_integrity"
            notes = "candidate would alter token inventory"
        elif risky_partial_spanish_candidate(candidate):
            decision = "blocked_by_uncertain_semantics"
            notes = "candidate would leave partial Spanish residue or awkward duplicated wording"
        else:
            decision = candidate_type
            final_type = candidate_type
            candidate_text = candidate
            would_change = True
    elif guards["dynamic_token_present"]:
        decision = "blocked_by_dynamic_token"
        notes = "dynamic token present and no safe lexical candidate generated"
    else:
        notes = "no low-risk lexical/semantic candidate generated"
    if any(family in row["all_families"] for family in ["gender_token_microagent"]):
        decision = "blocked_by_gender_or_perspective"
        candidate_text = current
        would_change = False
        final_type = ""
        notes = "gender/perspective family present"
    if any(family in row["all_families"] for family in ["title_policy_microagent", "nickname_name_policy", "culture_semantic_microagent", "religion_semantic_microagent"]):
        decision = "blocked_by_name_title_culture"
        candidate_text = current
        would_change = False
        final_type = ""
        notes = "name/title/culture/religion family present"
    return {
        "segment_id": int(row["segment_id"]),
        "discovery_key": DISCOVERY_KEY,
        "open_families": row["open_families"],
        "cohort_key": row["cohort_key"],
        "structural_guard_overlap": bool(row["structural_guard_overlap"]),
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
        "false_safe_risk": false_safe_risk,
        "notes": notes,
    }


def build_summary(records: list[dict[str, Any]], total_universe: int) -> dict[str, Any]:
    decisions = Counter(record["decision"] for record in records)
    cohorts = Counter(record["cohort_key"] for record in records)
    candidate_records = [record for record in records if record["would_change_output"]]
    summary = {
        "schema_version": 1,
        "source": "semantic_short_label_autofix_candidate_discovery_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "total_universe": total_universe,
        "total_reviewed": len(records),
        "candidate_count": len(candidate_records),
        "candidate_short_label_style_normalization": decisions.get("candidate_short_label_style_normalization", 0),
        "candidate_autofix_unknown_literal_cleanup": decisions.get("candidate_autofix_unknown_literal_cleanup", 0),
        "candidate_semantic_minor_lexical_repair": decisions.get("candidate_semantic_minor_lexical_repair", 0),
        "candidate_spacing_punctuation_cleanup": decisions.get("candidate_spacing_punctuation_cleanup", 0),
        "candidate_case_or_article_cleanup": decisions.get("candidate_case_or_article_cleanup", 0),
        "candidate_ptbr_naturalness_review": decisions.get("candidate_ptbr_naturalness_review", 0),
        "blocked_by_dynamic_token": decisions.get("blocked_by_dynamic_token", 0),
        "blocked_by_structural_guard_overlap": decisions.get("blocked_by_structural_guard_overlap", 0),
        "blocked_by_domain_context": decisions.get("blocked_by_domain_context", 0),
        "blocked_by_gender_or_perspective": decisions.get("blocked_by_gender_or_perspective", 0),
        "blocked_by_name_title_culture": decisions.get("blocked_by_name_title_culture", 0),
        "blocked_by_uncertain_semantics": decisions.get("blocked_by_uncertain_semantics", 0),
        "blocked_by_token_integrity": decisions.get("blocked_by_token_integrity", 0),
        "blocked_no_safe_candidate": decisions.get("blocked_no_safe_candidate", 0),
        "would_change_output_count": sum(1 for record in records if record["would_change_output"]),
        "false_safe_risk_count": sum(1 for record in records if record["false_safe_risk"]),
        "requires_apply_later_count": sum(1 for record in records if record["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for record in records if record["requires_lifecycle_later"]),
        "requires_human_review_count": sum(1 for record in records if record["requires_human_review"]),
        "top_cohorts": dict(cohorts.most_common(10)),
        "sample_candidate_ids": [int(record["segment_id"]) for record in candidate_records[:50]],
        "decision_counts": dict(sorted(decisions.items())),
        "next_prompt": "chat_exec_semantic_short_label_autofix_candidate_audit_prompt.md"
        if len(candidate_records) >= 20
        else "chat_exec_remaining_35_final_review_prompt.md",
    }
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
        "candidate_count",
        "candidate_short_label_style_normalization",
        "candidate_autofix_unknown_literal_cleanup",
        "candidate_semantic_minor_lexical_repair",
        "candidate_spacing_punctuation_cleanup",
        "candidate_case_or_article_cleanup",
        "candidate_ptbr_naturalness_review",
        "blocked_by_dynamic_token",
        "blocked_by_structural_guard_overlap",
        "blocked_by_domain_context",
        "blocked_by_gender_or_perspective",
        "blocked_by_name_title_culture",
        "blocked_by_uncertain_semantics",
        "blocked_by_token_integrity",
        "blocked_no_safe_candidate",
        "would_change_output_count",
        "false_safe_risk_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "requires_human_review_count",
    ]
    candidate_type = "nenhum"
    for key in [
        "candidate_semantic_minor_lexical_repair",
        "candidate_spacing_punctuation_cleanup",
        "candidate_short_label_style_normalization",
        "candidate_autofix_unknown_literal_cleanup",
    ]:
        if summary[key]:
            candidate_type = key
            break
    lines = [
        "semantic + short_label + autofix candidate discovery",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        f"top_cohorts={json.dumps(summary['top_cohorts'], ensure_ascii=False, sort_keys=True)}",
        f"sample_candidate_ids={summary['sample_candidate_ids']}",
        "",
        "analysis:",
        "1. Existe universo suficiente fora dos blocos estruturais guardados: sim, ha milhares de segmentos-alvo no ledger.",
        f"2. Surgiram candidatos reais de alteracao: {'sim' if summary['candidate_count'] else 'nao'} ({summary['candidate_count']}).",
        f"3. Tipo dominante: {candidate_type}.",
        f"4. Auditaveis em lote: {'sim' if summary['candidate_count'] >= 20 else 'ainda nao; volume abaixo do corte de 20'}.",
        "5. Risco principal: a maioria do universo ainda tem token dinamico, overlap estrutural ou semantica incerta.",
        f"6. Resolver especifico agora: {'sim, auditoria focal do maior tipo' if summary['candidate_count'] >= 20 else 'nao; ampliar/fechar cobertura antes'}",
        "7. Producao full agora: nao.",
        "8. Network agora: sem redesign; data-only futuro se esta familia virar resolver.",
        "",
        f"next_prompt={summary['next_prompt']}",
        "production_full_recommended=false",
        "network_update_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")
    validate_required_inputs()
    with connect_readonly() as conn:
        universe = fetch_universe(conn, args.segment_state_run_id, args.ledger_run_id)
    universe.sort(key=candidate_priority)
    selected = universe[: args.limit]
    records = [classify_record(row) for row in selected]
    summary = build_summary(records, len(universe))
    if summary["false_safe_risk_count"] or summary["requires_lifecycle_later_count"]:
        raise SystemExit("zero-risk discovery guard failed")
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"summary: {summary_path}")
    for key in [
        "total_universe",
        "total_reviewed",
        "candidate_count",
        "candidate_semantic_minor_lexical_repair",
        "candidate_spacing_punctuation_cleanup",
        "blocked_by_structural_guard_overlap",
        "blocked_by_dynamic_token",
        "blocked_by_token_integrity",
        "false_safe_risk_count",
        "requires_lifecycle_later_count",
        "next_prompt",
    ]:
        print(f"{key}: {summary[key]}")


if __name__ == "__main__":
    main()
