from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_parameter_modifier_readonly_sample_v1"
INPUT_PATTERN = "*_culture_religion_pending_readonly_triage.jsonl"
TARGET_LANE = "parameter_or_modifier_label"
MAX_SAMPLE = 40
SAMPLE_PER_BUCKET = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_triage_jsonl() -> Path:
    matches = sorted(reports_dir().glob(INPUT_PATTERN), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise SystemExit("missing culture/religion pending triage jsonl")
    return matches[0]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def bucket(row: dict[str, Any]) -> str:
    key = str(row.get("source_key") or "")
    text = str(row.get("current_output_text") or "")
    if "can_enact" in key or "succession_enabled" in key or "inheritance" in key:
        return "law_unlock_parameter"
    if "trait" in key or "GetTrait" in text:
        return "trait_parameter"
    if "building" in key or "holding" in key or "holdings" in text:
        return "holding_building_parameter"
    if "opinion" in key or "opinion" in text:
        return "opinion_parameter"
    if "knight" in key or "commander" in key or "martial" in key:
        return "martial_knight_parameter"
    if "doctrine" in key or str(row.get("relative_path") or "").startswith("religion/"):
        return "religion_doctrine_parameter"
    return "general_parameter"


def preliminary_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("current_output_text") or "")
    key = str(row.get("source_key") or "")
    if "$knight_culture_player_plural$" in text:
        return "hold_context", "$knight_culture_player_plural$ requires explicit context/policy"
    if row.get("token_count", 0) >= 4:
        return "needs_human_review", "dense protected-token surface"
    if "Pode promulgar a" in text and "GetLaw(" in text:
        return "policy_candidate", "repeated law unlock pattern looks policy-reviewable"
    if "tem bônus adicionais" in text or "concedem mais" in text:
        return "human_review_likely_ok", "natural PT-BR but benefits from human confirmation"
    return "human_review", "short parameter text; needs confirmation before learning"


def choose_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[bucket(row)].append(row)

    selected: list[dict[str, Any]] = []
    for group_key in sorted(groups, key=lambda key: (-len(groups[key]), key)):
        for row in groups[group_key][:SAMPLE_PER_BUCKET]:
            selected.append(row)
            if len(selected) >= MAX_SAMPLE:
                return selected
    return selected


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for row in rows:
        decision, rationale = preliminary_decision(row)
        records.append(
            {
                **row,
                "sample_bucket": bucket(row),
                "preliminary_decision": decision,
                "preliminary_rationale": rationale,
            }
        )
    return records


def write_outputs(input_path: Path, records: list[dict[str, Any]], total_lane_count: int) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_readonly_sample"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    bucket_counts = Counter(record["sample_bucket"] for record in records)
    decision_counts = Counter(record["preliminary_decision"] for record in records)
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_triage_jsonl": str(input_path),
        "target_lane": TARGET_LANE,
        "total_lane_count": total_lane_count,
        "sample_count": len(records),
        "bucket_counts": dict(bucket_counts),
        "preliminary_decision_counts": dict(decision_counts),
        "read_only": True,
        "candidate_generation_executed": False,
        "apply_executed": False,
        "recommended_next_step": "review_sample_with_user_then_ingest_only_confirmed_learning_signals",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "culture/religion parameter modifier read-only sample",
        f"source={RULE_VERSION}",
        f"input_triage_jsonl={input_path}",
        f"target_lane={TARGET_LANE}",
        f"total_lane_count={total_lane_count}",
        f"sample_count={len(records)}",
        "",
        "preliminary_decision_counts:",
        *[f"- {key}: {value}" for key, value in decision_counts.most_common()],
        "",
        "bucket_counts:",
        *[f"- {key}: {value}" for key, value in bucket_counts.most_common()],
        "",
        "sample:",
    ]
    for record in records:
        lines.extend(
            [
                f"- {record['segment_id']} | {record['sample_bucket']} | {record['preliminary_decision']}",
                f"  key: {record['source_key']}",
                f"  text: {record['current_output_text']}",
                f"  rationale: {record['preliminary_rationale']}",
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
    input_path = latest_triage_jsonl()
    rows = [row for row in read_jsonl(input_path) if row.get("lane") == TARGET_LANE]
    sample = build_records(choose_sample(rows))
    txt_path, jsonl_path, summary_path = write_outputs(input_path, sample, len(rows))
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"total_lane_count={len(rows)}")
    print(f"sample_count={len(sample)}")
    print("decision_counts=" + json.dumps(dict(Counter(r["preliminary_decision"] for r in sample)), ensure_ascii=False, sort_keys=True))
    print("candidate_generation_executed=False")
    print("apply_executed=False")


if __name__ == "__main__":
    main()
