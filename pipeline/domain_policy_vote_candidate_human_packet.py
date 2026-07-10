from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as deep_diagnostic
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_human_packet_v7_low_plain_domain_after_run492"
DEEP_DIAGNOSTIC_JSONL = Path("reports/20260628_222606_156978_domain_policy_vote_candidate_deep_diagnostic.jsonl")
DEEP_DIAGNOSTIC_SUMMARY = Path("reports/20260628_222606_156978_domain_policy_vote_candidate_deep_diagnostic_summary.json")
LIMIT = 60
KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS = {10476, 10532, 10540, 39106}

EXCLUDED_RISKS = {
    "high_structural_token_density",
    "high_multiline_effect_list",
    "high_spanish_residue_context",
    "high_select_cstring_or_es_helper",
}

DENSE_GETTER_RE = re.compile(r"\b(?:GetTrait|GetActivityType|GetCourtPositionType|GetMaA)\b")
SELECT_CSTRING_RE = re.compile(r"Select_CString\(")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)")
EFFECT_LIST_RE = re.compile(r"\$EFFECT_LIST_BULLET\$")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet"
    return (
        base.with_suffix(".md"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
        base.with_suffix(".txt"),
    )


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


def load_deep_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with DEEP_DIAGNOSTIC_JSONL.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_full_deep_rows() -> list[dict[str, Any]]:
    preflight_path, excluded_segment_ids = deep_diagnostic.load_preflight_exclusions()
    with deep_diagnostic.connect_readonly() as conn:
        segment_state_run_id = int(read_json(DEEP_DIAGNOSTIC_SUMMARY)["segment_state_run_id"])
        ledger_run_id = int(read_json(DEEP_DIAGNOSTIC_SUMMARY)["ledger_run_id"])
        rows = deep_diagnostic.fetch_rows(conn, segment_state_run_id, ledger_run_id, excluded_segment_ids)
    return [deep_diagnostic.enrich_row(row) for row in rows]


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "relative_path", "source_key")
    )


def known_learned_or_hold_segment_ids(conn: sqlite3.Connection) -> set[int]:
    ids: set[int] = set()
    for row in conn.execute(
        """
        SELECT DISTINCT segment_id
        FROM local_learning_candidates
        WHERE local_status IN ('high_confidence', 'blocked', 'hold', 'rejected')
           OR human_label IN ('correct', 'semantic_error', 'needs_more_context')
           OR corrected_text IS NOT NULL
        """
    ):
        ids.add(int(row["segment_id"]))
    return ids


def high_severity_open_issue_segment_ids(conn: sqlite3.Connection, segment_ids: list[int]) -> set[int]:
    if not segment_ids:
        return set()
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT segment_id
        FROM ml_issue_ledger_items
        WHERE segment_id IN ({placeholders})
          AND status = 'open'
          AND issue_severity IN ('high', 'critical')
        """,
        tuple(segment_ids),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def exclusion_reasons(row: dict[str, Any], learned_or_hold: set[int], high_severity_ids: set[int]) -> list[str]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    blob = text_blob(row)
    if segment_id in learned_or_hold:
        reasons.append("already_learned_or_known_hold")
    if segment_id in KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS:
        reasons.append("known_structural_blocked")
    if segment_id in high_severity_ids:
        reasons.append("open_high_severity_issue")
    if row.get("risk_bucket") in EXCLUDED_RISKS:
        reasons.append(f"excluded_risk:{row.get('risk_bucket')}")
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
    surface = str(row.get("surface_bucket") or "")
    risk = str(row.get("risk_bucket") or "")
    priority_group = {
        ("religion_faith_doctrine", "low_plain_domain"): 0,
        ("title_realm_governance", "low_plain_domain"): 1,
        ("culture_tradition_innovation", "low_plain_domain"): 2,
        ("culture_parameter_modifier", "low_plain_domain"): 3,
    }.get((surface, risk), 99)
    return (
        priority_group,
        int(row.get("token_count") or 0),
        int(row.get("text_length") or len(str(row.get("current_output_text") or ""))),
        int(row["segment_id"]),
    )


def eligible_rows(rows: list[dict[str, Any]], learned_or_hold: set[int], high_severity_ids: set[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    allowed_pairs = {
        ("religion_faith_doctrine", "low_plain_domain"),
        ("culture_tradition_innovation", "low_plain_domain"),
        ("culture_parameter_modifier", "low_plain_domain"),
        ("title_realm_governance", "low_plain_domain"),
    }
    for row in rows:
        reasons = exclusion_reasons(row, learned_or_hold, high_severity_ids)
        if (row.get("surface_bucket"), row.get("risk_bucket")) not in allowed_pairs:
            reasons.append("outside_allowed_surface_risk")
        record = dict(row)
        record["exclusion_reasons"] = reasons
        if reasons:
            excluded.append(record)
        else:
            record["inclusion_reason"] = f"{row.get('surface_bucket')} + {row.get('risk_bucket')}; low structural risk; read-only human learning packet"
            eligible.append(record)
    return sorted(eligible, key=priority), excluded


def make_packet_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    packet = []
    for idx, row in enumerate(rows[:LIMIT], start=1):
        output = str(row.get("current_output_text") or "")
        packet.append(
            {
                **row,
                "packet_index": idx,
                "protected_tokens": protected_tokens(output),
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
    return packet


def fenced(value: str) -> str:
    return "```text\n" + str(value or "") + "\n```"


def write_markdown(path: Path, packet_rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# Domain Policy Vote Candidate Human Packet",
        "",
        f"- source: `{SOURCE}`",
        f"- segment_state_run_id: `{summary['segment_state_run_id']}`",
        f"- ledger_run_id: `{summary['ledger_run_id']}`",
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


def write_outputs(packet_rows: list[dict[str, Any]], excluded: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    md_path, jsonl_path, summary_path, txt_path = output_paths()
    write_markdown(md_path, packet_rows, summary)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in packet_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain policy vote candidate human packet",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"packet_count={summary['packet_count']}",
        f"eligible_count={summary['eligible_count']}",
        f"excluded_count={summary['excluded_count']}",
        "surface_bucket_counts:",
        *[f"- {item['count']} | {item['key']}" for item in summary["packet_surface_bucket_counts"]],
        "risk_bucket_counts:",
        *[f"- {item['count']} | {item['key']}" for item in summary["packet_risk_bucket_counts"]],
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
    return md_path, jsonl_path, summary_path, txt_path


def top_counter(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def main() -> None:
    deep_summary = read_json(DEEP_DIAGNOSTIC_SUMMARY)
    deep_rows = load_full_deep_rows()
    segment_ids = [int(row["segment_id"]) for row in deep_rows]
    with connect_readonly() as conn:
        learned_or_hold = known_learned_or_hold_segment_ids(conn)
        high_severity_ids = high_severity_open_issue_segment_ids(conn, segment_ids)
    eligible, excluded = eligible_rows(deep_rows, learned_or_hold, high_severity_ids)
    packet_rows = make_packet_rows(eligible)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "deep_diagnostic_summary": str(DEEP_DIAGNOSTIC_SUMMARY),
        "deep_diagnostic_jsonl": str(DEEP_DIAGNOSTIC_JSONL),
        "segment_state_run_id": deep_summary["segment_state_run_id"],
        "ledger_run_id": deep_summary["ledger_run_id"],
        "packet_count": len(packet_rows),
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "known_structural_blocked_segment_ids": sorted(KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS),
        "packet_segment_ids": [row["segment_id"] for row in packet_rows],
        "packet_surface_bucket_counts": top_counter(Counter(row["surface_bucket"] for row in packet_rows)),
        "packet_risk_bucket_counts": top_counter(Counter(row["risk_bucket"] for row in packet_rows)),
        "eligible_surface_bucket_counts": top_counter(Counter(row["surface_bucket"] for row in eligible)),
        "eligible_risk_bucket_counts": top_counter(Counter(row["risk_bucket"] for row in eligible)),
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
    md_path, jsonl_path, summary_path, txt_path = write_outputs(packet_rows, excluded, summary)
    print(f"md={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"txt={txt_path}")
    print(f"packet_count={summary['packet_count']}")
    print(f"eligible_count={summary['eligible_count']}")
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
