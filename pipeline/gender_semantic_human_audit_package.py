from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
COHORT_KEY = "gender_token_microagent_plus_semantic_review_router"
PRIMARY_FAMILIES = {"gender_token_microagent", "semantic_review_router"}

SELECT_RE = re.compile(r"Select_CString|SelectLocalization|SelectLocalizationIf", re.IGNORECASE)
ES_RE = re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)\b|\.Custom\('ES_[A-Za-z0-9_]+'\)", re.IGNORECASE)
GETTER_RE = re.compile(r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+")
CONCEPT_RE = re.compile(r"\[[^\]|]+[|][^\]]+\]|\$[^$]+\$")
SHORT_RE = re.compile(r"^.{0,120}$", re.DOTALL)
SPANISH_RE = re.compile(
    r"\b(?:el|la|los|las|una|uno|mucho|mucha|muchos|muchas|verdadero|verdadera|"
    r"seg[uú]n|probabilidad|mientras|debe|puede|ser[aá]|est[aá])\b",
    re.IGNORECASE,
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


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH fam AS (
            SELECT
                l.segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN l.issue_family = 'gender_token_microagent' THEN 1 ELSE 0 END) AS gender_count,
                SUM(CASE WHEN l.issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN l.issue_family NOT IN ('gender_token_microagent','semantic_review_router') THEN 1 ELSE 0 END) AS structural_count,
                GROUP_CONCAT(DISTINCT l.issue_family) AS issue_families,
                GROUP_CONCAT(DISTINCT l.issue_kind) AS issue_kinds
            FROM ml_issue_ledger_items l
            JOIN segment_state_items s ON s.segment_id = l.segment_id AND s.run_id = ?
            WHERE l.run_id = ?
              AND l.status = 'open'
              AND s.state_group = 'pending'
              AND s.is_closed = 0
              AND s.needs_output_apply = 0
              AND s.confirmed_matches_output = 1
            GROUP BY l.segment_id
            HAVING gender_count > 0 AND semantic_count > 0
        )
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.review_state,
            s.needs_human,
            s.priority_score,
            f.open_issue_count,
            f.structural_count,
            f.issue_families,
            f.issue_kinds,
            src.english_text,
            src.spanish_text,
            src.old_text,
            out.portuguese_text AS current_output_text
        FROM fam f
        JOIN segment_state_items s ON s.segment_id = f.segment_id AND s.run_id = ?
        LEFT JOIN source_segments src ON src.id = f.segment_id
        LEFT JOIN output_segments out ON out.segment_id = f.segment_id
        ORDER BY
            CASE WHEN f.structural_count = 0 THEN 0 ELSE 1 END,
            s.needs_human DESC,
            s.priority_score DESC,
            s.segment_id
        """,
        (segment_state_run_id, ledger_run_id, segment_state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("current_output_text") or "")
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "issue_families", "issue_kinds", "current_output_text")
    )
    markers: list[str] = []
    if SELECT_RE.search(text):
        markers.append("select_cstring")
    if ES_RE.search(haystack):
        markers.append("es_helper")
    if GETTER_RE.search(text):
        markers.append("scope_getter")
    if CONCEPT_RE.search(text):
        markers.append("concept_or_variable")
    if SHORT_RE.search(text):
        markers.append("short_surface")
    if SPANISH_RE.search(text):
        markers.append("possible_spanish_residue")
    if int(row.get("structural_count") or 0) > 0:
        markers.append("structural_overlap")

    if "es_helper" in markers or "select_cstring" in markers:
        audit_lane = "gender_dynamic_policy_required"
    elif "possible_spanish_residue" in markers and "structural_overlap" not in markers:
        audit_lane = "literal_residue_candidate_human_review"
    elif "short_surface" in markers and "structural_overlap" not in markers:
        audit_lane = "short_gender_semantic_human_review"
    elif "structural_overlap" in markers:
        audit_lane = "blocked_structural_overlap_human_context"
    else:
        audit_lane = "semantic_gender_context_review"

    return {
        "audit_lane": audit_lane,
        "markers": markers,
        "human_prompt": "review_only_no_apply: decide whether this needs policy, context, literal repair, or hold",
        "candidate_text": "",
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
    }


def sample_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    enriched = []
    for row in rows:
        decision = classify(row)
        item = {**row, **decision}
        buckets.setdefault(decision["audit_lane"], []).append(item)
        enriched.append(item)

    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for lane in sorted(buckets):
        for row in buckets[lane][: max(1, limit // max(1, len(buckets)))]:
            segment_id = int(row["segment_id"])
            if segment_id not in seen and len(selected) < limit:
                selected.append(row)
                seen.add(segment_id)
    for row in enriched:
        segment_id = int(row["segment_id"])
        if segment_id not in seen and len(selected) < limit:
            selected.append(row)
            seen.add(segment_id)
    return selected


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_gender_semantic_human_audit_package"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), base.with_suffix(".csv"), reports_dir() / f"{base.name}_summary.json"


def write_outputs(rows: list[dict[str, Any]], selected: list[dict[str, Any]], retarget_summary: str | None) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    txt_path, jsonl_path, csv_path, summary_path = output_paths()
    lane_counts = Counter(row["audit_lane"] for row in (classify(row) | row for row in rows))
    selected_lane_counts = Counter(row["audit_lane"] for row in selected)
    summary = {
        "schema_version": 1,
        "source": "gender_semantic_human_audit_package_v1",
        "cohort_key": COHORT_KEY,
        "retarget_summary": retarget_summary,
        "total_cohort_rows": len(rows),
        "sampled_rows": len(selected),
        "lane_counts": dict(sorted(lane_counts.items())),
        "selected_lane_counts": dict(sorted(selected_lane_counts.items())),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_reindex_recommended_now": False,
        "next_action": "human_review_package_before_any_discovery_or_apply",
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "audit_lane",
        "markers",
        "issue_families",
        "issue_kinds",
        "current_output_text",
        "human_prompt",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow({key: row.get(key) for key in fieldnames})
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Gender + semantic human audit package",
        f"cohort_key={COHORT_KEY}",
        f"total_cohort_rows={len(rows)}",
        f"sampled_rows={len(selected)}",
        "",
        "Lane counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(lane_counts.items()))
    lines.extend(
        [
            "",
            "Safety: read-only package; no discovery apply, no output/source edit, no lifecycle/reindex, no training.",
            "next_action=human_review_package_before_any_discovery_or_apply",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, csv_path, summary_path, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, default=EXPECTED_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=EXPECTED_LEDGER_RUN_ID)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--retarget-summary")
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    if args.retarget_summary:
        retarget = read_json(db.project_path(args.retarget_summary))
        if retarget.get("recommended_cohort_key") != COHORT_KEY:
            raise SystemExit("retarget cohort guard failed")

    with connect_readonly() as conn:
        rows = fetch_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    selected = sample_rows(rows, args.limit)
    txt_path, jsonl_path, csv_path, summary_path, summary = write_outputs(rows, selected, args.retarget_summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"csv={csv_path}")
    print(f"summary={summary_path}")
    print(f"total_cohort_rows={summary['total_cohort_rows']}")
    print(f"sampled_rows={summary['sampled_rows']}")
    print("lane_counts=" + json.dumps(summary["lane_counts"], ensure_ascii=False, sort_keys=True))
    print("next_action=" + summary["next_action"])


if __name__ == "__main__":
    main()
