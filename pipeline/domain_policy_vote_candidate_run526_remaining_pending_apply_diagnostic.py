from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_run526_remaining_pending_apply_diagnostic_v1"
SEGMENT_STATE_RUN_ID = 526
KNOWN_HUMAN_OR_GUARD_HOLDS = {6694, 23482, 47168, 50741, 62620}
HOLY_SITE_TOKEN_CHANGING = {237388, 239477, 239479, 239507, 239509, 239511}


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def short(value: str | None, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def classify(row: dict[str, Any]) -> tuple[str, str, str]:
    segment_id = int(row["segment_id"])
    source_key = str(row["source_key"] or "")
    relative_path = str(row["relative_path"] or "")
    output_text = str(row["output_text"] or "")
    confirmed_text = str(row["confirmed_text"] or "")
    tokens_match = token_counts(output_text) == token_counts(confirmed_text)

    if segment_id in HOLY_SITE_TOKEN_CHANGING:
        return (
            "holy_site_token_changing_hold",
            "hold_token_policy",
            "Known holy-site token-changing family; requires explicit token policy before any apply.",
        )
    if segment_id == 62620:
        return (
            "guard_blocked_dynamic_spanish_residue",
            "hold_guard_or_architecture",
            "Dry-run blocked by quality_issue:spanish_residue around dynamic religion getter; needs policy/guard decision.",
        )
    if segment_id in KNOWN_HUMAN_OR_GUARD_HOLDS:
        return (
            "human_context_hold",
            "hold_human_context",
            "Previously held by human/context review; do not reapply without explicit fresh approval.",
        )
    if not tokens_match:
        return (
            "token_signature_mismatch",
            "hold_token_policy",
            "Protected token signature differs between output and confirmed text.",
        )
    if "[ROOT." in output_text or "[ROOT." in confirmed_text or "[" in output_text or "[" in confirmed_text:
        return (
            "dynamic_token_residual",
            "needs_architecture_or_guarded_review",
            "Token signature matches but dynamic token surface remains; needs a narrow review before apply.",
        )
    if relative_path.startswith("event_localization/"):
        return (
            "event_localization_residual",
            "needs_human_review",
            "Event-localization text remains outside the exhausted token-safe packet; review manually before any apply.",
        )
    if "building_" in source_key or relative_path.endswith("buildings_l_spanish.yml"):
        return (
            "building_description_residual",
            "needs_human_review",
            "Building description residual; likely semantic/fluency change, but not in approved token-safe packet.",
        )
    return (
        "residual_pending_apply_confirmed",
        "needs_human_review",
        "Residual real divergence not in approved token-safe packet; requires explicit review before apply.",
    )


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.final_state,
            state.state_group,
            state.review_state,
            state.apply_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            s.english_text,
            s.spanish_text,
            o.output_line_number,
            o.portuguese_text AS output_text,
            c.confirmed_text
        FROM segment_state_items state
        JOIN source_segments s ON s.id = state.segment_id
        JOIN output_segments o ON o.segment_id = state.segment_id
        JOIN segment_confirmations c ON c.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.final_state = 'pending_apply_confirmed'
          AND state.needs_output_apply = 1
        ORDER BY state.segment_id
        """,
        (SEGMENT_STATE_RUN_ID,),
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> None:
    with connect_readonly() as conn:
        run = dict(conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (SEGMENT_STATE_RUN_ID,)).fetchone())
        rows = fetch_rows(conn)

    records: list[dict[str, Any]] = []
    for row in rows:
        family, recommendation, note = classify(row)
        output_text = str(row["output_text"] or "")
        confirmed_text = str(row["confirmed_text"] or "")
        output_tokens = token_counts(output_text)
        confirmed_tokens = token_counts(confirmed_text)
        records.append(
            {
                "source": SOURCE,
                "segment_state_run_id": SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "output_line_number": row["output_line_number"],
                "final_state": row["final_state"],
                "review_state": row["review_state"],
                "apply_state": row["apply_state"],
                "needs_output_apply": int(row["needs_output_apply"] or 0),
                "confirmed_matches_output": int(row["confirmed_matches_output"] or 0),
                "family": family,
                "recommendation": recommendation,
                "note": note,
                "token_integrity_ok": output_tokens == confirmed_tokens,
                "output_tokens": output_tokens,
                "confirmed_tokens": confirmed_tokens,
                "english_text": row["english_text"],
                "spanish_text": row["spanish_text"],
                "output_text": output_text,
                "confirmed_text": confirmed_text,
            }
        )

    family_counts = Counter(record["family"] for record in records)
    recommendation_counts = Counter(record["recommendation"] for record in records)
    token_integrity_ok_count = sum(1 for record in records if record["token_integrity_ok"])
    token_integrity_failed_count = len(records) - token_integrity_ok_count
    immediately_apply_safe_count = sum(1 for record in records if record["recommendation"] == "ready_for_protected_apply")

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_remaining_pending_apply_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "run_finished_at": run.get("finished_at"),
        "global_pending_count": int(run.get("pending_count") or 0),
        "global_output_apply_pending_count": int(run.get("output_apply_pending_count") or 0),
        "record_count": len(records),
        "family_counts": dict(sorted(family_counts.items())),
        "recommendation_counts": dict(sorted(recommendation_counts.items())),
        "token_integrity_ok_count": token_integrity_ok_count,
        "token_integrity_failed_count": token_integrity_failed_count,
        "immediately_apply_safe_count": immediately_apply_safe_count,
        "human_or_guard_hold_count": sum(
            count
            for family, count in family_counts.items()
            if family in {"human_context_hold", "guard_blocked_dynamic_spanish_residue"}
        ),
        "holy_site_token_changing_count": family_counts.get("holy_site_token_changing_hold", 0),
        "residual_review_count": sum(
            count
            for family, count in family_counts.items()
            if family
            in {
                "building_description_residual",
                "event_localization_residual",
                "residual_pending_apply_confirmed",
                "dynamic_token_residual",
            }
        ),
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
            "Do not apply immediately. Review residual items by family; send holy-site/token-changing and dynamic getter guard cases to architecture/policy."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_run526_remaining_pending_apply_diagnostic"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}

    lines = [
        "Domain policy vote candidate - remaining pending apply diagnostic",
        f"Run: {SEGMENT_STATE_RUN_ID}",
        f"Records: {len(records)}",
        "",
        "Family counts:",
    ]
    for key, count in sorted(family_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Recommendation counts:"])
    for key, count in sorted(recommendation_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Representative records:"])
    for record in records:
        lines.extend(
            [
                f"- {record['segment_id']} | {record['family']} | {record['recommendation']}",
                f"  path: {record['relative_path']}:{record['output_line_number']}",
                f"  key: {record['source_key']}",
                f"  out: {short(record['output_text'])}",
                f"  confirmed: {short(record['confirmed_text'])}",
            ]
        )

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
