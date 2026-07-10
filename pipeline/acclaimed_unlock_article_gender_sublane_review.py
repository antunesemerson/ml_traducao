from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "acclaimed_unlock_article_gender_sublane_review_v1"
TARGET_POLICY_DECISION = "acclaimed_unlock_article_gender_guarded_pattern"

ACCOLADE_TYPE_RE = re.compile(r"\[GetAccoladeType\('[^']+'\)\.GetName\|l\]")
SUBJECT_RE = re.compile(r"^(\[[^\]]+\.GetName\])\s+")


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


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("policy_decision") == TARGET_POLICY_DECISION:
                rows.append(row)
    return rows


def current_variant(text: str) -> str:
    if "pode se tornar [acclaimed|El]" in text:
        return "already_canonical_se_tornar"
    if "pode ser [acclaimed|El]" in text:
        return "ser_acclaimed_variant"
    if "pode ser tornado [acclaimed|El]" in text:
        return "ser_tornado_variant"
    return "other_variant"


def suggested_text(row: dict[str, Any]) -> str | None:
    text = str(row.get("current_output_text") or "")
    subject_match = SUBJECT_RE.match(text)
    type_match = ACCOLADE_TYPE_RE.search(text)
    if not subject_match or not type_match:
        return None
    subject = subject_match.group(1)
    accolade_type = type_match.group(0)
    return f"{subject} pode se tornar [acclaimed|El] como {accolade_type}"


def classify(row: dict[str, Any]) -> tuple[str, str, str | None]:
    text = str(row.get("current_output_text") or "")
    suggestion = suggested_text(row)
    if suggestion is None:
        return "needs_more_context", "could not safely identify subject or accolade type token", None
    if text == suggestion:
        return "already_ok", "already matches conservative canonical PT-BR wording", suggestion
    variant = current_variant(text)
    if variant in {"ser_acclaimed_variant", "ser_tornado_variant", "other_variant"}:
        return "correction_needed", f"normalize {variant} to 'pode se tornar [acclaimed|El] como ...'", suggestion
    return "needs_more_context", "unrecognized wording variant", suggestion


def token_guard(row: dict[str, Any], suggestion: str | None) -> bool:
    if suggestion is None:
        return False
    tokens = [str(token) for token in row.get("pipe_tokens") or []]
    current = str(row.get("current_output_text") or "")
    return all(token in current and token in suggestion for token in tokens)


def build_summary(input_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    packet_rows = []
    for row in rows:
        decision, rationale, suggestion = classify(row)
        guard_ok = token_guard(row, suggestion)
        if not guard_ok:
            decision = "needs_more_context"
            rationale = "token preservation guard failed"
        packet_rows.append(
            {
                "segment_id": row.get("segment_id"),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "review_decision": decision,
                "rationale": rationale,
                "current_output_text": row.get("current_output_text"),
                "suggested_text": suggestion,
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "pipe_tokens": row.get("pipe_tokens") or [],
                "token_guard_ok": guard_ok,
                "requires_learning_if_approved": decision in {"already_ok", "correction_needed"},
                "requires_apply_later": False,
            }
        )
    decision_counts = Counter(row["review_decision"] for row in packet_rows)
    variant_counts = Counter(current_variant(str(row.get("current_output_text") or "")) for row in rows)
    correction_count = decision_counts.get("correction_needed", 0)
    already_ok_count = decision_counts.get("already_ok", 0)
    hold_count = decision_counts.get("needs_more_context", 0)
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_policy_review_jsonl": str(input_path),
        "reviewed_count": len(packet_rows),
        "already_ok_count": already_ok_count,
        "correction_needed_count": correction_count,
        "hold_count": hold_count,
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "variant_counts": [{"key": key, "count": value} for key, value in variant_counts.most_common()],
        "packet_rows": packet_rows,
        "guarded_human_packet_recommended": correction_count + already_ok_count > 0 and hold_count == 0,
        "auto_apply_allowed": False,
        "auto_lifecycle_allowed": False,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0 if hold_count == 0 else hold_count,
        "production_full_recommended_now": False,
        "apply_ready_now": 0,
        "segment_state_recommended_now": False,
        "next_action": "human_review_acclaimed_unlock_article_gender_batch",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_acclaimed_unlock_article_gender_sublane_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["packet_rows"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "acclaimed unlock article/gender sublane review",
        f"source={SOURCE}",
        f"input_policy_review_jsonl={summary['input_policy_review_jsonl']}",
        f"reviewed_count={summary['reviewed_count']}",
        f"already_ok_count={summary['already_ok_count']}",
        f"correction_needed_count={summary['correction_needed_count']}",
        f"hold_count={summary['hold_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "variant_counts:"])
    for item in summary["variant_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "packet_rows:"])
    for row in summary["packet_rows"]:
        lines.append(f"- {row['segment_id']} | {row['review_decision']} | {row['suggested_text']}")
    lines.extend(
        [
            "",
            f"guarded_human_packet_recommended={str(summary['guarded_human_packet_recommended']).lower()}",
            f"auto_apply_allowed={str(summary['auto_apply_allowed']).lower()}",
            f"auto_lifecycle_allowed={str(summary['auto_lifecycle_allowed']).lower()}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_policy_review_jsonl()
    rows = read_rows(input_path)
    summary = build_summary(input_path, rows)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"already_ok_count={summary['already_ok_count']}")
    print(f"correction_needed_count={summary['correction_needed_count']}")
    print(f"hold_count={summary['hold_count']}")
    print(f"guarded_human_packet_recommended={summary['guarded_human_packet_recommended']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
