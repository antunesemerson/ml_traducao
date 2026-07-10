from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "accolade_article_gender_pipe_token_architecture_v1"
SEGMENT_STATE_RUN_ID = 406
LEDGER_RUN_ID = 76
POST_LEARNING_SUMMARY = "reports/20260625_124820_241774_accolade_short_label_post_learning_status_summary.json"
INPUT_JSONL = "reports/20260625_124314_335331_accolade_short_label_tooltip_sublane_diagnostic.jsonl"
INPUT_DECISION = "needs_article_gender_policy_review"

ACCLAIMED_UNLOCK_RE = re.compile(
    r"\[(?:accolade_knight|marauder|besieger)\.GetName\].*?\[acclaimed\|El\].*?\[GetAccoladeType\('[^']+'\)\.GetName\|l\]",
    re.IGNORECASE,
)
PIPE_TOKEN_RE = re.compile(r"\[[^\]]+\|[^\]]+\]")
SELECT_RE = re.compile(r"SelectLocalization|Select_CString|AddLocalizationIf", re.IGNORECASE)
WARNING_RE = re.compile(r"@warning_icon|#X|#T|#!", re.IGNORECASE)
MULTILINE_RE = re.compile(r"\n|\\n|\$TAB\$|#indent_newline", re.IGNORECASE)
PRONOUN_RE = re.compile(r"GetSheHe|GetHerHis|GetHersHis|GetWomanMan|GetDaughterSon", re.IGNORECASE)
GETTER_RE = re.compile(r"\[[^\]]*(?:Get[A-Za-z0-9_]+|ROOT\.|FROM\.|SCOPE\.|TARGET\.)[^\]]*\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def project_path(value: str) -> Path:
    return db.project_path(value)


def reports_dir() -> Path:
    path = project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_accolade_article_gender_pipe_token_architecture"
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


def pipe_tokens(text: str) -> list[str]:
    return PIPE_TOKEN_RE.findall(text)


def subtype(row: dict[str, Any]) -> str:
    text = as_text(row.get("current_output_text"))
    if SELECT_RE.search(text):
        return "hold_select_localization_or_branching"
    if WARNING_RE.search(text) or MULTILINE_RE.search(text):
        return "hold_warning_or_multiline_surface"
    if PRONOUN_RE.search(text):
        return "hold_dynamic_pronoun_gender_surface"
    if ACCLAIMED_UNLOCK_RE.search(text):
        return "acclaimed_unlock_article_gender_pattern"
    if "[accolades|" in text and "GetAccoladeType(" in text:
        return "accolade_type_create_article_gender_pattern"
    if GETTER_RE.search(text) and len(pipe_tokens(text)) >= 2:
        return "general_getter_pipe_article_gender_pattern"
    return "general_pipe_article_gender_pattern"


def architecture_decision(row: dict[str, Any]) -> str:
    key = subtype(row)
    if key in {
        "acclaimed_unlock_article_gender_pattern",
        "accolade_type_create_article_gender_pattern",
        "general_getter_pipe_article_gender_pattern",
        "general_pipe_article_gender_pattern",
    }:
        return "guarded_human_policy_review_ready"
    return "hold_high_context_or_dynamic"


def architecture_row(row: dict[str, Any], state: sqlite3.Row | None) -> dict[str, Any]:
    text = as_text(row.get("current_output_text"))
    sub = subtype(row)
    decision = architecture_decision(row)
    return {
        "source": SOURCE,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "review_decision": row.get("decision"),
        "architecture_subtype": sub,
        "architecture_decision": decision,
        "allowed_for_next_policy_review": decision == "guarded_human_policy_review_ready",
        "auto_apply_allowed": False,
        "auto_lifecycle_allowed": False,
        "output_apply_allowed": False,
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "state_needs_output_apply": int(state["needs_output_apply"]) if state else None,
        "state_confirmed_matches_output": int(state["confirmed_matches_output"]) if state else None,
        "state_group": state["state_group"] if state else None,
        "final_state": state["final_state"] if state else row.get("final_state"),
        "token_count": int(row.get("token_count") or 0),
        "pipe_tokens": pipe_tokens(text),
        "has_pipe_token": bool(row.get("has_pipe_token")),
        "has_getter": bool(row.get("has_getter")),
        "has_select_localization": bool(row.get("has_select_localization")),
        "has_warning": bool(row.get("has_warning")),
        "is_multiline": bool(row.get("is_multiline")),
        "has_dynamic_pronoun": bool(PRONOUN_RE.search(text)),
        "current_output_text": compact(text),
        "spanish_text": compact(row.get("spanish_text")),
        "english_text": compact(row.get("english_text")),
        "rationale": row.get("rationale"),
    }


def counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": value} for key, value in counter.most_common()]


def sample_by(rows: list[dict[str, Any]], field: str, limit: int = 4) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = as_text(row.get(field))
        if len(grouped[key]) < limit:
            grouped[key].append(
                {
                    "segment_id": row["segment_id"],
                    "source_key": row.get("source_key"),
                    "current_output_text": row.get("current_output_text"),
                    "pipe_tokens": row.get("pipe_tokens"),
                }
            )
    return grouped


def fetch_state_rows(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, final_state, confirmed_matches_output, needs_output_apply
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        [SEGMENT_STATE_RUN_ID, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): row for row in rows}


def validate(rows: list[dict[str, Any]], learning_summary: dict[str, Any]) -> None:
    if len(rows) != 40:
        raise SystemExit(f"expected 40 article/gender rows, found {len(rows)}")
    if int(learning_summary.get("learned_candidate_count") or 0) != 4:
        raise SystemExit("post-learning summary does not confirm four learned candidates")
    if int(learning_summary.get("learned_unsynced_count") or 0) != 0:
        raise SystemExit("post-learning summary has unsynced learned candidates")
    if int(learning_summary.get("learned_state_mismatch_count") or 0) != 0:
        raise SystemExit("post-learning summary has state mismatches")
    if int(learning_summary.get("needs_output_apply_segments_count") or 0) != 0:
        raise SystemExit("post-learning summary has needs_output_apply segments")
    if any(row["state_needs_output_apply"] not in (0, None) for row in rows):
        raise SystemExit("state_needs_output_apply must be zero for all architecture rows")
    if any(row["state_confirmed_matches_output"] not in (1, None) for row in rows):
        raise SystemExit("state_confirmed_matches_output must be one for all architecture rows")


def main() -> None:
    learning_summary = read_json(project_path(POST_LEARNING_SUMMARY))
    source_rows = [
        row
        for row in read_jsonl(project_path(INPUT_JSONL))
        if as_text(row.get("decision")) == INPUT_DECISION
    ]
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with readonly_conn() as conn:
        latest_run = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs").fetchone()["id"]
        state_rows = fetch_state_rows(conn, segment_ids)

    rows = [architecture_row(row, state_rows.get(int(row["segment_id"]))) for row in source_rows]
    validate(rows, learning_summary)

    ready_rows = [row for row in rows if row["allowed_for_next_policy_review"]]
    hold_rows = [row for row in rows if not row["allowed_for_next_policy_review"]]
    subtype_counts = Counter(row["architecture_subtype"] for row in rows)
    decision_counts = Counter(row["architecture_decision"] for row in rows)
    pipe_counts = Counter(token for row in rows for token in row["pipe_tokens"])

    summary = {
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "latest_segment_state_run_id_seen": int(latest_run or 0),
        "ledger_run_id": LEDGER_RUN_ID,
        "post_learning_summary": str(project_path(POST_LEARNING_SUMMARY)),
        "input_jsonl": str(project_path(INPUT_JSONL)),
        "reviewed_count": len(rows),
        "guarded_policy_review_ready_count": len(ready_rows),
        "hold_count": len(hold_rows),
        "architecture_subtype_counts": counter_rows(subtype_counts),
        "architecture_decision_counts": counter_rows(decision_counts),
        "pipe_token_counts": counter_rows(pipe_counts),
        "auto_apply_allowed": False,
        "auto_lifecycle_allowed": False,
        "output_apply_allowed": False,
        "direct_lifecycle_candidate_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "production_full_recommended_now": False,
        "registry_registration_recommended_now": False,
        "next_prompt_recommended": "chat_exec_accolade_article_gender_pipe_token_policy_review_prompt.md",
        "architecture_recommendation": (
            "Open a guarded read-only policy review for the 40 article/gender pipe-token rows. "
            "Do not auto-apply or close lifecycle; first split deterministic acclaimed/accolade patterns from general pipe-token PT-BR wording."
        ),
    }
    spec = {
        "policy_key": "accolade_article_gender_pipe_token_policy_design",
        "policy_type": "read_only_guarded_policy_review",
        "parent_lane": "short_label_style/accolade",
        "input_decision": INPUT_DECISION,
        "eligible_count": len(ready_rows),
        "hold_count": len(hold_rows),
        "deterministic_auto_apply_allowed": False,
        "deterministic_auto_lifecycle_allowed": False,
        "guards": {
            "preserve_pipe_tokens_exactly": True,
            "preserve_getters_exactly": True,
            "preserve_formatting_tags_exactly": True,
            "needs_output_apply": 0,
            "confirmed_matches_output": 1,
            "exclude_select_localization": True,
            "exclude_warning_tooltips": True,
            "exclude_multiline_dense_context": True,
            "exclude_dynamic_pronoun_gender": True,
        },
        "subtypes": counter_rows(subtype_counts),
        "sample_by_subtype": sample_by(rows, "architecture_subtype"),
    }

    txt_path, jsonl_path, summary_json_path, spec_json_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    spec_json_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Accolade Article/Gender Pipe Token Architecture\n")
        handle.write("==============================================\n\n")
        handle.write(f"reviewed_count: {len(rows)}\n")
        handle.write(f"guarded_policy_review_ready_count: {len(ready_rows)}\n")
        handle.write(f"hold_count: {len(hold_rows)}\n")
        handle.write("auto_apply_allowed: false\n")
        handle.write("auto_lifecycle_allowed: false\n")
        handle.write("production_full_recommended_now: false\n\n")
        handle.write("Architecture subtype counts:\n")
        for item in summary["architecture_subtype_counts"]:
            handle.write(f"- {item['key']}: {item['count']}\n")
        handle.write("\nArchitecture decision counts:\n")
        for item in summary["architecture_decision_counts"]:
            handle.write(f"- {item['key']}: {item['count']}\n")
        handle.write("\nTop pipe tokens:\n")
        for item in summary["pipe_token_counts"][:20]:
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
