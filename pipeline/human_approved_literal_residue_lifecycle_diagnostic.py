from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


APPROVED_DECISIONS = {"human_approved_for_protected_apply"}


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


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def latest_run_id(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT MAX(id) AS id FROM {table_name}").fetchone()
    if not row or row["id"] is None:
        raise SystemExit(f"missing latest run in {table_name}")
    return int(row["id"])


def fetch_one_dict(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def state_totals(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = fetch_one_dict(conn, "SELECT * FROM segment_state_runs WHERE id = ?", (run_id,))
    if not run:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    pending = fetch_one_dict(
        conn,
        """
        SELECT
            COUNT(*) AS pending_count,
            SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS needs_output_apply,
            SUM(CASE WHEN needs_human = 1 THEN 1 ELSE 0 END) AS needs_human,
            SUM(CASE WHEN lifecycle_policy_allowed = 1 THEN 1 ELSE 0 END) AS lifecycle_policy_allowed
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
        """,
        (run_id,),
    ) or {}
    return {
        "run_id": run_id,
        "total_segments": int(run.get("total_segments") or 0),
        "closed_count": int(run.get("closed_count") or 0),
        "pending_count": int(run.get("pending_count") or 0),
        "output_apply_pending_count": int(run.get("output_apply_pending_count") or 0),
        "reopen_count": int(run.get("reopen_count") or 0),
        "pending_needs_human": int(pending.get("needs_human") or 0),
        "pending_needs_output_apply": int(pending.get("needs_output_apply") or 0),
        "pending_lifecycle_policy_allowed": int(pending.get("lifecycle_policy_allowed") or 0),
    }


def ledger_totals(conn: sqlite3.Connection, ledger_run_id: int) -> dict[str, Any]:
    total = conn.execute(
        "SELECT COUNT(*) FROM ml_issue_ledger_items WHERE run_id = ? AND status = 'open'",
        (ledger_run_id,),
    ).fetchone()[0]
    top = [
        dict(row)
        for row in conn.execute(
            """
            SELECT issue_family, issue_kind, issue_severity, COUNT(*) AS count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY issue_family, issue_kind, issue_severity
            ORDER BY count DESC, issue_family, issue_kind
            LIMIT 15
            """,
            (ledger_run_id,),
        )
    ]
    return {"ledger_run_id": ledger_run_id, "open_issue_total": int(total or 0), "top_open_issue_groups": top}


def segment_diagnostic(
    conn: sqlite3.Connection,
    decision: dict[str, Any],
    state_run_id: int,
    ledger_run_id: int,
) -> dict[str, Any]:
    segment_id = int(decision["segment_id"])
    output = fetch_one_dict(
        conn,
        """
        SELECT segment_id, relative_path, output_line_number, portuguese_text
        FROM output_segments
        WHERE segment_id = ?
        """,
        (segment_id,),
    )
    confirmation = fetch_one_dict(
        conn,
        """
        SELECT segment_id, confirmation_level, confirmed_text, confirmation_source,
               confirmation_label, locked, candidate_id, reviewer, confirmed_at, updated_at
        FROM segment_confirmations
        WHERE segment_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (segment_id,),
    )
    state = fetch_one_dict(
        conn,
        """
        SELECT segment_id, relative_path, source_key, final_state, state_group, review_state,
               apply_state, confirmation_level, confirmation_label, locked,
               confirmed_matches_output, needs_human, needs_output_apply,
               lifecycle_policy_action, lifecycle_policy_allowed, reasons_json
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (state_run_id, segment_id),
    )
    issues = [
        dict(row)
        for row in conn.execute(
            """
            SELECT issue_family, issue_kind, issue_severity, agent_key, status,
                   route_status, proposed_action, validation_status
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id = ?
              AND status = 'open'
            ORDER BY issue_severity DESC, issue_family, issue_kind
            """,
            (ledger_run_id, segment_id),
        )
    ]

    output_text = output.get("portuguese_text") if output else None
    confirmed_text = confirmation.get("confirmed_text") if confirmation else None
    candidate_text = decision.get("candidate_text")
    output_matches_confirmation = bool(output_text and confirmed_text and output_text == confirmed_text)
    output_matches_candidate = bool(output_text and candidate_text and output_text == candidate_text)
    confirmation_matches_candidate = bool(confirmed_text and candidate_text and confirmed_text == candidate_text)
    latest_confirmation_locked = bool(confirmation and int(confirmation.get("locked") or 0) == 1)

    bridge_blockers: list[str] = []
    if not state or state.get("state_group") != "pending":
        bridge_blockers.append("segment is not in current pending state expected by this diagnostic")
    if state and int(state.get("lifecycle_policy_allowed") or 0) != 1:
        bridge_blockers.append("latest segment-state did not allow any lifecycle policy for this segment")
    if state and state.get("lifecycle_policy_action") in (None, ""):
        bridge_blockers.append("latest segment-state has no lifecycle_policy_action")
    if output_text and "Esta personagem" not in output_text:
        bridge_blockers.append("existing trait_description_esta_personagem bridge requires current text containing 'Esta personagem'")
    if decision.get("audit_source") != "gender_semantic_literal_residue_audit_v1":
        bridge_blockers.append("decision source is outside the literal residue audit lane")
    if decision.get("human_decision") not in APPROVED_DECISIONS:
        bridge_blockers.append("human decision is not approved for protected apply")
    issue_families = sorted({issue["issue_family"] for issue in issues})
    if issue_families != ["gender_token_microagent", "semantic_review_router"]:
        bridge_blockers.append(f"open issue family shape is {issue_families}, not a known exact lifecycle bridge shape")
    if output_matches_confirmation and confirmation_matches_candidate and latest_confirmation_locked:
        signal_status = "confirmed_post_apply_output_aligned"
    else:
        signal_status = "not_fully_aligned"

    return {
        "segment_id": segment_id,
        "relative_path": decision.get("relative_path") or (state or {}).get("relative_path"),
        "source_key": decision.get("source_key") or (state or {}).get("source_key"),
        "human_decision": decision.get("human_decision"),
        "human_reason": decision.get("human_reason"),
        "candidate_text": candidate_text,
        "current_output_text": output_text,
        "latest_confirmation": confirmation,
        "latest_state_item": state,
        "open_issues": issues,
        "alignment": {
            "output_matches_confirmation": output_matches_confirmation,
            "output_matches_candidate": output_matches_candidate,
            "confirmation_matches_candidate": confirmation_matches_candidate,
            "latest_confirmation_locked": latest_confirmation_locked,
            "signal_status": signal_status,
        },
        "existing_bridge_blockers": bridge_blockers,
        "existing_architecture_can_close_now": False,
        "architecture_review_required": True,
    }


def build_architecture_prompt(summary: dict[str, Any]) -> str:
    segments = summary["segments"]
    segment_lines = []
    for item in segments:
        state = item.get("latest_state_item") or {}
        families = sorted({issue["issue_family"] for issue in item["open_issues"]})
        segment_lines.extend(
            [
                f"- Segmento {item['segment_id']} `{item['relative_path']}` / `{item['source_key']}`",
                f"  - estado atual: `{state.get('state_group')}` / `{state.get('final_state')}`",
                f"  - output=confirmation=candidate: {item['alignment']['output_matches_confirmation'] and item['alignment']['confirmation_matches_candidate']}",
                f"  - confirmação: `{(item.get('latest_confirmation') or {}).get('confirmation_level')}` / locked={(item.get('latest_confirmation') or {}).get('locked')}",
                f"  - issues abertas: {', '.join(families)}",
            ]
        )
    return "\n".join(
        [
            "# Prompt para revisão de arquitetura: fechamento lifecycle de reparo literal aprovado",
            "",
            "Contexto: após apply protegido e pós-validação OK, um reparo literal aprovado por humano foi ingerido no aprendizado local e confirmado no output, mas o `segment-state` permanece como pendente/reopen.",
            "",
            "Pedido para o chat de arquitetura:",
            "Revisar se devemos adicionar uma ponte/política governada para fechar segmentos com reparo literal aprovado por humano após apply protegido, ou se a correção deve ocorrer via issue-ledger/propagação de confirmação.",
            "",
            "Sinais confirmados disponíveis:",
            *segment_lines,
            "",
            "Relatórios relacionados:",
            f"- Decisões humanas: `{summary['input_decision_path']}`",
            f"- Segment-state latest run: `{summary['state']['run_id']}`",
            f"- Issue ledger latest run: `{summary['ledger']['ledger_run_id']}`",
            f"- Diagnóstico read-only: `{summary['txt_report_path']}`",
            f"- JSON do diagnóstico: `{summary['summary_path']}`",
            "",
            "Guardrails exigidos para qualquer solução:",
            "- usar apenas decisão humana aprovada e apply protegido pós-validado;",
            "- exigir output atual igual ao texto confirmado e ao candidato aplicado;",
            "- exigir confirmação `human_confirmed`, `local_learning`, `correct`, `locked=1`; ",
            "- exigir `needs_output_apply=0`, `confirmed_matches_output=1`, token/structure OK no pacote de origem;",
            "- fechar/atualizar somente issue shape explicitamente permitido;",
            "- não rodar produção full automaticamente;",
            "- não misturar novo apply com lifecycle/reindex.",
            "",
            "Pergunta de arquitetura:",
            "Isso deve ser uma nova ponte de `segment_state_snapshot`, uma política de fechamento/atualização no issue-ledger, ou uma propagação genérica de confirmação humana locked para substituir o estado auto-confirmed antigo?",
        ]
    ) + "\n"


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_human_approved_literal_residue_lifecycle_diagnostic"
    txt_path = base.with_suffix(".txt")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    prompt_path = reports_dir() / f"{base.name}_architecture_prompt.md"
    summary["txt_report_path"] = str(txt_path)
    summary["summary_path"] = str(summary_path)
    summary["architecture_prompt_path"] = str(prompt_path)
    prompt = build_architecture_prompt(summary)
    lines = [
        "Human-approved literal residue lifecycle diagnostic",
        f"decision_path={summary['input_decision_path']}",
        f"segment_state_run_id={summary['state']['run_id']}",
        f"ledger_run_id={summary['ledger']['ledger_run_id']}",
        "",
        "Execution stats:",
        f"- human_decision_rows: {summary['human_decision_rows']}",
        f"- approved_decision_rows: {summary['approved_decision_rows']}",
        f"- diagnosed_count: {summary['diagnosed_count']}",
        f"- existing_architecture_can_close_now: {summary['existing_architecture_can_close_now']}",
        f"- architecture_review_required: {summary['architecture_review_required']}",
        "",
        "State stats:",
        f"- total_segments: {summary['state']['total_segments']}",
        f"- closed_count: {summary['state']['closed_count']}",
        f"- pending_count: {summary['state']['pending_count']}",
        f"- output_apply_pending_count: {summary['state']['output_apply_pending_count']}",
        f"- pending_needs_human: {summary['state']['pending_needs_human']}",
        f"- pending_needs_output_apply: {summary['state']['pending_needs_output_apply']}",
        "",
        "Ledger stats:",
        f"- open_issue_total: {summary['ledger']['open_issue_total']}",
        "",
        "Segments:",
    ]
    for item in summary["segments"]:
        state = item.get("latest_state_item") or {}
        lines.extend(
            [
                f"- {item['segment_id']}: {item['relative_path']} / {item['source_key']}",
                f"  state={state.get('state_group')} / {state.get('final_state')}",
                f"  alignment={item['alignment']['signal_status']}",
                f"  blockers={'; '.join(item['existing_bridge_blockers'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Decision:",
            "- do_not_run_more_lifecycle_until_architecture_review=true",
            "- production_full_recommended_now=false",
            f"- architecture_prompt_path={prompt_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    prompt_path.write_text(prompt, encoding="utf-8")
    return txt_path, summary_path, prompt_path


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    decision_path = Path(args.decisions_jsonl) if args.decisions_jsonl else latest_file("*gender_semantic_literal_residue_human_decisions.jsonl")
    if not decision_path:
        raise SystemExit("missing gender semantic literal residue human decisions JSONL")
    decision_rows = read_jsonl(decision_path)
    approved = [row for row in decision_rows if row.get("human_decision") in APPROVED_DECISIONS]
    with connect_readonly() as conn:
        state_run_id = args.segment_state_run_id or latest_run_id(conn, "segment_state_runs")
        ledger_run_id = args.ledger_run_id or latest_run_id(conn, "ml_issue_ledger_runs")
        segments = [segment_diagnostic(conn, row, state_run_id, ledger_run_id) for row in approved]
        summary = {
            "schema_version": 1,
            "source": "human_approved_literal_residue_lifecycle_diagnostic_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "input_decision_path": str(decision_path),
            "human_decision_rows": len(decision_rows),
            "approved_decision_rows": len(approved),
            "diagnosed_count": len(segments),
            "state": state_totals(conn, state_run_id),
            "ledger": ledger_totals(conn, ledger_run_id),
            "segments": segments,
            "existing_architecture_can_close_now": False,
            "architecture_review_required": bool(segments),
            "do_not_run_more_lifecycle_until_architecture_review": bool(segments),
            "production_full_recommended_now": False,
            "recommended_next_action": "send_architecture_prompt_before_more_lifecycle_reindex_for_this_closure_pattern",
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions-jsonl")
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    args = parser.parse_args()
    summary = build_summary(args)
    txt_path, summary_path, prompt_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print(f"architecture_prompt={prompt_path}")
    print(f"diagnosed_count={summary['diagnosed_count']}")
    print(f"architecture_review_required={summary['architecture_review_required']}")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
