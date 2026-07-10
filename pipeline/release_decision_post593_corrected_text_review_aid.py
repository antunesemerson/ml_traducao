from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post593_corrected_text_review_aid_v1"
DEFAULT_PACKET_JSONL = Path("reports/20260704_101749_829339_release_decision_post593_corrected_text_human_packet.jsonl")
DEFAULT_PACKET_MD = Path("reports/20260704_101749_829339_release_decision_post593_corrected_text_human_packet.md")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact read-only review aid for post593 corrected-text packet.")
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    parser.add_argument("--packet-md", type=Path, default=DEFAULT_PACKET_MD)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def context_alert(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("output_text", "suggested_corrected_text", "english_text", "spanish_text"))
    alerts: list[str] = []
    if re.search(r"\b(eu|meu|minha|você|seu|sua|lhe|te)\b", text, re.IGNORECASE):
        alerts.append("pronoun_or_perspective_check")
    if re.search(r"\b[oae]s?\]|\bdele\b|\bdela\b|\bseu\b|\bsua\b", text, re.IGNORECASE):
        alerts.append("gender_or_possessive_check")
    if len(str(row.get("output_text") or "")) > 180:
        alerts.append("fluency_long_sentence_check")
    if row.get("tokens_present"):
        alerts.append("token_preservation_check")
    return ", ".join(dict.fromkeys(alerts)) if alerts else "low_context_risk"


def correction_note(row: dict[str, Any]) -> str:
    output = str(row.get("output_text") or "")
    suggestion = str(row.get("suggested_corrected_text") or "")
    if not suggestion:
        return "Sem sugestão assistida segura."
    if output == suggestion:
        return "A sugestão não altera o texto; revisar se é approve_already_ok ou hold."
    if row.get("tokens_present"):
        return "Remove resíduo espanhol preservando tokens/variáveis."
    return "Remove resíduo espanhol visível em texto curto/narrativo."


def main() -> None:
    args = parse_args()
    rows = read_jsonl(args.packet_jsonl)
    records: list[dict[str, Any]] = []
    for row in rows:
        records.append(
            {
                "segment_id": row["segment_id"],
                "packet_index": row["packet_index"],
                "source_key": row["source_key"],
                "relative_path": row["relative_path"],
                "output_text": row["output_text"],
                "suggested_corrected_text": row["suggested_corrected_text"],
                "tokens_present": row["tokens_present"],
                "primary_risk": row["primary_risk"],
                "correction_note": correction_note(row),
                "context_alert": context_alert(row),
            }
        )
    alert_counts = Counter(record["context_alert"] for record in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_compact_review_aid",
        "input_packet_markdown": str(args.packet_md),
        "input_packet_jsonl": str(args.packet_jsonl),
        "record_count": len(records),
        "context_alert_counts": dict(alert_counts.most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": "Use this compact aid to fill the original packet manually; do not treat suggestions as human decisions.",
    }
    base = reports_dir() / f"{stamp()}_release_decision_post593_corrected_text_review_aid"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Compact Review Aid - Post-593 Corrected Text Packet",
        "",
        "Auxiliar read-only. Não altera o pacote original e não registra decisão humana.",
        "",
    ]
    for record in records:
        md_lines.extend(
            [
                f"## {record['packet_index']}. {record['segment_id']} - `{record['source_key']}`",
                f"- File: `{record['relative_path']}`",
                f"- Tokens/variables: `{record['tokens_present']}`",
                f"- Primary risk: `{record['primary_risk']}`",
                f"- Why correction seems needed: {record['correction_note']}",
                f"- Alert: `{record['context_alert']}`",
                "",
                "**Current output**",
                "",
                "```text",
                str(record["output_text"] or ""),
                "```",
                "",
                "**Assisted corrected_text suggestion**",
                "",
                "```text",
                str(record["suggested_corrected_text"] or ""),
                "```",
                "",
            ]
        )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"context_alert_counts={json.dumps(summary['context_alert_counts'], ensure_ascii=False, sort_keys=True)}")
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
