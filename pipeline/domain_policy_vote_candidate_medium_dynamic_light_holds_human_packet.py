from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_holds_human_packet_v1"
HOLDS_JSONL = Path("reports/20260629_224912_810219_domain_policy_vote_candidate_medium_dynamic_light_remaining_holds_diagnostic.jsonl")
HOLDS_SUMMARY = Path("reports/20260629_224912_810219_domain_policy_vote_candidate_medium_dynamic_light_remaining_holds_diagnostic_summary.json")
EXPECTED_PACKET_COUNT = 13

TOKEN_RE = re.compile(
    r"(\[[^\[\]\n]+\]|\$[A-Za-z0-9_]+\$|#[A-Za-z0-9_]+|#!)"
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def protected_tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def fetch_full_rows(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_line_number,
            s.source_key,
            s.spanish_text,
            s.english_text,
            o.portuguese_text AS current_output_text,
            o.output_line_number
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def packet_focus(row: dict[str, Any]) -> str:
    family = row["hold_family"]
    if family == "article_preposition_uncertain":
        return "Validar artigo/preposição e termo de domínio; corrigir só se o texto atual estiver semanticamente errado."
    if row["source_key"] in {"claim_cb_victory_desc_attacker_claimant", "claim_cb_white_peace_desc_defender"}:
        return "Texto ainda em inglês no output; revisar tradução PT-BR preservando o token [title|lE]."
    if "adherent" in " ".join(row.get("dynamic_tokens") or []):
        return "Validar concordância singular/plural e posição de 'antigo/antigos' ou termo equivalente."
    if row["surface_bucket"] == "culture_tradition_innovation":
        return "Validar naturalidade e concordância ao redor de token cultural/cavaleiros."
    return "Revisar sem generalizar; aprovar como já ok ou fornecer corrected_text completo."


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_remaining_holds_diagnostic":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("human_packet_candidate_count") or 0) != EXPECTED_PACKET_COUNT:
        raise SystemExit("human_packet_candidate_count guard failed")
    if summary.get("production_full_recommended_now") is not False:
        raise SystemExit("production_full_recommended_now guard failed")
    packet_rows = [row for row in rows if row.get("operational_class") == "human_packet_now"]
    if len(packet_rows) != EXPECTED_PACKET_COUNT:
        raise SystemExit(f"packet row guard failed: {len(packet_rows)} expected {EXPECTED_PACKET_COUNT}")
    ids = [int(row["segment_id"]) for row in packet_rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id guard failed")
    return packet_rows


def build_packet_rows(rows: list[dict[str, Any]], full_rows: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    packet: list[dict[str, Any]] = []
    missing = [int(row["segment_id"]) for row in rows if int(row["segment_id"]) not in full_rows]
    if missing:
        raise SystemExit(f"missing full rows in database: {missing}")
    for index, row in enumerate(rows, start=1):
        segment_id = int(row["segment_id"])
        full = full_rows[segment_id]
        current = full.get("current_output_text") or row.get("current_output_text") or ""
        english = full.get("english_text") or row.get("english_text") or ""
        spanish = full.get("spanish_text") or row.get("spanish_text") or ""
        packet.append(
            {
                "packet_index": index,
                "segment_id": segment_id,
                "relative_path": full.get("relative_path") or row.get("relative_path"),
                "source_line_number": full.get("source_line_number"),
                "output_line_number": full.get("output_line_number"),
                "source_key": full.get("source_key") or row.get("source_key"),
                "hold_family": row.get("hold_family"),
                "surface_bucket": row.get("surface_bucket"),
                "route": row.get("route"),
                "next_action": row.get("next_action"),
                "review_focus": packet_focus(row),
                "dynamic_tokens": row.get("dynamic_tokens") or [],
                "protected_tokens_current_output": protected_tokens(current),
                "protected_tokens_english": protected_tokens(english),
                "protected_tokens_spanish": protected_tokens(spanish),
                "english_text": english,
                "spanish_text": spanish,
                "current_output_text": current,
                "allowed_decisions": [
                    "approve_already_ok",
                    "approve_correction",
                    "reject",
                    "needs_more_context",
                    "hold_structural_or_domain_risk",
                ],
                "human_decision": "",
                "corrected_text": "",
                "review_notes": "",
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )
    return packet


def fenced(value: str) -> str:
    return "```text\n" + str(value or "") + "\n```"


def write_markdown(path: Path, packet_rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# Medium Dynamic Light Holds Human Packet",
        "",
        f"- source: `{SOURCE}`",
        f"- packet_count: `{summary['packet_count']}`",
        "- allowed decisions: `approve_already_ok`, `approve_correction`, `reject`, `needs_more_context`, `hold_structural_or_domain_risk`",
        "- no candidate generation, apply, lifecycle, segment-state, reindex, or full production was run",
        "",
    ]
    for row in packet_rows:
        lines.extend(
            [
                f"## {row['packet_index']}. segment_id={row['segment_id']}",
                "",
                f"- hold_family: `{row['hold_family']}`",
                f"- surface_bucket: `{row['surface_bucket']}`",
                f"- relative_path: `{row['relative_path']}`",
                f"- source_key: `{row['source_key']}`",
                f"- source_line_number: `{row['source_line_number']}`",
                f"- output_line_number: `{row['output_line_number']}`",
                f"- review_focus: {row['review_focus']}",
                f"- dynamic_tokens: `{row['dynamic_tokens']}`",
                f"- protected_tokens_current_output: `{row['protected_tokens_current_output']}`",
                "",
                "**English**",
                fenced(row["english_text"]),
                "**Spanish**",
                fenced(row["spanish_text"]),
                "**Current output**",
                fenced(row["current_output_text"]),
                "**Human decision:**",
                "",
                "**Corrected text, if any:**",
                "",
                "**Notes:**",
                "",
                "---",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "medium_dynamic_light holds human packet",
        f"packet_count={summary['packet_count']}",
        "hold_family_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["hold_family_counts"])
    lines.extend(
        [
            "surface_bucket_counts:",
            *[f"- {item['count']} | {item['key']}" for item in summary["surface_bucket_counts"]],
            "candidate_generation_count=0",
            "apply_output_count=0",
            "lifecycle_count=0",
            "segment_state_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "source_changed=false",
            "output_changed=false",
            f"next_action={summary['next_action']}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    holds_summary = read_json(HOLDS_SUMMARY)
    holds_rows = read_jsonl(HOLDS_JSONL)
    source_rows = validate_inputs(holds_summary, holds_rows)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        full_rows = fetch_full_rows(conn, segment_ids)
    packet_rows = build_packet_rows(source_rows, full_rows)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_review_packet",
        "input_holds_jsonl": str(HOLDS_JSONL),
        "input_holds_summary": str(HOLDS_SUMMARY),
        "packet_count": len(packet_rows),
        "expected_packet_count": EXPECTED_PACKET_COUNT,
        "count_matches_expected": len(packet_rows) == EXPECTED_PACKET_COUNT,
        "packet_segment_ids": [row["segment_id"] for row in packet_rows],
        "hold_family_counts": top_counter(Counter(str(row["hold_family"]) for row in packet_rows)),
        "surface_bucket_counts": top_counter(Counter(str(row["surface_bucket"]) for row in packet_rows)),
        "allowed_decisions": [
            "approve_already_ok",
            "approve_correction",
            "reject",
            "needs_more_context",
            "hold_structural_or_domain_risk",
        ],
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "next_action": "human_review_then_decision_ingest; protected apply only for approve_correction rows",
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    txt_path = base.with_suffix(".txt")
    summary_path = Path(str(base) + "_summary.json")
    write_markdown(md_path, packet_rows, summary)
    write_jsonl(jsonl_path, packet_rows)
    summary["output_files"] = {
        "md": str(md_path),
        "jsonl": str(jsonl_path),
        "txt": str(txt_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
