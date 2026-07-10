from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_pending_apply_confirmed_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 514
HOLY_SITE_TOKEN_POLICY_IDS = {237388, 239477, 239479, 239507, 239509, 239511}


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


def diff_kind(output_text: str, confirmed_text: str, segment_id: int) -> str:
    if output_text == confirmed_text:
        return "already_equal_unexpected"
    if segment_id in HOLY_SITE_TOKEN_POLICY_IDS:
        return "holy_sites_plural_to_singular_needs_token_policy"
    if token_counts(output_text) == token_counts(confirmed_text):
        return "same_token_signature_apply_candidate"
    return "token_signature_mismatch_needs_policy"


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                state.segment_id,
                state.relative_path,
                state.source_key,
                state.state_group,
                state.final_state,
                state.needs_output_apply,
                state.confirmed_matches_output,
                state.review_state,
                state.apply_state,
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
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_examples(rows: list[dict[str, Any]], limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        bucket = row["diagnostic_bucket"]
        if len(buckets[bucket]) >= limit:
            continue
        buckets[bucket].append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "confirmation_level": row["confirmation_level"],
                "confirmation_source": row["confirmation_source"],
                "confirmation_label": row["confirmation_label"],
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
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        segment_id = int(row["segment_id"])
        bucket = diff_kind(output_text, confirmed_text, segment_id)
        output_tokens = token_counts(output_text)
        confirmed_tokens = token_counts(confirmed_text)
        records.append(
            {
                "source": SOURCE,
                "segment_state_run_id": SEGMENT_STATE_RUN_ID,
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "state_group": row.get("state_group"),
                "final_state": row.get("final_state"),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "review_state": row.get("review_state"),
                "apply_state": row.get("apply_state"),
                "output_line_number": row.get("output_line_number"),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "locked": int(row.get("locked") or 0),
                "output_text": output_text,
                "confirmed_text": confirmed_text,
                "diagnostic_bucket": bucket,
                "token_integrity_ok": output_tokens == confirmed_tokens,
                "output_tokens": output_tokens,
                "confirmed_tokens": confirmed_tokens,
                "is_holy_site_token_policy_focus": segment_id in HOLY_SITE_TOKEN_POLICY_IDS,
                "recommended_next_action": (
                    "protected_apply_dry_run_later"
                    if bucket == "same_token_signature_apply_candidate"
                    else "token_policy_or_hold_before_apply"
                    if bucket in {"token_signature_mismatch_needs_policy", "holy_sites_plural_to_singular_needs_token_policy"}
                    else "investigate_unexpected_equal"
                ),
            }
        )

    bucket_counts = Counter(row["diagnostic_bucket"] for row in records)
    confirmation_counts = Counter(
        f"{row['confirmation_level']}|{row['confirmation_source']}|{row['confirmation_label']}|locked={row['locked']}"
        for row in records
    )
    path_group_counts = Counter(str(row["relative_path"]).split("/", 1)[0] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_pending_apply_confirmed_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_state_finished_at": run["finished_at"],
        "pending_apply_confirmed_count": len(records),
        "diagnostic_bucket_counts": dict(bucket_counts),
        "same_token_signature_apply_candidate_count": bucket_counts.get("same_token_signature_apply_candidate", 0),
        "token_signature_mismatch_needs_policy_count": bucket_counts.get("token_signature_mismatch_needs_policy", 0),
        "holy_sites_plural_to_singular_needs_token_policy_count": bucket_counts.get(
            "holy_sites_plural_to_singular_needs_token_policy", 0
        ),
        "already_equal_unexpected_count": bucket_counts.get("already_equal_unexpected", 0),
        "confirmation_counts_top": [{"key": key, "count": count} for key, count in confirmation_counts.most_common(20)],
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_group_counts.most_common(20)],
        "representative_examples": representative_examples(records),
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
            "Do not apply yet. First split pending_apply_confirmed into a protected same-token apply plan and a token-policy hold plan; "
            "keep the six holy-site token-changing rows blocked until explicit architecture/token policy approval."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_pending_apply_confirmed_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate pending_apply_confirmed diagnostic",
        "",
        f"segment_state_run_id: {SEGMENT_STATE_RUN_ID}",
        f"pending_apply_confirmed_count: {len(records)}",
        "",
        "diagnostic_bucket_counts:",
        *[f"- {count} | {key}" for key, count in bucket_counts.most_common()],
        "",
        "path_group_counts_top:",
        *[f"- {item['count']} | {item['key']}" for item in summary["path_group_counts_top"]],
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
