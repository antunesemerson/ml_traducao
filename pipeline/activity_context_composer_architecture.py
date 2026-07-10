from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "activity_context_composer_architecture_v1"
SEGMENT_STATE_RUN_ID = 406
LEDGER_RUN_ID = 76
INPUT_REVIEW_JSONL = "reports/20260624_234803_057458_activity_context_composer_run406_review.jsonl"
INPUT_SUMMARY_JSON = "reports/20260624_234803_057458_activity_context_composer_run406_review_summary.json"

READY_DECISIONS = {
    "activity_context_ready_medium_plain_context",
    "activity_context_ready_short_plain_context",
    "activity_context_ready_light_token_composer",
    "activity_context_ready_tournament_short_context",
}
HOLD_DECISIONS = {
    "needs_activity_long_context_human_or_composer",
    "needs_ptbr_fluency_review",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def project_path(value: str) -> Path:
    return db.project_path(value)


def reports_dir() -> Path:
    path = project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_activity_context_composer_architecture"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
        reports_dir() / f"{base.name}_spec.json",
    )


def readonly_conn() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def as_text(value: Any) -> str:
    return str(value or "")


def compact(value: Any, limit: int = 420) -> str:
    text = " ".join(as_text(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def row_decision(row: dict[str, Any]) -> str:
    decision = as_text(row.get("decision"))
    if decision in READY_DECISIONS:
        return "composer_dry_run_ready"
    if decision in HOLD_DECISIONS:
        return "hold_for_human_or_long_context"
    return "hold_unknown_decision"


def architecture_row(row: dict[str, Any]) -> dict[str, Any]:
    arch_decision = row_decision(row)
    return {
        "source": SOURCE,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "activity_family": row.get("activity_family"),
        "review_decision": row.get("decision"),
        "architecture_decision": arch_decision,
        "allowed_for_next_composer_dry_run": arch_decision == "composer_dry_run_ready",
        "excluded_from_lifecycle": True,
        "excluded_from_apply": True,
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "requires_apply_later": bool(row.get("requires_apply_later")),
        "requires_lifecycle_later": bool(row.get("requires_lifecycle_later")),
        "false_safe_risk": bool(row.get("false_safe_risk")),
        "token_count": int(row.get("token_count") or 0),
        "word_count": int(row.get("word_count") or 0),
        "text_length": int(row.get("text_length") or 0),
        "current_output_text": compact(row.get("current_output_text")),
        "english_text": compact(row.get("english_text")),
        "spanish_text": compact(row.get("spanish_text")),
        "reason": row.get("reason"),
    }


def counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": value} for key, value in counter.most_common()]


def sample_by(rows: list[dict[str, Any]], field: str, limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = as_text(row.get(field))
        if len(grouped[key]) < limit:
            grouped[key].append(
                {
                    "segment_id": row["segment_id"],
                    "activity_family": row.get("activity_family"),
                    "review_decision": row.get("review_decision"),
                    "current_output_text": row.get("current_output_text"),
                }
            )
    return grouped


def validate(rows: list[dict[str, Any]], source_summary: dict[str, Any]) -> None:
    if len(rows) != 105:
        raise SystemExit(f"expected 105 review rows, found {len(rows)}")
    if int(source_summary.get("reviewed_count") or 0) != 105:
        raise SystemExit("source summary reviewed_count is not 105")
    if any(row["needs_output_apply"] != 0 for row in rows):
        raise SystemExit("needs_output_apply must be zero for all rows")
    if any(row["confirmed_matches_output"] != 1 for row in rows):
        raise SystemExit("confirmed_matches_output must be one for all rows")
    if any(row["requires_apply_later"] for row in rows):
        raise SystemExit("requires_apply_later must be false for all rows")
    if any(row["requires_lifecycle_later"] for row in rows):
        raise SystemExit("requires_lifecycle_later must be false for all rows")
    if any(row["false_safe_risk"] for row in rows):
        raise SystemExit("false_safe_risk must be false for all rows")


def main() -> None:
    review_path = project_path(INPUT_REVIEW_JSONL)
    summary_path = project_path(INPUT_SUMMARY_JSON)
    source_summary = read_json(summary_path)
    raw_rows = read_jsonl(review_path)
    rows = [architecture_row(row) for row in raw_rows]
    validate(rows, source_summary)

    with readonly_conn() as conn:
        latest_run = conn.execute("SELECT MAX(id) AS run_id FROM segment_state_runs").fetchone()["run_id"]

    decision_counts = Counter(row["review_decision"] for row in rows)
    architecture_counts = Counter(row["architecture_decision"] for row in rows)
    family_counts = Counter(row["activity_family"] for row in rows)
    ready_rows = [row for row in rows if row["allowed_for_next_composer_dry_run"]]
    hold_rows = [row for row in rows if not row["allowed_for_next_composer_dry_run"]]

    summary = {
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "latest_segment_state_run_id_seen": int(latest_run or 0),
        "ledger_run_id": LEDGER_RUN_ID,
        "input_review_jsonl": str(review_path),
        "input_summary_json": str(summary_path),
        "reviewed_count": len(rows),
        "composer_ready_for_dry_run_count": len(ready_rows),
        "hold_count": len(hold_rows),
        "decision_counts": counter_rows(decision_counts),
        "architecture_decision_counts": counter_rows(architecture_counts),
        "activity_family_counts": counter_rows(family_counts),
        "direct_lifecycle_candidate_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_allowed_now": False,
        "lifecycle_allowed_now": False,
        "production_full_recommended_now": False,
        "registry_registration_recommended_now": False,
        "policy_materialization_recommended_now": False,
        "next_prompt_recommended": "chat_exec_activity_context_composer_guarded_dry_run_prompt.md",
        "architecture_recommendation": (
            "Treat the 77 ready rows as a guarded contextual composer dry-run queue only. "
            "Do not close lifecycle, do not apply output, and keep long-context/fluency rows out."
        ),
    }

    spec = {
        "policy_key": "activity_context_guarded_composer_run406",
        "policy_type": "read_only_contextual_composer_lane",
        "parent_lane": "semantic_review_router",
        "source_issue_families": ["autofix_unknown_microagent", "semantic_review_router"],
        "ready_decisions": sorted(READY_DECISIONS),
        "hold_decisions": sorted(HOLD_DECISIONS),
        "eligible_count": len(ready_rows),
        "hold_count": len(hold_rows),
        "guards": {
            "needs_output_apply": 0,
            "confirmed_matches_output": 1,
            "requires_apply_later": False,
            "requires_lifecycle_later": False,
            "false_safe_risk": False,
            "preserve_ck3_tokens_variables_tags_structure": True,
        },
        "explicitly_forbidden": [
            "output_apply",
            "direct_lifecycle_closure",
            "production_full",
            "reindex",
            "segment_state",
            "registry_registration",
        ],
        "next_execution_contract": {
            "mode": "read_only",
            "input_architecture_jsonl": "reports/<generated>_activity_context_composer_architecture.jsonl",
            "allowed_filter": "allowed_for_next_composer_dry_run=true",
            "expected_max_rows": len(ready_rows),
            "must_report": [
                "suggestion_candidates",
                "guarded_no_apply",
                "blocked_by_context_ambiguity",
                "blocked_by_token_integrity",
                "false_safe_risk_count",
                "requires_apply_later_count",
                "requires_lifecycle_later_count",
            ],
        },
        "sample_ready": sample_by(ready_rows, "review_decision", 2),
        "sample_hold": sample_by(hold_rows, "review_decision", 2),
    }

    txt_path, jsonl_path, summary_json_path, spec_json_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    spec_json_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Activity Context Composer Architecture\n")
        handle.write("======================================\n\n")
        handle.write(f"reviewed_count: {len(rows)}\n")
        handle.write(f"composer_ready_for_dry_run_count: {len(ready_rows)}\n")
        handle.write(f"hold_count: {len(hold_rows)}\n")
        handle.write("direct_lifecycle_candidate_count: 0\n")
        handle.write("apply_allowed_now: false\n")
        handle.write("lifecycle_allowed_now: false\n")
        handle.write("registry_registration_recommended_now: false\n")
        handle.write("production_full_recommended_now: false\n\n")
        handle.write("Decision counts:\n")
        for item in summary["decision_counts"]:
            handle.write(f"- {item['key']}: {item['count']}\n")
        handle.write("\nArchitecture decision counts:\n")
        for item in summary["architecture_decision_counts"]:
            handle.write(f"- {item['key']}: {item['count']}\n")
        handle.write("\nRecommendation:\n")
        handle.write(summary["architecture_recommendation"] + "\n")
        handle.write(f"\nNext prompt: {summary['next_prompt_recommended']}\n")

    print(f"wrote {txt_path}")
    print(f"wrote {jsonl_path}")
    print(f"wrote {summary_json_path}")
    print(f"wrote {spec_json_path}")


if __name__ == "__main__":
    main()
