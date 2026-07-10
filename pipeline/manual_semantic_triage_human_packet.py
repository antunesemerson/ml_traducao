from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "manual_semantic_triage_human_packet_v2_after_run485"
ROUTER_JSONL = Path("reports/20260628_175545_131929_semantic_review_router_pending_deep_diagnostic.jsonl")
ROUTER_SUMMARY = Path("reports/20260628_175545_131929_semantic_review_router_pending_deep_diagnostic_summary.json")
SEGMENT_STATE_RUN_ID = 485
LIMIT = 30
KNOWN_HOLD_OR_BLOCKED_SEGMENT_IDS = {112295, 162872, 120831, 100757, 62620}

SELECT_CSTRING_RE = re.compile(r"Select_CString\(")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
EFFECT_LIST_RE = re.compile(r"\$EFFECT_LIST_BULLET\$")
DENSE_GETTER_RE = re.compile(r"\b(?:GetTrait|GetActivityType|GetCourtPositionType|GetMaA)\b")


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


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_router_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with ROUTER_JSONL.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get("policy_lane") == "manual_semantic_triage":
                    rows.append(row)
    return rows


def known_learning_segment_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT segment_id
        FROM local_learning_candidates
        WHERE local_status IN ('high_confidence', 'blocked', 'hold', 'rejected')
           OR human_label IN ('correct', 'semantic_error', 'needs_more_context')
           OR corrected_text IS NOT NULL
        """
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def full_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            state.final_state,
            state.state_group,
            state.needs_output_apply,
            state.confirmed_matches_output
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id = ?
        """,
        (SEGMENT_STATE_RUN_ID, segment_id),
    ).fetchone()
    return dict(row) if row else None


def text_blob(row: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    source = context or row
    return "\n".join(
        str(source.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "relative_path", "source_key")
    )


def exclusion_reasons(row: dict[str, Any], context: dict[str, Any] | None, known_ids: set[int]) -> list[str]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    blob = text_blob(row, context)
    if context is None:
        reasons.append("missing_context")
        return reasons
    if segment_id in known_ids:
        reasons.append("already_learned_or_human_reviewed")
    if segment_id in KNOWN_HOLD_OR_BLOCKED_SEGMENT_IDS:
        reasons.append("known_hold_or_blocked")
    if context.get("state_group") != "pending" or context.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("not_pending_expected_state")
    if int(context.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if int(context.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_output_mismatch")
    if row.get("risk_bucket") not in {"low_plain_text", "medium_dynamic_light"}:
        reasons.append(f"risk_not_low_enough:{row.get('risk_bucket')}")
    if SELECT_CSTRING_RE.search(blob):
        reasons.append("select_cstring")
    if ES_HELPER_RE.search(blob):
        reasons.append("es_helper")
    if EFFECT_LIST_RE.search(blob):
        reasons.append("effect_list_bullet")
    if DENSE_GETTER_RE.search(blob):
        reasons.append("dense_getter")
    return reasons


def priority(row: dict[str, Any]) -> tuple[int, int, int, int]:
    risk_rank = {"low_plain_text": 0, "medium_dynamic_light": 1}.get(str(row.get("risk_bucket")), 9)
    surface_rank = {"name_nickname_dynasty": 0, "general_semantic_prose": 1, "accolade_knight_glory": 2, "activity_contract_event": 3}.get(
        str(row.get("surface_bucket")), 9
    )
    return (
        risk_rank,
        int(row.get("token_count") or 0),
        surface_rank,
        int(row.get("text_length") or 0),
        int(row["segment_id"]),
    )


def make_packet(router_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    with connect_readonly() as conn:
        known_ids = known_learning_segment_ids(conn)
        eligible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in router_rows:
            context = full_context(conn, int(row["segment_id"]))
            reasons = exclusion_reasons(row, context, known_ids)
            record = {**row, **(context or {})}
            record["exclusion_reasons"] = reasons
            if reasons:
                excluded.append(record)
            else:
                output = str(record.get("current_output_text") or "")
                record["protected_tokens"] = protected_tokens(output)
                record["inclusion_reason"] = (
                    f"manual_semantic_triage + {record.get('risk_bucket')}; "
                    "read-only human learning packet; reviewed/closed/known learning excluded"
                )
                eligible.append(record)
    eligible.sort(key=priority)
    packet = []
    for index, row in enumerate(eligible[:LIMIT], start=1):
        packet.append(
            {
                **row,
                "packet_index": index,
                "human_decision": "",
                "corrected_text": "",
                "allowed_decisions": [
                    "approve_already_ok",
                    "approve_correction",
                    "reject",
                    "needs_more_context",
                    "hold_structural_or_domain_risk",
                ],
            }
        )
    return packet, excluded, len(eligible)


def fenced(value: str) -> str:
    return "```text\n" + str(value or "") + "\n```"


def write_markdown(path: Path, packet_rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# Manual Semantic Triage Human Packet",
        "",
        f"- source: `{SOURCE}`",
        f"- segment_state_run_id: `{summary['segment_state_run_id']}`",
        f"- packet_count: `{summary['packet_count']}`",
        "- allowed decisions: `approve_already_ok`, `approve_correction`, `reject`, `needs_more_context`, `hold_structural_or_domain_risk`",
        "",
        "No apply/lifecycle/segment-state/reindex/full production was run.",
        "",
    ]
    for row in packet_rows:
        lines.extend(
            [
                f"## {row['packet_index']}. segment_id={row['segment_id']}",
                "",
                f"- relative_path: `{row['relative_path']}`",
                f"- source_key: `{row['source_key']}`",
                f"- source_line_number: `{row['source_line_number']}`",
                f"- surface_bucket: `{row['surface_bucket']}`",
                f"- risk_bucket: `{row['risk_bucket']}`",
                f"- token_count: `{row['token_count']}`",
                f"- inclusion_reason: {row['inclusion_reason']}",
                f"- protected_tokens: `{row['protected_tokens']}`",
                "",
                "**English**",
                fenced(row.get("english_text", "")),
                "**Spanish**",
                fenced(row.get("spanish_text", "")),
                "**Current output**",
                fenced(row.get("current_output_text", "")),
                "**Human decision:** ",
                "",
                "**Corrected text, if any:**",
                "",
                "---",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def top_counter(values: list[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in Counter(values).most_common()]


def main() -> None:
    router_summary = load_json(ROUTER_SUMMARY)
    router_rows = load_router_rows()
    packet_rows, excluded, eligible_count = make_packet(router_rows)
    base = reports_dir() / f"{stamp()}_manual_semantic_triage_human_packet"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    txt_path = base.with_suffix(".txt")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "router_summary": str(ROUTER_SUMMARY),
        "router_jsonl": str(ROUTER_JSONL),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": router_summary["ledger_run_id"],
        "manual_semantic_triage_count": router_summary["recommended_lane_count"],
        "packet_count": len(packet_rows),
        "eligible_count": eligible_count,
        "excluded_count": len(excluded),
        "packet_segment_ids": [row["segment_id"] for row in packet_rows],
        "packet_risk_bucket_counts": top_counter([row["risk_bucket"] for row in packet_rows]),
        "packet_surface_bucket_counts": top_counter([row["surface_bucket"] for row in packet_rows]),
        "apply_ready_now": False,
        "lifecycle_ready_now": False,
        "production_full_recommended_now": False,
        "ran_apply": False,
        "ran_lifecycle": False,
        "ran_segment_state": False,
        "ran_reindex": False,
        "ran_production_full": False,
        "source_changed": False,
        "output_changed": False,
        "next_action": "human_review_packet_then_controlled_decision_ingest",
    }
    write_markdown(md_path, packet_rows, summary)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in packet_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "manual semantic triage human packet",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"packet_count={summary['packet_count']}",
        f"excluded_count={summary['excluded_count']}",
        "packet_risk_bucket_counts:",
        *[f"- {item['count']} | {item['key']}" for item in summary["packet_risk_bucket_counts"]],
        "packet_surface_bucket_counts:",
        *[f"- {item['count']} | {item['key']}" for item in summary["packet_surface_bucket_counts"]],
        "apply_ready_now=false",
        "lifecycle_ready_now=false",
        "production_full_recommended_now=false",
        "ran_apply=false",
        "ran_lifecycle=false",
        "ran_segment_state=false",
        "ran_reindex=false",
        "ran_production_full=false",
        "source_changed=false",
        "output_changed=false",
        f"next_action={summary['next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"md={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"txt={txt_path}")
    print(f"packet_count={summary['packet_count']}")
    print(f"excluded_count={summary['excluded_count']}")
    print("packet_segment_ids=" + ",".join(str(row["segment_id"]) for row in packet_rows))
    print("apply_ready_now=false")
    print("lifecycle_ready_now=false")
    print("production_full_recommended_now=false")
    print("ran_apply=false")
    print("ran_lifecycle=false")
    print("ran_segment_state=false")
    print("ran_reindex=false")
    print("ran_production_full=false")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
