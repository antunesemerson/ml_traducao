from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import db


PACKET_MD = Path("reports/20260704_101749_829339_release_decision_post593_corrected_text_human_packet.md")
AID_JSONL = Path("reports/20260704_102540_336837_release_decision_post593_corrected_text_review_aid.jsonl")


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


def tokens_preserved(row: dict[str, object]) -> bool:
    output_tokens = list(row.get("tokens_present") or [])
    suggested = str(row.get("suggested_corrected_text") or "")
    token_re = re.compile(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!|Glossary\([^)]+\)")
    suggested_tokens = token_re.findall(suggested)
    return output_tokens == suggested_tokens


def decision_for(row: dict[str, object]) -> tuple[str, str, str]:
    alert = str(row.get("context_alert") or "")
    suggestion = str(row.get("suggested_corrected_text") or "")
    if alert == "low_context_risk":
        return "corrected_text", suggestion, "assistant_review_aid_accepted_low_context_risk"
    if alert == "token_preservation_check" and tokens_preserved(row):
        return "corrected_text", suggestion, "assistant_review_aid_accepted_tokens_preserved_exact_signature"
    return "needs_more_context", "", f"hold_context_alert_{alert}"


def main() -> None:
    aid = load_aid()
    packet_path = db.project_path(PACKET_MD)
    original = packet_path.read_text(encoding="utf-8-sig")
    backup = db.project_path(Path("reports") / f"{stamp()}_release_decision_post593_corrected_text_human_packet_before_fill.md")
    shutil.copyfile(packet_path, backup)
    header_pattern = re.compile(r"^##\s+\d+\.\s+Segment\s+(\d+)\s*$", re.MULTILINE)
    matches = list(header_pattern.finditer(original))
    parts: list[str] = []
    cursor = 0
    corrected = 0
    holds = 0
    for index, match in enumerate(matches):
        segment_id = int(match.group(1))
        start = match.start()
        body_start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(original)
        parts.append(original[cursor:start])
        parts.append(original[start:body_start])
        body = original[body_start:end]
        row = aid.get(segment_id)
        if row is None:
            raise SystemExit(f"missing aid row for {segment_id}")
        decision, corrected_text, note = decision_for(row)
        body = replace_field(body, "Human decision", decision)
        body = replace_field(body, "Corrected text", corrected_text)
        body = replace_field(body, "Notes", note)
        corrected += int(decision == "corrected_text")
        holds += int(decision == "needs_more_context")
        parts.append(body)
        cursor = end
    parts.append(original[cursor:])
    packet_path.write_text("".join(parts), encoding="utf-8")
    print(f"packet={packet_path}")
    print(f"backup={backup}")
    print(f"changed_count={len(matches)}")
    print(f"corrected_text_filled={corrected}")
    print(f"needs_more_context_filled={holds}")
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
