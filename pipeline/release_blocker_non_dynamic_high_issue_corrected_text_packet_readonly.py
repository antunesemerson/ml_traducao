from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_blocker_non_dynamic_high_issue_corrected_text_packet_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_204147_294822_release_blocker_non_dynamic_high_issue_triage_readonly.jsonl")
EXCLUDED_APPROVE_OK_IDS = {33718, 37839, 42196, 48500, 54856, 67282, 67319, 76756, 99715, 105133, 105383, 114115}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only human-editable packet for corrected_text_possible high issue rows.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("triage_class") != "corrected_text_possible":
                continue
            if int(row.get("segment_id") or 0) in EXCLUDED_APPROVE_OK_IDS:
                continue
            row["_line_number"] = line_number
            rows.append(row)
    return rows


def item(row: dict[str, Any]) -> dict[str, Any]:
    suggested = row.get("corrected_text") or row.get("suggested_corrected_text") or ""
    return {
        "source": SOURCE,
        "record_type": "corrected_text_human_packet_item",
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "correction_reason": row.get("triage_reason") or "residuo espanhol literal visivel no output atual.",
        "source_text": row.get("source_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "suggested_initial_text": suggested,
        "corrected_text": "",
        "corrected_text_required": True,
        "decision_options": ["corrected_text", "needs_more_context", "parser_later", "reject"],
        "initial_decision": "corrected_text",
        "has_explicit_corrected_text": bool(suggested),
        "ready_for_diff_preview": False,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def markdown(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Corrected Text Packet - High Issue Non-Dynamic Narrative",
        "",
        f"- Itens: {summary['record_count']}",
        "- Preencher `Corrected text` com o texto final exato em PT-BR.",
        "- Nao usar sugestao inicial como pronta sem confirmacao humana explicita.",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## {row['segment_id']} | {row['source_key']}",
                f"- Path: `{row['relative_path']}`",
                f"- Token surface: `{row['token_surface']}`",
                f"- Issues: `{row['issue_families']} | {row['issue_kinds']}`",
                f"- Open/high: `{row['open_issue_count']}/{row['high_issue_count']}`",
                f"- Razao da correcao: {row['correction_reason']}",
                f"- Source/espanhol: {row['source_text']}",
                f"- Output atual: {row['output_text']}",
                f"- Confirmed atual: {row['confirmed_text']}",
                f"- Sugestao inicial: {row['suggested_initial_text'] or '(nenhuma; preencher manualmente)'}",
                "- Decision: corrected_text",
                "- Corrected text: ",
                "",
            ]
        )
    return "\n".join(lines)


def write(rows: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_non_dynamic_high_issue_corrected_text_packet_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text(markdown(rows, summary), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows = [item(row) for row in read_rows(args.input_jsonl)]
    has_explicit = [row for row in rows if row["has_explicit_corrected_text"]]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_editable_packet",
        "input_jsonl": str(args.input_jsonl),
        "record_count": len(rows),
        "explicit_corrected_text_count": len(has_explicit),
        "diff_preview_generated": False,
        "ready_for_diff_preview_count": 0,
        "token_surface_counts": dict(Counter(row["token_surface"] for row in rows).most_common()),
        "issue_family_counts": dict(Counter(row["issue_families"] for row in rows).most_common()),
        "segment_ids": [row["segment_id"] for row in rows],
        "excluded_approve_ok_segment_ids": sorted(EXCLUDED_APPROVE_OK_IDS),
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
        "single_operational_recommendation": (
            "Preencher corrected_text explicitamente no Markdown. Depois rerodar extracao/diff preview read-only antes de qualquer apply."
        ),
    }
    outputs = write(rows, summary)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"summary={outputs['summary']}")
    print(f"record_count={summary['record_count']}")
    print(f"explicit_corrected_text_count={summary['explicit_corrected_text_count']}")
    print(f"segment_ids={json.dumps(summary['segment_ids'], ensure_ascii=False)}")
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
