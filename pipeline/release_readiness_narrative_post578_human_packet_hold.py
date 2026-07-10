from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import db


SOURCE = "release_readiness_narrative_post578_human_packet_hold_v1"
HELD_PACKET_MD = "reports/20260703_144255_182813_release_readiness_narrative_post578_correction_human_packet.md"
HELD_PACKET_JSONL = "reports/20260703_144255_182813_release_readiness_narrative_post578_correction_human_packet.jsonl"
HELD_EXTRACT_SUMMARY = "reports/20260703_144717_640442_release_readiness_narrative_post578_human_decision_extract_summary.json"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_hold_marker",
        "hold_state": "hold_human_review_pending",
        "held_packet_markdown": HELD_PACKET_MD,
        "held_packet_jsonl": HELD_PACKET_JSONL,
        "held_extract_summary": HELD_EXTRACT_SUMMARY,
        "reason": "Human decision fields and corrected_text blocks are not filled yet.",
        "do_not_extract_again_until_markdown_filled": True,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": "Wait for the Markdown to be filled before extracting decisions from this packet again.",
    }
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_post578_human_packet_hold"
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    summary["output_files"] = {"summary": str(summary_path), "txt": str(txt_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Narrative post578 correction human packet hold",
                "hold_state=hold_human_review_pending",
                f"held_packet_markdown={HELD_PACKET_MD}",
                "do_not_extract_again_until_markdown_filled=true",
                "candidate_generation_count=0",
                "apply_count=0",
                "learning_ingest_count=0",
                "issue_closure_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"summary={summary_path}")
    print(f"txt={txt_path}")
    print("hold_state=hold_human_review_pending")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
