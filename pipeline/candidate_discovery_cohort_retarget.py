from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
INPUTS = {
    "prior_discovery_jsonl": "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery.jsonl",
    "prior_discovery_summary": "reports/20260623_220129_729915_semantic_short_label_autofix_candidate_discovery_summary.json",
    "audit_jsonl": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit.jsonl",
    "audit_summary": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit_summary.json",
    "expand_jsonl": "reports/20260623_233604_073950_semantic_short_label_autofix_candidate_discovery_expand.jsonl",
    "expand_summary": "reports/20260623_233604_073950_semantic_short_label_autofix_candidate_discovery_expand_summary.json",
    "architecture_inventory": "reports/20260623_164747_499852_global_final_architecture_before_resolution_diagnostic_inventory.json",
}

DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|Select_CString\(|\.Custom\('ES_|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+"
)
SHORT_LABEL_RE = re.compile(r"^\s*(?:#T|\[[^\]]+\]|\$[^$]+\$|[A-Z_]+:|\w+_name\b|[^\n]{0,80}$)", re.IGNORECASE)
AUTOFIX_LITERAL_RE = re.compile(
    r"\b(?:acuerdo|bueno|buenos|buena|buenas|eso|poco|poca|pocos|pocas|mucho|mucha|muchos|muchas|estudios|nivel)\b|"
    r"muchos m(?:\u00c3\u00a1|á)s",
    re.IGNORECASE,
)
NAME_TITLE_CULTURE_FAMILIES = {
    "title_policy_microagent",
    "nickname_name_policy",
    "culture_semantic_microagent",
    "religion_semantic_microagent",
}
GENDER_FAMILIES = {"gender_token_microagent", "gender_local_player_policy"}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_candidate_discovery_cohort_retarget"
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


def load_and_validate_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    paths = {key: db.project_path(value) for key, value in INPUTS.items()}
    for path in paths.values():
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    prior = read_json(paths["prior_discovery_summary"])
    audit = read_json(paths["audit_summary"])
    expand = read_json(paths["expand_summary"])
    if len([row for row in read_jsonl(paths["audit_jsonl"]) if row.get("safe_for_future_apply_batch")]) != 8:
        raise SystemExit("audit safe seed row guard failed")
    if len(read_jsonl(paths["expand_jsonl"])) != 600:
        raise SystemExit("expand JSONL line guard failed")
    required_prior = {
        "total_reviewed": 240,
        "candidate_count": 8,
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
    }
    for key, expected in required_audit.items():
        if int(audit.get(key) or 0) != expected:
            raise SystemExit(f"audit guard failed: {key}")
    required_expand = {
        "total_reviewed": 600,
        "candidate_count": 0,
        "false_safe_risk_count": 0,
        "requires_lifecycle_later_count": 0,
    }
    for key, expected in required_expand.items():
        if int(expand.get(key) or 0) != expected:
            raise SystemExit(f"expand guard failed: {key}")
    return prior, audit, expand


def fetch_pending_universe(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
        SELECT
            l.segment_id,
            GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
            GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
            COUNT(*) AS open_issue_count
        FROM ml_issue_ledger_items l
        JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
        WHERE l.run_id = ?
          AND l.status = 'open'
          AND s.state_group = 'pending'
          AND s.is_closed = 0
          AND s.needs_output_apply = 0
          AND s.confirmed_matches_output = 1
        GROUP BY l.segment_id
    )
    SELECT
        f.segment_id,
        f.all_families,
        f.all_agents,
        f.open_issue_count,
        src.old_text,
        out.portuguese_text AS current_output_text
    FROM fam f
    LEFT JOIN source_segments src ON src.id = f.segment_id
    LEFT JOIN output_segments out ON out.segment_id = f.segment_id
    """
    rows: list[dict[str, Any]] = []
    for row in conn.execute(query, (segment_state_run_id, ledger_run_id)):
        families = {part for part in str(row["all_families"] or "").split(",") if part}
        text = str(row["current_output_text"] or "")
        rows.append(
            {
                "segment_id": int(row["segment_id"]),
                "families": families,
                "agents": {part for part in str(row["all_agents"] or "").split(",") if part},
                "open_issue_count": int(row["open_issue_count"] or 0),
                "original_text": str(row["old_text"] or ""),
                "current_output_text": text,
                "dynamic": bool(DYNAMIC_RE.search(text)),
                "short_label_surface": bool(SHORT_LABEL_RE.search(text)) and len(text) <= 140,
                "autofix_literal_surface": bool(AUTOFIX_LITERAL_RE.search(text)),
            }
        )
    return rows


def structural_overlap(row: dict[str, Any], primary: set[str]) -> bool:
    return bool(row["families"] - primary)


def density(rows: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if predicate(row)) / len(rows), 4)


def level_from_density(value: float, inverse: bool = False) -> str:
    if inverse:
        if value <= 0.20:
            return "high"
        if value <= 0.55:
            return "medium"
        return "low"
    if value >= 0.50:
        return "high"
    if value >= 0.15:
        return "medium"
    return "low"


def false_safe_level(structural_rate: float, dynamic_density: float, name_density: float, gender_density: float) -> str:
    risk_score = structural_rate * 0.35 + dynamic_density * 0.25 + name_density * 0.25 + gender_density * 0.15
    if risk_score >= 0.45:
        return "high"
    if risk_score >= 0.20:
        return "medium"
    return "low"


def analyze_cohort(
    universe: list[dict[str, Any]],
    cohort_key: str,
    selector: Callable[[dict[str, Any]], bool],
    primary_families: set[str],
    discovery_type: str,
) -> dict[str, Any]:
    rows = [row for row in universe if selector(row)]
    eligible_count = len(rows)
    sample_available_count = min(eligible_count, 600)
    structural_rate = density(rows, lambda row: structural_overlap(row, primary_families))
    dynamic_density = density(rows, lambda row: row["dynamic"])
    name_density = density(rows, lambda row: bool(row["families"] & NAME_TITLE_CULTURE_FAMILIES))
    gender_density = density(rows, lambda row: bool(row["families"] & GENDER_FAMILIES))
    short_density = density(rows, lambda row: row["short_label_surface"])
    literal_density = density(rows, lambda row: row["autofix_literal_surface"])
    if discovery_type == "autofix_literal":
        signal = literal_density
    elif discovery_type == "short_label":
        signal = short_density
    elif discovery_type == "semantic_single_family":
        signal = density(rows, lambda row: not row["dynamic"] and not row["families"] & NAME_TITLE_CULTURE_FAMILIES)
    elif discovery_type == "gender_perspective":
        signal = gender_density
    else:
        signal = max(short_density, literal_density)
    penalty = max(structural_rate, name_density, gender_density if discovery_type != "gender_perspective" else 0.0)
    yield_score = max(0.0, signal * (1.0 - penalty))
    expected_yield = "high" if yield_score >= 0.18 and eligible_count >= 30 else "medium" if yield_score >= 0.05 and eligible_count >= 10 else "low"
    risk = false_safe_level(structural_rate, dynamic_density, name_density, gender_density)
    recommended_limit = 600 if eligible_count >= 600 else eligible_count
    notes = (
        f"signal={round(signal, 4)}; penalty={round(penalty, 4)}; "
        f"yield_score={round(yield_score, 4)}"
    )
    return {
        "cohort_key": cohort_key,
        "eligible_count": eligible_count,
        "sample_available_count": sample_available_count,
        "structural_guard_overlap_rate": structural_rate,
        "dynamic_token_density": dynamic_density,
        "name_title_culture_density": name_density,
        "gender_perspective_density": gender_density,
        "short_label_surface_density": short_density,
        "autofix_literal_surface_density": literal_density,
        "expected_candidate_yield": expected_yield,
        "expected_false_safe_risk": risk,
        "recommended_limit": recommended_limit,
        "recommended_discovery_type": discovery_type if expected_yield != "low" else "hold",
        "recommended": False,
        "notes": notes,
        "_yield_score": yield_score,
    }


def build_records(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        analyze_cohort(
            universe,
            "short_label_style_microagent_clean_no_dynamic_parser",
            lambda row: "short_label_style_microagent" in row["families"] and "dynamic_ck3_expression_microagent" not in row["families"],
            {"short_label_style_microagent"},
            "short_label",
        ),
        analyze_cohort(
            universe,
            "autofix_unknown_microagent_single_family",
            lambda row: row["families"] == {"autofix_unknown_microagent"},
            {"autofix_unknown_microagent"},
            "autofix_literal",
        ),
        analyze_cohort(
            universe,
            "semantic_review_router_single_family",
            lambda row: row["families"] == {"semantic_review_router"},
            {"semantic_review_router"},
            "semantic_single_family",
        ),
        analyze_cohort(
            universe,
            "culture_semantic_microagent_single_family",
            lambda row: row["families"] == {"culture_semantic_microagent"},
            {"culture_semantic_microagent"},
            "semantic_single_family",
        ),
        analyze_cohort(
            universe,
            "gender_token_microagent_plus_semantic_review_router",
            lambda row: {"gender_token_microagent", "semantic_review_router"} <= row["families"],
            {"gender_token_microagent", "semantic_review_router"},
            "gender_perspective",
        ),
        analyze_cohort(
            universe,
            "dynamic_ck3_expression_microagent_plus_gender_token_microagent",
            lambda row: {"dynamic_ck3_expression_microagent", "gender_token_microagent"} <= row["families"],
            {"dynamic_ck3_expression_microagent", "gender_token_microagent"},
            "gender_perspective",
        ),
        analyze_cohort(
            universe,
            "semantic_review_router_plus_short_label_no_structural_overlap",
            lambda row: {"semantic_review_router", "short_label_style_microagent"} <= row["families"]
            and not structural_overlap(row, {"semantic_review_router", "short_label_style_microagent"}),
            {"semantic_review_router", "short_label_style_microagent"},
            "short_label",
        ),
        analyze_cohort(
            universe,
            "autofix_unknown_microagent_plus_semantic_no_structural_overlap",
            lambda row: {"autofix_unknown_microagent", "semantic_review_router"} <= row["families"]
            and not structural_overlap(row, {"autofix_unknown_microagent", "semantic_review_router"}),
            {"autofix_unknown_microagent", "semantic_review_router"},
            "autofix_literal",
        ),
    ]


def choose_recommendation(records: list[dict[str, Any]], excluded_cohorts: set[str] | None = None) -> dict[str, Any]:
    excluded_cohorts = excluded_cohorts or set()
    for record in records:
        risk_penalty = {"low": 0.0, "medium": 0.04, "high": 0.12}[record["expected_false_safe_risk"]]
        type_bonus = {
            "autofix_literal": 0.04,
            "short_label": 0.03,
            "semantic_single_family": 0.01,
            "gender_perspective": -0.04,
            "hold": -0.10,
        }.get(record["recommended_discovery_type"], 0.0)
        volume_bonus = min(record["eligible_count"], 600) / 6000
        record["_score"] = record["_yield_score"] + type_bonus + volume_bonus - risk_penalty
    viable = [
        record
        for record in records
        if record["eligible_count"] > 0
        and record["expected_false_safe_risk"] != "high"
        and record["recommended_discovery_type"] != "hold"
        and record["cohort_key"] not in excluded_cohorts
    ]
    if not viable:
        viable = [record for record in records if record["eligible_count"] > 0 and record["cohort_key"] not in excluded_cohorts]
    if not viable:
        viable = [record for record in records if record["eligible_count"] > 0]
    recommended = max(viable, key=lambda row: (row["_score"], row["eligible_count"])) if viable else records[0]
    recommended["recommended"] = True
    return recommended


def build_summary(records: list[dict[str, Any]], recommended: dict[str, Any], excluded_cohorts: set[str] | None = None) -> dict[str, Any]:
    excluded_cohorts = excluded_cohorts or set()
    avoid = [
        row["cohort_key"]
        for row in records
        if row["expected_false_safe_risk"] == "high"
        or row["structural_guard_overlap_rate"] >= 0.5
        or row["gender_perspective_density"] >= 0.5
        or row["name_title_culture_density"] >= 0.5
    ]
    fallback_candidates = [row for row in sorted(records, key=lambda row: row.get("_score", 0), reverse=True) if not row["recommended"]]
    fallback = fallback_candidates[0]["cohort_key"] if fallback_candidates else ""
    summary = {
        "schema_version": 1,
        "source": "candidate_discovery_cohort_retarget_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "cohorts_reviewed": len(records),
        "recommended_cohort_key": recommended["cohort_key"],
        "excluded_cohorts": sorted(excluded_cohorts),
        "recommended_limit": recommended["recommended_limit"],
        "recommended_discovery_type": recommended["recommended_discovery_type"],
        "reason": recommended["notes"],
        "fallback_cohort_key": fallback,
        "avoid_cohorts": avoid,
        "production_full_recommended_now": False,
        "apply_ready_now": 0,
        "network_update_recommended": False,
        "next_prompt": next_prompt_for(recommended),
    }
    return summary


def next_prompt_for(recommended: dict[str, Any]) -> str:
    key = recommended["cohort_key"]
    if key == "autofix_unknown_microagent_single_family":
        return "chat_exec_autofix_unknown_single_family_candidate_discovery_prompt.md"
    if key == "short_label_style_microagent_clean_no_dynamic_parser":
        return "chat_exec_short_label_clean_candidate_discovery_prompt.md"
    if key == "semantic_review_router_single_family":
        return "chat_exec_semantic_single_family_candidate_discovery_prompt.md"
    if recommended["recommended_discovery_type"] == "short_label":
        return "chat_exec_short_label_clean_candidate_discovery_prompt.md"
    if recommended["recommended_discovery_type"] == "autofix_literal":
        return "chat_exec_autofix_unknown_single_family_candidate_discovery_prompt.md"
    return "chat_exec_semantic_single_family_candidate_discovery_prompt.md"


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    public_records = [{key: value for key, value in record.items() if not key.startswith("_")} for record in records]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in public_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recommended = next(record for record in public_records if record["recommended"])
    lines = [
        "candidate discovery cohort retarget",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"cohorts_reviewed={summary['cohorts_reviewed']}",
        "",
        "recommended:",
        f"recommended_cohort_key={summary['recommended_cohort_key']}",
        f"recommended_limit={summary['recommended_limit']}",
        f"recommended_discovery_type={summary['recommended_discovery_type']}",
        f"reason={summary['reason']}",
        f"fallback_cohort_key={summary['fallback_cohort_key']}",
        f"avoid_cohorts={summary['avoid_cohorts']}",
        "",
        "recommended_metrics:",
        json.dumps(recommended, ensure_ascii=False, sort_keys=True),
        "",
        "analysis:",
        "1. A expansao anterior nao generalizou porque os 8 seeds eram residuos literais raros dentro de um cohort amplo; o lote expandido virou 500 sem candidato seguro, 98 overlaps estruturais e 2 ambiguos.",
        f"2. Melhor chance agora: {summary['recommended_cohort_key']}.",
        f"3. Risco principal: {recommended['expected_false_safe_risk']} false-safe risk, com dynamic_density={recommended['dynamic_token_density']} e structural_overlap={recommended['structural_guard_overlap_rate']}.",
        f"4. Limite do proximo discovery: {summary['recommended_limit']}.",
        f"5. Tipo de busca: {summary['recommended_discovery_type']}.",
        "6. Producao full agora: false.",
        "7. Network precisa atualizar: false.",
        "",
        f"next_prompt={summary['next_prompt']}",
        "apply_ready_now=0",
        "production_full_recommended_now=false",
        "network_update_recommended=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--exclude-cohort", action="append", default=[])
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    load_and_validate_inputs()
    with connect_readonly() as conn:
        universe = fetch_pending_universe(conn, args.segment_state_run_id, args.ledger_run_id)
    records = build_records(universe)
    excluded_cohorts = set(args.exclude_cohort or [])
    recommended = choose_recommendation(records, excluded_cohorts)
    summary = build_summary(records, recommended, excluded_cohorts)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"cohorts_reviewed={summary['cohorts_reviewed']}")
    print(f"recommended_cohort_key={summary['recommended_cohort_key']}")
    print(f"recommended_limit={summary['recommended_limit']}")
    print(f"recommended_discovery_type={summary['recommended_discovery_type']}")
    print(f"next_prompt={summary['next_prompt']}")


if __name__ == "__main__":
    main()
