from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "dynamic_gender_high_risk_sublane_diagnostic_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
COHORT_KEY = "dynamic_ck3_expression_microagent_plus_gender_token_microagent"
PRIMARY_FAMILIES = {"dynamic_ck3_expression_microagent", "gender_token_microagent"}

SELECT_CSTRING_RE = re.compile(r"Select_CString\(", re.IGNORECASE)
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
LOCAL_PLAYER_RE = re.compile(r"\b(?:IsLocalPlayer|GetPlayer|LocalPlayer)\b")
SCOPE_GETTER_RE = re.compile(r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+")
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
SPANISH_RESIDUE_RE = re.compile(
    r"[\u00bf\u00a1]|\b(?:muchos|muchas|poco|poca|buenos|buenas|acuerdo|ganas|gana|han|hab[eé]is|los|las|del|por debajo)\b",
    re.IGNORECASE,
)
SHORT_LABEL_RE = re.compile(r"^[^\n]{0,120}$")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_database_path(db.load_settings())}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_rows(conn: sqlite3.Connection, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH fam AS (
            SELECT
                l.segment_id,
                GROUP_CONCAT(DISTINCT l.issue_family) AS all_families,
                GROUP_CONCAT(DISTINCT l.issue_kind) AS all_issue_kinds,
                GROUP_CONCAT(DISTINCT l.agent_key) AS all_agents,
                COUNT(*) AS open_issue_count
            FROM ml_issue_ledger_items l
            JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
            WHERE l.run_id = ?
              AND l.status = 'open'
              AND s.state_group = 'pending'
              AND s.is_closed = 0
              AND s.needs_output_apply = 0
              AND s.confirmed_matches_output = 1
            GROUP BY l.segment_id
            HAVING instr(all_families, 'dynamic_ck3_expression_microagent') > 0
               AND instr(all_families, 'gender_token_microagent') > 0
        )
        SELECT
            f.segment_id,
            f.all_families,
            f.all_issue_kinds,
            f.all_agents,
            f.open_issue_count,
            src.relative_path,
            src.source_key,
            src.english_text,
            src.spanish_text,
            src.old_text,
            out.portuguese_text AS current_output_text
        FROM fam f
        JOIN source_segments src ON src.id = f.segment_id
        LEFT JOIN output_segments out ON out.segment_id = f.segment_id
        """,
        (state_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def domain_bucket(relative_path: str) -> str:
    if relative_path.startswith("event_localization/"):
        return "events"
    if relative_path.startswith("gui/") or "window" in relative_path:
        return "ui"
    if relative_path.startswith("dlc/"):
        return "dlc"
    if relative_path.startswith("culture/"):
        return "culture"
    if relative_path.startswith("religion/"):
        return "religion"
    return "core"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("current_output_text") or "")
    families = {part for part in str(row.get("all_families") or "").split(",") if part}
    issue_kinds = {part for part in str(row.get("all_issue_kinds") or "").split(",") if part}
    structural_families = sorted(families - PRIMARY_FAMILIES)
    token_count = len(BRACKET_RE.findall(text)) + len(VARIABLE_RE.findall(text))
    select_cstring = bool(SELECT_CSTRING_RE.search(text))
    es_helper = bool(ES_HELPER_RE.search(text))
    local_player = bool(LOCAL_PLAYER_RE.search(text))
    scope_getters = len(SCOPE_GETTER_RE.findall(text))
    spanish_residue = bool(SPANISH_RESIDUE_RE.search(text))
    short_label = bool(SHORT_LABEL_RE.match(text))
    dom = domain_bucket(str(row.get("relative_path") or ""))

    if structural_families:
        sublane = "blocked_structural_overlap"
        risk = "high"
        recommendation = "hold_structural_policy"
    elif select_cstring and local_player and short_label and token_count <= 4:
        sublane = "local_player_select_cstring_short_label"
        risk = "medium"
        recommendation = "build_read_only_micro_audit"
    elif select_cstring and token_count <= 5 and spanish_residue:
        sublane = "select_cstring_spanish_residue_candidate"
        risk = "medium"
        recommendation = "build_read_only_micro_audit"
    elif es_helper:
        sublane = "es_helper_gender_surface"
        risk = "high"
        recommendation = "hold_requires_es_helper_policy"
    elif select_cstring:
        sublane = "select_cstring_context_required"
        risk = "high"
        recommendation = "hold_context_required"
    elif spanish_residue and short_label and token_count <= 3:
        sublane = "short_label_spanish_residue_no_select"
        risk = "medium"
        recommendation = "build_read_only_micro_audit"
    else:
        sublane = "dynamic_gender_context_required"
        risk = "high"
        recommendation = "hold_context_required"

    return {
        "segment_id": int(row["segment_id"]),
        "cohort_key": COHORT_KEY,
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "domain_bucket": dom,
        "sublane": sublane,
        "risk": risk,
        "recommendation": recommendation,
        "all_families": sorted(families),
        "structural_families": structural_families,
        "issue_kinds": sorted(issue_kinds),
        "token_count": token_count,
        "scope_getter_count": scope_getters,
        "select_cstring": select_cstring,
        "es_helper": es_helper,
        "local_player": local_player,
        "spanish_residue_hint": spanish_residue,
        "short_label_surface": short_label,
        "text_preview": text[:240],
    }


def build_summary(records: list[dict[str, Any]], sample_limit: int) -> dict[str, Any]:
    sublanes = Counter(row["sublane"] for row in records)
    risk_counts = Counter(row["risk"] for row in records)
    recommendation_counts = Counter(row["recommendation"] for row in records)
    domain_counts = Counter(row["domain_bucket"] for row in records)
    audit_candidates = [
        row for row in records
        if row["recommendation"] == "build_read_only_micro_audit"
        and row["risk"] != "high"
    ]
    # Prefer the smallest, most constrained surface first.
    audit_candidates.sort(
        key=lambda row: (
            row["sublane"] != "short_label_spanish_residue_no_select",
            row["sublane"] != "local_player_select_cstring_short_label",
            row["token_count"],
            row["scope_getter_count"],
            row["segment_id"],
        )
    )
    recommended_sublane = audit_candidates[0]["sublane"] if audit_candidates else ""
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "cohort_key": COHORT_KEY,
        "total_rows": len(records),
        "sample_limit": sample_limit,
        "sublane_counts": dict(sorted(sublanes.items())),
        "risk_counts": dict(sorted(risk_counts.items())),
        "recommendation_counts": dict(sorted(recommendation_counts.items())),
        "domain_counts": dict(sorted(domain_counts.items())),
        "read_only_micro_audit_candidate_count": len(audit_candidates),
        "recommended_sublane": recommended_sublane,
        "recommended_sample_ids": [row["segment_id"] for row in audit_candidates[:50]],
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_reindex_recommended_now": False,
        "recommended_next_action": (
            "build_read_only_micro_audit_for_recommended_sublane"
            if audit_candidates
            else "hold_dynamic_gender_until_policy_design"
        ),
        "recommended_next_prompt": (
            "chat_exec_dynamic_gender_micro_audit_prompt.md"
            if audit_candidates
            else "chat_exec_dynamic_gender_policy_design_prompt.md"
        ),
    }
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_dynamic_gender_high_risk_sublane_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "dynamic gender high-risk sublane diagnostic",
        f"cohort_key={summary['cohort_key']}",
        f"total_rows={summary['total_rows']}",
        f"sublane_counts={json.dumps(summary['sublane_counts'], ensure_ascii=False, sort_keys=True)}",
        f"risk_counts={json.dumps(summary['risk_counts'], ensure_ascii=False, sort_keys=True)}",
        f"recommendation_counts={json.dumps(summary['recommendation_counts'], ensure_ascii=False, sort_keys=True)}",
        f"read_only_micro_audit_candidate_count={summary['read_only_micro_audit_candidate_count']}",
        f"recommended_sublane={summary['recommended_sublane']}",
        f"recommended_sample_ids={summary['recommended_sample_ids'][:20]}",
        "apply_ready_now=0",
        "production_full_recommended_now=false",
        "lifecycle_reindex_recommended_now=false",
        f"recommended_next_action={summary['recommended_next_action']}",
        f"recommended_next_prompt={summary['recommended_next_prompt']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--sample-limit", type=int, default=600)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    with connect_readonly() as conn:
        raw_rows = fetch_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    records = [classify(row) for row in raw_rows]
    records.sort(key=lambda row: (row["risk"], row["sublane"], row["token_count"], row["segment_id"]))
    if args.sample_limit > 0:
        records = records[: args.sample_limit]
    summary = build_summary(records, args.sample_limit)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_rows",
        "read_only_micro_audit_candidate_count",
        "recommended_sublane",
        "apply_ready_now",
        "recommended_next_action",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
