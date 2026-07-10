from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_pending_apply_confirmed_run515_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 515
HOLY_SITE_TOKEN_POLICY_IDS = {237388, 239477, 239479, 239507, 239509, 239511}


DENSE_MARKERS = (
    "$EFFECT_LIST_BULLET$",
    "Select_CString",
    "SelectLocalization",
    "ES_",
    "GetTrait(",
    "GetCultureTradition(",
    "ScriptValue(",
    "GetVassalStance(",
    "ROOT.",
    "Scope.",
    "GetPlayerHeir",
    "[",
    "]",
)


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


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def canonical_l10n(value: str | None) -> str:
    return (value or "").replace('\\"', '"')


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def compact_text(value: str | None, limit: int = 1200) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def structural_surface(output_text: str, confirmed_text: str) -> str:
    blob = f"{output_text}\n{confirmed_text}"
    if "\\n" in blob or "\n" in blob:
        return "multiline"
    if any(marker in blob for marker in DENSE_MARKERS):
        return "dynamic_or_token_surface"
    return "plain_text"


def diff_bucket(output_text: str, confirmed_text: str, segment_id: int) -> str:
    if output_text == confirmed_text:
        return "already_equal_unexpected"
    if canonical_l10n(output_text) == canonical_l10n(confirmed_text):
        return "canonical_equal_unexpected"
    if segment_id in HOLY_SITE_TOKEN_POLICY_IDS:
        return "holy_sites_plural_to_singular_token_policy_hold"
    if token_counts(output_text) == token_counts(confirmed_text):
        return "token_safe_text_replacement_review_later"
    return "protected_token_signature_mismatch_hold"


def recommended_next_action(bucket: str, surface: str) -> str:
    if bucket == "token_safe_text_replacement_review_later":
        if surface == "plain_text":
            return "future_diff_preview_human_audit_then_protected_apply_if_approved"
        return "future_diff_preview_structural_sample_before_any_apply"
    if bucket in {"holy_sites_plural_to_singular_token_policy_hold", "protected_token_signature_mismatch_hold"}:
        return "hold_for_token_policy_or_architecture"
    return "investigate_state_unexpected"


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.state_group,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.review_state,
            state.apply_state,
            state.candidate_action,
            state.policy_action,
            state.lifecycle_policy_action,
            state.lifecycle_policy_allowed,
            o.portuguese_text AS output_text,
            o.output_line_number,
            c.confirmed_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked
        FROM segment_state_items state
        JOIN output_segments o ON o.segment_id = state.segment_id
        JOIN segment_confirmations c ON c.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.final_state = 'pending_apply_confirmed'
        ORDER BY state.segment_id
        """,
        (SEGMENT_STATE_RUN_ID,),
    ).fetchall()
    return [dict(row) for row in rows]


def representative_examples(rows: list[dict[str, Any]], group_field: str, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(row.get(group_field) or "")
        if len(buckets[key]) >= limit:
            continue
        buckets[key].append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "output_line_number": row["output_line_number"],
                "diagnostic_bucket": row["diagnostic_bucket"],
                "structural_surface": row["structural_surface"],
                "recommended_next_action": row["recommended_next_action"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
            }
        )
    return dict(buckets)


def main() -> None:
    with connect_readonly() as conn:
        run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (SEGMENT_STATE_RUN_ID,)).fetchone()
        if run is None or run["finished_at"] is None:
            raise SystemExit(f"segment_state_run_id {SEGMENT_STATE_RUN_ID} missing or incomplete")
        source_rows = fetch_rows(conn)

    records: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        output_tokens = token_counts(output_text)
        confirmed_tokens = token_counts(confirmed_text)
        bucket = diff_bucket(output_text, confirmed_text, segment_id)
        surface = structural_surface(output_text, confirmed_text)
        records.append(
            {
                "source": SOURCE,
                "segment_state_run_id": SEGMENT_STATE_RUN_ID,
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "output_line_number": row.get("output_line_number"),
                "state_group": row.get("state_group"),
                "final_state": row.get("final_state"),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "review_state": row.get("review_state"),
                "apply_state": row.get("apply_state"),
                "candidate_action": row.get("candidate_action"),
                "policy_action": row.get("policy_action"),
                "lifecycle_policy_action": row.get("lifecycle_policy_action"),
                "lifecycle_policy_allowed": int(row.get("lifecycle_policy_allowed") or 0),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "locked": int(row.get("locked") or 0),
                "diagnostic_bucket": bucket,
                "structural_surface": surface,
                "token_integrity_ok": output_tokens == confirmed_tokens,
                "canonical_equal": canonical_l10n(output_text) == canonical_l10n(confirmed_text),
                "is_holy_site_token_policy_focus": segment_id in HOLY_SITE_TOKEN_POLICY_IDS,
                "output_tokens": output_tokens,
                "confirmed_tokens": confirmed_tokens,
                "recommended_next_action": recommended_next_action(bucket, surface),
                "output_text": compact_text(output_text),
                "confirmed_text": compact_text(confirmed_text),
            }
        )

    bucket_counts = Counter(row["diagnostic_bucket"] for row in records)
    surface_counts = Counter(row["structural_surface"] for row in records)
    next_action_counts = Counter(row["recommended_next_action"] for row in records)
    token_safe_rows = [row for row in records if row["diagnostic_bucket"] == "token_safe_text_replacement_review_later"]
    token_hold_rows = [
        row
        for row in records
        if row["diagnostic_bucket"]
        in {"holy_sites_plural_to_singular_token_policy_hold", "protected_token_signature_mismatch_hold"}
    ]
    plain_token_safe = [row for row in token_safe_rows if row["structural_surface"] == "plain_text"]

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_pending_apply_confirmed_run515_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_state_finished_at": run["finished_at"],
        "pending_apply_confirmed_count": len(records),
        "diagnostic_bucket_counts": dict(bucket_counts),
        "structural_surface_counts": dict(surface_counts),
        "recommended_next_action_counts": dict(next_action_counts),
        "token_safe_text_replacement_count": len(token_safe_rows),
        "token_safe_plain_text_count": len(plain_token_safe),
        "token_safe_structural_count": len(token_safe_rows) - len(plain_token_safe),
        "token_policy_hold_count": len(token_hold_rows),
        "protected_token_signature_mismatch_hold_count": bucket_counts.get("protected_token_signature_mismatch_hold", 0),
        "holy_sites_plural_to_singular_token_policy_hold_count": bucket_counts.get(
            "holy_sites_plural_to_singular_token_policy_hold", 0
        ),
        "unexpected_state_count": bucket_counts.get("already_equal_unexpected", 0)
        + bucket_counts.get("canonical_equal_unexpected", 0),
        "token_safe_segment_ids": [int(row["segment_id"]) for row in token_safe_rows],
        "token_policy_hold_segment_ids": [int(row["segment_id"]) for row in token_hold_rows],
        "holy_site_hold_segment_ids": [
            int(row["segment_id"]) for row in records if row["diagnostic_bucket"] == "holy_sites_plural_to_singular_token_policy_hold"
        ],
        "representative_examples_by_bucket": representative_examples(records, "diagnostic_bucket"),
        "representative_examples_by_action": representative_examples(records, "recommended_next_action"),
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
            "Prepare a separate read-only diff preview packet for the 44 token-safe text replacements, "
            "then submit that packet for human approval before any protected apply. Keep the 13 token-changing "
            "rows in explicit hold until token policy/architecture approval."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_pending_apply_confirmed_run515_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    token_safe_path = Path(str(base) + "_token_safe_review_later.jsonl")
    token_hold_path = Path(str(base) + "_token_policy_hold.jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    write_jsonl(token_safe_path, token_safe_rows)
    write_jsonl(token_hold_path, token_hold_rows)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "token_safe_review_later_jsonl": str(token_safe_path),
        "token_policy_hold_jsonl": str(token_hold_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate pending_apply_confirmed run515 diagnostic",
        "",
        f"segment_state_run_id: {SEGMENT_STATE_RUN_ID}",
        f"pending_apply_confirmed_count: {len(records)}",
        "",
        "diagnostic_bucket_counts:",
        *[f"- {count} | {key}" for key, count in bucket_counts.most_common()],
        "",
        "structural_surface_counts:",
        *[f"- {count} | {key}" for key, count in surface_counts.most_common()],
        "",
        "recommended_next_action_counts:",
        *[f"- {count} | {key}" for key, count in next_action_counts.most_common()],
        "",
        "guards:",
        "- candidate_generation: not_run",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- segment_state: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        f"recommendation: {summary['single_operational_recommendation']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
