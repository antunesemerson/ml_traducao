from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "medium_dynamic_light_getter_perspective_notes_update_v1"
AGENT_KEY = "medium_dynamic_light_getter_perspective_omitted_policy"
REVIEW_SUMMARY = Path("reports/20260630_003933_488544_medium_dynamic_light_getter_perspective_omitted_review_summary.json")
REVIEW_JSONL = Path("reports/20260630_003933_488544_medium_dynamic_light_getter_perspective_omitted_review.jsonl")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
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
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_review(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if summary.get("mode") != "read_only_getter_perspective_omitted_review":
        raise SystemExit("review summary mode guard failed")
    if summary.get("agent_key") != AGENT_KEY:
        raise SystemExit("review agent_key guard failed")
    if int(summary.get("review_count") or 0) != 5 or len(rows) != 5:
        raise SystemExit("review_count guard failed")
    if int(summary.get("needs_human_packet_count", -1)) != 0:
        raise SystemExit("needs_human_packet_count guard failed")
    if int(summary.get("output_preserves_meaning_count") or 0) != 5:
        raise SystemExit("output_preserves_meaning_count guard failed")
    for key in ("candidate_generation_count", "apply_output_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
        if int(summary.get(key) or 0) != 0:
            raise SystemExit(f"{key} guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")


def fetch_record(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing registry agent: {AGENT_KEY}")
    return dict(row)


def build_notes(existing: dict[str, Any], review_summary: dict[str, Any], review_rows: list[dict[str, Any]]) -> dict[str, Any]:
    notes = json.loads(existing.get("notes_json") or "{}")
    for key in ("candidate_generation_allowed", "auto_apply_allowed", "lifecycle_allowed", "production_release_allowed"):
        if notes.get(key) is not False:
            raise SystemExit(f"existing {key} guard failed")
    notes.update(
        {
            "last_policy_review_source": SOURCE,
            "last_policy_review_summary": str(REVIEW_SUMMARY),
            "last_policy_review_jsonl": str(REVIEW_JSONL),
            "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            "review_count": int(review_summary["review_count"]),
            "omission_role_counts": review_summary.get("omission_role_counts"),
            "operational_subtype_counts": review_summary.get("operational_subtype_counts"),
            "allowed_read_only_classifications": {
                "subject_pronoun_omitted_fluency_ok": {
                    "count": 1,
                    "meaning": "PT-BR null subject is acceptable when the verb makes the subject recoverable.",
                    "candidate_generation_allowed": False,
                },
                "possessive_lexicalized_output_ok": {
                    "count": 3,
                    "meaning": "PT-BR non-gendered possessive lexicalization such as seu/sua may preserve meaning without runtime gender getter.",
                    "candidate_generation_allowed": False,
                },
            },
            "false_positive_guards": {
                "primary_getter_present_possessive_rephrased": {
                    "count": 1,
                    "meaning": "Do not flag omitted perspective when the primary getter is present and only a possessive surface is rephrased.",
                    "candidate_generation_allowed": False,
                }
            },
            "human_packet_recommended_now": False,
            "policy_notes_recommendation": review_summary.get("single_operational_recommendation"),
            "review_segment_ids": sorted(int(row["segment_id"]) for row in review_rows),
            "candidate_generation_allowed": False,
            "auto_apply_allowed": False,
            "lifecycle_allowed": False,
            "production_release_allowed": False,
            "segment_state_allowed": False,
            "reindex_allowed": False,
            "full_production_allowed": False,
        }
    )
    return notes


def validate_updated(conn: sqlite3.Connection) -> dict[str, Any]:
    row = fetch_record(conn)
    notes = json.loads(row.get("notes_json") or "{}")
    return {
        "exists": True,
        "agent_key": row.get("agent_key"),
        "operational_state": row.get("operational_state"),
        "decision_role": row.get("decision_role"),
        "parent_agent_key": row.get("parent_agent_key"),
        "candidate_generation_allowed": bool(notes.get("candidate_generation_allowed")),
        "auto_apply_allowed": bool(notes.get("auto_apply_allowed")),
        "lifecycle_allowed": bool(notes.get("lifecycle_allowed")),
        "production_release_allowed": bool(notes.get("production_release_allowed")),
        "allowed_read_only_classifications": notes.get("allowed_read_only_classifications"),
        "false_positive_guards": notes.get("false_positive_guards"),
        "human_packet_recommended_now": bool(notes.get("human_packet_recommended_now")),
    }


def write_reports(mode: str, updated: int, validation: dict[str, Any], notes: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_{AGENT_KEY}_notes_update_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "agent_key": AGENT_KEY,
        "updated": updated,
        "review_summary": str(REVIEW_SUMMARY),
        "review_jsonl": str(REVIEW_JSONL),
        "registry_validation": validation,
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    jsonl_path.write_text(json.dumps({"agent_key": AGENT_KEY, "notes_json": notes}, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "medium_dynamic_light getter perspective notes update",
                "",
                f"mode: {mode}",
                f"agent_key: {AGENT_KEY}",
                f"updated: {updated}",
                "",
                "allowed_read_only_classifications:",
                "- subject_pronoun_omitted_fluency_ok: 1",
                "- possessive_lexicalized_output_ok: 3",
                "",
                "false_positive_guards:",
                "- primary_getter_present_possessive_rephrased: 1",
                "",
                "guards:",
                "- candidate_generation: not_run",
                "- apply: not_run",
                "- lifecycle: not_run",
                "- segment_state: not_run",
                "- reindex: not_run",
                "- full_production: not_run",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    review_summary = read_json(REVIEW_SUMMARY)
    review_rows = read_jsonl(REVIEW_JSONL)
    validate_review(review_summary, review_rows)
    updated = 0
    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            conn.row_factory = sqlite3.Row
            record = fetch_record(conn)
            notes = build_notes(record, review_summary, review_rows)
            conn.execute(
                "UPDATE ml_agent_registry SET notes_json = ?, updated_at = ? WHERE agent_key = ?",
                (json.dumps(notes, ensure_ascii=False, sort_keys=True), db.utc_now(), AGENT_KEY),
            )
            updated = conn.total_changes
            conn.commit()
            validation = validate_updated(conn)
    else:
        with connect_readonly() as conn:
            record = fetch_record(conn)
            notes = build_notes(record, review_summary, review_rows)
            validation = validate_updated(conn)
    write_reports("apply" if args.apply else "dry_run", updated, validation, notes)


if __name__ == "__main__":
    main()
