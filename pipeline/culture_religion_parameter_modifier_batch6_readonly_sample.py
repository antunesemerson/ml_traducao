from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_parameter_modifier_batch6_readonly_sample_v1"
STATUS_PATTERN = "*_culture_religion_parameter_modifier_remaining_status.jsonl"
TRIAGE_PATTERN = "*_culture_religion_pending_readonly_triage.jsonl"
TARGET_LANE = "parameter_or_modifier_label"
MAX_SAMPLE = 20

EXCLUDED_RISK_DECISIONS = {
    "known_hold",
    "hold_context",
    "needs_human_review",
    "semantic_or_language_correction",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_path(pattern: str) -> Path:
    matches = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise SystemExit(f"missing report for pattern {pattern}")
    return matches[0]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def token_surface(row: dict[str, Any]) -> int:
    return int(row.get("token_count") or 0)


def text_length(row: dict[str, Any]) -> int:
    return int(row.get("text_length") or len(str(row.get("current_output_text") or "")))


def bucket_rank(bucket: str) -> int:
    order = {
        "general_parameter": 0,
        "trait_parameter": 1,
        "religion_doctrine_parameter": 2,
    }
    return order.get(bucket, 99)


def review_note(row: dict[str, Any]) -> str:
    key = str(row.get("source_key") or "")
    text = str(row.get("current_output_text") or "")
    if "next_level" in key:
        return "padrao de parametro de construcao/era; confirmar fluidez e genero do objeto"
    if "GetTrait" in text or "trait" in key:
        return "parametro com trait dinamico; confirmar concordancia e preservacao de getter"
    if "doctrine" in key:
        return "parametro de doutrina; confirmar termo religioso e artigo"
    if "education" in key or "education" in text:
        return "parametro de educacao; confirmar leitura natural em PT-BR"
    return "parametro curto; confirmar semantica e naturalidade antes de aprender"


def choose_sample(status_rows: list[dict[str, Any]], triage_by_segment: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for status in status_rows:
        if status.get("status") != "eligible":
            continue
        if status.get("risk_decision") in EXCLUDED_RISK_DECISIONS:
            continue
        if status.get("risk_decision") != "human_review":
            continue
        segment_id = int(status["segment_id"])
        triage = triage_by_segment.get(segment_id)
        if not triage:
            continue
        if triage.get("lane") != TARGET_LANE:
            continue
        if int(triage.get("needs_output_apply") or 0) != 0:
            continue
        merged = {**triage, **status}
        eligible.append(merged)

    eligible.sort(
        key=lambda row: (
            bucket_rank(str(row.get("bucket") or "")),
            token_surface(row),
            text_length(row),
            int(row["segment_id"]),
        )
    )

    selected: list[dict[str, Any]] = []
    bucket_counts: Counter[str] = Counter()
    first_pass_limit = 14
    for row in eligible:
        bucket = str(row.get("bucket") or "")
        if bucket_counts[bucket] >= first_pass_limit:
            continue
        selected.append(row)
        bucket_counts[bucket] += 1
        if len(selected) >= MAX_SAMPLE:
            return selected
    return selected


def write_outputs(
    status_path: Path,
    triage_path: Path,
    records: list[dict[str, Any]],
    status_counts: Counter[str],
    eligible_counts: Counter[str],
) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_batch6_readonly_sample"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"

    output_records: list[dict[str, Any]] = []
    for record in records:
        output_records.append(
            {
                "segment_id": int(record["segment_id"]),
                "source_key": record.get("source_key"),
                "relative_path": record.get("relative_path"),
                "source_line_number": record.get("source_line_number"),
                "bucket": record.get("bucket"),
                "risk_decision": record.get("risk_decision"),
                "risk_reason": record.get("risk_reason"),
                "token_count": token_surface(record),
                "text_length": text_length(record),
                "word_count": record.get("word_count"),
                "current_output_text": record.get("current_output_text"),
                "english_text": record.get("english_text"),
                "spanish_text": record.get("spanish_text"),
                "latest_final_state": record.get("latest_final_state"),
                "latest_state_group": record.get("latest_state_group"),
                "latest_needs_output_apply": int(record.get("latest_needs_output_apply") or 0),
                "review_note": review_note(record),
                "preliminary_decision": "needs_human_confirmation",
            }
        )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in output_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    bucket_counts = Counter(str(record["bucket"]) for record in output_records)
    token_counts = Counter(str(record["token_count"]) for record in output_records)
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status_jsonl": str(status_path),
        "triage_jsonl": str(triage_path),
        "target_lane": TARGET_LANE,
        "sample_count": len(output_records),
        "sample_limit": MAX_SAMPLE,
        "bucket_counts": dict(bucket_counts),
        "token_count_distribution": dict(token_counts),
        "input_status_counts": dict(status_counts),
        "eligible_human_review_counts": dict(eligible_counts),
        "read_only": True,
        "candidate_generation_executed": False,
        "apply_executed": False,
        "recommended_next_step": "review_batch6_with_user_then_ingest_only_confirmed_learning_signals",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "culture/religion parameter modifier batch6 read-only sample",
        f"source={RULE_VERSION}",
        f"status_jsonl={status_path}",
        f"triage_jsonl={triage_path}",
        f"target_lane={TARGET_LANE}",
        "",
        "selection_rules:",
        "- status=eligible",
        "- risk_decision=human_review",
        "- excluded known_hold/hold_context/needs_human_review/semantic_or_language_correction",
        "- needs_output_apply=0",
        "- no candidate generation and no apply",
        "",
        "sample_stats:",
        f"- sample_count: {len(output_records)}",
        *[f"- {key}: {value}" for key, value in bucket_counts.most_common()],
        "",
        "sample:",
    ]
    for record in output_records:
        lines.extend(
            [
                f"- {record['segment_id']} | {record['bucket']} | tokens={record['token_count']} | {record['source_key']}",
                f"  text: {record['current_output_text']}",
                f"  english: {record['english_text']}",
                f"  note: {record['review_note']}",
            ]
        )
    lines.extend(
        [
            "",
            "execution_flags:",
            "- read_only=true",
            "- candidate_generation_executed=false",
            "- apply_executed=false",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    status_path = latest_path(STATUS_PATTERN)
    triage_path = latest_path(TRIAGE_PATTERN)
    status_rows = read_jsonl(status_path)
    triage_rows = read_jsonl(triage_path)
    triage_by_segment = {int(row["segment_id"]): row for row in triage_rows if row.get("lane") == TARGET_LANE}

    status_counts = Counter(str(row.get("status")) for row in status_rows)
    eligible_counts: Counter[str] = Counter()
    for row in status_rows:
        if row.get("status") == "eligible":
            eligible_counts[str(row.get("risk_decision"))] += 1

    records = choose_sample(status_rows, triage_by_segment)
    txt_path, jsonl_path, summary_path = write_outputs(
        status_path=status_path,
        triage_path=triage_path,
        records=records,
        status_counts=status_counts,
        eligible_counts=eligible_counts,
    )

    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"sample_count={len(records)}")
    print("bucket_counts=" + json.dumps(dict(Counter(str(row.get("bucket")) for row in records)), ensure_ascii=False, sort_keys=True))
    print("candidate_generation_executed=False")
    print("apply_executed=False")


if __name__ == "__main__":
    main()
