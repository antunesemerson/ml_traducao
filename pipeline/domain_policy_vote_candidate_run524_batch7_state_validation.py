from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_run524_batch7_state_validation_v1"
FROM_RUN_ID = 523
TO_RUN_ID = 524
BATCH_SEGMENT_IDS = [62379, 66840, 75688, 142595, 142598]
HELD_SEGMENT_IDS = [6694, 23482, 47168, 50741, 62620]
ESCAPED_QUOTE_JSONL = Path("reports/20260630_154643_540225_domain_policy_vote_candidate_escaped_quote_only_diagnostic.jsonl")
REAL_DIVERGENCE_JSONL = Path("reports/20260630_143758_178640_domain_policy_vote_candidate_confirmed_output_state_divergence_audit.jsonl")
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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def canonical_l10n(value: str | None) -> str:
    return (value or "").replace('\\"', '"')


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["finished_at"] is None:
        raise SystemExit(f"segment_state_run {run_id} missing or incomplete")
    return dict(row)


def fetch_items(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.final_state,
            state.state_group,
            state.confirmed_matches_output,
            state.needs_output_apply,
            o.portuguese_text AS output_text,
            c.confirmed_text,
            o.portuguese_text = c.confirmed_text AS raw_output_equals_confirmed
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


def record_for(category: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    output_text = str(after.get("output_text") or "")
    confirmed_text = str(after.get("confirmed_text") or "")
    return {
        "source": SOURCE,
        "category": category,
        "segment_id": int(after["segment_id"]),
        "relative_path": after["relative_path"],
        "source_key": after["source_key"],
        "from_final_state": before["final_state"],
        "to_final_state": after["final_state"],
        "from_state_group": before["state_group"],
        "to_state_group": after["state_group"],
        "to_confirmed_matches_output": int(after["confirmed_matches_output"] or 0),
        "to_needs_output_apply": int(after["needs_output_apply"] or 0),
        "raw_output_equals_confirmed": bool(after["raw_output_equals_confirmed"]),
        "canonical_equal": canonical_l10n(output_text) == canonical_l10n(confirmed_text),
        "token_integrity_ok": token_counts(output_text) == token_counts(confirmed_text),
        "output_text": output_text,
        "confirmed_text": confirmed_text,
    }


def main() -> None:
    escaped_ids = sorted({int(row["segment_id"]) for row in read_jsonl(ESCAPED_QUOTE_JSONL)})
    real_ids = sorted({int(row["segment_id"]) for row in read_jsonl(REAL_DIVERGENCE_JSONL)})
    all_ids = sorted(set(BATCH_SEGMENT_IDS) | set(HELD_SEGMENT_IDS) | set(escaped_ids) | set(real_ids) | HOLY_SITE_SEGMENT_IDS)
    with connect_readonly() as conn:
        from_run = fetch_run(conn, FROM_RUN_ID)
        to_run = fetch_run(conn, TO_RUN_ID)
        before = fetch_items(conn, FROM_RUN_ID, all_ids)
        after = fetch_items(conn, TO_RUN_ID, all_ids)

    records: list[dict[str, Any]] = []
    for segment_id in BATCH_SEGMENT_IDS:
        records.append(record_for("batch7_applied_focus", before[segment_id], after[segment_id]))
    for segment_id in HELD_SEGMENT_IDS:
        records.append(record_for("held_context_focus", before[segment_id], after[segment_id]))
    for segment_id in escaped_ids:
        records.append(record_for("escaped_quote_only", before[segment_id], after[segment_id]))
    for segment_id in real_ids:
        row = record_for("real_divergence_canonical_unequal", before[segment_id], after[segment_id])
        if not row["canonical_equal"]:
            records.append(row)
    for segment_id in sorted(HOLY_SITE_SEGMENT_IDS):
        records.append(record_for("holy_sites_token_changing_focus", before[segment_id], after[segment_id]))

    batch = [row for row in records if row["category"] == "batch7_applied_focus"]
    held = [row for row in records if row["category"] == "held_context_focus"]
    escaped = [row for row in records if row["category"] == "escaped_quote_only"]
    real = [row for row in records if row["category"] == "real_divergence_canonical_unequal"]
    holy = [row for row in records if row["category"] == "holy_sites_token_changing_focus"]

    batch_ok = [row for row in batch if row["raw_output_equals_confirmed"] and row["to_confirmed_matches_output"] == 1 and row["to_needs_output_apply"] == 0 and row["to_final_state"] != "pending_apply_confirmed"]
    held_ok = [row for row in held if row["to_final_state"] == "pending_apply_confirmed" and row["to_needs_output_apply"] == 1]
    escaped_ok = [row for row in escaped if row["canonical_equal"] and row["to_needs_output_apply"] == 0 and row["to_final_state"] != "pending_apply_confirmed"]
    real_ok = [row for row in real if not row["canonical_equal"] and row["to_needs_output_apply"] == 1 and row["to_confirmed_matches_output"] == 0 and row["to_final_state"] == "pending_apply_confirmed"]
    holy_ok = [row for row in holy if row["to_needs_output_apply"] == 1 and row["to_final_state"] == "pending_apply_confirmed" and not row["token_integrity_ok"]]

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_segment_state_delta_validation",
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
        "category_counts": dict(Counter(row["category"] for row in records)),
        "batch_focus_count": len(batch),
        "batch_valid_count": len(batch_ok),
        "held_focus_count": len(held),
        "held_valid_count": len(held_ok),
        "batch_final_state_counts": dict(Counter(row["to_final_state"] for row in batch)),
        "held_final_state_counts": dict(Counter(row["to_final_state"] for row in held)),
        "escaped_quote_count": len(escaped),
        "escaped_quote_valid_count": len(escaped_ok),
        "escaped_quote_needs_output_apply_count": sum(row["to_needs_output_apply"] for row in escaped),
        "real_divergence_count": len(real),
        "real_divergence_valid_count": len(real_ok),
        "real_divergence_needs_output_apply_count": sum(row["to_needs_output_apply"] for row in real),
        "holy_site_focus_count": len(holy),
        "holy_site_valid_count": len(holy_ok),
        "holy_site_needs_output_apply_count": sum(row["to_needs_output_apply"] for row in holy),
        "holy_site_token_integrity_failed_count": sum(1 for row in holy if not row["token_integrity_ok"]),
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
        "single_operational_recommendation": "",
        "output_files": {},
    }
    summary["validation_passed"] = len(batch_ok) == len(batch) and len(held_ok) == len(held) and len(escaped_ok) == len(escaped) and len(real_ok) == len(real) and len(holy_ok) == len(holy)
    summary["single_operational_recommendation"] = "Continue reviewing remaining token-safe rows in small batches; keep held/token-changing rows out of apply." if summary["validation_passed"] else "Hold further execution and investigate failing validation counts."

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_run524_batch7_state_validation"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
