from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet4_learning_ingest as base


base.RULE_VERSION = "release_readiness_ui_tooltips_final6_learning_ingest_v1"
base.PACKET_JSONL = Path("reports/20260703_142012_800811_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
base.APPLY_JSONL = Path("reports/20260703_142221_812081_release_readiness_ui_tooltips_packet4_corrected_preview.jsonl")
base.QUEUE_SOURCE = "release-readiness-ui-tooltips-final6"
base.ORIGIN = "human_confirmed_release_readiness_ui_tooltips_final6"
base.REVIEWER = "user_human_review_release_readiness_ui_tooltips_final6"
base.FOCUS_GROUP = "release_readiness_ui_tooltips_final6"
base.CORRECTED_SEGMENT_IDS = {50666, 60232, 69330, 160251, 160493}
base.APPROVE_SEGMENT_IDS = {160244}


def build_records(args):
    packet_rows = {int(row["segment_id"]): row for row in base.read_jsonl(args.packet_jsonl)}
    if len(packet_rows) != args.expected_count:
        raise SystemExit(f"expected packet count {args.expected_count}, got {len(packet_rows)}")
    records = []
    for segment_id in sorted(packet_rows):
        packet = packet_rows[segment_id]
        if segment_id not in base.CORRECTED_SEGMENT_IDS | base.APPROVE_SEGMENT_IDS:
            raise SystemExit(f"unresolved decision for segment {segment_id}")
        decision_kind = "corrected_text" if segment_id in base.CORRECTED_SEGMENT_IDS else "approve_already_ok"
        suggested = str(packet.get("confirmed_text") or packet.get("output_text") or "")
        records.append(
            {
                "source": base.RULE_VERSION,
                "record_type": "learning_ingest_item",
                "segment_id": segment_id,
                "relative_path": packet.get("relative_path"),
                "source_key": packet.get("source_key"),
                "decision_kind": decision_kind,
                "suggested_text": suggested,
                "suggested_hash": base.sha256_text(suggested),
                "status": "pending",
                "block_reasons": [],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


base.build_records = build_records


if __name__ == "__main__":
    base.main()
