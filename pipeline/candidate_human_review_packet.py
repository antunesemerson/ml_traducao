from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
REVIEW_OPTIONS = [
    "approve_for_future_apply",
    "reject",
    "needs_more_context",
    "duplicate_of_existing_candidate",
]
INPUTS = {
    "final_jsonl": "reports/20260624_122842_736549_candidate_discovery_final_status_and_human_review_plan.jsonl",
    "final_summary": "reports/20260624_122842_736549_candidate_discovery_final_status_and_human_review_plan_summary.json",
    "semantic_autofix_jsonl": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit.jsonl",
    "short_label_jsonl": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit.jsonl",
    "semantic_single_jsonl": "reports/20260624_121516_803530_semantic_single_family_candidate_small_audit.jsonl",
}


def output_paths() -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_candidate_human_review_packet"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_summary.json",
        base.with_suffix(".md"),
    )


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def load_inputs() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = {key: db.project_path(value) for key, value in INPUTS.items()}
    for path in paths.values():
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    final_rows = read_jsonl(paths["final_jsonl"])
    final_summary = read_json(paths["final_summary"])
    for key in ["semantic_autofix_jsonl", "short_label_jsonl", "semantic_single_jsonl"]:
        if not read_jsonl(paths[key]):
            raise SystemExit(f"empty source audit JSONL: {paths[key]}")
    return final_rows, final_summary


def validate_summary(summary: dict[str, Any], segment_state_run_id: int, ledger_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")
    expected = {
        "total_final_candidates": 17,
        "unique_final_segments": 16,
        "duplicate_candidate_count": 1,
        "accepted_candidates": 17,
        "rejected_candidates": 0,
        "requires_human_review_count": 17,
        "token_integrity_ok_count": 17,
        "structure_integrity_ok_count": 17,
        "safe_for_future_apply_batch_count": 17,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_ready_now": 0,
    }
    for key, value in expected.items():
        if int(summary.get(key) or 0) != value:
            raise SystemExit(f"summary guard failed: {key}")
    if bool(summary.get("production_full_recommended_now")) or bool(summary.get("model_training_recommended")):
        raise SystemExit("production/model summary guard failed")


def validate_pending(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> None:
    unique_ids = sorted(set(segment_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *unique_ids),
    ).fetchall()
    if len(rows) != len(unique_ids):
        raise SystemExit(f"missing state rows: expected {len(unique_ids)}, got {len(rows)}")
    bad = [
        dict(row)
        for row in rows
        if row["state_group"] != "pending"
        or int(row["is_closed"] or 0) != 0
        or int(row["needs_output_apply"] or 0) != 0
        or int(row["confirmed_matches_output"] or 0) != 1
    ]
    if bad:
        raise SystemExit(f"pending guard failed: {bad[:3]}")


def summarize_change(current: str, candidate: str, candidate_type: str) -> str:
    if current == candidate:
        return "No text change"
    if candidate_type.endswith("spacing_punctuation_cleanup"):
        return "Mechanical spacing/punctuation cleanup"
    if "minor_lexical_repair" in candidate_type:
        return "Minor lexical repair"
    if "style_normalization" in candidate_type:
        return "Short-label style normalization"
    if "ptbr_naturalness" in candidate_type:
        return "PT-BR naturalness cleanup"
    if "article_preposition" in candidate_type:
        return "Article/preposition cleanup"
    return "Small audited text cleanup"


def duplicate_groups(rows: list[dict[str, Any]]) -> dict[int, str]:
    counts = Counter(int(row["segment_id"]) for row in rows)
    duplicate_ids = [segment_id for segment_id, count in counts.items() if count > 1]
    return {segment_id: f"dup-{index + 1}" for index, segment_id in enumerate(sorted(duplicate_ids))}


def build_packet_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = duplicate_groups(rows)
    packet_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        segment_id = int(row["segment_id"])
        duplicate_group = groups.get(segment_id, "")
        packet_rows.append(
            {
                "candidate_index": index,
                "segment_id": segment_id,
                "duplicate_group_id": duplicate_group,
                "is_duplicate": bool(duplicate_group),
                "origin_audit": str(row.get("origin_audit") or ""),
                "candidate_type": str(row.get("candidate_type") or ""),
                "human_review_priority": str(row.get("human_review_priority") or "medium"),
                "original_text": str(row.get("original_text") or ""),
                "current_output_text": str(row.get("current_output_text") or ""),
                "candidate_text": str(row.get("candidate_text") or ""),
                "change_summary": summarize_change(
                    str(row.get("current_output_text") or ""),
                    str(row.get("candidate_text") or ""),
                    str(row.get("candidate_type") or ""),
                ),
                "audit_reason": str(row.get("notes") or ""),
                "token_integrity_ok": bool(row.get("token_integrity_ok")),
                "structure_integrity_ok": bool(row.get("structure_integrity_ok")),
                "safe_for_future_apply_batch": bool(row.get("safe_for_future_apply_batch")),
                "requires_human_review": True,
                "review_options": REVIEW_OPTIONS,
                "requires_apply_later": False,
                "requires_lifecycle_later": False,
                "false_safe_risk": False,
                "notes": str(row.get("notes") or ""),
            }
        )
    return packet_rows


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    origin_counts = Counter(row["origin_audit"] for row in rows)
    type_counts = Counter(row["candidate_type"] for row in rows)
    priority_counts = Counter(row["human_review_priority"] for row in rows)
    duplicate_ids = {row["segment_id"] for row in rows if row["is_duplicate"]}
    summary = {
        "schema_version": 1,
        "source": "candidate_human_review_packet_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "raw_candidate_count": len(rows),
        "unique_segment_count": len({row["segment_id"] for row in rows}),
        "duplicate_count": len(rows) - len({row["segment_id"] for row in rows}),
        "duplicate_group_count": len(duplicate_ids),
        "accepted_candidate_count": sum(1 for row in rows if row["safe_for_future_apply_batch"]),
        "rejected_candidate_count": 0,
        "pending_human_review_count": sum(1 for row in rows if row["requires_human_review"]),
        "high_priority_count": priority_counts.get("high", 0),
        "medium_priority_count": priority_counts.get("medium", 0),
        "low_priority_count": priority_counts.get("low", 0),
        "by_origin_audit": dict(sorted(origin_counts.items())),
        "by_candidate_type": dict(sorted(type_counts.items())),
        "token_integrity_ok_count": sum(1 for row in rows if row["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for row in rows if row["structure_integrity_ok"]),
        "safe_for_future_apply_batch_count": sum(1 for row in rows if row["safe_for_future_apply_batch"]),
        "requires_apply_later_count": sum(1 for row in rows if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in rows if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in rows if row["false_safe_risk"]),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "model_training_recommended": False,
        "network_update_recommended": False,
        "network_update_data_only_optional": True,
        "recommended_next_prompt": "chat_human_review_candidate_packet_prompt.md",
    }
    expected = {
        "raw_candidate_count": 17,
        "unique_segment_count": 16,
        "duplicate_count": 1,
        "accepted_candidate_count": 17,
        "rejected_candidate_count": 0,
        "pending_human_review_count": 17,
        "token_integrity_ok_count": 17,
        "structure_integrity_ok_count": 17,
        "safe_for_future_apply_batch_count": 17,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_ready_now": 0,
    }
    for key, value in expected.items():
        if int(summary[key]) != value:
            raise SystemExit(f"packet summary guard failed: {key}")
    return summary


def fenced(value: str) -> str:
    return f"```text\n{value}\n```"


def write_markdown(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines: list[str] = [
        "# Candidate Human Review Packet",
        "",
        "## Resumo executivo",
        "",
        f"- Candidatos brutos: {summary['raw_candidate_count']}",
        f"- Segmentos unicos: {summary['unique_segment_count']}",
        f"- Duplicatas: {summary['duplicate_count']}",
        f"- Pendentes de revisao humana: {summary['pending_human_review_count']}",
        f"- Apply pronto agora: {summary['apply_ready_now']}",
        f"- Producao full recomendada agora: {str(summary['production_full_recommended_now']).lower()}",
        "",
        "## Tabela de contadores",
        "",
        "| Metrica | Valor |",
        "| --- | ---: |",
        f"| Raw candidates | {summary['raw_candidate_count']} |",
        f"| Unique segments | {summary['unique_segment_count']} |",
        f"| Duplicate count | {summary['duplicate_count']} |",
        f"| Token integrity ok | {summary['token_integrity_ok_count']} |",
        f"| Structure integrity ok | {summary['structure_integrity_ok_count']} |",
        f"| High priority | {summary['high_priority_count']} |",
        f"| Medium priority | {summary['medium_priority_count']} |",
        f"| Low priority | {summary['low_priority_count']} |",
        "",
        "## Como revisar",
        "",
        "Para cada candidato, escolha uma opcao: `approve_for_future_apply`, `reject`, `needs_more_context` ou `duplicate_of_existing_candidate`.",
        "Nenhuma decisao sera aplicada automaticamente a partir deste pacote.",
        "",
        "## Lista de candidatos",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## Candidate {row['candidate_index']} - segment {row['segment_id']}",
                "",
                f"Origem: `{row['origin_audit']}`",
                f"Tipo: `{row['candidate_type']}`",
                f"Prioridade: `{row['human_review_priority']}`",
                f"Duplicate group: `{row['duplicate_group_id'] or 'none'}`",
                f"Resumo da mudanca: {row['change_summary']}",
                "",
                "Original:",
                fenced(row["original_text"]),
                "",
                "Output atual:",
                fenced(row["current_output_text"]),
                "",
                "Candidato:",
                fenced(row["candidate_text"]),
                "",
                "Motivo da auditoria:",
                fenced(row["audit_reason"]),
                "",
                "Opcoes de revisao:",
                "- approve_for_future_apply",
                "- reject",
                "- needs_more_context",
                "- duplicate_of_existing_candidate",
                "",
            ]
        )
    duplicate_rows = [row for row in rows if row["is_duplicate"]]
    lines.extend(["## Duplicatas", ""])
    if duplicate_rows:
        for row in duplicate_rows:
            lines.append(f"- `{row['duplicate_group_id']}` segment `{row['segment_id']}` candidate `{row['candidate_index']}` origem `{row['origin_audit']}`")
    else:
        lines.append("- Nenhuma duplicata.")
    lines.extend(
        [
            "",
            "## Bloqueios e proibicoes",
            "",
            "- Nao aplicar automaticamente.",
            "- Nao rodar producao full agora.",
            "- Nao rodar lifecycle, segment-state, issue-ledger, confirmations, reindex ou treino/model promotion.",
            "- Duplicata nao deve ser somada como mudanca independente em apply futuro.",
            "",
            "## Proximo passo apos revisao humana",
            "",
            "Depois da revisao humana, gerar um plano de apply protegido somente para candidatos aprovados, com diff preview e rollback path.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(rows: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    txt_path, jsonl_path, summary_path, md_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(md_path, rows, summary)
    metric_keys = [
        "raw_candidate_count",
        "unique_segment_count",
        "duplicate_count",
        "accepted_candidate_count",
        "rejected_candidate_count",
        "pending_human_review_count",
        "high_priority_count",
        "medium_priority_count",
        "low_priority_count",
        "by_origin_audit",
        "by_candidate_type",
        "token_integrity_ok_count",
        "structure_integrity_ok_count",
        "safe_for_future_apply_batch_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
        "apply_ready_now",
        "production_full_recommended_now",
        "model_training_recommended",
    ]
    lines = [
        "candidate human review packet",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "analysis:",
        "1. Pacote pronto para revisao humana: true.",
        f"2. Candidatos brutos={summary['raw_candidate_count']}; segmentos unicos={summary['unique_segment_count']}.",
        "3. Duplicidade mantida nas 17 linhas e marcada com duplicate_group_id; nao contar como mudanca independente.",
        "4. Pronto para apply automatico: false.",
        "5. Depois da revisao humana: gerar plano de apply protegido apenas para aprovados.",
        "6. Producao full agora: false.",
        "7. Network agora: sem redesign; data-only opcional.",
        "",
        f"markdown_packet={md_path}",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    args = parser.parse_args()
    final_rows, final_summary = load_inputs()
    validate_summary(final_summary, args.segment_state_run_id, args.ledger_run_id)
    with connect_readonly() as conn:
        validate_pending(conn, [int(row["segment_id"]) for row in final_rows], args.segment_state_run_id)
    packet_rows = build_packet_rows(final_rows)
    summary = build_summary(packet_rows)
    txt_path, jsonl_path, summary_path, md_path = write_outputs(packet_rows, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"markdown={md_path}")
    for key in [
        "raw_candidate_count",
        "unique_segment_count",
        "duplicate_count",
        "pending_human_review_count",
        "requires_apply_later_count",
        "false_safe_risk_count",
        "apply_ready_now",
        "production_full_recommended_now",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
