from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post593_corrected_text_human_packet_v1"
DEFAULT_INPUT_JSONL = Path("reports/20260704_101317_604881_release_decision_post593_report_readonly.jsonl")
LIMIT = 20
EXCLUDED_IDS = {79601, 104908, 120831}
TARGET_IDS = (
    30464,
    45089,
    54888,
    65282,
    68315,
    75914,
    76377,
    77588,
    100383,
    100967,
    103547,
    104983,
    108759,
    112620,
    114090,
    114174,
    114261,
    114264,
    114265,
    114271,
)
SUGGESTIONS = {
    30464: "A coroa é colocada sobre sua fronte.",
    45089: "Cada favor equipado #positive_value aumenta#! ligeiramente a probabilidade de vencer [contests|lE]",
    54888: "\"Posso trocar este jovem aprendiz por suprimentos.\"",
    65282: "#EMP Revistas o cadáver; não é como se ele ainda fosse precisar de algo#!",
    68315: "Você lhe conta que é uma farsa",
    75914: "#help Quanto mais duelos forem vencidos, mais forte será o modificador acima#!",
    76377: "Já recita com facilidade parágrafos dos clássicos para uma plateia atenta. Eu também sinto como me atraem as perguntas convincentes que faz sobre os textos.",
    77588: "#EMP Você continua segurando o [Glossary( 'sensu', 'SENSU_GLOSS' )] de maneira incorreta.#!",
    100383: "O lançamento deve ter a força #EMP certa#!…",
    100967: "#weak Vamos procurar um novo lar para este filhotinho#!",
    103547: "Ofereça-lhe um bom trato.",
    104983: "Embora eu mantenha o ceticismo, #EMP foi#! bastante grandioso.",
    108759: "O pregador infantil",
    112620: "#weak A personalidade, a [opinion|lE] e o tipo de [petition_liege|El] afetam as chances de aceitação#!",
    114090: "#weak Os trabalhos serão avaliados…#!",
    114174: "#weak O oráculo será avaliado…#!",
    114261: "Com a respiração controlada, realiza uma exibição de lança perfeita.",
    114264: "Com tranquilidade, começa a recitar as Analectas de memória.",
    114265: "Com um floreio audacioso, lança-se em um debate animado, no qual desafia até a minha sabedoria.",
    114271: "Admitindo suas falhas, realiza uma demonstração trêmula de lança para que seja corrigido.",
}
DYNAMIC_RE = re.compile(r"Concept\(|Select_CString|SelectLocalization|\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\(")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only editable human packet for post593 corrected-text blockers.")
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
        ORDER BY
          CASE issue_severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
          issue_family, issue_kind
        """,
        (segment_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def protected_tokens(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!|Glossary\([^)]+\)", text or "")


def main() -> None:
    args = parse_args()
    rows_by_id = read_jsonl(args.input_jsonl)
    records: list[dict[str, Any]] = []
    conn = db.connect(db.load_settings())
    for index, segment_id in enumerate(TARGET_IDS[:LIMIT], 1):
        row = rows_by_id.get(segment_id)
        if not row:
            raise SystemExit(f"missing target id {segment_id} in {args.input_jsonl}")
        output = str(row.get("output_text") or "")
        if segment_id in EXCLUDED_IDS:
            raise SystemExit(f"excluded id selected: {segment_id}")
        if row.get("classification") != "corrected_text_needs_human_text":
            raise SystemExit(f"target id {segment_id} is not corrected_text_needs_human_text")
        if "\n" in output or DYNAMIC_RE.search(output):
            raise SystemExit(f"target id {segment_id} violates filter")
        source = fetch_source(conn, segment_id)
        issues = fetch_open_issues(conn, segment_id)
        suggestion = SUGGESTIONS.get(segment_id, "")
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
                "spanish_text": source.get("spanish_text"),
                "english_text": source.get("english_text"),
                "output_text": output,
                "confirmed_text": row.get("confirmed_text"),
                "open_issue_count": row.get("open_issue_count"),
                "high_issue_count": row.get("high_issue_count"),
                "issue_families": row.get("issue_families"),
                "issue_kinds": row.get("issue_kinds"),
                "open_issue_preview": issues[:8],
                "blocker_reason": "corrected_text_needs_human_text",
                "primary_risk": "spanish_residue_visible_or_high_issue_non_dynamic",
                "tokens_present": protected_tokens(output),
                "assisted_suggestion_not_human_decision": True,
                "suggested_human_decision": "corrected_text" if suggestion else "needs_more_context",
                "suggested_corrected_text": suggestion,
                "human_decision": "",
                "corrected_text": "",
                "notes": "",
            }
        )
    counts = {
        "token_surface": dict(Counter(str(r["token_surface"]) for r in records).most_common()),
        "relative_path": dict(Counter(str(r["relative_path"]) for r in records).most_common()),
        "suggested_decision": dict(Counter(str(r["suggested_human_decision"]) for r in records).most_common()),
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "human_editable_packet_read_only",
        "input_jsonl": str(args.input_jsonl),
        "record_count": len(records),
        "target_segment_ids": [r["segment_id"] for r in records],
        "excluded_ids": sorted(EXCLUDED_IDS),
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
        "single_operational_recommendation": "Fill human_decision/corrected_text manually, then run read-only extraction and diff preview.",
    }
    base = reports_dir() / f"{stamp()}_release_decision_post593_corrected_text_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Release Decision Post-593 Corrected Text Human Packet",
        "",
        "Campos `Human decision` e `Corrected text` ficam vazios de propósito.",
        "As sugestões são auxiliares e não contam como decisão humana registrada.",
        "Decisões válidas: `corrected_text`, `approve_already_ok`, `needs_more_context`, `parser_later`, `reject`.",
        "",
    ]
    for record in records:
        md_lines.extend(
            [
                f"## {record['packet_index']}. Segment {record['segment_id']}",
                f"- Source key: `{record['source_key']}`",
                f"- File/line: `{record['relative_path']}:{record['source_line_number']}`",
                f"- Token surface: `{record['token_surface']}`",
                f"- Open/high issues: `{record['open_issue_count']}` / `{record['high_issue_count']}`",
                f"- Issue families: `{record['issue_families']}`",
                f"- Issue kinds: `{record['issue_kinds']}`",
                f"- Blocker reason: `{record['blocker_reason']}`",
                f"- Primary risk: `{record['primary_risk']}`",
                f"- Tokens/variables: `{record['tokens_present']}`",
                f"- Assisted suggestion: `{record['suggested_human_decision']}`",
                "",
                "**English/source if available**",
                "",
                "```text",
                str(record.get("english_text") or ""),
                "```",
                "",
                "**Spanish/source**",
                "",
                "```text",
                str(record.get("spanish_text") or ""),
                "```",
                "",
                "**Current output**",
                "",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
                "**Current confirmed/target**",
                "",
                "```text",
                str(record.get("confirmed_text") or ""),
                "```",
                "",
                "**Suggested corrected_text (assistant aid only)**",
                "",
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
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
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
