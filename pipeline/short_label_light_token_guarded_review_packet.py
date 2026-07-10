from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "short_label_light_token_guarded_review_packet_v1"
TARGET_DECISION = "guarded_human_sample_candidate"
FIRST_BATCH_LIMIT = 20

CREDITS_RE = re.compile(r"credits[/\\]|#credits_header|^HEADER_", re.IGNORECASE)
POETRY_RE = re.compile(r"poetry|GeneratePoem", re.IGNORECASE)
GETTER_RE = re.compile(r"\[[^\]]*Get[A-Za-z0-9_]+[^\]]*\]")
SPANISH_RESIDUAL_RE = re.compile(
    r"\b(?:operaciones|desarrollo|datos|finanzas|legal|proveedores|localizaci[oó]n|colaboradores|independientes|"
    r"actual|siguiente|sin|con|para|del|de la|este|esta|tiene|puede|debe)\b",
    re.IGNORECASE,
)
LOCALIZATION_FN_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf", re.IGNORECASE)
MULTILINE_RE = re.compile(r"\\n|\n")
TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|SelectLocalization\([^)]*\)|AddLocalizationIf\([^)]*\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_diagnostic_jsonl() -> Path:
    matches = sorted(
        reports_dir().glob("*_short_label_style_post_plain_learning_sublane_diagnostic.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing short_label_style_post_plain_learning_sublane_diagnostic jsonl")
    return matches[0]


def read_target_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("decision") == TARGET_DECISION:
                rows.append(row)
    return rows


def token_count(text: str) -> int:
    return len(TOKEN_RE.findall(text))


def classify(row: dict[str, Any]) -> tuple[str, str, str]:
    text = str(row.get("current_output_text") or "")
    key = str(row.get("source_key") or "")
    path = str(row.get("relative_path") or "")
    haystack = " ".join([path, key, text])
    if CREDITS_RE.search(haystack):
        if SPANISH_RESIDUAL_RE.search(text):
            return "hold_credits_spanish_residual", "credits_spanish_residual", "credits heading still appears to be Spanish/localization source text"
        return "human_review_credits_header", "credits_header", "credits heading with formatting token; human can confirm/correct"
    if POETRY_RE.search(haystack) or GETTER_RE.search(text):
        return "hold_poetry_or_getter_context", "poetry_or_getter", "getter/poetry surface needs context-preserving human review before learning"
    if LOCALIZATION_FN_RE.search(text):
        return "hold_localization_function", "localization_function", "localization function surface is not suitable for this guarded sample"
    if MULTILINE_RE.search(text):
        return "hold_multiline_light_token", "multiline_light_token", "multiline token surface needs separate policy review"
    if token_count(text) > 2:
        return "hold_token_count_gt_2", "token_count_gt_2", "more than two tokens after diagnostic filtering"
    if SPANISH_RESIDUAL_RE.search(text):
        return "human_review_light_token_residual", "light_token_residual", "short token text with possible residual-language artifact"
    return "human_review_light_token_message", "light_token_message", "short token text; human can mark ok/correction while preserving tokens"


def build_summary(input_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    packet = []
    for row in rows:
        decision, subtype, rationale = classify(row)
        packet.append(
            {
                "segment_id": row.get("segment_id"),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "packet_decision": decision,
                "subtype": subtype,
                "rationale": rationale,
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "shape_bucket": row.get("shape_bucket"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": row.get("token_count"),
                "requires_apply_later": False,
                "requires_learning_if_approved": decision.startswith("human_review"),
            }
        )
    decision_counts = Counter(row["packet_decision"] for row in packet)
    subtype_counts = Counter(row["subtype"] for row in packet)
    file_counts = Counter(str(row["relative_path"]) for row in packet)
    human_rows = [row for row in packet if row["packet_decision"].startswith("human_review")]
    hold_rows = [row for row in packet if row["packet_decision"].startswith("hold")]
    first_batch = human_rows[:FIRST_BATCH_LIMIT]
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "target_decision": TARGET_DECISION,
        "packet_count": len(packet),
        "human_review_count": len(human_rows),
        "hold_count": len(hold_rows),
        "first_batch_count": len(first_batch),
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "subtype_counts": [{"key": key, "count": value} for key, value in subtype_counts.most_common()],
        "file_counts": [{"key": key, "count": value} for key, value in file_counts.most_common()],
        "first_batch_rows": first_batch,
        "packet_rows": packet,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "segment_state_recommended_now": False,
        "next_action": "review_first_guarded_light_token_batch_no_apply",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_short_label_light_token_guarded_review_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["packet_rows"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "short label light-token guarded review packet",
        f"source={SOURCE}",
        f"input_jsonl={summary['input_jsonl']}",
        f"packet_count={summary['packet_count']}",
        f"human_review_count={summary['human_review_count']}",
        f"hold_count={summary['hold_count']}",
        f"first_batch_count={summary['first_batch_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "subtype_counts:"])
    for item in summary["subtype_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "file_counts:"])
    for item in summary["file_counts"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "first_batch_rows:"])
    for row in summary["first_batch_rows"]:
        lines.append(f"- {row['segment_id']} | {row['packet_decision']} | {row['current_output_text']}")
    lines.extend(
        [
            "",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"segment_state_recommended_now={str(summary['segment_state_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_diagnostic_jsonl()
    rows = read_target_rows(input_path)
    summary = build_summary(input_path, rows)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"packet_count={summary['packet_count']}")
    print(f"human_review_count={summary['human_review_count']}")
    print(f"hold_count={summary['hold_count']}")
    print(f"first_batch_count={summary['first_batch_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
