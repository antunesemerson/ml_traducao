from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "post_bridge_closure_readonly_status_v1"
SEGMENT_STATE_RUN_ID = 410
BASELINE_RUN_ID = 409
RECENT_LEARNING_RUN_ID = 703
RECENT_CLOSED_SEGMENTS = [20762, 60278, 230843]
KNOWN_HOLD_SEGMENTS = {
    21002: "needs_more_context_knight_culture_player_plural",
    21003: "needs_more_context_knight_culture_player_plural",
    21004: "needs_more_context_knight_culture_player_plural",
    239966: "needs_more_context_knight_culture_player_plural",
    239970: "needs_more_context_knight_culture_player_plural",
    240178: "needs_more_context_knight_culture_player_plural",
}


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


def all_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def latest_ledger_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL").fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing finished issue ledger run")
    return int(row["id"])


def run_totals(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM segment_state_runs WHERE id = ?", (run_id,))
    if not row:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    keys = [
        "id",
        "total_segments",
        "closed_count",
        "pending_count",
        "output_apply_pending_count",
        "blank_valid_count",
        "experimental_watch_count",
        "reopen_count",
        "finished_at",
    ]
    return {key: row.get(key) for key in keys}


def pending_by_package(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return all_rows(
        conn,
        """
        SELECT
            CASE
                WHEN relative_path LIKE 'dlc/%' THEN 'dlc'
                WHEN relative_path LIKE 'event_localization/%' THEN 'event_localization'
                WHEN instr(relative_path, '/') > 0 THEN substr(relative_path, 1, instr(relative_path, '/') - 1)
                ELSE relative_path
            END AS package_key,
            COUNT(*) AS pending_count,
            SUM(COALESCE(needs_output_apply, 0)) AS needs_output_apply_count,
            SUM(COALESCE(needs_reopen, 0)) AS reopen_count,
            COUNT(DISTINCT relative_path) AS file_count
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
        GROUP BY package_key
        ORDER BY pending_count DESC, package_key
        LIMIT 20
        """,
        (SEGMENT_STATE_RUN_ID,),
    )


def pending_by_file(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return all_rows(
        conn,
        """
        SELECT relative_path, COUNT(*) AS pending_count,
               SUM(COALESCE(needs_output_apply, 0)) AS needs_output_apply_count,
               SUM(COALESCE(needs_reopen, 0)) AS reopen_count
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
        GROUP BY relative_path
        ORDER BY pending_count DESC, relative_path
        LIMIT 20
        """,
        (SEGMENT_STATE_RUN_ID,),
    )


def pending_issue_groups(conn: sqlite3.Connection, ledger_run_id: int) -> list[dict[str, Any]]:
    return all_rows(
        conn,
        """
        SELECT
            COALESCE(ledger.agent_key, ledger.issue_family, 'no_open_issue') AS agent_key,
            COALESCE(ledger.issue_family, 'no_open_issue') AS issue_family,
            COALESCE(ledger.issue_kind, 'no_open_issue') AS issue_kind,
            COALESCE(ledger.issue_severity, 'none') AS issue_severity,
            COALESCE(ledger.route_status, 'none') AS route_status,
            COUNT(DISTINCT state.segment_id) AS pending_count,
            COUNT(*) AS issue_row_count
        FROM segment_state_items state
        LEFT JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = ?
         AND ledger.segment_id = state.segment_id
         AND ledger.status = 'open'
        WHERE state.run_id = ?
          AND state.state_group = 'pending'
        GROUP BY agent_key, issue_family, issue_kind, issue_severity, route_status
        ORDER BY pending_count DESC, issue_row_count DESC
        LIMIT 30
        """,
        (ledger_run_id, SEGMENT_STATE_RUN_ID),
    )


def pending_state_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return all_rows(
        conn,
        """
        SELECT final_state, review_state, apply_state, COUNT(*) AS count
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
        GROUP BY final_state, review_state, apply_state
        ORDER BY count DESC
        LIMIT 20
        """,
        (SEGMENT_STATE_RUN_ID,),
    )


def recent_learning_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = all_rows(
        conn,
        """
        SELECT segment_id, match_type, source_language, learned_at, confirmation_synced_at
        FROM local_learning_candidates
        WHERE run_id = ?
        ORDER BY segment_id
        """,
        (RECENT_LEARNING_RUN_ID,),
    )
    return {
        "run_id": RECENT_LEARNING_RUN_ID,
        "row_count": len(rows),
        "learned_count": sum(1 for row in rows if row.get("learned_at")),
        "confirmation_synced_count": sum(1 for row in rows if row.get("confirmation_synced_at")),
        "match_type_counts": dict(Counter(str(row.get("match_type") or "") for row in rows)),
        "rows": rows,
    }


def segment_states(conn: sqlite3.Connection, segment_ids: list[int]) -> list[dict[str, Any]]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    return all_rows(
        conn,
        f"""
        SELECT segment_id, relative_path, source_key, final_state, state_group, review_state,
               apply_state, needs_human, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id
        """,
        (SEGMENT_STATE_RUN_ID, *segment_ids),
    )


def close_delta(conn: sqlite3.Connection) -> dict[str, Any]:
    before = run_totals(conn, BASELINE_RUN_ID)
    after = run_totals(conn, SEGMENT_STATE_RUN_ID)
    return {
        "baseline_run_id": BASELINE_RUN_ID,
        "current_run_id": SEGMENT_STATE_RUN_ID,
        "closed_delta": int(after["closed_count"] or 0) - int(before["closed_count"] or 0),
        "pending_delta": int(after["pending_count"] or 0) - int(before["pending_count"] or 0),
        "reopen_delta": int(after["reopen_count"] or 0) - int(before["reopen_count"] or 0),
        "output_apply_pending_delta": int(after["output_apply_pending_count"] or 0)
        - int(before["output_apply_pending_count"] or 0),
    }


def recommend(summary: dict[str, Any]) -> dict[str, Any]:
    issue_groups = summary["pending_issue_groups"]
    package_groups = summary["pending_by_package"]
    short_label = [
        row for row in issue_groups
        if "short_label" in row["agent_key"] or "short_label" in row["issue_family"]
    ]
    semantic = [
        row for row in issue_groups
        if "semantic" in row["agent_key"] or "semantic" in row["issue_family"]
    ]
    accolade_related = [
        row for row in summary["pending_by_file"]
        if str(row["relative_path"]).startswith("accolades/")
    ]
    if short_label and semantic:
        focus = "short_label_semantic_overlap_readonly_triage"
        rationale = "continua sendo o maior padrão recorrente com histórico recente de fechamento seguro por revisão/bridge."
    elif accolade_related:
        focus = "accolades_pending_surface_readonly_triage"
        rationale = "há pendência concentrada em accolades e o ciclo atual já está nessa vizinhança."
    else:
        focus = f"{package_groups[0]['package_key']}_readonly_triage" if package_groups else "no_pending_focus"
        rationale = "maior pacote pendente atual por contagem simples."
    return {
        "recommended_focus": focus,
        "rationale": rationale,
        "next_step": "gerar somente uma triagem/amostra read-only do grupo recomendado; nao gerar candidatos nem apply no mesmo ciclo",
        "production_full_recommended_now": False,
        "reindex_recommended_now": False,
        "segment_state_recommended_now": False,
    }


def build_summary() -> dict[str, Any]:
    with connect_readonly() as conn:
        ledger_run_id = latest_ledger_run_id(conn)
        summary = {
            "schema_version": 1,
            "source": RULE_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "segment_state_run_id": SEGMENT_STATE_RUN_ID,
            "latest_ledger_run_id": ledger_run_id,
            "current_totals": run_totals(conn, SEGMENT_STATE_RUN_ID),
            "delta_from_baseline": close_delta(conn),
            "pending_state_groups": pending_state_groups(conn),
            "pending_by_package": pending_by_package(conn),
            "pending_by_file": pending_by_file(conn),
            "pending_issue_groups": pending_issue_groups(conn, ledger_run_id),
            "recent_learning": recent_learning_summary(conn),
            "recent_closed_segments": segment_states(conn, RECENT_CLOSED_SEGMENTS),
            "known_hold_segments": segment_states(conn, sorted(KNOWN_HOLD_SEGMENTS)),
            "known_hold_reasons": KNOWN_HOLD_SEGMENTS,
            "read_only": True,
            "apply_executed": False,
            "candidate_generation_executed": False,
            "retarget_executed": False,
            "reindex_executed": False,
            "production_full_executed": False,
        }
    summary["recommendation"] = recommend(summary)
    return summary


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_post_bridge_closure_readonly_status"
    txt_path = base.with_suffix(".txt")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post bridge closure read-only status",
        f"source={RULE_VERSION}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"latest_ledger_run_id={summary['latest_ledger_run_id']}",
        "",
        "current_totals:",
        *[f"- {key}: {value}" for key, value in summary["current_totals"].items()],
        "",
        "delta_from_baseline:",
        *[f"- {key}: {value}" for key, value in summary["delta_from_baseline"].items()],
        "",
        "recent_learning:",
        f"- run_id={summary['recent_learning']['run_id']}",
        f"- row_count={summary['recent_learning']['row_count']}",
        f"- learned_count={summary['recent_learning']['learned_count']}",
        f"- confirmation_synced_count={summary['recent_learning']['confirmation_synced_count']}",
        f"- match_type_counts={json.dumps(summary['recent_learning']['match_type_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "known_holds:",
        *[
            f"- {row['segment_id']}: {summary['known_hold_reasons'].get(str(row['segment_id']), summary['known_hold_reasons'].get(row['segment_id']))} | {row['state_group']} / {row['final_state']}"
            for row in summary["known_hold_segments"]
        ],
        "",
        "top pending packages:",
        *[
            f"- {row['package_key']}: pending={row['pending_count']} files={row['file_count']} needs_output_apply={row['needs_output_apply_count']}"
            for row in summary["pending_by_package"][:15]
        ],
        "",
        "top pending files:",
        *[
            f"- {row['relative_path']}: pending={row['pending_count']} needs_output_apply={row['needs_output_apply_count']}"
            for row in summary["pending_by_file"][:15]
        ],
        "",
        "top pending issue groups:",
        *[
            f"- {row['agent_key']} / {row['issue_family']}:{row['issue_kind']} / {row['route_status']}: pending={row['pending_count']}"
            for row in summary["pending_issue_groups"][:15]
        ],
        "",
        "recommendation:",
        f"- recommended_focus={summary['recommendation']['recommended_focus']}",
        f"- rationale={summary['recommendation']['rationale']}",
        f"- next_step={summary['recommendation']['next_step']}",
        f"- reindex_recommended_now={str(summary['recommendation']['reindex_recommended_now']).lower()}",
        f"- production_full_recommended_now={str(summary['recommendation']['production_full_recommended_now']).lower()}",
        "",
        "execution_flags:",
        "- read_only=true",
        "- apply_executed=false",
        "- candidate_generation_executed=false",
        "- retarget_executed=false",
        "- reindex_executed=false",
        "- production_full_executed=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    summary = build_summary()
    txt_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print(f"pending_count={summary['current_totals']['pending_count']}")
    print(f"needs_output_apply={summary['current_totals']['output_apply_pending_count']}")
    print(f"recent_learning_rows={summary['recent_learning']['row_count']}")
    print(f"known_hold_count={len(summary['known_hold_segments'])}")
    print(f"recommended_focus={summary['recommendation']['recommended_focus']}")
    print(f"next_step={summary['recommendation']['next_step']}")
    print(f"production_full_recommended_now={summary['recommendation']['production_full_recommended_now']}")


if __name__ == "__main__":
    main()
