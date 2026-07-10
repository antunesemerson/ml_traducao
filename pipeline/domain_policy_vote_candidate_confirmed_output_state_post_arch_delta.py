from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_confirmed_output_state_post_arch_delta_v1"
FROM_RUN_ID = 513
TO_RUN_ID = 514
BASE_AUDIT_JSONL = Path(
    "reports/20260630_143758_178640_domain_policy_vote_candidate_confirmed_output_state_divergence_audit.jsonl"
)
HOLY_SITE_SEGMENT_IDS = {237388, 239477, 239479, 239507, 239509, 239511}


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
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run {run_id}")
    if row["finished_at"] is None:
        raise SystemExit(f"segment_state_run {run_id} incomplete")
    return dict(row)


def fetch_items(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            state.run_id,
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.state_group,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output AS state_confirmed_matches_output,
            state.review_state,
            state.apply_state,
            o.portuguese_text AS output_text,
            c.confirmed_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked
        FROM segment_state_items state
        JOIN output_segments o ON o.segment_id = state.segment_id
        JOIN segment_confirmations c ON c.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    base_rows = read_jsonl(BASE_AUDIT_JSONL)
    if len(base_rows) != 390:
        raise SystemExit(f"base audit row count guard failed: {len(base_rows)}")
    segment_ids = sorted({int(row["segment_id"]) for row in base_rows})
    if len(segment_ids) != len(base_rows):
        raise SystemExit("duplicate segment ids in base audit")

    base_by_id = {int(row["segment_id"]): row for row in base_rows}
    with connect_readonly() as conn:
        from_run = fetch_run(conn, FROM_RUN_ID)
        to_run = fetch_run(conn, TO_RUN_ID)
        before = fetch_items(conn, FROM_RUN_ID, segment_ids)
        after = fetch_items(conn, TO_RUN_ID, segment_ids)

    missing = sorted(set(segment_ids) - set(before) | (set(segment_ids) - set(after)))
    if missing:
        raise SystemExit(f"missing state rows: {missing[:20]}")

    records: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        base = base_by_id[segment_id]
        old = before[segment_id]
        new = after[segment_id]
        output_text = str(new.get("output_text") or "")
        confirmed_text = str(new.get("confirmed_text") or "")
        real_matches = output_text == confirmed_text
        token_integrity_ok = token_counts(output_text) == token_counts(confirmed_text)
        records.append(
            {
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": new.get("relative_path"),
                "source_key": new.get("source_key"),
                "base_diff_kind": base.get("diff_kind"),
                "is_holy_site_focus": segment_id in HOLY_SITE_SEGMENT_IDS,
                "from_state_group": old.get("state_group"),
                "to_state_group": new.get("state_group"),
                "from_final_state": old.get("final_state"),
                "to_final_state": new.get("final_state"),
                "from_confirmed_matches_output": int(old.get("state_confirmed_matches_output") or 0),
                "to_confirmed_matches_output": int(new.get("state_confirmed_matches_output") or 0),
                "from_needs_output_apply": int(old.get("needs_output_apply") or 0),
                "to_needs_output_apply": int(new.get("needs_output_apply") or 0),
                "real_confirmed_matches_output": real_matches,
                "token_integrity_ok": token_integrity_ok,
                "output_text": output_text,
                "confirmed_text": confirmed_text,
                "confirmation_level": new.get("confirmation_level"),
                "confirmation_source": new.get("confirmation_source"),
                "confirmation_label": new.get("confirmation_label"),
                "locked": int(new.get("locked") or 0),
                "guard_ok_confirmed_matches_output": int(new.get("state_confirmed_matches_output") or 0) == int(real_matches),
                "guard_ok_needs_output_apply": int(new.get("needs_output_apply") or 0) == int(not real_matches),
                "closed_unexpectedly": new.get("state_group") == "closed",
            }
        )

    diff_counts = Counter(row["base_diff_kind"] for row in records)
    final_counts = Counter(str(row["to_final_state"]) for row in records)
    state_counts = Counter(str(row["to_state_group"]) for row in records)
    holy_rows = [row for row in records if row["is_holy_site_focus"]]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post_arch_segment_state_delta",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "from_run_id": FROM_RUN_ID,
        "to_run_id": TO_RUN_ID,
        "from_run_finished_at": from_run["finished_at"],
        "to_run_finished_at": to_run["finished_at"],
        "global_before": {
            "closed_count": int(from_run["closed_count"] or 0),
            "pending_count": int(from_run["pending_count"] or 0),
            "output_apply_pending_count": int(from_run["output_apply_pending_count"] or 0),
            "reopen_count": int(from_run["reopen_count"] or 0),
        },
        "global_after": {
            "closed_count": int(to_run["closed_count"] or 0),
            "pending_count": int(to_run["pending_count"] or 0),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0),
        },
        "global_delta": {
            "closed_count": int(to_run["closed_count"] or 0) - int(from_run["closed_count"] or 0),
            "pending_count": int(to_run["pending_count"] or 0) - int(from_run["pending_count"] or 0),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0) - int(from_run["output_apply_pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0) - int(from_run["reopen_count"] or 0),
        },
        "audited_divergence_count": len(records),
        "diff_kind_counts": dict(diff_counts),
        "to_state_group_counts": dict(state_counts),
        "to_final_state_counts": dict(final_counts),
        "confirmed_matches_output_still_wrong_count": sum(1 for row in records if not row["guard_ok_confirmed_matches_output"]),
        "needs_output_apply_still_wrong_count": sum(1 for row in records if not row["guard_ok_needs_output_apply"]),
        "now_confirmed_matches_output_1_count": sum(row["to_confirmed_matches_output"] for row in records),
        "now_needs_output_apply_1_count": sum(row["to_needs_output_apply"] for row in records),
        "closed_unexpectedly_count": sum(1 for row in records if row["closed_unexpectedly"]),
        "pending_apply_confirmed_count": sum(1 for row in records if row["to_final_state"] == "pending_apply_confirmed"),
        "holy_site_focus_count": len(holy_rows),
        "holy_site_needs_output_apply_count": sum(row["to_needs_output_apply"] for row in holy_rows),
        "holy_site_pending_apply_confirmed_count": sum(1 for row in holy_rows if row["to_final_state"] == "pending_apply_confirmed"),
        "holy_site_token_integrity_failed_count": sum(1 for row in holy_rows if not row["token_integrity_ok"]),
        "holy_site_closed_unexpectedly_count": sum(1 for row in holy_rows if row["closed_unexpectedly"]),
        "validation_passed": False,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 1,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "records": records,
        "output_files": {},
    }
    summary["validation_passed"] = (
        summary["confirmed_matches_output_still_wrong_count"] == 0
        and summary["needs_output_apply_still_wrong_count"] == 0
        and summary["closed_unexpectedly_count"] == 0
        and summary["now_confirmed_matches_output_1_count"] == 0
        and summary["now_needs_output_apply_1_count"] == len(records)
        and summary["pending_apply_confirmed_count"] == len(records)
        and summary["holy_site_needs_output_apply_count"] == len(holy_rows)
        and summary["holy_site_pending_apply_confirmed_count"] == len(holy_rows)
        and summary["holy_site_token_integrity_failed_count"] == len(holy_rows)
        and summary["holy_site_closed_unexpectedly_count"] == 0
    )

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_confirmed_output_state_post_arch_delta"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "confirmed/output state post-architecture delta",
        "",
        f"from_run_id: {FROM_RUN_ID}",
        f"to_run_id: {TO_RUN_ID}",
        f"validation_passed: {str(summary['validation_passed']).lower()}",
        f"audited_divergence_count: {len(records)}",
        f"confirmed_matches_output_still_wrong_count: {summary['confirmed_matches_output_still_wrong_count']}",
        f"needs_output_apply_still_wrong_count: {summary['needs_output_apply_still_wrong_count']}",
        f"now_confirmed_matches_output_1_count: {summary['now_confirmed_matches_output_1_count']}",
        f"now_needs_output_apply_1_count: {summary['now_needs_output_apply_1_count']}",
        f"pending_apply_confirmed_count: {summary['pending_apply_confirmed_count']}",
        f"closed_unexpectedly_count: {summary['closed_unexpectedly_count']}",
        "",
        "diff_kind_counts:",
        *[f"- {count} | {key}" for key, count in diff_counts.most_common()],
        "",
        "holy-site focus:",
        f"- count: {len(holy_rows)}",
        f"- needs_output_apply: {summary['holy_site_needs_output_apply_count']}",
        f"- pending_apply_confirmed: {summary['holy_site_pending_apply_confirmed_count']}",
        f"- token_integrity_failed: {summary['holy_site_token_integrity_failed_count']}",
        f"- closed_unexpectedly: {summary['holy_site_closed_unexpectedly_count']}",
        "",
        "global_delta:",
        f"- closed_count: {summary['global_delta']['closed_count']}",
        f"- pending_count: {summary['global_delta']['pending_count']}",
        f"- output_apply_pending_count: {summary['global_delta']['output_apply_pending_count']}",
        f"- reopen_count: {summary['global_delta']['reopen_count']}",
        "",
        "guards:",
        "- discovery: not_run",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
