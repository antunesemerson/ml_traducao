from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_dense_unknown_architecture_packet_v1"
RESIDUAL_JSONL_PATH = Path(
    "reports/20260630_122853_418678_domain_policy_vote_candidate_religion_faith_human_unknown_residual_post509_diagnostic.jsonl"
)
SEGMENT_STATE_RUN_ID = 509
EXPECTED_COUNT = 4


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def classify_family(row: dict[str, Any]) -> tuple[str, str]:
    key = str(row.get("source_key") or "")
    text = str(row.get("current_output_text") or "")
    blob = f"{key} {text}"
    if "Legend.GetProperty" in blob:
        return ("legend_property_getter_relation", "Needs parser for ROOT.Legend.GetProperty title/founder role and omitted perspective relation.")
    if "petition_liege" in key or "ROOT.Char.GetLiege" in blob:
        return ("liege_faith_conversion_request", "Needs role-aware parser for liege actor + faith/counties conversion phrase.")
    if "state_faith" in blob:
        return ("state_faith_relation", "Needs parser for relation between state_faith and faith, with article/gender around faith terms.")
    if "scoped_ruler.GetCulture" in blob:
        return ("culture_faith_historical_context", "Needs parser for culture getter used as demonym/group name plus faith prose.")
    return ("dense_unknown_token_context", "Needs architecture review.")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    source_rows = read_jsonl(RESIDUAL_JSONL_PATH)
    rows = [
        row
        for row in source_rows
        if row.get("operational_bucket") == "architecture_later_dense_unknown_token_context"
    ]
    if len(rows) != EXPECTED_COUNT:
        raise SystemExit(f"dense unknown count guard failed: {len(rows)} expected {EXPECTED_COUNT}")

    packet: list[dict[str, Any]] = []
    for row in rows:
        family, recommendation = classify_family(row)
        packet.append(
            {
                "record_type": "architecture_packet_item",
                "source": SOURCE,
                "segment_state_run_id": SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "risk_bucket": row.get("risk_bucket"),
                "architecture_family": family,
                "parser_recommendation": recommendation,
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "state_group": row.get("state_group"),
                "final_state": row.get("final_state"),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
                "recommended_action": "architecture_review_before_policy_or_human_packet",
            }
        )

    family_counts = Counter(row["architecture_family"] for row in packet)
    risk_counts = Counter(str(row.get("risk_bucket") or "") for row in packet)
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_dense_unknown_architecture_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, packet)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_architecture_packet",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "input_residual_jsonl": str(RESIDUAL_JSONL_PATH),
        "packet_count": len(packet),
        "architecture_family_counts": dict(family_counts),
        "risk_bucket_counts": dict(risk_counts),
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
            "Send these 4 dense getter/context samples to architecture before creating any splitter; "
            "no candidate or human packet is recommended until parser roles are defined."
        ),
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith dense unknown architecture packet",
        "",
        f"packet_count: {len(packet)}",
        "",
        "architecture_family_counts:",
        *[f"- {count} | {key}" for key, count in family_counts.most_common()],
        "",
        "items:",
    ]
    for row in packet:
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['source_key']}",
                f"- family: {row['architecture_family']}",
                f"- recommendation: {row['parser_recommendation']}",
                f"- current_output_text: {row['current_output_text']}",
                f"- english_text: {row['english_text']}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
