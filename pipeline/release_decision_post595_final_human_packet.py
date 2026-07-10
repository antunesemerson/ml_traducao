from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post595_final_human_packet_v1"
DEFAULT_INPUT_JSONL = Path("reports/20260704_113116_544062_release_decision_post594_report_readonly.jsonl")
TARGET_IDS = (99428, 130189, 130329, 121728, 119985)
EXCLUDED_IDS = {
    120831,
    79601,
    104908,
    65282,
    75914,
    30464,
    45089,
    54888,
    68315,
    76377,
    77588,
    103547,
    104983,
    112620,
    114265,
}
SUGGESTIONS = {
    99428: "Não vou tirar os olhos dessa pessoa…",
    130189: "#F Tento chamar sua atenção de maneira discreta#!",
    130329: "#F Espero que este interesse explícito seja bem recebido#!",
    121728: "Bem, certamente é uma forma de impedir que continuem brigando #EMP entre si#!…",
    119985: "@warning_icon! #warning Devido à minha natureza compassiva, seguir este caminho aumentará consideravelmente meu [stress|lE].#!",
}
RISK_ALERTS = {
    99428: "pronoun_softened_neutral_rewrite_check",
    130189: "light_token_simple_tag_preserve_exact",
    130329: "light_token_simple_tag_preserve_exact",
    121728: "light_token_emp_tag_preserve_exact",
    119985: "light_token_icon_warning_stress_preserve_exact",
}
DYNAMIC_RE = re.compile(r"Concept\(|Glossary\(|Select_CString|SelectLocalization|\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\(|\n")
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Final read-only pre-release human packet after run 595.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT_JSONL)
    return parser.parse_args()


def read_jsonl(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
    return rows


def fetch_source(conn, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id AS segment_id, relative_path, source_line_number, source_key, spanish_text, english_text
        FROM source_segments
        WHERE id = ?
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else {}


def fetch_open_issues(conn, segment_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT issue_family, issue_kind, issue_severity, status, validation_status
        FROM ml_issue_ledger_items
        WHERE run_id = 76
          AND segment_id = ?
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        ORDER BY CASE issue_severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 issue_family, issue_kind
        LIMIT 12
        """,
        (segment_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def validate(row: dict[str, Any], segment_id: int) -> None:
    if segment_id in EXCLUDED_IDS:
        raise SystemExit(f"excluded id selected: {segment_id}")
    if row.get("classification") != "corrected_text_needs_human_text":
        raise SystemExit(f"target id {segment_id} is not corrected_text_needs_human_text")
    if row.get("visibility_group") != "narrative_events":
        raise SystemExit(f"target id {segment_id} is not narrative_events")
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        raise SystemExit(f"target id {segment_id} has unsupported token surface")
    if DYNAMIC_RE.search(str(row.get("output_text") or "")):
        raise SystemExit(f"target id {segment_id} violates dynamic/parser filter")


def main() -> None:
    args = parse_args()
    rows_by_id = read_jsonl(args.input_jsonl)
    conn = db.connect(db.load_settings())
    records: list[dict[str, Any]] = []
    for index, segment_id in enumerate(TARGET_IDS, 1):
        row = rows_by_id.get(segment_id)
        if not row:
            raise SystemExit(f"missing target id {segment_id}")
        validate(row, segment_id)
        source = fetch_source(conn, segment_id)
        output = str(row.get("output_text") or "")
        records.append(
            {
                "schema_version": 1,
                "source": SOURCE,
                "packet_index": index,
                "segment_id": segment_id,
                "source_key": row.get("source_key") or source.get("source_key"),
                "relative_path": row.get("relative_path") or source.get("relative_path"),
                "source_line_number": source.get("source_line_number"),
                "token_surface": row.get("token_surface"),
                "visibility_group": row.get("visibility_group"),
                "spanish_text": source.get("spanish_text"),
                "english_text": source.get("english_text"),
                "output_text": output,
                "confirmed_text": row.get("confirmed_text"),
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "open_issue_preview": fetch_open_issues(conn, segment_id),
                "blocker_reason": "corrected_text_needs_human_text",
                "selection_reason": "final_pre_release_narrative_short_plain_or_simple_light_token",
                "risk_alert": RISK_ALERTS[segment_id],
                "tokens_present": tokens(output),
                "suggested_corrected_text": SUGGESTIONS[segment_id],
                "human_decision": "",
                "corrected_text": "",
                "notes": "",
            }
        )
    counts = {
        "token_surface": dict(Counter(str(r["token_surface"]) for r in records).most_common()),
        "risk_alert": dict(Counter(str(r["risk_alert"]) for r in records).most_common()),
        "relative_path": dict(Counter(str(r["relative_path"]) for r in records).most_common()),
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "human_editable_packet_read_only",
        "input_jsonl": str(args.input_jsonl),
        "record_count": len(records),
        "target_segment_ids": [r["segment_id"] for r in records],
        "excluded_ids": sorted(EXCLUDED_IDS),
        "candidate_pool_note": "Only 5 narrative_events records passed the strict no dynamic/select/multiline/parser_later/Concept filter; package intentionally not padded with higher-risk items.",
        "counts": counts,
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
        "single_operational_recommendation": "Review these 5; if fewer than 4 are ready, stop pre-release and prepare release notes/backlog.",
    }
    base = reports_dir() / f"{stamp()}_release_decision_post595_final_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Release Decision Post-595 Final Human Packet",
        "",
        "Read-only packet. `Human decision` and `Corrected text` are intentionally blank.",
        "Valid decisions: `corrected_text`, `approve_already_ok`, `needs_more_context`, `parser_later`, `reject`.",
        "Only 5 items passed the strict final pre-release filters; this package is not padded with higher-risk items.",
        "",
    ]
    for record in records:
        issue_lines = [
            f"- {issue.get('issue_severity')} | {issue.get('issue_family')} | {issue.get('issue_kind')} | {issue.get('validation_status')}"
            for issue in record["open_issue_preview"]
        ]
        md_lines.extend(
            [
                f"## {record['packet_index']}. Segment {record['segment_id']}",
                f"- Source key: `{record['source_key']}`",
                f"- File/line: `{record['relative_path']}:{record['source_line_number']}`",
                f"- Token surface: `{record['token_surface']}`",
                f"- Open/high issues: `{record['open_issue_count']}` / `{record['high_issue_count']}`",
                f"- Blocker reason: `{record['blocker_reason']}`",
                f"- Risk alert: `{record['risk_alert']}`",
                f"- Tokens/variables: `{record['tokens_present']}`",
                "",
                "**Open issues preview**",
                *(issue_lines or ["- none"]),
                "",
                "**English/source if available**",
                "```text",
                str(record.get("english_text") or ""),
                "```",
                "",
                "**Spanish/source**",
                "```text",
                str(record.get("spanish_text") or ""),
                "```",
                "",
                "**Current output**",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Assisted corrected_text suggestion**",
                "```text",
                str(record.get("suggested_corrected_text") or ""),
                "```",
                "",
                "Human decision:",
                "",
                "Corrected text:",
                "",
                "Notes:",
                "",
            ]
        )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"target_segment_ids={summary['target_segment_ids']}")
    print(f"counts={json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
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
