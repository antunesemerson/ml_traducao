from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import db


PACKET_MD = Path("reports/20260703_235712_724351_release_decision_post592_top15_human_packet.md")
AID_JSONL = Path("reports/20260704_004226_736165_release_decision_post592_top15_decision_aid.jsonl")
HOLD_REASON = "hold_concept_literal_surface_review"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def load_aid() -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    with db.project_path(AID_JSONL).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
    return rows


def replace_field(body: str, field: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(field)}:\s*.*?(?=^(?:Human decision|Corrected text|Notes):|\Z)", re.MULTILINE | re.DOTALL)
    replacement = f"{field}: {value}\n\n"
    if not pattern.search(body):
        raise SystemExit(f"missing field {field}")
    return pattern.sub(replacement, body, count=1)


def main() -> None:
    packet_path = db.project_path(PACKET_MD)
    aid = load_aid()
    original = packet_path.read_text(encoding="utf-8-sig")
    backup = db.project_path(Path("reports") / f"{stamp()}_release_decision_post592_top15_human_packet_before_fill.md")
    shutil.copyfile(packet_path, backup)
    header_pattern = re.compile(r"^##\s+\d+\.\s+Segment\s+(\d+)\s*$", re.MULTILINE)
    matches = list(header_pattern.finditer(original))
    parts: list[str] = []
    cursor = 0
    changed = 0
    corrected = 0
    holds = 0
    for index, match in enumerate(matches):
        segment_id = int(match.group(1))
        start = match.start()
        body_start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(original)
        parts.append(original[cursor:start])
        body = original[body_start:end]
        row = aid.get(segment_id)
        if row is None:
            raise SystemExit(f"missing aid row for {segment_id}")
        if row["token_delta_assessment"] == "tokens_preserved_exact_signature":
            body = replace_field(body, "Human decision", "corrected_text")
            body = replace_field(body, "Corrected text", row["suggested_corrected_text"])
            body = replace_field(body, "Notes", "assistant_aid_accepted_tokens_preserved_exact_signature")
            corrected += 1
        else:
            body = replace_field(body, "Human decision", "needs_more_context")
            body = replace_field(body, "Corrected text", "")
            body = replace_field(body, "Notes", HOLD_REASON)
            holds += 1
        parts.append(original[start:body_start])
        parts.append(body)
        cursor = end
        changed += 1
    parts.append(original[cursor:])
    packet_path.write_text("".join(parts), encoding="utf-8")
    print(f"packet={packet_path}")
    print(f"backup={backup}")
    print(f"changed_count={changed}")
    print(f"corrected_text_count={corrected}")
    print(f"needs_more_context_count={holds}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
