from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_custom_localization_composition_audit_v1"
AUDIT_NAME = "custom_localization_full_coverage_composition_audit_v1"
PRODUCTION_RELEASE_ALLOWED = 0
DEFAULT_SCOPE_PREFIX = "custom_localization/%"

DOMAIN_CAUTION_FILES = {
    "custom_localization/ach_custom_loc_l_spanish.yml",
    "custom_localization/personality_quirks_l_spanish.yml",
    "custom_localization/signature_weapon_custom_loc_l_spanish.yml",
}
DOMAIN_CAUTION_KEYWORDS = {
    "adjective_khanal",
    "adjective_khaganal",
}


def latest_id(conn, table_name: str, where_sql: str = "1 = 1") -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return float(part) / float(total) * 100.0


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def report_paths(settings: dict[str, Any], partial_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_custom_localization_composition_audit_partial_run_{partial_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_composition_audit_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            audit_name TEXT NOT NULL,
            audit_status TEXT NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            scope_prefix TEXT NOT NULL,
            total_custom_segments INTEGER NOT NULL DEFAULT 0,
            full_coverage_segments INTEGER NOT NULL DEFAULT 0,
            partial_coverage_segments INTEGER NOT NULL DEFAULT 0,
            uncovered_segments INTEGER NOT NULL DEFAULT 0,
            recheck_candidate_count INTEGER NOT NULL DEFAULT 0,
            strong_recheck_candidate_count INTEGER NOT NULL DEFAULT 0,
            semantic_only_recheck_candidate_count INTEGER NOT NULL DEFAULT 0,
            domain_caution_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            readiness_counts_json TEXT,
            evidence_strength_counts_json TEXT,
            file_counts_json TEXT,
            issue_family_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_composition_audit_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            final_state TEXT,
            state_group TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            coverage_state TEXT NOT NULL,
            review_state TEXT NOT NULL,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            reviewed_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            coverage_ratio REAL NOT NULL DEFAULT 0,
            evidence_strength TEXT NOT NULL,
            readiness_status TEXT NOT NULL,
            recommended_next_step TEXT NOT NULL,
            potential_segment_recheck INTEGER NOT NULL DEFAULT 0,
            potential_close_after_recheck INTEGER NOT NULL DEFAULT 0,
            caution_flags_json TEXT,
            issue_families_json TEXT,
            covered_families_json TEXT,
            open_families_json TEXT,
            covered_agents_json TEXT,
            coverage_sources_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_custom_localization_composition_audit_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_custom_loc_composition_audit_items_run_status
        ON ml_issue_custom_localization_composition_audit_items(run_id, readiness_status, evidence_strength);

        CREATE INDEX IF NOT EXISTS idx_custom_loc_composition_audit_items_segment
        ON ml_issue_custom_localization_composition_audit_items(segment_id);
        """
    )


def latest_partial_coverage_run(conn, partial_run_id: int | None) -> dict[str, Any]:
    if partial_run_id is None:
        partial_run_id = latest_id(conn, "ml_issue_partial_coverage_runs", "finished_at IS NOT NULL")
    if partial_run_id is None:
        raise RuntimeError("No finished partial coverage run found.")
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (partial_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_run_id}")
    return dict(row)


def fetch_custom_segments(conn, *, partial_run_id: int, scope_prefix: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND relative_path LIKE ?
        ORDER BY coverage_state, relative_path, source_line_number, source_key
        """,
        (partial_run_id, scope_prefix),
    ).fetchall()
    return [dict(row) for row in rows]


def evidence_strength(coverage_sources: dict[str, Any]) -> str:
    has_semantic_pair = any(str(key).startswith("semantic_short_label_pair_checkpoint") for key in coverage_sources)
    has_custom_context = any(str(key).startswith("short_label_context_lane_checkpoint") for key in coverage_sources)
    if has_semantic_pair and has_custom_context:
        return "semantic_plus_custom_context"
    if has_semantic_pair:
        return "semantic_only"
    if has_custom_context:
        return "custom_context_only"
    if coverage_sources:
        return "other_coverage"
    return "no_coverage"


def caution_flags(row: dict[str, Any], strength: str) -> list[str]:
    flags: list[str] = []
    relative_path = str(row.get("relative_path") or "")
    source_key = str(row.get("source_key") or "")
    if relative_path in DOMAIN_CAUTION_FILES:
        flags.append("domain_file_requires_manual_context")
    if source_key.strip().lower() in DOMAIN_CAUTION_KEYWORDS:
        flags.append("sensitive_domain_key")
    if strength == "semantic_only" and str(row.get("coverage_state")) == "full":
        flags.append("semantic_only_no_custom_context_evidence")
    if int(row.get("is_closed") or 0):
        flags.append("already_closed")
    if int(row.get("blocked_issue_count") or 0) > 0:
        flags.append("blocked_issue_present")
    if int(row.get("open_issue_count") or 0) > 0:
        flags.append("open_issue_present")
    if str(row.get("coverage_state") or "") != "full":
        flags.append("not_full_coverage")
    return flags


def classify(row: dict[str, Any], strength: str, flags: list[str]) -> tuple[str, str, int, int]:
    coverage_state = str(row.get("coverage_state") or "")
    if int(row.get("is_closed") or 0):
        return "already_closed", "no_action", 0, 0
    if coverage_state == "none":
        return "not_ready_uncovered", "train_or_route_first_issue_family", 0, 0
    if int(row.get("blocked_issue_count") or 0) > 0:
        return "blocked_issue_present", "inspect_blocked_evidence_before_composition", 0, 0
    if int(row.get("open_issue_count") or 0) > 0:
        return "not_ready_partial_missing_issue", "train_remaining_open_issue_family", 0, 0
    if coverage_state != "full":
        return "not_ready_insufficient_coverage", "raise_issue_coverage_before_segment_recheck", 0, 0
    if "domain_file_requires_manual_context" in flags or "sensitive_domain_key" in flags:
        return "full_coverage_domain_caution_recheck", "domain_context_review_required", 1, 0
    if strength == "semantic_plus_custom_context":
        return "full_coverage_strong_recheck_candidate", "segment_composition_recheck", 1, 1
    if strength == "semantic_only":
        return "full_coverage_semantic_only_recheck_candidate", "validate_semantic_only_source_before_lifecycle", 1, 0
    return "full_coverage_other_recheck_candidate", "segment_composition_recheck", 1, 0


def build_rows(source_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Counter[str]]]:
    rows: list[dict[str, Any]] = []
    counters = {
        "readiness": Counter(),
        "strength": Counter(),
        "files": Counter(),
        "families": Counter(),
    }
    for source in source_rows:
        issue_families = parse_json_dict(source.get("issue_families_json"))
        covered_families = parse_json_dict(source.get("covered_families_json"))
        open_families = parse_json_dict(source.get("open_families_json"))
        covered_agents = parse_json_dict(source.get("covered_agents_json"))
        coverage_sources = parse_json_dict(source.get("coverage_sources_json"))
        strength = evidence_strength(coverage_sources)
        flags = caution_flags(source, strength)
        readiness, next_step, potential_recheck, potential_close = classify(source, strength, flags)

        counters["readiness"][readiness] += 1
        counters["strength"][strength] += 1
        counters["files"][str(source["relative_path"])] += 1
        for family, count in issue_families.items():
            counters["families"][str(family)] += int(count or 0)

        rows.append(
            {
                "partial_coverage_item_id": int(source["id"]),
                "ledger_run_id": int(source["ledger_run_id"]),
                "segment_state_run_id": int(source["segment_state_run_id"]),
                "segment_id": int(source["segment_id"]),
                "relative_path": str(source["relative_path"]),
                "source_key": str(source["source_key"]),
                "source_line_number": source.get("source_line_number"),
                "final_state": source.get("final_state") or "",
                "state_group": source.get("state_group") or "",
                "is_closed": int(source.get("is_closed") or 0),
                "coverage_state": str(source.get("coverage_state") or ""),
                "review_state": str(source.get("review_state") or ""),
                "total_issue_count": int(source.get("total_issue_count") or 0),
                "covered_issue_count": int(source.get("covered_issue_count") or 0),
                "reviewed_issue_count": int(source.get("reviewed_issue_count") or 0),
                "blocked_issue_count": int(source.get("blocked_issue_count") or 0),
                "open_issue_count": int(source.get("open_issue_count") or 0),
                "coverage_ratio": float(source.get("coverage_ratio") or 0.0),
                "evidence_strength": strength,
                "readiness_status": readiness,
                "recommended_next_step": next_step,
                "potential_segment_recheck": potential_recheck,
                "potential_close_after_recheck": potential_close,
                "caution_flags_json": json.dumps(flags, ensure_ascii=False, sort_keys=True),
                "issue_families_json": json.dumps(issue_families, ensure_ascii=False, sort_keys=True),
                "covered_families_json": json.dumps(covered_families, ensure_ascii=False, sort_keys=True),
                "open_families_json": json.dumps(open_families, ensure_ascii=False, sort_keys=True),
                "covered_agents_json": json.dumps(covered_agents, ensure_ascii=False, sort_keys=True),
                "coverage_sources_json": json.dumps(coverage_sources, ensure_ascii=False, sort_keys=True),
            }
        )
    return rows, counters


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    audit_run_id: int,
    partial_run: dict[str, Any],
    scope_prefix: str,
    rows: list[dict[str, Any]],
    counters: dict[str, Counter[str]],
) -> None:
    fields = [
        "readiness_status",
        "recommended_next_step",
        "evidence_strength",
        "potential_segment_recheck",
        "potential_close_after_recheck",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "final_state",
        "coverage_state",
        "total_issue_count",
        "covered_issue_count",
        "open_issue_count",
        "coverage_ratio",
        "caution_flags_json",
        "issue_families_json",
        "coverage_sources_json",
        "covered_agents_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    candidate_rows = [row for row in rows if int(row["potential_segment_recheck"])]
    close_after_review_rows = [row for row in rows if int(row["potential_close_after_recheck"])]
    lines = [
        "Issue custom localization composition audit",
        f"Rule version: {RULE_VERSION}",
        f"Audit run id: {audit_run_id}",
        f"Partial coverage run id: {partial_run['id']}",
        f"Ledger run id: {partial_run['ledger_run_id']}",
        f"Segment-state run id: {partial_run['segment_state_run_id']}",
        f"Scope prefix: {scope_prefix}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Custom localization issue segments: {len(rows):,}",
        f"- Full coverage segments: {counters['readiness']['full_coverage_strong_recheck_candidate'] + counters['readiness']['full_coverage_semantic_only_recheck_candidate'] + counters['readiness']['full_coverage_domain_caution_recheck'] + counters['readiness']['full_coverage_other_recheck_candidate']:,}",
        f"- Segment recheck candidates: {len(candidate_rows):,}",
        f"- Strong candidates after recheck: {counters['readiness']['full_coverage_strong_recheck_candidate']:,}",
        f"- Semantic-only recheck candidates: {counters['readiness']['full_coverage_semantic_only_recheck_candidate']:,}",
        f"- Domain caution recheck candidates: {counters['readiness']['full_coverage_domain_caution_recheck']:,}",
        f"- Potential close after recheck: {len(close_after_review_rows):,}",
        "",
        "Readiness:",
        *[f"- {key}: {value:,}" for key, value in counters["readiness"].most_common()],
        "",
        "Evidence strength:",
        *[f"- {key}: {value:,}" for key, value in counters["strength"].most_common()],
        "",
        "Top files:",
        *[f"- {key}: {value:,}" for key, value in counters["files"].most_common(20)],
        "",
        "Issue families in scope:",
        *[f"- {key}: {value:,}" for key, value in counters["families"].most_common()],
        "",
        "Interpretation:",
        "- `full_coverage_strong_recheck_candidate` has both semantic pair evidence and custom-context evidence.",
        "- `full_coverage_semantic_only_recheck_candidate` is promising but should be validated before lifecycle because it lacks custom-context file-profile evidence.",
        "- `full_coverage_domain_caution_recheck` is intentionally held behind domain review even with full issue coverage.",
        "- This audit does not close segments, create confirmations, promote lifecycle, or write output.",
        "",
        "Samples:",
    ]
    for row in candidate_rows[:50]:
        lines.append(
            (
                f"- {row['readiness_status']} | segment={row['segment_id']} | "
                f"{row['relative_path']}::{row['source_key']} | "
                f"strength={row['evidence_strength']} | flags={row['caution_flags_json']} | "
                f"sources={short(row['coverage_sources_json'], 160)}"
            )
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, partial_run_id: int | None = None, scope_prefix: str = DEFAULT_SCOPE_PREFIX) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        partial_run = latest_partial_coverage_run(conn, partial_run_id)
        source_rows = fetch_custom_segments(conn, partial_run_id=int(partial_run["id"]), scope_prefix=scope_prefix)
        rows, counters = build_rows(source_rows)

        txt_path, csv_path, jsonl_path = report_paths(settings, int(partial_run["id"]))
        now = db.utc_now()
        readiness = counters["readiness"]
        strength = counters["strength"]
        full_count = sum(
            readiness[key]
            for key in (
                "full_coverage_strong_recheck_candidate",
                "full_coverage_semantic_only_recheck_candidate",
                "full_coverage_domain_caution_recheck",
                "full_coverage_other_recheck_candidate",
            )
        )
        partial_count = sum(1 for row in rows if row["coverage_state"] == "partial")
        uncovered_count = sum(1 for row in rows if row["coverage_state"] == "none")
        recheck_count = sum(int(row["potential_segment_recheck"]) for row in rows)
        close_after_review_count = sum(int(row["potential_close_after_recheck"]) for row in rows)
        blocked_count = sum(
            readiness[key]
            for key in (
                "blocked_issue_present",
                "not_ready_partial_missing_issue",
                "not_ready_uncovered",
                "not_ready_insufficient_coverage",
            )
        )

        cur = conn.execute(
            """
            INSERT INTO ml_issue_custom_localization_composition_audit_runs (
                rule_version,
                audit_name,
                audit_status,
                partial_coverage_run_id,
                ledger_run_id,
                segment_state_run_id,
                scope_prefix,
                total_custom_segments,
                full_coverage_segments,
                partial_coverage_segments,
                uncovered_segments,
                recheck_candidate_count,
                strong_recheck_candidate_count,
                semantic_only_recheck_candidate_count,
                domain_caution_count,
                blocked_count,
                production_release_allowed,
                readiness_counts_json,
                evidence_strength_counts_json,
                file_counts_json,
                issue_family_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, 'shadow_audit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                AUDIT_NAME,
                int(partial_run["id"]),
                int(partial_run["ledger_run_id"]),
                int(partial_run["segment_state_run_id"]),
                scope_prefix,
                len(rows),
                full_count,
                partial_count,
                uncovered_count,
                recheck_count,
                readiness["full_coverage_strong_recheck_candidate"],
                readiness["full_coverage_semantic_only_recheck_candidate"],
                readiness["full_coverage_domain_caution_recheck"],
                blocked_count,
                json.dumps(dict(readiness), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(strength), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(counters["files"]), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(counters["families"]), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        audit_run_id = int(cur.lastrowid)

        conn.executemany(
            """
            INSERT INTO ml_issue_custom_localization_composition_audit_items (
                run_id,
                partial_coverage_run_id,
                partial_coverage_item_id,
                ledger_run_id,
                segment_state_run_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                final_state,
                state_group,
                is_closed,
                coverage_state,
                review_state,
                total_issue_count,
                covered_issue_count,
                reviewed_issue_count,
                blocked_issue_count,
                open_issue_count,
                coverage_ratio,
                evidence_strength,
                readiness_status,
                recommended_next_step,
                potential_segment_recheck,
                potential_close_after_recheck,
                caution_flags_json,
                issue_families_json,
                covered_families_json,
                open_families_json,
                covered_agents_json,
                coverage_sources_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    audit_run_id,
                    int(partial_run["id"]),
                    row["partial_coverage_item_id"],
                    row["ledger_run_id"],
                    row["segment_state_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["final_state"],
                    row["state_group"],
                    row["is_closed"],
                    row["coverage_state"],
                    row["review_state"],
                    row["total_issue_count"],
                    row["covered_issue_count"],
                    row["reviewed_issue_count"],
                    row["blocked_issue_count"],
                    row["open_issue_count"],
                    row["coverage_ratio"],
                    row["evidence_strength"],
                    row["readiness_status"],
                    row["recommended_next_step"],
                    row["potential_segment_recheck"],
                    row["potential_close_after_recheck"],
                    row["caution_flags_json"],
                    row["issue_families_json"],
                    row["covered_families_json"],
                    row["open_families_json"],
                    row["covered_agents_json"],
                    row["coverage_sources_json"],
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        audit_run_id=audit_run_id,
        partial_run=partial_run,
        scope_prefix=scope_prefix,
        rows=rows,
        counters=counters,
    )

    print("[issue_custom_localization_composition_audit] Audit generated")
    print(f"[issue_custom_localization_composition_audit] Audit run id: {audit_run_id}")
    print(f"[issue_custom_localization_composition_audit] Partial coverage run id: {partial_run['id']}")
    print(f"[issue_custom_localization_composition_audit] Custom segments: {len(rows):,}")
    print(f"[issue_custom_localization_composition_audit] Full coverage: {full_count:,}")
    print(f"[issue_custom_localization_composition_audit] Recheck candidates: {recheck_count:,}")
    print(f"[issue_custom_localization_composition_audit] Potential close after recheck: {close_after_review_count:,}")
    print(f"[issue_custom_localization_composition_audit] Report: {txt_path}")
    return {
        "audit_run_id": audit_run_id,
        "partial_coverage_run_id": int(partial_run["id"]),
        "custom_segments": len(rows),
        "full_coverage": full_count,
        "recheck_candidates": recheck_count,
        "potential_close_after_recheck": close_after_review_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit custom_localization high/full issue coverage for segment-level composition opportunities.")
    parser.add_argument("--partial-run-id", type=int, default=None)
    parser.add_argument("--scope-prefix", default=DEFAULT_SCOPE_PREFIX)
    args = parser.parse_args()
    main(partial_run_id=args.partial_run_id, scope_prefix=args.scope_prefix)
