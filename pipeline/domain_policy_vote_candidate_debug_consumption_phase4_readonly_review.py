from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "domain_policy_vote_candidate_debug_consumption_phase4_readonly_review_v1"
DEFAULT_PACKET_JSONL = Path("reports/20260702_003028_941234_domain_policy_vote_candidate_closure_debt_architecture_packet_512_538.jsonl")
DEFAULT_RUN_ID = 538
OPEN_STATUSES = ("closed", "resolved", "dismissed")
HIGH_SEVERITIES = {"high", "error", "critical"}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only debug-consumption and phase-4 review.")
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def token_surface(text: str | None) -> str:
    tokens = protected_tokens(text or "")
    if not tokens:
        return "plain_text"
    token_blob = " ".join(tokens)
    if "\\n" in tokens:
        return "multiline"
    if "Select_CString" in token_blob or "SelectLocalization" in token_blob:
        return "dynamic_select"
    if any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "GetScriptValue", "SCOPE."]):
        return "dynamic_getter"
    return "light_token"


def dynamic_token_flags(text: str | None) -> dict[str, int]:
    tokens = protected_tokens(text or "")
    token_blob = " ".join(tokens)
    return {
        "has_select_cstring": int("Select_CString" in token_blob),
        "has_select_localization": int("SelectLocalization" in token_blob),
        "has_es_helper": int("ES_" in token_blob or "ES_OA" in token_blob or "ES_XA" in token_blob),
        "has_scope_getter": int(any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE."])),
        "has_multiline": int("\\n" in tokens or "\n" in (text or "")),
        "protected_token_count": len(tokens),
    }


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run {run_id}")
    return dict(row)


def fetch_state_rows(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(segment_ids), 800):
        chunk = segment_ids[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                item.segment_id,
                item.final_state,
                item.state_group,
                item.is_closed,
                item.confirmed_matches_output,
                item.needs_output_apply,
                item.policy_action,
                item.lifecycle_policy_allowed,
                item.lifecycle_policy_action,
                item.confirmation_level,
                item.confirmation_label,
                item.locked,
                output.portuguese_text AS output_text,
                conf.confirmed_text,
                conf.confirmation_source
            FROM segment_state_items item
            LEFT JOIN output_segments output ON output.segment_id = item.segment_id
            LEFT JOIN segment_confirmations conf ON conf.segment_id = item.segment_id
            WHERE item.run_id = ? AND item.segment_id IN ({placeholders})
            """,
            (run_id, *chunk),
        ).fetchall()
        for row in rows:
            out[int(row["segment_id"])] = dict(row)
    return out


def fetch_open_issues(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not segment_ids:
        return {}
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for start in range(0, len(segment_ids), 800):
        chunk = segment_ids[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT segment_id, issue_family, issue_kind, issue_severity, agent_key, route_status, proposed_action, status
            FROM ml_issue_ledger_items
            WHERE segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            ORDER BY segment_id, issue_family, issue_kind, issue_severity
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            out[int(row["segment_id"])].append(dict(row))
    return dict(out)


def issue_signature(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "no_open_issue"
    pairs = sorted({f"{issue.get('issue_family')}:{issue.get('issue_severity')}" for issue in issues})
    return ";".join(pairs)


def issue_families(issues: list[dict[str, Any]], high_only: bool = False) -> list[str]:
    values = set()
    for issue in issues:
        severity = str(issue.get("issue_severity") or "").lower()
        if high_only and severity not in HIGH_SEVERITIES:
            continue
        values.add(str(issue.get("issue_family") or "unknown"))
    return sorted(values)


def review_phase4_row(row: dict[str, Any], state: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    output_text = state.get("output_text") if state else row.get("output_text")
    confirmed_text = state.get("confirmed_text") if state else row.get("confirmed_text")
    canonical_equal = canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
    flags = dynamic_token_flags(confirmed_text or output_text)
    open_issue_count = len(issues)
    high_issue_count = len([i for i in issues if str(i.get("issue_severity") or "").lower() in HIGH_SEVERITIES])
    surface = token_surface(confirmed_text or output_text)
    guard_failures = []
    if surface not in {"plain_text", "light_token"}:
        guard_failures.append("not_plain_or_light_token")
    if not canonical_equal:
        guard_failures.append("canonical_output_confirmed_mismatch")
    if int((state or {}).get("needs_output_apply") or row.get("needs_output_apply") or 0) != 0:
        guard_failures.append("needs_output_apply")
    if open_issue_count != 0:
        guard_failures.append("open_issue")
    if high_issue_count != 0:
        guard_failures.append("high_issue")
    if flags["has_select_cstring"] or flags["has_select_localization"] or flags["has_scope_getter"] or flags["has_multiline"]:
        guard_failures.append("dynamic_or_multiline_escaped")
    if str((state or {}).get("confirmation_level") or row.get("confirmation_level") or "") != "auto_confirmed":
        guard_failures.append("not_auto_confirmed")
    return {
        "source": SOURCE,
        "record_type": "phase4_candidate_review",
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "final_state": (state or {}).get("final_state") or row.get("to_final_state"),
        "token_surface": surface,
        "canonical_l10n_output_equals_confirmed": int(canonical_equal),
        "confirmed_matches_output": int((state or {}).get("confirmed_matches_output") or row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int((state or {}).get("needs_output_apply") or row.get("needs_output_apply") or 0),
        "open_issue_count": open_issue_count,
        "high_issue_count": high_issue_count,
        "open_issue_families": issue_families(issues),
        "high_issue_families": issue_families(issues, high_only=True),
        "confirmation_level": (state or {}).get("confirmation_level") or row.get("confirmation_level"),
        "confirmation_source": (state or {}).get("confirmation_source") or row.get("confirmation_source"),
        "confirmation_label": (state or {}).get("confirmation_label") or row.get("confirmation_label"),
        "locked": int((state or {}).get("locked") or row.get("locked") or 0),
        "is_human_confirmation": int(
            str((state or {}).get("confirmation_level") or row.get("confirmation_level") or "") == "human_confirmed"
        ),
        "is_auto_confirmation": int(
            str((state or {}).get("confirmation_level") or row.get("confirmation_level") or "") == "auto_confirmed"
        ),
        **flags,
        "guard_ok": len(guard_failures) == 0,
        "guard_failures": guard_failures,
        "recommended_action": "phase4_bridge_dry_run_eligible" if not guard_failures else "phase4_hold_or_human_packet",
        "output_text": output_text,
        "confirmed_text": confirmed_text,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    packet_rows = read_jsonl(args.packet_jsonl)
    debug_rows = [row for row in packet_rows if row.get("phase") == "debug_existing_policy_consumption"]
    phase4_rows = [row for row in packet_rows if row.get("phase") == "phase_4_auto_confirmed_plain_or_light_bridge"]
    segment_ids = [int(row["segment_id"]) for row in debug_rows + phase4_rows]

    with connect_readonly() as conn:
        run = fetch_run(conn, args.run_id)
        states = fetch_state_rows(conn, args.run_id, segment_ids)
        issues_by_segment = fetch_open_issues(conn, segment_ids)

    records: list[dict[str, Any]] = []
    for row in debug_rows:
        segment_id = int(row["segment_id"])
        issues = issues_by_segment.get(segment_id, [])
        high_families = issue_families(issues, high_only=True)
        all_families = issue_families(issues)
        records.append(
            {
                "source": SOURCE,
                "record_type": "debug_issue_classification",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "final_state": (states.get(segment_id) or {}).get("final_state") or row.get("to_final_state"),
                "lifecycle_policy_allowed": int((states.get(segment_id) or {}).get("lifecycle_policy_allowed") or row.get("lifecycle_policy_allowed") or 0),
                "lifecycle_policy_action": (states.get(segment_id) or {}).get("lifecycle_policy_action") or row.get("lifecycle_policy_action"),
                "open_issue_count": len(issues),
                "high_issue_count": len([i for i in issues if str(i.get("issue_severity") or "").lower() in HIGH_SEVERITIES]),
                "open_issue_families": all_families,
                "high_issue_families": high_families,
                "issue_signature": issue_signature(issues),
                "token_surface": row.get("token_surface"),
                "classification": "blocked_by_open_or_high_issue" if issues else "no_open_issue_debug_consumption",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    for row in phase4_rows:
        segment_id = int(row["segment_id"])
        records.append(review_phase4_row(row, states.get(segment_id, {}), issues_by_segment.get(segment_id, [])))

    debug_records = [record for record in records if record["record_type"] == "debug_issue_classification"]
    phase4_records = [record for record in records if record["record_type"] == "phase4_candidate_review"]
    debug_open_family_counts = Counter()
    debug_high_family_counts = Counter()
    debug_signature_counts = Counter()
    for record in debug_records:
        debug_signature_counts[str(record["issue_signature"])] += 1
        for family in record["open_issue_families"]:
            debug_open_family_counts[family] += 1
        for family in record["high_issue_families"]:
            debug_high_family_counts[family] += 1

    phase4_surface_counts = Counter(str(record["token_surface"]) for record in phase4_records)
    phase4_guard_counts = Counter("guard_ok" if record["guard_ok"] else "guard_blocked" for record in phase4_records)
    phase4_label_counts = Counter(str(record["confirmation_label"]) for record in phase4_records)
    phase4_source_counts = Counter(str(record["confirmation_source"]) for record in phase4_records)
    phase4_failure_counts = Counter()
    for record in phase4_records:
        for failure in record["guard_failures"]:
            phase4_failure_counts[failure] += 1

    phase4_guard_ok = phase4_guard_counts.get("guard_ok", 0)
    if phase4_guard_ok == len(phase4_records) and len(phase4_records) > 0:
        phase4_recommendation = (
            "Phase 4 can proceed to a separate narrow bridge dry-run: all 116 rows are auto-confirmed plain/light-token, canonical equal, no output apply, and no open/high issue. "
            "Do not materialize until that dry-run reports 116 released and 0 blocked."
        )
    elif phase4_guard_ok > 0:
        phase4_recommendation = (
            "Phase 4 should split: send guard_ok rows to a narrow bridge dry-run and move blocked rows to human packet or hold by failure reason."
        )
    else:
        phase4_recommendation = "Phase 4 should not become a bridge now; use a human packet or hold."

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_debug_consumption_and_phase4_review",
        "run_id": args.run_id,
        "current_run": {
            "closed_count": int(run.get("closed_count") or 0),
            "pending_count": int(run.get("pending_count") or 0),
            "reopen_count": int(run.get("reopen_count") or 0),
            "output_apply_pending_count": int(run.get("output_apply_pending_count") or 0),
        },
        "debug_existing_policy_consumption": {
            "record_count": len(debug_records),
            "no_open_issue_count": sum(1 for record in debug_records if record["open_issue_count"] == 0),
            "with_open_issue_count": sum(1 for record in debug_records if record["open_issue_count"] > 0),
            "with_high_issue_count": sum(1 for record in debug_records if record["high_issue_count"] > 0),
            "open_issue_family_counts": dict(debug_open_family_counts.most_common()),
            "high_issue_family_counts": dict(debug_high_family_counts.most_common()),
            "issue_signature_counts": dict(debug_signature_counts.most_common(40)),
        },
        "phase_4_auto_confirmed_plain_or_light": {
            "record_count": len(phase4_records),
            "guard_ok_count": phase4_guard_ok,
            "guard_blocked_count": phase4_guard_counts.get("guard_blocked", 0),
            "token_surface_counts": dict(phase4_surface_counts.most_common()),
            "confirmation_label_counts": dict(phase4_label_counts.most_common()),
            "confirmation_source_counts": dict(phase4_source_counts.most_common()),
            "guard_failure_counts": dict(phase4_failure_counts.most_common()),
            "canonical_equal_count": sum(1 for record in phase4_records if record["canonical_l10n_output_equals_confirmed"] == 1),
            "needs_output_apply_count": sum(1 for record in phase4_records if record["needs_output_apply"] == 1),
            "open_issue_count_segments": sum(1 for record in phase4_records if record["open_issue_count"] > 0),
            "high_issue_count_segments": sum(1 for record in phase4_records if record["high_issue_count"] > 0),
            "human_confirmation_count": sum(1 for record in phase4_records if record["is_human_confirmation"] == 1),
            "auto_confirmation_count": sum(1 for record in phase4_records if record["is_auto_confirmation"] == 1),
            "locked_count": sum(1 for record in phase4_records if record["locked"] == 1),
            "dynamic_or_multiline_escaped_count": sum(
                1
                for record in phase4_records
                if record["has_select_cstring"]
                or record["has_select_localization"]
                or record["has_scope_getter"]
                or record["has_multiline"]
            ),
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Keep debug_existing_policy_consumption as issue-blocked/no-close classification only. "
            + phase4_recommendation
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_debug_consumption_phase4_readonly_review_run{summary['run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    debug = summary["debug_existing_policy_consumption"]
    phase4 = summary["phase_4_auto_confirmed_plain_or_light"]
    lines = [
        "Debug consumption + phase 4 read-only review",
        f"run_id={summary['run_id']}",
        f"current_run={json.dumps(summary['current_run'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Debug existing policy consumption:",
        f"- record_count={debug['record_count']}",
        f"- no_open_issue_count={debug['no_open_issue_count']}",
        f"- with_open_issue_count={debug['with_open_issue_count']}",
        f"- with_high_issue_count={debug['with_high_issue_count']}",
        f"- open_issue_family_counts={json.dumps(debug['open_issue_family_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- high_issue_family_counts={json.dumps(debug['high_issue_family_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Phase 4:",
        f"- record_count={phase4['record_count']}",
        f"- guard_ok_count={phase4['guard_ok_count']}",
        f"- guard_blocked_count={phase4['guard_blocked_count']}",
        f"- token_surface_counts={json.dumps(phase4['token_surface_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- confirmation_label_counts={json.dumps(phase4['confirmation_label_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- confirmation_source_counts={json.dumps(phase4['confirmation_source_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- guard_failure_counts={json.dumps(phase4['guard_failure_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    phase4 = summary["phase_4_auto_confirmed_plain_or_light"]
    debug = summary["debug_existing_policy_consumption"]
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"debug_record_count={debug['record_count']}")
    print(f"debug_with_open_issue_count={debug['with_open_issue_count']}")
    print(f"debug_with_high_issue_count={debug['with_high_issue_count']}")
    print(f"phase4_record_count={phase4['record_count']}")
    print(f"phase4_guard_ok_count={phase4['guard_ok_count']}")
    print(f"phase4_guard_blocked_count={phase4['guard_blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
