from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "accolade_article_gender_pipe_token_policy_review_v1"
TARGET_REVIEW_DECISION = "needs_article_gender_policy_review"
SAMPLE_PER_DECISION = 8

SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf", re.IGNORECASE)
WARNING_RE = re.compile(r"@warning_icon|#X|#warning", re.IGNORECASE)
DYNAMIC_PRONOUN_RE = re.compile(r"Get(?:SheHe|HerHis|HerHim|HerselfHimself|WomanMan|LadyLord)", re.IGNORECASE)
MULTILINE_RE = re.compile(r"\\n|\n")
GET_ACCOLADE_TYPE_RE = re.compile(r"\[GetAccoladeType\('[^']+'\)\.GetName\|l\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_architecture_jsonl() -> Path:
    matches = sorted(
        reports_dir().glob("*_accolade_article_gender_pipe_token_architecture.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing accolade_article_gender_pipe_token_architecture jsonl")
    return matches[0]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_architecture_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("allowed_for_next_policy_review") is not True:
                continue
            if row.get("review_decision") != TARGET_REVIEW_DECISION:
                continue
            rows.append(row)
    return rows


def fetch_state_rows(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            state_group,
            final_state,
            needs_output_apply,
            confirmed_matches_output,
            review_state,
            locked
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def has_dynamic_pronoun(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            str(row.get("current_output_text") or ""),
            str(row.get("english_text") or ""),
            str(row.get("spanish_text") or ""),
            " ".join(str(token) for token in row.get("pipe_tokens") or []),
        ]
    )
    return bool(row.get("has_dynamic_pronoun") or DYNAMIC_PRONOUN_RE.search(haystack))


def is_guardable_surface(row: dict[str, Any], state_row: dict[str, Any] | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    text = str(row.get("current_output_text") or "")
    if row.get("state_needs_output_apply") or (state_row and int(state_row.get("needs_output_apply") or 0) != 0):
        reasons.append("needs_output_apply")
    if SELECT_RE.search(text) or row.get("has_select_localization"):
        reasons.append("select_localization")
    if WARNING_RE.search(text) or row.get("has_warning"):
        reasons.append("warning_tooltip")
    if MULTILINE_RE.search(text) or row.get("is_multiline"):
        reasons.append("multiline")
    if has_dynamic_pronoun(row):
        reasons.append("dynamic_pronoun")
    if int(row.get("token_count") or 0) > 3:
        reasons.append("token_count_gt_3")
    if state_row and int(state_row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("state_not_confirmed_matches_output")
    return not reasons, reasons


def classify(row: dict[str, Any], state_row: dict[str, Any] | None) -> tuple[str, str, bool, list[str]]:
    text = str(row.get("current_output_text") or "")
    subtype = str(row.get("architecture_subtype") or "")
    pipe_tokens = [str(token) for token in row.get("pipe_tokens") or []]
    guardable, guard_reasons = is_guardable_surface(row, state_row)
    if SELECT_RE.search(text) or row.get("has_select_localization"):
        return "blocked_select_localization_or_branching", "branching localization token is out of scope", True, guard_reasons
    if has_dynamic_pronoun(row):
        return "blocked_dynamic_pronoun_gender", "dynamic pronoun/gender getter is out of scope", True, guard_reasons
    if WARNING_RE.search(text) or row.get("has_warning") or MULTILINE_RE.search(text) or row.get("is_multiline"):
        return "blocked_warning_or_multiline", "warning or multiline token surface is out of scope", True, guard_reasons
    if not guardable:
        return "needs_pipe_article_gender_context_window", "guard checks require a smaller context window", False, guard_reasons
    if subtype == "acclaimed_unlock_article_gender_pattern" and "[acclaimed|El]" in pipe_tokens and GET_ACCOLADE_TYPE_RE.search(text):
        return (
            "acclaimed_unlock_article_gender_guarded_pattern",
            "deterministic acclaimed unlock pattern with article/gender pipe token",
            False,
            guard_reasons,
        )
    if subtype == "accolade_type_create_article_gender_pattern" and "[accolades|lE]" in pipe_tokens and GET_ACCOLADE_TYPE_RE.search(text):
        return (
            "accolade_type_create_article_gender_guarded_pattern",
            "deterministic accolade-type create pattern with article/gender pipe token",
            False,
            guard_reasons,
        )
    if subtype == "general_getter_pipe_article_gender_pattern":
        return "needs_pipe_article_gender_context_window", "general getter pipe token needs local context window", False, guard_reasons
    return "general_pipe_article_gender_human_review", "general article/gender pipe token needs guarded human review", False, guard_reasons


def enrich(row: dict[str, Any], state_row: dict[str, Any] | None) -> dict[str, Any]:
    decision, rationale, false_safe_risk, guard_reasons = classify(row, state_row)
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "review_decision": row.get("review_decision"),
        "architecture_decision": row.get("architecture_decision"),
        "architecture_subtype": row.get("architecture_subtype"),
        "policy_decision": decision,
        "rationale": rationale,
        "guard_reasons": guard_reasons,
        "false_safe_risk": false_safe_risk,
        "pipe_tokens": row.get("pipe_tokens") or [],
        "token_count": row.get("token_count"),
        "current_output_text": row.get("current_output_text"),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "state": state_row,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "auto_apply_allowed": False,
        "auto_lifecycle_allowed": False,
    }


def build_spec(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": summary["generated_at"],
        "scope": {
            "input_architecture_jsonl": summary["input_architecture_jsonl"],
            "filter": {
                "allowed_for_next_policy_review": True,
                "review_decision": TARGET_REVIEW_DECISION,
            },
            "excluded": [
                "needs_dense_multiline_token_policy",
                "needs_guarded_multiline_review",
                "needs_getter_context_review",
                "needs_select_localization_policy_review",
                "needs_warning_tooltip_guarded_review",
                "needs_human_residual_review",
                "SelectLocalization/Select_CString/AddLocalizationIf",
                "dynamic pronoun getters",
                "warning tooltips",
                "dense multiline",
            ],
        },
        "decisions": [
            "acclaimed_unlock_article_gender_guarded_pattern",
            "accolade_type_create_article_gender_guarded_pattern",
            "general_pipe_article_gender_human_review",
            "needs_pipe_article_gender_context_window",
            "blocked_dynamic_pronoun_gender",
            "blocked_select_localization_or_branching",
            "blocked_warning_or_multiline",
        ],
        "gates": {
            "auto_apply_allowed": False,
            "auto_lifecycle_allowed": False,
            "requires_apply_later_count": 0,
            "requires_lifecycle_later_count": 0,
            "production_full_recommended_now": False,
            "segment_state_run": "read-only reference only",
        },
        "dominant_pattern_rule": {
            "threshold": "guarded_pattern_count >= 20 and false_safe_risk_count == 0",
            "result_if_met": "recommend guarded human packet or read-only micro-policy",
            "result_if_not_met": "recommend smaller sublane, preferably acclaimed_unlock_article_gender",
        },
    }


def build_summary(input_path: Path, rows: list[dict[str, Any]], state_rows: dict[int, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = [enrich(row, state_rows.get(int(row["segment_id"]))) for row in rows]
    decision_counts = Counter(row["policy_decision"] for row in records)
    subtype_counts = Counter(str(row["architecture_subtype"]) for row in records)
    pipe_token_counts = Counter(token for row in records for token in row["pipe_tokens"])
    sample_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(sample_by_decision[row["policy_decision"]]) < SAMPLE_PER_DECISION:
            sample_by_decision[row["policy_decision"]].append(row)
    guarded_pattern_count = (
        decision_counts.get("acclaimed_unlock_article_gender_guarded_pattern", 0)
        + decision_counts.get("accolade_type_create_article_gender_guarded_pattern", 0)
    )
    false_safe_risk_count = sum(1 for row in records if row["false_safe_risk"])
    human_review_count = decision_counts.get("general_pipe_article_gender_human_review", 0)
    hold_count = len(records) - guarded_pattern_count - human_review_count
    if guarded_pattern_count >= 20 and false_safe_risk_count == 0:
        next_prompt = "chat_exec_accolade_article_gender_guarded_human_packet_prompt.md"
        recommendation = "guarded pattern volume is sufficient for a guarded human packet or read-only micro-policy"
    else:
        next_prompt = "chat_exec_acclaimed_unlock_article_gender_sublane_review_prompt.md"
        recommendation = "patterns are fragmented; split to smaller acclaimed_unlock_article_gender sublane before any package"
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_architecture_jsonl": str(input_path),
        "reviewed_count": len(records),
        "guarded_pattern_count": guarded_pattern_count,
        "human_review_count": human_review_count,
        "hold_count": hold_count,
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "architecture_subtype_counts": [{"key": key, "count": value} for key, value in subtype_counts.most_common()],
        "pipe_token_counts": [{"key": key, "count": value} for key, value in pipe_token_counts.most_common()],
        "sample_by_decision": dict(sample_by_decision),
        "dominant_guarded_pattern": guarded_pattern_count >= 20 and false_safe_risk_count == 0,
        "next_prompt_recommended": next_prompt,
        "recommendation": recommendation,
        "auto_apply_allowed": False,
        "auto_lifecycle_allowed": False,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": false_safe_risk_count,
        "production_full_recommended_now": False,
        "apply_ready_now": 0,
        "segment_state_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
    }
    return summary, records


def write_outputs(summary: dict[str, Any], spec: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_accolade_article_gender_pipe_token_policy_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    spec_path = reports_dir() / f"{base.name}_spec.json"
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "accolade article/gender pipe token policy review",
        f"source={SOURCE}",
        f"input_architecture_jsonl={summary['input_architecture_jsonl']}",
        f"reviewed_count={summary['reviewed_count']}",
        f"guarded_pattern_count={summary['guarded_pattern_count']}",
        f"human_review_count={summary['human_review_count']}",
        f"hold_count={summary['hold_count']}",
        f"false_safe_risk_count={summary['false_safe_risk_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "architecture_subtype_counts:"])
    for item in summary["architecture_subtype_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"dominant_guarded_pattern={str(summary['dominant_guarded_pattern']).lower()}",
            f"auto_apply_allowed={str(summary['auto_apply_allowed']).lower()}",
            f"auto_lifecycle_allowed={str(summary['auto_lifecycle_allowed']).lower()}",
            f"requires_apply_later_count={summary['requires_apply_later_count']}",
            f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"next_prompt_recommended={summary['next_prompt_recommended']}",
            f"recommendation={summary['recommendation']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, spec_path, summary_path


def main() -> None:
    input_path = latest_architecture_jsonl()
    rows = read_architecture_rows(input_path)
    run_ids = {int(row.get("segment_state_run_id") or 0) for row in rows if row.get("segment_state_run_id")}
    if len(run_ids) != 1:
        raise SystemExit(f"expected exactly one segment_state_run_id, got {sorted(run_ids)}")
    segment_ids = sorted({int(row["segment_id"]) for row in rows})
    with connect_readonly() as conn:
        state_rows = fetch_state_rows(conn, run_ids.pop(), segment_ids)
    summary, records = build_summary(input_path, rows, state_rows)
    spec = build_spec(summary)
    txt_path, jsonl_path, spec_path, summary_path = write_outputs(summary, spec, records)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"spec={spec_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"guarded_pattern_count={summary['guarded_pattern_count']}")
    print(f"human_review_count={summary['human_review_count']}")
    print(f"hold_count={summary['hold_count']}")
    print(f"false_safe_risk_count={summary['false_safe_risk_count']}")
    print(f"dominant_guarded_pattern={summary['dominant_guarded_pattern']}")
    print(f"next_prompt_recommended={summary['next_prompt_recommended']}")


if __name__ == "__main__":
    main()
