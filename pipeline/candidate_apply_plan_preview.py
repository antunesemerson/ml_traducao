from __future__ import annotations

import argparse
import difflib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PLAN_KEY = "candidate_apply_plan_preview"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
APPROVED_INDEXES = {1, 2, 3, 4, 5, 7, 8, 13, 14, 15, 16, 17}
NEEDS_CONTEXT_INDEXES = {6, 9, 10, 12}
DUPLICATE_INDEXES = {11}
REJECT_INDEXES: set[int] = set()


def output_paths() -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_candidate_apply_plan_preview"
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


def validate_inputs(decision_rows: list[dict[str, Any]], decision_summary: dict[str, Any], packet_rows: list[dict[str, Any]], packet_summary: dict[str, Any]) -> None:
    expected_decision = {
        "raw_candidate_count": 17,
        "unique_segment_count": 16,
        "approve_for_future_apply_count": 12,
        "needs_more_context_count": 4,
        "duplicate_of_existing_candidate_count": 1,
        "reject_count": 0,
        "eligible_for_future_apply_plan_count": 12,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_ready_now": 0,
    }
    for key, expected in expected_decision.items():
        actual = int(decision_summary.get(key) or 0)
        if actual != expected:
            raise SystemExit(f"decision summary guard failed: {key} expected {expected}, got {actual}")
    expected_packet = {
        "raw_candidate_count": 17,
        "unique_segment_count": 16,
        "duplicate_count": 1,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_ready_now": 0,
    }
    for key, expected in expected_packet.items():
        actual = int(packet_summary.get(key) or 0)
        if actual != expected:
            raise SystemExit(f"packet summary guard failed: {key} expected {expected}, got {actual}")
    if len(decision_rows) != 17 or len(packet_rows) != 17:
        raise SystemExit("input row count guard failed")
    indexes = {int(row["candidate_index"]) for row in decision_rows}
    if indexes != set(range(1, 18)):
        raise SystemExit(f"candidate index guard failed: {sorted(indexes)}")


def fetch_state(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, sqlite3.Row]:
    unique_ids = sorted(set(segment_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.confirmed_matches_output,
            out.portuguese_text AS current_db_output_text
        FROM segment_state_items s
        LEFT JOIN output_segments out ON out.segment_id = s.segment_id
        WHERE s.run_id = ? AND s.segment_id IN ({placeholders})
        """,
        (run_id, *unique_ids),
    ).fetchall()
    if len(rows) != len(unique_ids):
        raise SystemExit(f"missing state rows: expected {len(unique_ids)}, got {len(rows)}")
    return {int(row["segment_id"]): row for row in rows}


def diff_preview(current: str, candidate: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            current.splitlines(),
            candidate.splitlines(),
            fromfile="current_output",
            tofile="candidate_text",
            lineterm="",
        )
    )


def build_plan_rows(decision_rows: list[dict[str, Any]], packet_rows: list[dict[str, Any]], state_rows: dict[int, sqlite3.Row]) -> list[dict[str, Any]]:
    packet_by_index = {int(row["candidate_index"]): row for row in packet_rows}
    plan_rows: list[dict[str, Any]] = []
    for decision in sorted(decision_rows, key=lambda row: int(row["candidate_index"])):
        index = int(decision["candidate_index"])
        if decision.get("human_review_decision") != "approve_for_future_apply" or not decision.get("eligible_for_future_apply_plan"):
            continue
        if index not in APPROVED_INDEXES:
            raise SystemExit(f"unexpected approved candidate index: {index}")
        packet = packet_by_index[index]
        segment_id = int(decision["segment_id"])
        state = state_rows[segment_id]
        current_text = str(packet.get("current_output_text") or "")
        candidate_text = str(packet.get("candidate_text") or "")
        state_guard_ok = (
            state["state_group"] == "pending"
            and int(state["is_closed"] or 0) == 0
            and int(state["needs_output_apply"] or 0) == 0
            and int(state["confirmed_matches_output"] or 0) == 1
        )
        output_guard_ok = str(state["current_db_output_text"] or "") == current_text
        token_ok = bool(packet.get("token_integrity_ok"))
        structure_ok = bool(packet.get("structure_integrity_ok"))
        eligible = bool(
            state_guard_ok
            and output_guard_ok
            and token_ok
            and structure_ok
            and candidate_text != current_text
            and not decision.get("requires_lifecycle_later")
            and not decision.get("false_safe_risk")
        )
        plan_rows.append(
            {
                "candidate_index": index,
                "segment_id": segment_id,
                "apply_plan_key": PLAN_KEY,
                "origin_audit": str(packet.get("origin_audit") or ""),
                "candidate_type": str(packet.get("candidate_type") or ""),
                "current_output_text": current_text,
                "candidate_text": candidate_text,
                "diff_preview": diff_preview(current_text, candidate_text),
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": structure_ok,
                "state_guard_ok": state_guard_ok,
                "output_guard_ok": output_guard_ok,
                "eligible_for_future_apply": eligible,
                "apply_now": False,
                "requires_apply_later": False,
                "requires_lifecycle_later": False,
                "false_safe_risk": False,
                "rollback_note": "Future protected apply must snapshot target files, apply only still-valid approved lines, validate, and restore snapshot on failure.",
            }
        )
    return plan_rows


def build_summary(decision_rows: list[dict[str, Any]], plan_rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(str(row["human_review_decision"]) for row in decision_rows)
    guard_fail_count = sum(1 for row in plan_rows if not row["eligible_for_future_apply"])
    summary = {
        "schema_version": 1,
        "source": "candidate_apply_plan_preview_v1",
        "approved_input_count": decisions.get("approve_for_future_apply", 0),
        "plan_candidate_count": len(plan_rows),
        "unique_segment_count": len({row["segment_id"] for row in plan_rows}),
        "excluded_needs_more_context_count": decisions.get("needs_more_context", 0),
        "excluded_duplicate_count": decisions.get("duplicate_of_existing_candidate", 0),
        "excluded_reject_count": decisions.get("reject", 0),
        "guard_pass_count": len(plan_rows) - guard_fail_count,
        "guard_fail_count": guard_fail_count,
        "eligible_for_future_apply_count": sum(1 for row in plan_rows if row["eligible_for_future_apply"]),
        "apply_now_count": sum(1 for row in plan_rows if row["apply_now"]),
        "requires_apply_later_count": sum(1 for row in plan_rows if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in plan_rows if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in plan_rows if row["false_safe_risk"]),
        "source_output_modified": False,
        "production_full_recommended_now": False,
        "next_prompt": "chat_exec_candidate_apply_protected_prompt.md"
        if guard_fail_count == 0
        else "chat_exec_candidate_apply_plan_guard_audit_prompt.md",
    }
    expected = {
        "approved_input_count": 12,
        "plan_candidate_count": 12,
        "unique_segment_count": 12,
        "excluded_needs_more_context_count": 4,
        "excluded_duplicate_count": 1,
        "excluded_reject_count": 0,
        "guard_fail_count": 0,
        "eligible_for_future_apply_count": 12,
        "apply_now_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
    }
    for key, expected_value in expected.items():
        actual = int(summary.get(key) or 0)
        if actual != expected_value:
            raise SystemExit(f"summary guard failed: {key} expected {expected_value}, got {actual}")
    return summary


def fenced(value: str) -> str:
    return f"```text\n{value}\n```"


def write_markdown(path: Path, plan_rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines: list[str] = [
        "# Candidate Apply Plan Preview",
        "",
        "## Resumo executivo",
        "",
        f"- Candidatos aprovados no input: {summary['approved_input_count']}",
        f"- Candidatos no plano: {summary['plan_candidate_count']}",
        f"- Segmentos unicos: {summary['unique_segment_count']}",
        f"- Guardas aprovadas: {summary['guard_pass_count']}",
        f"- Guardas com falha: {summary['guard_fail_count']}",
        f"- Apply agora: {summary['apply_now_count']}",
        "",
        "## Contadores",
        "",
        "| Metrica | Valor |",
        "| --- | ---: |",
        f"| Excluidos needs_more_context | {summary['excluded_needs_more_context_count']} |",
        f"| Excluidos duplicata | {summary['excluded_duplicate_count']} |",
        f"| Excluidos reject | {summary['excluded_reject_count']} |",
        f"| Elegiveis para apply futuro | {summary['eligible_for_future_apply_count']} |",
        "",
        "## Excluidos do plano",
        "",
        "- needs_more_context: candidates 6, 9, 10, 12",
        "- duplicate_of_existing_candidate: candidate 11",
        "- reject: none",
        "",
        "## Plano por candidato",
        "",
    ]
    for row in plan_rows:
        lines.extend(
            [
                f"## Candidate {row['candidate_index']} / segment {row['segment_id']}",
                "",
                f"Tipo: `{row['candidate_type']}`",
                f"Origem: `{row['origin_audit']}`",
                f"Resumo: futuro apply protegido substituiria o output atual pelo candidato, se todos os guards continuarem validos.",
                "",
                "Atual:",
                fenced(row["current_output_text"]),
                "",
                "Proposto:",
                fenced(row["candidate_text"]),
                "",
                "Diff preview:",
                fenced(row["diff_preview"]),
                "",
                "Guards:",
                f"- token_integrity_ok: {str(row['token_integrity_ok']).lower()}",
                f"- structure_integrity_ok: {str(row['structure_integrity_ok']).lower()}",
                f"- state_guard_ok: {str(row['state_guard_ok']).lower()}",
                f"- output_guard_ok: {str(row['output_guard_ok']).lower()}",
                "",
                "Apply agora: false",
                "",
            ]
        )
    lines.extend(
        [
            "## Rollback path",
            "",
            "1. Antes de apply real, gerar diff preview novamente.",
            "2. Salvar snapshot dos arquivos alvo.",
            "3. Aplicar somente linhas aprovadas e ainda validas.",
            "4. Rodar validadores.",
            "5. Se qualquer validacao falhar, reverter snapshot dos arquivos alterados.",
            "",
            "## Proximas travas antes de apply",
            "",
            "- Confirmacao explicita do usuario antes de qualquer apply real.",
            "- Revalidar estado pending e output atual.",
            "- Revalidar tokens, estrutura, diff preview e rollback path.",
            "- Nao rodar producao full como substituto do apply protegido.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(plan_rows: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    txt_path, jsonl_path, summary_path, md_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in plan_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(md_path, plan_rows, summary)
    lines = [
        "candidate apply plan preview",
        *[f"{key}={summary[key]}" for key in [
            "approved_input_count",
            "plan_candidate_count",
            "unique_segment_count",
            "excluded_needs_more_context_count",
            "excluded_duplicate_count",
            "excluded_reject_count",
            "guard_pass_count",
            "guard_fail_count",
            "eligible_for_future_apply_count",
            "apply_now_count",
            "requires_apply_later_count",
            "requires_lifecycle_later_count",
            "false_safe_risk_count",
            "source_output_modified",
            "production_full_recommended_now",
            "next_prompt",
        ]],
        "",
        "analysis:",
        "1. Candidatos no plano: 12.",
        "2. Excluidos: candidates 6,9,10,12 por needs_more_context; candidate 11 por duplicata; rejects 0.",
        f"3. Guardas com falha: {summary['guard_fail_count']}.",
        "4. Plano pronto para prompt de apply protegido futuro: true.",
        "5. Prompt futuro deve revalidar estado, output atual, tokens, diff preview, snapshots e rollback antes de aplicar.",
        "6. Producao full agora: nao.",
        f"markdown_preview={md_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision-jsonl", required=True)
    parser.add_argument("--decision-summary-json", required=True)
    parser.add_argument("--packet-jsonl", required=True)
    parser.add_argument("--packet-summary-json", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")

    decision_rows = read_jsonl(db.project_path(args.decision_jsonl))
    decision_summary = read_json(db.project_path(args.decision_summary_json))
    packet_rows = read_jsonl(db.project_path(args.packet_jsonl))
    packet_summary = read_json(db.project_path(args.packet_summary_json))
    validate_inputs(decision_rows, decision_summary, packet_rows, packet_summary)

    approved_ids = [int(row["segment_id"]) for row in decision_rows if row.get("human_review_decision") == "approve_for_future_apply"]
    with connect_readonly() as conn:
        state_rows = fetch_state(conn, approved_ids, args.segment_state_run_id)
    plan_rows = build_plan_rows(decision_rows, packet_rows, state_rows)
    summary = build_summary(decision_rows, plan_rows)
    txt_path, jsonl_path, summary_path, md_path = write_outputs(plan_rows, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"markdown={md_path}")
    for key in [
        "approved_input_count",
        "plan_candidate_count",
        "unique_segment_count",
        "guard_fail_count",
        "eligible_for_future_apply_count",
        "apply_now_count",
        "source_output_modified",
        "production_full_recommended_now",
        "next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
