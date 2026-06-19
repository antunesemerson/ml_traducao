from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import issue_review_queue


RULE_VERSION = "issue_custom_localization_composition_recheck_queue_v1"
QUEUE_NAME = "custom_localization_composition_recheck_queue_v1"
QUEUE_STRATEGY = "custom_localization_composition_recheck"
AGENT_KEY = "composition_coordinator_v1"
ISSUE_FAMILY = "custom_localization_composition_recheck"
ISSUE_KIND = "whole_segment_composition_recheck"
TARGET_READINESS = "full_coverage_strong_recheck_candidate"
DEFAULT_LIMIT = 25
DEFAULT_PER_FILE = 15
PRODUCTION_RELEASE_ALLOWED = 0

DECISION_OPTIONS = [
    "composition_ready",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
    "false_positive_reopen",
]


def latest_audit_run_id(conn, audit_run_id: int | None) -> int:
    if audit_run_id is not None:
        return audit_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_custom_localization_composition_audit_runs
        WHERE audit_status = 'shadow_audit'
          AND strong_recheck_candidate_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No custom localization composition audit run with strong candidates found.")
    return int(row["id"])


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def file_profile(relative_path: str) -> str:
    stem = Path(relative_path).stem
    if stem.endswith("_l_spanish"):
        stem = stem.removesuffix("_l_spanish")
    return stem


def report_paths(settings: dict[str, Any], audit_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_custom_localization_composition_recheck_queue_audit_run_{audit_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_composition_recheck_queue_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            queue_name TEXT NOT NULL,
            queue_strategy TEXT NOT NULL,
            audit_run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            review_queue_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            limit_count INTEGER NOT NULL DEFAULT 0,
            per_file INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            file_counts_json TEXT,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_custom_localization_composition_recheck_queue_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            audit_run_id INTEGER NOT NULL,
            audit_item_id INTEGER NOT NULL,
            review_queue_run_id INTEGER NOT NULL,
            review_queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            readiness_status TEXT NOT NULL,
            evidence_strength TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            suggested_decision TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            caution_flags_json TEXT,
            coverage_sources_json TEXT,
            covered_agents_json TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_custom_localization_composition_recheck_queue_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_custom_loc_comp_recheck_items_run
        ON ml_issue_custom_localization_composition_recheck_queue_items(run_id, queue_bucket);

        CREATE INDEX IF NOT EXISTS idx_custom_loc_comp_recheck_items_segment
        ON ml_issue_custom_localization_composition_recheck_queue_items(segment_id);
        """
    )


def fetch_audit_run(conn, audit_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_custom_localization_composition_audit_runs
        WHERE id = ?
        """,
        (audit_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Custom localization composition audit run not found: {audit_run_id}")
    return dict(row)


def representative_ledger_item_id(conn, *, ledger_run_id: int, segment_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
        ORDER BY
          CASE issue_family
            WHEN 'semantic_review_router' THEN 0
            WHEN 'short_label_style_microagent' THEN 1
            ELSE 2
          END,
          id
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return int(row["id"]) if row else None


def fetch_candidates(conn, *, audit_run: dict[str, Any]) -> list[dict[str, Any]]:
    ledger_run_id = int(audit_run["ledger_run_id"])
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_custom_localization_composition_audit_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.id = (
            SELECT c.id
            FROM segment_confirmations c
            WHERE c.segment_id = item.segment_id
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT 1
        )
        WHERE item.run_id = ?
          AND item.readiness_status = ?
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              JOIN ml_issue_review_queue_runs queue_run ON queue_run.id = queued.run_id
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = ?
                AND queue_run.queue_strategy = ?
                AND queued.review_status NOT IN ('superseded', 'stale_superseded')
          )
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (int(audit_run["id"]), TARGET_READINESS, AGENT_KEY, QUEUE_STRATEGY),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        ledger_item_id = representative_ledger_item_id(
            conn,
            ledger_run_id=ledger_run_id,
            segment_id=int(payload["segment_id"]),
        )
        if ledger_item_id is None:
            continue
        audit_item_id = int(payload["id"])
        profile = file_profile(str(payload["relative_path"]))
        coverage_sources = parse_json_dict(payload.get("coverage_sources_json"))
        covered_agents = parse_json_dict(payload.get("covered_agents_json"))
        caution_flags = json.loads(payload.get("caution_flags_json") or "[]")
        evidence = {
            "source": "ml_issue_custom_localization_composition_audit",
            "audit_run_id": int(audit_run["id"]),
            "audit_item_id": audit_item_id,
            "partial_coverage_run_id": int(audit_run["partial_coverage_run_id"]),
            "ledger_run_id": ledger_run_id,
            "segment_state_run_id": int(audit_run["segment_state_run_id"]),
            "recheck_goal": "Validate whether fully covered issue families jointly close the whole custom_localization segment.",
            "recheck_scope": "whole_segment_composition_recheck",
            "readiness_status": payload["readiness_status"],
            "evidence_strength": payload["evidence_strength"],
            "file_profile": profile,
            "total_issue_count": int(payload["total_issue_count"] or 0),
            "covered_issue_count": int(payload["covered_issue_count"] or 0),
            "open_issue_count": int(payload["open_issue_count"] or 0),
            "coverage_ratio": float(payload["coverage_ratio"] or 0.0),
            "coverage_sources": coverage_sources,
            "covered_agents": covered_agents,
            "caution_flags": caution_flags,
            "required_checks": [
                "semantic_equivalence_against_english_reference",
                "pt_br_fluency_in_game_fragment_context",
                "no_residual_spanish_visible_text",
                "ck3_token_placeholder_integrity",
                "no_manual_exception_violation",
                "confirm_whole_segment_not_only_individual_issues",
            ],
        }
        payload.update(
            {
                "representative_ledger_item_id": ledger_item_id,
                "audit_item_id": audit_item_id,
                "queue_bucket": f"strong_custom_context:{profile}",
                "suggested_decision": "composition_ready",
                "priority_score": priority_score(payload),
                "issue_family": ISSUE_FAMILY,
                "issue_kind": ISSUE_KIND,
                "agent_key": AGENT_KEY,
                "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                "evidence_text": (
                    "Whole-segment custom_localization composition recheck: "
                    f"{payload['covered_issue_count']}/{payload['total_issue_count']} issues covered; "
                    f"strength={payload['evidence_strength']}; file_profile={profile}; "
                    f"sources={', '.join(sorted(coverage_sources)[:4])}."
                ),
                "id": ledger_item_id,
            }
        )
        candidates.append(payload)
    return candidates


def priority_score(row: dict[str, Any]) -> float:
    score = 1000.0
    score += float(row.get("coverage_ratio") or 0.0) * 500
    score += int(row.get("covered_issue_count") or 0) * 100
    score += len(parse_json_dict(row.get("coverage_sources_json"))) * 25
    profile = file_profile(str(row.get("relative_path") or ""))
    if profile in {"regional_custom_loc", "pet_custom_loc"}:
        score += 40
    score += int(row.get("segment_id") or 0) % 29 / 100
    return round(score, 4)


def select_rows(candidates: list[dict[str, Any]], *, limit: int, per_file: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[str(row["relative_path"])].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (-float(row["priority_score"]), int(row["source_line_number"] or 0), row["source_key"]))

    selected: list[dict[str, Any]] = []
    selected_segments: set[int] = set()
    file_order = sorted(grouped, key=lambda path: (-len(grouped[path]), path))
    for index in range(per_file):
        for path in file_order:
            rows = grouped[path]
            if index >= len(rows):
                continue
            if len(selected) >= limit:
                break
            row = rows[index]
            segment_id = int(row["segment_id"])
            if segment_id in selected_segments:
                continue
            selected.append(row)
            selected_segments.add(segment_id)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for row in sorted(candidates, key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"])):
            if len(selected) >= limit:
                break
            segment_id = int(row["segment_id"])
            if segment_id in selected_segments:
                continue
            selected.append(row)
            selected_segments.add(segment_id)
    return selected


def insert_review_queue_run(
    conn,
    *,
    audit_run: dict[str, Any],
    limit: int,
    per_file: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    return int(
        conn.execute(
            """
            INSERT INTO ml_issue_review_queue_runs (
                rule_version,
                ledger_run_id,
                agent_key,
                issue_family,
                queue_strategy,
                limit_count,
                per_bucket,
                selected_count,
                open_count,
                reviewed_count,
                bucket_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                int(audit_run["ledger_run_id"]),
                AGENT_KEY,
                ISSUE_FAMILY,
                QUEUE_STRATEGY,
                limit,
                per_file,
                len(selected),
                len(selected),
                json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                now,
                now,
                now,
            ),
        ).lastrowid
    )


def fetch_queue_items(conn, *, review_queue_run_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
        ORDER BY id
        """,
        (review_queue_run_id,),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def insert_custom_queue_run(
    conn,
    *,
    audit_run: dict[str, Any],
    review_queue_run_id: int,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
    per_file: int,
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    file_counts = Counter(row["relative_path"] for row in selected)
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    return int(
        conn.execute(
            """
            INSERT INTO ml_issue_custom_localization_composition_recheck_queue_runs (
                rule_version,
                queue_name,
                queue_strategy,
                audit_run_id,
                partial_coverage_run_id,
                ledger_run_id,
                segment_state_run_id,
                review_queue_run_id,
                candidate_count,
                selected_count,
                open_count,
                reviewed_count,
                limit_count,
                per_file,
                production_release_allowed,
                file_counts_json,
                bucket_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                QUEUE_NAME,
                QUEUE_STRATEGY,
                int(audit_run["id"]),
                int(audit_run["partial_coverage_run_id"]),
                int(audit_run["ledger_run_id"]),
                int(audit_run["segment_state_run_id"]),
                review_queue_run_id,
                len(candidates),
                len(selected),
                len(selected),
                limit,
                per_file,
                json.dumps(dict(file_counts.most_common()), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                now,
                now,
                now,
            ),
        ).lastrowid
    )


def insert_custom_queue_items(
    conn,
    *,
    run_id: int,
    audit_run: dict[str, Any],
    review_queue_run_id: int,
    selected: list[dict[str, Any]],
    queue_items_by_segment: dict[int, dict[str, Any]],
) -> None:
    now = db.utc_now()
    payload = []
    for row in selected:
        segment_id = int(row["segment_id"])
        queue_item = queue_items_by_segment.get(segment_id)
        if queue_item is None:
            raise RuntimeError(f"Missing review queue item for segment {segment_id}")
        payload.append(
            (
                run_id,
                int(audit_run["id"]),
                int(row["audit_item_id"]),
                review_queue_run_id,
                int(queue_item["id"]),
                int(audit_run["ledger_run_id"]),
                int(row["representative_ledger_item_id"]),
                segment_id,
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["readiness_status"],
                row["evidence_strength"],
                row["queue_bucket"],
                row["suggested_decision"],
                row["priority_score"],
                row["caution_flags_json"],
                row["coverage_sources_json"],
                row["covered_agents_json"],
                row["evidence_json"],
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO ml_issue_custom_localization_composition_recheck_queue_items (
            run_id,
            audit_run_id,
            audit_item_id,
            review_queue_run_id,
            review_queue_item_id,
            ledger_run_id,
            ledger_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            readiness_status,
            evidence_strength,
            queue_bucket,
            suggested_decision,
            priority_score,
            caution_flags_json,
            coverage_sources_json,
            covered_agents_json,
            evidence_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    custom_queue_run_id: int,
    review_queue_run_id: int,
    audit_run: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    queue_items_by_segment: dict[int, dict[str, Any]],
    limit: int,
    per_file: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    file_counts = Counter(row["relative_path"] for row in selected)
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    fieldnames = [
        "queue_item_id",
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "evidence_strength",
        "caution_flags_json",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in selected:
            queue_item = queue_items_by_segment[int(row["segment_id"])]
            writer.writerow(
                {
                    **row,
                    "queue_item_id": queue_item["id"],
                    "queue_run_id": review_queue_run_id,
                    "ledger_item_id": row["representative_ledger_item_id"],
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            queue_item = queue_items_by_segment[int(row["segment_id"])]
            payload = {
                "queue_item_id": int(queue_item["id"]),
                "queue_run_id": review_queue_run_id,
                "custom_queue_run_id": custom_queue_run_id,
                "audit_run_id": int(audit_run["id"]),
                "ledger_item_id": int(row["representative_ledger_item_id"]),
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": AGENT_KEY,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "decision_options": DECISION_OPTIONS,
                "evidence": json.loads(row["evidence_json"]),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            queue_item = queue_items_by_segment[int(row["segment_id"])]
            payload = {
                "queue_item_id": int(queue_item["id"]),
                "queue_run_id": review_queue_run_id,
                "ledger_item_id": int(row["representative_ledger_item_id"]),
                "segment_id": int(row["segment_id"]),
                "decision": "",
                "decision_options": DECISION_OPTIONS,
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue custom localization composition recheck queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue name: {QUEUE_NAME}",
        f"Custom queue run id: {custom_queue_run_id}",
        f"Review queue run id: {review_queue_run_id}",
        f"Audit run id: {audit_run['id']}",
        f"Partial coverage run id: {audit_run['partial_coverage_run_id']}",
        f"Ledger run id: {audit_run['ledger_run_id']}",
        f"Segment-state run id: {audit_run['segment_state_run_id']}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Selection:",
        f"- Candidates available: {len(candidates):,}",
        f"- Selected: {len(selected):,}",
        f"- Limit: {limit:,}",
        f"- Per file: {per_file:,}",
        "",
        "Files:",
        *[f"- {path}: {count:,}" for path, count in file_counts.most_common()],
        "",
        "Buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Review guidance:",
        "- Review the whole segment, not only the individual issue evidence.",
        "- Use `composition_ready` only when English meaning, current confirmed text, PT-BR fluency, and CK3 structure are all safe together.",
        "- Use `needs_repair` when the current text needs a concrete correction.",
        "- Use `needs_domain_context` when the text may be correct but depends on in-game context or neighboring localization.",
        "- Use `needs_new_microagent` when failure suggests a reusable missing specialist.",
        "- Do not apply output from this queue; it is evidence for the learning front.",
        "",
        "Samples:",
    ]
    for row in selected[:30]:
        queue_item = queue_items_by_segment[int(row["segment_id"])]
        lines.append(
            (
                f"- queue_item={queue_item['id']} | segment={row['segment_id']} | "
                f"{row['queue_bucket']} | {row['relative_path']}::{row['source_key']} | "
                f"confirmed={short(row.get('confirmed_text'), 120)}"
            )
        )
        lines.append(f"  evidence: {short(row.get('evidence_text'), 220)}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, audit_run_id: int | None = None, limit: int = DEFAULT_LIMIT, per_file: int = DEFAULT_PER_FILE) -> dict[str, Any]:
    settings = db.load_settings()
    paths: tuple[Path, Path, Path, Path] | None = None
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_audit_run_id = latest_audit_run_id(conn, audit_run_id)
        audit_run = fetch_audit_run(conn, selected_audit_run_id)
        candidates = fetch_candidates(conn, audit_run=audit_run)
        selected = select_rows(candidates, limit=limit, per_file=per_file)
        paths = report_paths(settings, selected_audit_run_id)
        review_queue_run_id = insert_review_queue_run(
            conn,
            audit_run=audit_run,
            limit=limit,
            per_file=per_file,
            selected=selected,
            paths=paths,
        )
        issue_review_queue.insert_queue_items(conn, review_queue_run_id, int(audit_run["ledger_run_id"]), selected)
        queue_items_by_segment = fetch_queue_items(conn, review_queue_run_id=review_queue_run_id)
        custom_queue_run_id = insert_custom_queue_run(
            conn,
            audit_run=audit_run,
            review_queue_run_id=review_queue_run_id,
            candidates=candidates,
            selected=selected,
            limit=limit,
            per_file=per_file,
            paths=paths,
        )
        insert_custom_queue_items(
            conn,
            run_id=custom_queue_run_id,
            audit_run=audit_run,
            review_queue_run_id=review_queue_run_id,
            selected=selected,
            queue_items_by_segment=queue_items_by_segment,
        )
        conn.commit()

    assert paths is not None
    write_outputs(
        paths=paths,
        custom_queue_run_id=custom_queue_run_id,
        review_queue_run_id=review_queue_run_id,
        audit_run=audit_run,
        candidates=candidates,
        selected=selected,
        queue_items_by_segment=queue_items_by_segment,
        limit=limit,
        per_file=per_file,
    )

    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_custom_localization_composition_recheck_queue] Queue generated")
    print(f"[issue_custom_localization_composition_recheck_queue] Custom queue run id: {custom_queue_run_id}")
    print(f"[issue_custom_localization_composition_recheck_queue] Review queue run id: {review_queue_run_id}")
    print(f"[issue_custom_localization_composition_recheck_queue] Audit run id: {audit_run['id']}")
    print(f"[issue_custom_localization_composition_recheck_queue] Candidates: {len(candidates):,}")
    print(f"[issue_custom_localization_composition_recheck_queue] Selected: {len(selected):,}")
    print(f"[issue_custom_localization_composition_recheck_queue] Report: {txt_path}")
    print(f"[issue_custom_localization_composition_recheck_queue] Decisions template: {decisions_template_path}")
    return {
        "custom_queue_run_id": custom_queue_run_id,
        "review_queue_run_id": review_queue_run_id,
        "audit_run_id": int(audit_run["id"]),
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a small whole-segment composition recheck queue for strong custom_localization candidates.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--per-file", type=int, default=DEFAULT_PER_FILE)
    args = parser.parse_args()
    main(audit_run_id=args.audit_run_id, limit=args.limit, per_file=args.per_file)
