from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_spanish_residue_human_packet_v1"
REVIEW_JSONL_PATH = Path(
    "reports/20260630_115930_392736_domain_policy_vote_candidate_religion_faith_human_unknown_hold_review.jsonl"
)
REVIEW_SUMMARY_PATH = Path(
    "reports/20260630_115930_392736_domain_policy_vote_candidate_religion_faith_human_unknown_hold_review_summary.json"
)
EXPECTED_COUNT = 5
EXPECTED_ACTION = "small_human_review_possible"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_suggestion(row: dict) -> tuple[str, str]:
    text = str(row.get("current_output_text") or "")
    source_key = str(row.get("source_key") or "")
    if source_key == "sort_tt_total_tax":
        return (
            "approve_correction_candidate",
            "Total de [taxes|El] disponíveis\n#low O conjunto de todos os [taxes|El] que as [holdings|El] deste [title|El] fornecem, sem contar a quantia que os [vassals|El] guardam para si.#!",
        )
    if source_key == "sort_tt_total_levies":
        return (
            "approve_correction_candidate",
            "Total de [levies|El] disponíveis\n#low O conjunto de todas as [levies|El] que as [holdings|El] deste [title|El] fornecem, sem contar os bônus e a quantia que os [vassals|El] guardam para si.#!",
        )
    if source_key == "sort_tt_total_income":
        return (
            "approve_correction_candidate",
            "Total de [income|El] disponível\n#low O conjunto de todo o [income|El] fornecido pelas [holdings|El] deste [title|El], sem contar a quantia que os [vassals|El] guardam para si.#!",
        )
    if "dispon" in text and "#low" in text:
        return ("needs_human_review", "")
    return ("needs_human_review", "")


def main() -> None:
    summary = read_json(REVIEW_SUMMARY_PATH)
    rows = read_jsonl(REVIEW_JSONL_PATH)
    if summary.get("mode") != "read_only_review":
        raise SystemExit("review summary mode guard failed")
    selected = [row for row in rows if row.get("recommended_action") == EXPECTED_ACTION]
    if len(selected) != EXPECTED_COUNT:
        raise SystemExit(f"selected count guard failed: {len(selected)} expected {EXPECTED_COUNT}")

    packet_rows = []
    for index, row in enumerate(sorted(selected, key=lambda item: (str(item.get("source_key") or ""), int(item["segment_id"]))), 1):
        suggested_decision, suggested_text = build_suggestion(row)
        packet_rows.append(
            {
                "record_type": "human_review_packet_item",
                "source": SOURCE,
                "packet_index": index,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "risk_bucket": row.get("risk_bucket"),
                "operational_subtype": row.get("operational_subtype"),
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "suggested_decision": suggested_decision,
                "suggested_corrected_text": suggested_text,
                "allowed_decisions": [
                    "approve_correction_candidate",
                    "approve_already_ok_candidate",
                    "needs_more_context",
                    "semantic_error",
                    "hold_context",
                ],
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )

    subtype_counts = Counter(str(row["operational_subtype"]) for row in packet_rows)
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_spanish_residue_human_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in packet_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    result = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_packet",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "packet_count": len(packet_rows),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(packet_rows) == EXPECTED_COUNT,
        "subtype_counts": dict(subtype_counts),
        "suggested_decision_counts": dict(Counter(row["suggested_decision"] for row in packet_rows)),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Review these 5 items with the user; then ingest approved decisions and use protected apply only for corrections.",
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine Spanish residue human packet",
        "",
        f"packet_count: {len(packet_rows)}",
        "",
        "items:",
    ]
    for row in packet_rows:
        lines.extend(
            [
                "",
                f"{row['packet_index']}. segment_id {row['segment_id']} | {row['source_key']} | {row['operational_subtype']}",
                f"current: {str(row['current_output_text']).replace(chr(10), '\\n')}",
                f"suggested_decision: {row['suggested_decision']}",
                f"suggested_corrected_text: {str(row['suggested_corrected_text']).replace(chr(10), '\\n')}",
            ]
        )
    lines.extend(
        [
            "",
            "guards:",
            "- candidate_generation_allowed: false",
            "- auto_apply_allowed: false",
            "- lifecycle_allowed: false",
            "- production_release_allowed: false",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary={summary_path}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"packet_count={len(packet_rows)}")
    print(f"subtype_counts={json.dumps(dict(subtype_counts), ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
