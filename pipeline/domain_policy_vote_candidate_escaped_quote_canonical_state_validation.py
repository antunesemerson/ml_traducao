from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_escaped_quote_canonical_state_validation_v1"
FROM_RUN_ID = 514
TO_RUN_ID = 515
ESCAPED_QUOTE_JSONL = Path(
    "reports/20260630_154643_540225_domain_policy_vote_candidate_escaped_quote_only_diagnostic.jsonl"
)
REAL_DIVERGENCE_JSONL = Path(
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


def canonical_l10n(value: str | None) -> str:
    return (value or "").replace('\\"', '"')


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run {run_id}")
    if row["finished_at"] is None:
        raise SystemExit(f"segment_state_run {run_id} is incomplete")
    return dict(row)


def fetch_items(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
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
            state.output_state,
            state.review_state,
            state.apply_state,
            state.active_action,
            state.candidate_action,
            state.policy_action,
            state.lifecycle_policy_action,
            state.lifecycle_policy_allowed,
            state.confirmed_matches_output AS state_confirmed_matches_output,
            state.needs_output_apply,
            state.is_closed,
            state.locked AS state_locked,
            o.portuguese_text AS output_text,
            c.confirmed_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked AS confirmation_locked
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


def compact_text(value: str | None, limit: int = 500) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_record(
    *,
    category: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    input_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_text = str(current.get("output_text") or "")
    confirmed_text = str(current.get("confirmed_text") or "")
    canonical_equal = canonical_l10n(output_text) == canonical_l10n(confirmed_text)
    raw_equal = output_text == confirmed_text
    token_integrity_ok = token_counts(output_text) == token_counts(confirmed_text)
    return {
        "source": SOURCE,
        "category": category,
        "segment_id": int(current["segment_id"]),
        "relative_path": current.get("relative_path"),
        "source_key": current.get("source_key"),
        "input_family": (input_row or {}).get("family"),
        "input_diff_kind": (input_row or {}).get("diff_kind"),
        "input_change_shape": (input_row or {}).get("change_shape"),
        "from_state_group": previous.get("state_group"),
        "to_state_group": current.get("state_group"),
        "from_final_state": previous.get("final_state"),
        "to_final_state": current.get("final_state"),
        "from_needs_output_apply": int(previous.get("needs_output_apply") or 0),
        "to_needs_output_apply": int(current.get("needs_output_apply") or 0),
        "from_confirmed_matches_output": int(previous.get("state_confirmed_matches_output") or 0),
        "to_confirmed_matches_output": int(current.get("state_confirmed_matches_output") or 0),
        "from_lifecycle_policy_action": previous.get("lifecycle_policy_action"),
        "from_lifecycle_policy_allowed": int(previous.get("lifecycle_policy_allowed") or 0),
        "to_is_closed": int(current.get("is_closed") or 0),
        "to_candidate_action": current.get("candidate_action"),
        "to_policy_action": current.get("policy_action"),
        "to_lifecycle_policy_action": current.get("lifecycle_policy_action"),
        "to_lifecycle_policy_allowed": int(current.get("lifecycle_policy_allowed") or 0),
        "canonical_equal": canonical_equal,
        "raw_equal": raw_equal,
        "token_integrity_ok": token_integrity_ok,
        "is_holy_site_focus": int(current["segment_id"]) in HOLY_SITE_SEGMENT_IDS,
        "confirmation_level": current.get("confirmation_level"),
        "confirmation_source": current.get("confirmation_source"),
        "confirmation_label": current.get("confirmation_label"),
        "confirmation_locked": int(current.get("confirmation_locked") or 0),
        "output_text": compact_text(output_text),
        "confirmed_text": compact_text(confirmed_text),
    }


def main() -> None:
    escaped_rows = read_jsonl(ESCAPED_QUOTE_JSONL)
    real_rows = read_jsonl(REAL_DIVERGENCE_JSONL)
    escaped_ids = sorted({int(row["segment_id"]) for row in escaped_rows})
    real_ids = sorted({int(row["segment_id"]) for row in real_rows})
    if len(escaped_ids) != 3853:
        raise SystemExit(f"escaped quote input guard failed: {len(escaped_ids)}")
    if len(real_ids) != 390:
        raise SystemExit(f"real divergence input guard failed: {len(real_ids)}")

    escaped_by_id = {int(row["segment_id"]): row for row in escaped_rows}
    real_by_id = {int(row["segment_id"]): row for row in real_rows}
    all_ids = sorted(set(escaped_ids) | set(real_ids) | HOLY_SITE_SEGMENT_IDS)

    with connect_readonly() as conn:
        from_run = fetch_run(conn, FROM_RUN_ID)
        to_run = fetch_run(conn, TO_RUN_ID)
        before = fetch_items(conn, FROM_RUN_ID, all_ids)
        after = fetch_items(conn, TO_RUN_ID, all_ids)

    missing = sorted(set(all_ids) - set(before) | (set(all_ids) - set(after)))
    if missing:
        raise SystemExit(f"missing segment state rows: {missing[:20]}")

    records: list[dict[str, Any]] = []
    for segment_id in escaped_ids:
        records.append(
            build_record(
                category="escaped_quote_canonical_equal",
                previous=before[segment_id],
                current=after[segment_id],
                input_row=escaped_by_id[segment_id],
            )
        )

    real_records: list[dict[str, Any]] = []
    for segment_id in real_ids:
        record = build_record(
            category="real_divergence_canonical_unequal",
            previous=before[segment_id],
            current=after[segment_id],
            input_row=real_by_id[segment_id],
        )
        if not record["canonical_equal"]:
            real_records.append(record)
            records.append(record)

    holy_records = [
        build_record(
            category="holy_sites_plural_to_singular_focus",
            previous=before[segment_id],
            current=after[segment_id],
            input_row=real_by_id.get(segment_id),
        )
        for segment_id in sorted(HOLY_SITE_SEGMENT_IDS)
    ]
    records.extend(holy_records)

    escaped_records = [row for row in records if row["category"] == "escaped_quote_canonical_equal"]
    real_records = [row for row in records if row["category"] == "real_divergence_canonical_unequal"]
    holy_records = [row for row in records if row["category"] == "holy_sites_plural_to_singular_focus"]

    category_counts = Counter(row["category"] for row in records)
    escaped_final_counts = Counter(row["to_final_state"] for row in escaped_records)
    real_final_counts = Counter(row["to_final_state"] for row in real_records)

    escaped_canonical_equal_count = sum(1 for row in escaped_records if row["canonical_equal"])
    escaped_still_needs_apply = [row for row in escaped_records if row["to_needs_output_apply"] == 1]
    escaped_pending_apply = [row for row in escaped_records if row["to_final_state"] == "pending_apply_confirmed"]
    escaped_closed = [row for row in escaped_records if row["to_is_closed"] == 1 or row["to_state_group"] == "closed"]
    escaped_lifecycle_allowed = [row for row in escaped_records if row["to_lifecycle_policy_allowed"] == 1]
    escaped_lifecycle_new_allowed = [
        row
        for row in escaped_records
        if row["from_lifecycle_policy_allowed"] == 0 and row["to_lifecycle_policy_allowed"] == 1
    ]
    escaped_lifecycle_action_changed = [
        row
        for row in escaped_records
        if (row["from_lifecycle_policy_action"] or "") != (row["to_lifecycle_policy_action"] or "")
    ]

    real_needs_apply = [row for row in real_records if row["to_needs_output_apply"] == 1]
    real_pending_apply = [row for row in real_records if row["to_final_state"] == "pending_apply_confirmed"]
    real_confirmed_match_wrong = [row for row in real_records if row["to_confirmed_matches_output"] == 1]
    real_closed = [row for row in real_records if row["to_is_closed"] == 1 or row["to_state_group"] == "closed"]

    holy_needs_apply = [row for row in holy_records if row["to_needs_output_apply"] == 1]
    holy_pending_apply = [row for row in holy_records if row["to_final_state"] == "pending_apply_confirmed"]
    holy_token_mismatch = [row for row in holy_records if not row["token_integrity_ok"]]
    holy_closed = [row for row in holy_records if row["to_is_closed"] == 1 or row["to_state_group"] == "closed"]

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_segment_state_validation",
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
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0)
            - int(from_run["output_apply_pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0) - int(from_run["reopen_count"] or 0),
        },
        "category_counts": dict(category_counts),
        "escaped_quote_input_count": len(escaped_records),
        "escaped_quote_canonical_equal_count": escaped_canonical_equal_count,
        "escaped_quote_still_needs_output_apply_count": len(escaped_still_needs_apply),
        "escaped_quote_pending_apply_confirmed_count": len(escaped_pending_apply),
        "escaped_quote_closed_new_count": len(escaped_closed),
        "escaped_quote_lifecycle_policy_allowed_count": len(escaped_lifecycle_allowed),
        "escaped_quote_lifecycle_new_allowed_count": len(escaped_lifecycle_new_allowed),
        "escaped_quote_lifecycle_action_changed_count": len(escaped_lifecycle_action_changed),
        "escaped_quote_to_final_state_counts": dict(escaped_final_counts),
        "real_divergence_input_count": len(real_ids),
        "real_divergence_checked_count": len(real_records),
        "real_divergence_needs_output_apply_count": len(real_needs_apply),
        "real_divergence_pending_apply_confirmed_count": len(real_pending_apply),
        "real_divergence_confirmed_matches_output_wrong_count": len(real_confirmed_match_wrong),
        "real_divergence_closed_unexpectedly_count": len(real_closed),
        "real_divergence_to_final_state_counts": dict(real_final_counts),
        "holy_site_focus_count": len(holy_records),
        "holy_site_needs_output_apply_count": len(holy_needs_apply),
        "holy_site_pending_apply_confirmed_count": len(holy_pending_apply),
        "holy_site_token_integrity_failed_count": len(holy_token_mismatch),
        "holy_site_closed_unexpectedly_count": len(holy_closed),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 1,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "validation_passed": False,
        "single_operational_recommendation": "",
        "output_files": {},
    }
    summary["validation_passed"] = (
        summary["escaped_quote_canonical_equal_count"] == summary["escaped_quote_input_count"]
        and summary["escaped_quote_still_needs_output_apply_count"] == 0
        and summary["escaped_quote_pending_apply_confirmed_count"] == 0
        and summary["escaped_quote_closed_new_count"] == 0
        and summary["escaped_quote_lifecycle_new_allowed_count"] == 0
        and summary["escaped_quote_lifecycle_action_changed_count"] == 0
        and summary["real_divergence_needs_output_apply_count"] == summary["real_divergence_checked_count"]
        and summary["real_divergence_pending_apply_confirmed_count"] == summary["real_divergence_checked_count"]
        and summary["real_divergence_confirmed_matches_output_wrong_count"] == 0
        and summary["real_divergence_closed_unexpectedly_count"] == 0
        and summary["holy_site_needs_output_apply_count"] == summary["holy_site_focus_count"]
        and summary["holy_site_pending_apply_confirmed_count"] == summary["holy_site_focus_count"]
        and summary["holy_site_token_integrity_failed_count"] == summary["holy_site_focus_count"]
        and summary["holy_site_closed_unexpectedly_count"] == 0
    )
    if summary["validation_passed"]:
        summary["single_operational_recommendation"] = (
            "Keep escaped_quote_only out of apply/candidate/lifecycle flows; continue only with real "
            "pending_apply_confirmed divergences, separating token-safe text replacement from token-changing policy work."
        )
    else:
        summary["single_operational_recommendation"] = (
            "Hold apply/discovery/lifecycle and send failing counts to architecture before further training execution."
        )

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_escaped_quote_canonical_state_validation"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate escaped quote canonical state validation",
        "",
        f"from_run_id: {FROM_RUN_ID}",
        f"to_run_id: {TO_RUN_ID}",
        f"validation_passed: {str(summary['validation_passed']).lower()}",
        "",
        "global_delta:",
        f"- closed_count: {summary['global_delta']['closed_count']}",
        f"- pending_count: {summary['global_delta']['pending_count']}",
        f"- output_apply_pending_count: {summary['global_delta']['output_apply_pending_count']}",
        f"- reopen_count: {summary['global_delta']['reopen_count']}",
        "",
        "escaped_quote_only:",
        f"- input_count: {summary['escaped_quote_input_count']}",
        f"- canonical_equal_count: {summary['escaped_quote_canonical_equal_count']}",
        f"- still_needs_output_apply_count: {summary['escaped_quote_still_needs_output_apply_count']}",
        f"- pending_apply_confirmed_count: {summary['escaped_quote_pending_apply_confirmed_count']}",
        f"- closed_new_count: {summary['escaped_quote_closed_new_count']}",
        f"- lifecycle_policy_allowed_count: {summary['escaped_quote_lifecycle_policy_allowed_count']}",
        f"- lifecycle_new_allowed_count: {summary['escaped_quote_lifecycle_new_allowed_count']}",
        f"- lifecycle_action_changed_count: {summary['escaped_quote_lifecycle_action_changed_count']}",
        "",
        "real_divergence:",
        f"- checked_count: {summary['real_divergence_checked_count']}",
        f"- needs_output_apply_count: {summary['real_divergence_needs_output_apply_count']}",
        f"- pending_apply_confirmed_count: {summary['real_divergence_pending_apply_confirmed_count']}",
        f"- confirmed_matches_output_wrong_count: {summary['real_divergence_confirmed_matches_output_wrong_count']}",
        f"- closed_unexpectedly_count: {summary['real_divergence_closed_unexpectedly_count']}",
        "",
        "holy_sites_plural_to_singular:",
        f"- focus_count: {summary['holy_site_focus_count']}",
        f"- needs_output_apply_count: {summary['holy_site_needs_output_apply_count']}",
        f"- pending_apply_confirmed_count: {summary['holy_site_pending_apply_confirmed_count']}",
        f"- token_integrity_failed_count: {summary['holy_site_token_integrity_failed_count']}",
        f"- closed_unexpectedly_count: {summary['holy_site_closed_unexpectedly_count']}",
        "",
        "guards:",
        "- candidate_generation: not_run",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        f"recommendation: {summary['single_operational_recommendation']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
