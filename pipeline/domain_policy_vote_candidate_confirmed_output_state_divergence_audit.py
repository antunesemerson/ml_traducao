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


SOURCE = "domain_policy_vote_candidate_confirmed_output_state_divergence_audit_v1"
SEGMENT_STATE_RUN_ID = 512
FOCUS_SEGMENT_IDS = (237388, 239477, 239479, 239507, 239509, 239511)
HOLY_SITE_PLURAL_TO_SINGULAR_RE = re.compile(r"\[holy_sites\|lE\].*\[holy_site\|lE\]|\[holy_site\|lE\].*\[holy_sites\|lE\]")


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


def diff_kind(output_text: str, confirmed_text: str) -> str:
    if output_text == confirmed_text:
        return "no_text_divergence"
    output_only = output_text.replace("[holy_sites|lE]", "[holy_site|lE]")
    confirmed_only = confirmed_text.replace("[holy_sites|lE]", "[holy_site|lE]")
    if output_only == confirmed_only and output_text.count("[holy_sites|lE]") != confirmed_text.count("[holy_sites|lE]"):
        return "holy_sites_plural_to_singular_only"
    if token_counts(output_text) != token_counts(confirmed_text):
        return "protected_token_signature_mismatch"
    return "text_divergence_same_token_signature"


def fetch_focus_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in FOCUS_SEGMENT_IDS)
    return [
        dict(row)
        for row in conn.execute(
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
            (SEGMENT_STATE_RUN_ID, *FOCUS_SEGMENT_IDS),
        ).fetchall()
    ]


def fetch_global_divergences(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
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
              AND state.state_group = 'pending'
              AND COALESCE(c.confirmed_text, '') <> COALESCE(o.portuguese_text, '')
              AND (
                COALESCE(state.confirmed_matches_output, 0) = 1
                OR COALESCE(state.needs_output_apply, 0) = 0
              )
            ORDER BY state.segment_id
            """,
            (SEGMENT_STATE_RUN_ID,),
        ).fetchall()
    ]


def enrich(row: dict[str, Any], focus: bool) -> dict[str, Any]:
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    real_matches = output_text == confirmed_text
    state_matches = int(row.get("state_confirmed_matches_output") or 0)
    needs_output_apply = int(row.get("needs_output_apply") or 0)
    kind = diff_kind(output_text, confirmed_text)
    return {
        "source": SOURCE,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_id": int(row["segment_id"]),
        "is_focus_segment": focus,
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "state_group": row.get("state_group"),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "apply_state": row.get("apply_state"),
        "state_confirmed_matches_output": state_matches,
        "state_needs_output_apply": needs_output_apply,
        "real_confirmed_matches_output": real_matches,
        "state_flag_divergence": (state_matches == 1 and not real_matches) or (needs_output_apply == 0 and not real_matches),
        "diff_kind": kind,
        "is_only_holy_sites_to_holy_site": kind == "holy_sites_plural_to_singular_only",
        "output_text": output_text,
        "confirmed_text": confirmed_text,
        "output_tokens": token_counts(output_text),
        "confirmed_tokens": token_counts(confirmed_text),
        "token_integrity_ok": token_counts(output_text) == token_counts(confirmed_text),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    with connect_readonly() as conn:
        run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (SEGMENT_STATE_RUN_ID,)).fetchone()
        if run is None or run["finished_at"] is None:
            raise SystemExit(f"segment_state_run_id {SEGMENT_STATE_RUN_ID} missing or incomplete")
        focus_rows = fetch_focus_rows(conn)
        global_rows = fetch_global_divergences(conn)

    focus_ids = {int(row["segment_id"]) for row in focus_rows}
    missing_focus = sorted(set(FOCUS_SEGMENT_IDS) - focus_ids)
    if missing_focus:
        raise SystemExit(f"missing focus segment ids: {missing_focus}")

    by_id: dict[int, dict[str, Any]] = {}
    for row in global_rows:
        by_id[int(row["segment_id"])] = enrich(row, int(row["segment_id"]) in FOCUS_SEGMENT_IDS)
    for row in focus_rows:
        segment_id = int(row["segment_id"])
        if segment_id not in by_id:
            enriched = enrich(row, True)
            if enriched["state_flag_divergence"]:
                by_id[segment_id] = enriched

    records = [by_id[key] for key in sorted(by_id)]
    diff_counts = Counter(row["diff_kind"] for row in records)
    state_counts = Counter(str(row["final_state"]) for row in records)
    focus_records = [row for row in records if row["is_focus_segment"]]
    non_focus_records = [row for row in records if not row["is_focus_segment"]]
    state_bad_confirmed_match_count = sum(1 for row in records if row["state_confirmed_matches_output"] == 1 and not row["real_confirmed_matches_output"])
    state_bad_needs_apply_count = sum(1 for row in records if row["state_needs_output_apply"] == 0 and not row["real_confirmed_matches_output"])

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_confirmed_output_state_divergence_audit",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_state_finished_at": run["finished_at"],
        "focus_segment_ids": list(FOCUS_SEGMENT_IDS),
        "focus_count": len(focus_records),
        "total_divergence_count": len(records),
        "non_focus_divergence_count": len(non_focus_records),
        "state_confirmed_matches_output_false_positive_count": state_bad_confirmed_match_count,
        "needs_output_apply_false_negative_count": state_bad_needs_apply_count,
        "diff_kind_counts": dict(diff_counts),
        "final_state_counts": dict(state_counts),
        "focus_all_only_holy_sites_to_holy_site": all(row["is_only_holy_sites_to_holy_site"] for row in focus_records),
        "focus_token_integrity_failed_count": sum(1 for row in focus_records if not row["token_integrity_ok"]),
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
            "Architecture should review segment-state guards: confirmed_matches_output and needs_output_apply appear stale/semantic "
            "for pending human-confirmed divergences. Add a real text equality guard, and keep token-changing corrections blocked "
            "until an explicit token-change exception policy exists."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_confirmed_output_state_divergence_audit"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "confirmed/output vs segment_state divergence audit",
        "",
        f"segment_state_run_id: {SEGMENT_STATE_RUN_ID}",
        f"total_divergence_count: {len(records)}",
        f"focus_count: {len(focus_records)}",
        f"non_focus_divergence_count: {len(non_focus_records)}",
        f"state_confirmed_matches_output_false_positive_count: {state_bad_confirmed_match_count}",
        f"needs_output_apply_false_negative_count: {state_bad_needs_apply_count}",
        "",
        "diff_kind_counts:",
        *[f"- {count} | {key}" for key, count in diff_counts.most_common()],
        "",
        "focus divergences:",
    ]
    for row in focus_records:
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['source_key']} | {row['diff_kind']}",
                f"- state: confirmed_matches_output={row['state_confirmed_matches_output']} needs_output_apply={row['state_needs_output_apply']} final_state={row['final_state']}",
                f"- output: {row['output_text']}",
                f"- confirmed: {row['confirmed_text']}",
            ]
        )
    if non_focus_records:
        lines.extend(["", "other divergences:"])
        for row in non_focus_records[:80]:
            lines.append(f"- {row['segment_id']} | {row['source_key']} | {row['diff_kind']} | state_confirmed_matches_output={row['state_confirmed_matches_output']} needs_output_apply={row['state_needs_output_apply']}")
    lines.extend(
        [
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
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
