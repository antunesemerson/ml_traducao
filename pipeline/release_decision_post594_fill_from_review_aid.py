from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import db


PACKET_MD = Path("reports/20260704_111007_137039_release_decision_post594_corrected_text_human_packet.md")
AID_JSONL = Path("reports/20260704_111404_396142_release_decision_post594_corrected_text_review_aid.jsonl")
APPROVE_IDS = {61067, 128258, 112945}
CORRECTED_IDS = {158536, 74979, 120190, 246184, 114285, 114297, 121588}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def load_aid() -> dict[int, dict[str, object]]:
    rows: dict[int, dict[str, object]] = {}
    with db.project_path(AID_JSONL).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
    return rows


def replace_field(body: str, field: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(field)}:\s*.*?(?=^(?:Human decision|Corrected text|Notes):|\Z)", re.MULTILINE | re.DOTALL)
    if not pattern.search(body):
        raise SystemExit(f"missing field {field}")
    return pattern.sub(f"{field}: {value}\n\n", body, count=1)


def main() -> None:
    aid = load_aid()
    expected = APPROVE_IDS | CORRECTED_IDS
    if set(aid) != expected:
        raise SystemExit(f"aid id mismatch: {sorted(aid)}")
    packet_path = db.project_path(PACKET_MD)
    original = packet_path.read_text(encoding="utf-8-sig")
    backup = db.project_path(Path("reports") / f"{stamp()}_release_decision_post594_corrected_text_human_packet_before_fill.md")
    shutil.copyfile(packet_path, backup)
    header_pattern = re.compile(r"^##\s+\d+\.\s+Segment\s+(\d+)\s*$", re.MULTILINE)
    matches = list(header_pattern.finditer(original))
    if len(matches) != 10:
        raise SystemExit(f"expected 10 packet blocks, got {len(matches)}")
    parts: list[str] = []
    cursor = 0
    corrected = 0
    approve = 0
    for index, match in enumerate(matches):
        segment_id = int(match.group(1))
        if segment_id not in expected:
            raise SystemExit(f"unexpected packet segment {segment_id}")
        start = match.start()
        body_start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(original)
        parts.append(original[cursor:start])
        parts.append(original[start:body_start])
        body = original[body_start:end]
        row = aid[segment_id]
        if segment_id in APPROVE_IDS:
            decision = "approve_already_ok"
            corrected_text = ""
            note = "accepted_review_aid_possible_false_positive_or_already_ok"
            approve += 1
        else:
            decision = "corrected_text"
            corrected_text = str(row.get("suggested_corrected_text") or "")
            if not corrected_text:
                raise SystemExit(f"missing corrected text suggestion for {segment_id}")
            note = "accepted_review_aid_corrected_text"
            corrected += 1
        body = replace_field(body, "Human decision", decision)
        body = replace_field(body, "Corrected text", corrected_text)
        body = replace_field(body, "Notes", note)
        parts.append(body)
        cursor = end
    parts.append(original[cursor:])
    packet_path.write_text("".join(parts), encoding="utf-8")
    print(f"packet={packet_path}")
    print(f"backup={backup}")
    print(f"changed_count={len(matches)}")
    print(f"corrected_text_filled={corrected}")
    print(f"approve_already_ok_filled={approve}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
