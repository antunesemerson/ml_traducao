from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "post_acclaimed_unlock_article_gender_policy_reassessment_v1"
LEARNING_RUN_IDS = (700, 701, 702)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_policy_review_jsonl() -> Path:
    matches = sorted(
        reports_dir().glob("*_accolade_article_gender_pipe_token_policy_review.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing accolade_article_gender_pipe_token_policy_review jsonl")
    return matches[0]


def learned_segments() -> set[int]:
    placeholders = ",".join("?" for _ in LEARNING_RUN_IDS)
    with db.connect(db.load_settings()) as conn:
        rows = conn.execute(
            f"SELECT segment_id FROM local_learning_candidates WHERE run_id IN ({placeholders})",
            LEARNING_RUN_IDS,
        ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def read_remaining(path: Path, excluded_ids: set[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row["segment_id"]) in excluded_ids:
                continue
            rows.append(row)
    return rows


def build_summary(input_path: Path, rows: list[dict[str, Any]], excluded_ids: set[int]) -> dict[str, Any]:
    decision_counts = Counter(str(row.get("policy_decision")) for row in rows)
    subtype_counts = Counter(str(row.get("architecture_subtype")) for row in rows)
    candidate_order = [
        "accolade_type_create_article_gender_guarded_pattern",
        "general_pipe_article_gender_human_review",
        "needs_pipe_article_gender_context_window",
    ]
    recommended = next((decision for decision in candidate_order if decision_counts.get(decision)), None)
    next_action = (
        "review_single_accolade_type_create_article_gender"
        if recommended == "accolade_type_create_article_gender_guarded_pattern"
        else "review_general_pipe_article_gender_human_batch"
        if recommended == "general_pipe_article_gender_human_review"
        else "hold_for_context_window_or_architecture"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_policy_review_jsonl": str(input_path),
        "excluded_learning_run_ids": list(LEARNING_RUN_IDS),
        "excluded_segment_count": len(excluded_ids),
        "remaining_count": len(rows),
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "architecture_subtype_counts": [{"key": key, "count": value} for key, value in subtype_counts.most_common()],
        "recommended_decision": recommended,
        "recommended_count": int(decision_counts.get(recommended or "", 0)),
        "recommended_sample": [row for row in rows if row.get("policy_decision") == recommended][:8],
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "segment_state_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_post_acclaimed_unlock_article_gender_policy_reassessment"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post acclaimed unlock article/gender policy reassessment",
        f"source={SOURCE}",
        f"remaining_count={summary['remaining_count']}",
        f"excluded_segment_count={summary['excluded_segment_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"recommended_decision={summary['recommended_decision']}",
            f"recommended_count={summary['recommended_count']}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_policy_review_jsonl()
    excluded_ids = learned_segments()
    rows = read_remaining(input_path, excluded_ids)
    summary = build_summary(input_path, rows, excluded_ids)
    txt_path, jsonl_path, summary_path = write_outputs(summary, rows)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"remaining_count={summary['remaining_count']}")
    print(f"excluded_segment_count={summary['excluded_segment_count']}")
    print(f"recommended_decision={summary['recommended_decision']}")
    print(f"recommended_count={summary['recommended_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
