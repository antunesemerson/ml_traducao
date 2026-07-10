from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_select_singleline_architecture_packet_v1"
INPUT_JSONL = Path("reports/20260701_172230_137174_domain_policy_vote_candidate_phase3_dynamic_select_review.jsonl")
CURRENT_RUN_ID = 533
EXPECTED_COUNT = 14
SAMPLE_LIMIT = 8

SELECT_EXPR_RE = re.compile(r"\[(?:[^\[\]]*?)(?:Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString)\s*\([^\]]+\)\]")
SELECT_KIND_RE = re.compile(r"(Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString)\s*\(", re.IGNORECASE)
PROTECTED_TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|!/-]+|#!|@[A-Za-z0-9_]+!|\b(?:ROOT|FROM|SCOPE|TARGET|THIS)\."
)
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"](ES_[A-Za-z0-9_]+)['\"]\)\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only architecture packet for single-line Select_CString phase 3 residuals.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def fetch_segment_context(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_line_number,
          s.source_key,
          s.english_text,
          s.spanish_text,
          s.old_text,
          o.portuguese_text AS indexed_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        segment_ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_registry(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    keys = [
        "select_cstring_player_target_direct_policy",
        "select_cstring_es_helper_policy",
        "select_cstring_requirement_policy",
        "gender_local_player_requirement_policy",
        "select_cstring_ui_subpolicy",
        "select_cstring_same_token_lifecycle_policy",
        "medium_dynamic_light_selectlocalization_affix_policy",
    ]
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT agent_key, operational_state, decision_role, notes_json FROM ml_agent_registry WHERE agent_key IN ({placeholders})",
        keys,
    ).fetchall()
    return {str(row["agent_key"]): dict(row) for row in rows}


def select_expressions(text: str) -> list[str]:
    matches = SELECT_EXPR_RE.findall(text or "")
    if matches:
        return matches
    return [match.group(0) for match in re.finditer(r"\[[^\]]*(?:Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString)[^\]]*\]", text or "")]


def select_functions(expressions: list[str]) -> list[str]:
    functions: list[str] = []
    for expression in expressions:
        functions.extend(match.group(1) for match in SELECT_KIND_RE.finditer(expression))
    return sorted(set(functions))


def protected_tokens(*texts: str) -> list[str]:
    tokens: set[str] = set()
    for text in texts:
        tokens.update(PROTECTED_TOKEN_RE.findall(text or ""))
    return sorted(tokens)


def architecture_family(row: dict[str, Any], text: str) -> str:
    if row.get("risk_bucket") == "needs_select_cstring_player_policy_review":
        return "select_cstring_player_perspective_singleline"
    if "IsFemale" in text:
        if ES_TOKEN_RE.search(text):
            return "select_cstring_gender_literal_plus_es_helper_singleline"
        return "select_cstring_gender_literal_singleline"
    return "select_singleline_unknown"


def policy_fit(family: str) -> tuple[str, str]:
    if family == "select_cstring_player_perspective_singleline":
        return (
            "likely_absorb_existing_select_cstring_player_target_direct_policy",
            "Player/perspective branch matches existing Select_CString player-target terminal policy surface; architecture should confirm guards.",
        )
    if family == "select_cstring_gender_literal_singleline":
        return (
            "likely_absorb_existing_gender_or_select_cstring_requirement_policy",
            "Single Select_CString IsFemale literal branch without multiline or ES helper; likely absorbed by existing gender/select-cstring requirement routing after architecture review.",
        )
    if family == "select_cstring_gender_literal_plus_es_helper_singleline":
        return (
            "needs_subpolicy_or_es_helper_absorption_review",
            "Gender literal and ES helper coexist in one single-line segment; architecture should decide precedence between Select_CString gender routing and ES helper policy.",
        )
    return ("needs_architecture_review", "No existing policy fit can be inferred safely.")


def build_record(row: dict[str, Any], context: dict[str, Any], current_run_id: int) -> dict[str, Any]:
    confirmed_text = str(row.get("confirmed_text") or "")
    output_text = str(row.get("output_text") or context.get("indexed_output_text") or "")
    source_text = str(context.get("spanish_text") or "")
    english_text = str(context.get("english_text") or "")
    expressions = select_expressions(confirmed_text)
    family = architecture_family(row, confirmed_text)
    fit, rationale = policy_fit(family)
    return {
        "source": SOURCE,
        "current_run_id": current_run_id,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or context.get("relative_path"),
        "source_line_number": context.get("source_line_number"),
        "source_key": row.get("source_key") or context.get("source_key"),
        "risk_bucket": row.get("risk_bucket"),
        "structural_subtype": row.get("structural_subtype"),
        "architecture_family": family,
        "policy_fit_recommendation": fit,
        "policy_fit_rationale": rationale,
        "select_functions": select_functions(expressions),
        "select_expressions": expressions,
        "has_gender_literal": "IsFemale" in confirmed_text,
        "has_player_perspective": any(marker in confirmed_text for marker in ("IsLocalPlayer", "GetLocalPlayer", ".IsPlayer")),
        "has_es_helper": bool(ES_TOKEN_RE.search(confirmed_text)),
        "es_token_types": sorted(set(ES_TOKEN_RE.findall(confirmed_text))),
        "protected_tokens": protected_tokens(source_text, confirmed_text, output_text),
        "protected_token_count": len(protected_tokens(source_text, confirmed_text, output_text)),
        "structure_flags": {
            "single_line": "\\n" not in confirmed_text and "\n" not in confirmed_text,
            "select_total_count": int(row.get("select_total_count") or 0),
            "getter_count": int(row.get("getter_count") or 0),
            "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
            "needs_output_apply": int(row.get("needs_output_apply") or 0),
            "open_issue_count": int(row.get("open_issue_count") or 0),
            "high_issue_count": int(row.get("high_issue_count") or 0),
        },
        "english_text": english_text,
        "source_text": source_text,
        "old_text": context.get("old_text"),
        "output_text": output_text,
        "confirmed_text": confirmed_text,
        "indexed_output_text": context.get("indexed_output_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def top(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def build(input_jsonl: Path, current_run_id: int, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(input_jsonl)
        if row.get("risk_bucket")
        in {"needs_select_cstring_gender_policy_review", "needs_select_cstring_player_policy_review"}
    ]
    if len(rows) != expected_count:
        raise SystemExit(f"single-line select count guard failed: {len(rows)}")

    segment_ids = [int(row["segment_id"]) for row in rows]
    with connect_readonly() as conn:
        contexts = fetch_segment_context(conn, segment_ids)
        registry = fetch_registry(conn)

    records = [build_record(row, contexts.get(int(row["segment_id"]), {}), current_run_id) for row in rows]
    records.sort(key=lambda item: (item["architecture_family"], item["segment_id"]))

    family_counts = Counter(row["architecture_family"] for row in records)
    risk_counts = Counter(row["risk_bucket"] for row in records)
    fit_counts = Counter(row["policy_fit_recommendation"] for row in records)
    function_counts = Counter(function for row in records for function in row["select_functions"])
    es_counts = Counter("has_es_helper" if row["has_es_helper"] else "no_es_helper" for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["architecture_family"]]) < SAMPLE_LIMIT:
            samples[row["architecture_family"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_select_singleline_architecture_packet",
        "input_jsonl": str(input_jsonl),
        "current_run_id": current_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "architecture_family_counts": dict(family_counts.most_common()),
        "risk_bucket_counts": dict(risk_counts.most_common()),
        "policy_fit_recommendation_counts": dict(fit_counts.most_common()),
        "select_function_counts": dict(function_counts.most_common()),
        "es_helper_counts": dict(es_counts.most_common()),
        "registry_context": registry,
        "samples_by_architecture_family": samples,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Send this read-only packet to architecture. Do not materialize lifecycle yet. "
            "The 13 gender-literal single-line cases likely belong under existing gender/select-cstring routing, "
            "while the 1 player-perspective case likely belongs under select_cstring_player_target_direct_policy."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_select_singleline_architecture_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 Select_CString single-line architecture packet",
        f"record_count={summary['record_count']}",
        "",
        "Architecture families:",
    ]
    for key, count in summary["architecture_family_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Policy fit recommendations:"])
    for key, count in summary["policy_fit_recommendation_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Items:"])
    for row in records:
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['architecture_family']}",
                f"- source_key: {row['source_key']}",
                f"- path: {row['relative_path']}",
                f"- policy_fit: {row['policy_fit_recommendation']}",
                f"- select_functions: {', '.join(row['select_functions'])}",
                f"- has_gender_literal: {row['has_gender_literal']}",
                f"- has_player_perspective: {row['has_player_perspective']}",
                f"- has_es_helper: {row['has_es_helper']}",
                f"- protected_token_count: {row['protected_token_count']}",
                f"- select_expressions: {json.dumps(row['select_expressions'], ensure_ascii=False)}",
                f"- source_text: {row['source_text']}",
                f"- output_text: {row['output_text']}",
                f"- confirmed_text: {row['confirmed_text']}",
            ]
        )
    lines.extend(
        [
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "segment_state_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "source_changed=false",
            "output_changed=false",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args.input_jsonl, args.current_run_id, args.expected_count)
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
