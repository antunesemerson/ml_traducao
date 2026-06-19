from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "parallel_review_loop_v1"
VALID_LABELS = {
    "correct",
    "minor_fix",
    "major_fix",
    "residual_spanish",
    "structure_error",
    "semantic_error",
    "rejected_suggestion",
    "token_mismatch",
    "rejected",
    "contextual_exception",
}
POSITIVE_LABELS = {"correct", "minor_fix", "major_fix"}
CONTEXTUAL_LABELS = {"contextual_exception"}
EDIT_LABELS = {"minor_fix", "major_fix"}
NEGATIVE_LABELS = VALID_LABELS - POSITIVE_LABELS - CONTEXTUAL_LABELS
SECTION_CONFIG = {
    "priority": {
        "title": "Priority review samples",
        "focus_group": "priority_review",
        "where": "m.final_action IN ('needs_human', 'needs_autofix', 'blocked_structure')",
        "order": """
            CASE m.final_action
                WHEN 'blocked_structure' THEN 0
                WHEN 'needs_autofix' THEN 1
                ELSE 2
            END,
            m.high_issue_count DESC,
            m.model_safe_probability DESC,
            m.segment_id
        """,
    },
    "deterministic": {
        "title": "Deterministic override samples",
        "focus_group": "deterministic_override",
        "where": "m.deterministic_blocked = 1",
        "order": "m.model_safe_probability DESC, m.segment_id",
    },
    "high_safe": {
        "title": "High-safe-probability review samples",
        "focus_group": "high_safe_review",
        "where": "m.final_action <> 'auto_safe' AND m.model_safe_probability >= 0.80",
        "order": "m.model_safe_probability DESC, m.issue_count DESC, m.segment_id",
    },
    "clean_auto_safe": {
        "title": "Clean auto-safe samples",
        "focus_group": "clean_auto_safe",
        "where": "m.final_action = 'auto_safe' AND m.issue_count = 0 AND m.token_status = 'ok'",
        "order": "m.model_safe_probability DESC, m.segment_id",
    },
    "high_safe_non_names": {
        "title": "High-safe non-name frontier samples",
        "focus_group": "high_safe_non_names",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.model_safe_probability >= 0.90
            AND m.issue_count = 0
            AND m.token_status = 'ok'
            AND m.relative_path NOT LIKE 'names/%'
            AND m.relative_path NOT LIKE 'dynasties/%'
        """,
        "order": "m.model_safe_probability DESC, m.segment_id",
    },
    "coverage_recovery": {
        "title": "Clean coverage-recovery frontier samples",
        "focus_group": "coverage_recovery",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.model_safe_probability >= 0.88
            AND m.model_safe_probability < 0.94
            AND m.issue_count = 0
            AND m.high_issue_count = 0
            AND m.token_status = 'ok'
            AND m.relative_path NOT IN ('core_l_spanish.yml', 'game_concepts_l_spanish.yml', 'titles_l_spanish.yml')
            AND m.candidate_text NOT LIKE '%Select_CString%'
            AND m.candidate_text NOT LIKE '%LocalPlayerString%'
            AND m.candidate_text NOT LIKE '%SelectLocalization%'
            AND m.candidate_text NOT LIKE '%Custom(''ES_%'
        """,
        "order": "m.model_safe_probability DESC, m.word_count ASC, m.segment_id",
    },
    "coverage_recovery_clean": {
        "title": "Clean non-sensitive coverage-recovery samples",
        "focus_group": "coverage_recovery_clean",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.model_safe_probability >= 0.88
            AND m.model_safe_probability < 0.96
            AND m.issue_count = 0
            AND m.high_issue_count = 0
            AND m.token_status = 'ok'
            AND m.relative_path NOT IN ('core_l_spanish.yml', 'game_concepts_l_spanish.yml', 'titles_l_spanish.yml')
            AND m.relative_path NOT LIKE 'names/%'
            AND m.relative_path NOT LIKE 'dynasties/%'
            AND m.candidate_text NOT LIKE '%Select_CString%'
            AND m.candidate_text NOT LIKE '%LocalPlayerString%'
            AND m.candidate_text NOT LIKE '%SelectLocalization%'
            AND m.candidate_text NOT LIKE '%Custom(''ES_%'
            AND m.candidate_text NOT LIKE '%Gran %'
            AND m.candidate_text NOT LIKE '% Oeste%'
            AND m.candidate_text NOT LIKE '% Este%'
            AND m.candidate_text NOT LIKE '% y %'
            AND m.candidate_text NOT LIKE '%El Cairo%'
            AND m.candidate_text NOT LIKE '%El Pireo%'
            AND m.candidate_text NOT LIKE '%Qum El Aoiun%'
        """,
        "order": "m.model_safe_probability DESC, m.word_count ASC, m.segment_id",
    },
    "conditional_negative": {
        "title": "Conditional residual-language risk samples",
        "focus_group": "conditional_negative",
        "where": """
            m.final_action IN ('needs_autofix', 'needs_human')
            AND (
                m.candidate_text LIKE '%Select_CString%'
                OR m.candidate_text LIKE '%LocalPlayerString%'
                OR m.candidate_text LIKE '%SelectLocalization%'
            )
        """,
        "order": "m.model_safe_probability DESC, m.issue_count DESC, m.segment_id",
    },
    "sensitive_core": {
        "title": "Sensitive core/concept/title samples",
        "focus_group": "sensitive_core",
        "where": """
            m.final_action <> 'auto_safe'
            AND (
                m.relative_path = 'core_l_spanish.yml'
                OR m.relative_path = 'game_concepts_l_spanish.yml'
                OR m.relative_path = 'titles_l_spanish.yml'
            )
        """,
        "order": "m.model_safe_probability DESC, m.issue_count DESC, m.segment_id",
    },
    "exact_english_visible": {
        "title": "Exact-English visible text review samples",
        "focus_group": "exact_english_visible",
        "where": """
            m.deterministic_blocked = 1
            AND m.reasons_json LIKE '%RISK_EXACT_ENGLISH_VISIBLE%'
            AND m.issue_count = 0
            AND m.token_status = 'ok'
        """,
        "order": """
            CASE
                WHEN m.relative_path IN ('traits_l_spanish.yml', 'nicknames_l_spanish.yml') THEN 0
                WHEN m.relative_path LIKE 'culture/%' THEN 1
                ELSE 2
            END,
            m.model_safe_probability DESC,
            m.segment_id
        """,
    },
    "title_adjective_frontier": {
        "title": "Title adjective frontier samples",
        "focus_group": "title_adjective_frontier",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.relative_path IN ('titles_l_spanish.yml', 'titles_cultural_names_l_spanish.yml')
            AND m.source_key LIKE '%_adj'
            AND m.model_safe_probability >= 0.80
            AND m.issue_count = 0
            AND m.token_status = 'ok'
        """,
        "order": "m.model_safe_probability DESC, m.relative_path, m.source_key",
    },
    "title_adjective_frontier_wide": {
        "title": "Wide title adjective frontier samples",
        "focus_group": "title_adjective_frontier_wide",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.relative_path IN ('titles_l_spanish.yml', 'titles_cultural_names_l_spanish.yml')
            AND m.source_key LIKE '%_adj'
            AND m.model_safe_probability >= 0.70
            AND m.issue_count = 0
            AND m.token_status = 'ok'
        """,
        "order": "m.model_safe_probability DESC, m.relative_path, m.source_key",
    },
    "title_adjective_suffix_frontier": {
        "title": "Title adjective suffix frontier samples",
        "focus_group": "title_adjective_suffix_frontier",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.relative_path = 'titles_l_spanish.yml'
            AND m.source_key LIKE '%_adj'
            AND m.model_safe_probability >= 0.90
            AND m.issue_count = 0
            AND m.token_status = 'ok'
            AND (
                lower(m.candidate_text) GLOB '*iano'
                OR lower(m.candidate_text) GLOB '*ano'
                OR lower(m.candidate_text) GLOB '*ense'
                OR lower(m.candidate_text) GLOB '*eiro'
            )
        """,
        "order": "m.model_safe_probability DESC, m.source_key",
    },
    "title_barony_frontier": {
        "title": "Title barony frontier samples",
        "focus_group": "title_barony_frontier",
        "where": """
            m.final_action <> 'auto_safe'
            AND m.relative_path = 'titles_l_spanish.yml'
            AND m.source_key LIKE 'b_%'
            AND m.source_key NOT LIKE '%_adj'
            AND m.source_key NOT LIKE '%_pre'
            AND m.model_safe_probability >= 0.80
            AND m.token_status = 'ok'
        """,
        "order": """
            m.issue_count DESC,
            m.model_safe_probability DESC,
            m.source_key
        """,
    },
}
DEFAULT_SECTION_ORDER = ["priority", "deterministic", "high_safe", "clean_auto_safe"]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def latest_score_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_score_runs
        WHERE scored_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_score_runs found.")
    return int(row["id"])


def latest_audit_report(settings: dict) -> str | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(
        (
            path
            for path in reports_dir.glob("*_ml_score_audit.txt")
            if "human_review" not in path.name and "main_" not in path.name
        ),
        key=lambda path: path.stat().st_mtime,
    )
    if not reports:
        return None
    return str(reports[-1].relative_to(db.PROJECT_ROOT)).replace("\\", "/")


def latest_holdout_queue(settings: dict) -> Path | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(
        reports_dir.glob("*_ml_holdout_review_queue.csv"),
        key=lambda path: path.stat().st_mtime,
    )
    if not reports:
        return None
    return reports[-1]


def latest_group_candidate_queue(settings: dict) -> Path | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(
        reports_dir.glob("*_ml_group_candidate_queue.csv"),
        key=lambda path: path.stat().st_mtime,
    )
    if not reports:
        return None
    return reports[-1]


def latest_policy_audit_queue(settings: dict) -> Path | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(
        reports_dir.glob("*_ml_policy_audit_queue.csv"),
        key=lambda path: path.stat().st_mtime,
    )
    if not reports:
        return None
    return reports[-1]


def latest_specialist_auditor_queue(settings: dict) -> Path | None:
    reports_dir = db.project_path(settings["reports_dir"])
    reports = sorted(
        reports_dir.glob("*_ml_specialist_auditor.csv"),
        key=lambda path: path.stat().st_mtime,
    )
    if not reports:
        return None
    return reports[-1]


def normalize_project_relative(path_value: str | Path | None) -> str | None:
    if path_value is None:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = db.project_path(str(path))
    try:
        return str(path.relative_to(db.PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def next_lote_number(conn) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review%lote%'
        """
    ).fetchall()
    highest = 0
    for row in rows:
        match = re.search(r"lote(\d+)", row["mode"] or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def fetch_section_candidates(conn, score_run_id: int, section: str, limit: int, seen: set[int]) -> list[dict[str, Any]]:
    config = SECTION_CONFIG[section]
    query = f"""
        SELECT
            m.*,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM ml_score_items m
        JOIN source_segments s ON s.id = m.segment_id
        LEFT JOIN output_segments o ON o.segment_id = m.segment_id
        WHERE m.run_id = ?
          AND {config["where"]}
          AND NOT EXISTS (
              SELECT 1
              FROM local_learning_candidates c
              JOIN local_learning_runs r ON r.id = c.run_id
              WHERE c.segment_id = m.segment_id
                AND r.mode LIKE 'human_review%'
          )
        ORDER BY {config["order"]}
        LIMIT ?
    """
    rows = conn.execute(query, (score_run_id, limit + len(seen))).fetchall()
    selected: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        selected.append(dict(row))
        if len(selected) >= limit:
            break
    return selected


def select_candidates(conn, score_run_id: int, section_order: list[str], total: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for section in section_order:
        needed = total - len(selected)
        if needed <= 0:
            break
        rows = fetch_section_candidates(conn, score_run_id, section, needed, seen)
        for row in rows:
            row["source_section"] = SECTION_CONFIG[section]["title"]
            row["focus_group"] = SECTION_CONFIG[section]["focus_group"]
        selected.extend(rows)
    return selected


def candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "source_section": row["source_section"],
        "focus_group": row["focus_group"],
        "final_action": row["final_action"],
        "risk_class": row["risk_class"],
        "model_safe_probability": row["model_safe_probability"],
        "issue_count": row["issue_count"],
        "token_status": row["token_status"],
        "english_text": row["english_text"],
        "spanish_text": row["spanish_text"],
        "old_text": row["old_text"],
        "current_output_text": row["current_output_text"],
        "suggested_text": row["candidate_text"],
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def holdout_candidate_payload(row: dict[str, Any], source_line_number: int | None) -> dict[str, Any]:
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": source_line_number,
        "source_section": "ML holdout false-safe review queue",
        "focus_group": row.get("priority_bucket") or "holdout_false_safe",
        "final_action": "auto_safe_false_positive",
        "risk_class": row.get("truth_label") or "needs_human",
        "model_safe_probability": float(row.get("model_safe_probability") or 0),
        "issue_count": None,
        "token_status": "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": row.get("candidate_text"),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def policy_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": int(row["source_line_number"]) if row.get("source_line_number") else None,
        "source_section": "ML policy audit queue",
        "focus_group": row.get("policy_group") or "policy_audit",
        "audit_kind": row.get("audit_kind") or "policy_review",
        "policy_group": row.get("policy_group") or "unknown",
        "policy_threshold": float(row.get("policy_threshold") or 0),
        "policy_require_learned_positive": int(row.get("policy_require_learned_positive") or 0),
        "score_final_action": row.get("score_final_action") or "unknown",
        "policy_action": row.get("policy_action") or "unknown",
        "new_safe": int(row.get("new_safe") or 0),
        "demoted_safe": int(row.get("demoted_safe") or 0),
        "learned_positive": int(row.get("learned_positive") or 0),
        "learned_negative": int(row.get("learned_negative") or 0),
        "final_action": row.get("policy_action") or row.get("score_final_action") or "unknown",
        "risk_class": row.get("audit_kind") or "policy_review",
        "model_safe_probability": float(row.get("model_safe_probability") or 0),
        "issue_count": None,
        "token_status": "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": row.get("candidate_text"),
        "policy_reasons_json": row.get("policy_reasons_json"),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def specialist_auditor_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": int(row["source_line_number"]) if row.get("source_line_number") else None,
        "source_section": "ML specialist auditor queue",
        "focus_group": row.get("auditor_action") or "specialist_auditor",
        "auditor_action": row.get("auditor_action") or "specialist_auditor",
        "general_action": row.get("general_action") or "unknown",
        "specialist_action": row.get("specialist_action") or "unknown",
        "general_safe_probability": float(row.get("general_safe_probability") or 0),
        "specialist_safe_probability": float(row.get("specialist_safe_probability") or 0),
        "final_action": row.get("specialist_action") or row.get("auditor_action") or "specialist_auditor",
        "risk_class": row.get("auditor_action") or "specialist_auditor",
        "model_safe_probability": float(row.get("specialist_safe_probability") or 0),
        "issue_count": int(row["specialist_issue_count"]) if row.get("specialist_issue_count") not in {None, ""} else None,
        "token_status": row.get("specialist_token_status") or "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": row.get("candidate_text"),
        "candidate_text": row.get("candidate_text"),
        "auditor_reasons_json": row.get("auditor_reasons_json"),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def group_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": int(row["source_line_number"]) if row.get("source_line_number") else None,
        "source_section": "ML group candidate queue",
        "focus_group": row.get("group_name") or "group_candidate",
        "group_name": row.get("group_name") or "unknown",
        "candidate_kind": row.get("candidate_kind") or "group_candidate",
        "final_action": row.get("candidate_kind") or "group_candidate",
        "risk_class": row.get("candidate_kind") or "group_candidate",
        "model_safe_probability": float(row.get("model_safe_probability") or 0),
        "proposed_threshold": float(row.get("proposed_threshold") or 0),
        "active_threshold": float(row.get("active_threshold") or 0),
        "issue_count": None,
        "token_status": "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": row.get("candidate_text"),
        "candidate_text": row.get("candidate_text"),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def prepare(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    section_order = args.sections or DEFAULT_SECTION_ORDER
    unknown = [section for section in section_order if section not in SECTION_CONFIG]
    if unknown:
        raise ValueError(f"Unknown sections: {', '.join(unknown)}")
    total = args.batches * args.batch_size
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        score_run_id = args.score_run_id or latest_score_run_id(conn)
        rows = select_candidates(conn, score_run_id, section_order, total)
        first_lote = args.start_lote or next_lote_number(conn)

    batches = []
    for offset in range(0, len(rows), args.batch_size):
        batch_rows = rows[offset : offset + args.batch_size]
        if not batch_rows:
            continue
        lote_number = first_lote + len(batches)
        batches.append(
            {
                "lote_number": lote_number,
                "source_section": batch_rows[0]["source_section"],
                "focus_group": batch_rows[0]["focus_group"],
                "candidates": [candidate_payload(row) for row in batch_rows],
            }
        )

    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_score_audit",
        "score_run_id": score_run_id,
        "source_report": latest_audit_report(settings),
        "batch_size": args.batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": sorted(VALID_LABELS),
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else reports_dir / f"{timestamp()}_parallel_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[parallel_review_loop] Prepared batches: {len(batches)}")
    print(f"[parallel_review_loop] Candidates: {sum(len(batch['candidates']) for batch in batches)}")
    print(f"[parallel_review_loop] Decision template: {output_path}")


def prepare_holdout(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    queue_path = Path(args.queue) if args.queue else latest_holdout_queue(settings)
    if queue_path is None:
        raise RuntimeError("No ml_holdout_review_queue CSV found. Run ml-holdout-review-queue first.")
    if not queue_path.is_absolute():
        queue_path = db.project_path(str(queue_path))

    priority_order = {
        "core_context_sensitive": 0,
        "title_or_rank": 1,
        "concept_definition": 2,
        "token_context": 3,
        "semantic_or_style": 4,
    }
    with queue_path.open("r", encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    csv_rows.sort(
        key=lambda row: (
            priority_order.get(row.get("priority_bucket") or "", 99),
            row.get("relative_path") or "",
            -float(row.get("model_safe_probability") or 0),
            row.get("source_key") or "",
        )
    )

    total = args.batches * args.batch_size
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        reviewed_rows = conn.execute(
            """
            SELECT segment_id
            FROM local_learning_candidates
            WHERE queue_source = 'ml_holdout_review_queue'
              AND local_status = 'reviewed_human'
            """
        ).fetchall()
        reviewed = {int(row["segment_id"]) for row in reviewed_rows}
        selected_rows = []
        seen_segments: set[int] = set()
        for row in csv_rows:
            segment_id = int(row["segment_id"])
            if segment_id in reviewed or segment_id in seen_segments:
                continue
            segment = conn.execute(
                """
                SELECT source_line_number
                FROM source_segments
                WHERE id = ?
                """,
                (segment_id,),
            ).fetchone()
            if segment is None:
                continue
            row["source_line_number"] = int(segment["source_line_number"])
            selected_rows.append(row)
            seen_segments.add(segment_id)
            if len(selected_rows) >= total:
                break
        first_lote = args.start_lote or next_lote_number(conn)

    batches = []
    for offset in range(0, len(selected_rows), args.batch_size):
        batch_rows = selected_rows[offset : offset + args.batch_size]
        if not batch_rows:
            continue
        lote_number = first_lote + len(batches)
        focus_group = batch_rows[0].get("priority_bucket") or "holdout_false_safe"
        batches.append(
            {
                "lote_number": lote_number,
                "source_section": "ML holdout false-safe review queue",
                "focus_group": focus_group,
                "queue_source": "ml_holdout_review_queue",
                "candidates": [
                    holdout_candidate_payload(row, int(row["source_line_number"]))
                    for row in batch_rows
                ],
            }
        )

    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_holdout_review_queue",
        "score_run_id": None,
        "source_report": str(queue_path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "batch_size": args.batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": sorted(VALID_LABELS),
            "recommended_labels": [
                "rejected_suggestion",
                "semantic_error",
                "structure_error",
                "token_mismatch",
                "minor_fix",
                "major_fix",
                "contextual_exception",
                "correct",
            ],
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else reports_dir / f"{timestamp()}_holdout_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[parallel_review_loop] Source queue: {queue_path}")
    print(f"[parallel_review_loop] Prepared holdout batches: {len(batches)}")
    print(f"[parallel_review_loop] Candidates: {sum(len(batch['candidates']) for batch in batches)}")
    print(f"[parallel_review_loop] Decision template: {output_path}")


def prepare_group(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    queue_path = Path(args.queue) if args.queue else latest_group_candidate_queue(settings)
    if queue_path is None:
        raise RuntimeError("No ml_group_candidate_queue CSV found. Run ml-group-candidate-queue first.")
    if not queue_path.is_absolute():
        queue_path = db.project_path(str(queue_path))

    with queue_path.open("r", encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    csv_rows.sort(
        key=lambda row: (
            row.get("group_name") or "",
            row.get("candidate_kind") != "new_at_proposed_threshold",
            -float(row.get("model_safe_probability") or 0),
            row.get("relative_path") or "",
            row.get("source_key") or "",
        )
    )

    total = args.batches * args.batch_size
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        reviewed_rows = conn.execute(
            """
            SELECT segment_id
            FROM local_learning_candidates
            WHERE queue_source = 'ml_group_candidate_queue'
              AND local_status = 'reviewed_human'
            """
        ).fetchall()
        reviewed = {int(row["segment_id"]) for row in reviewed_rows}
        selected_rows = []
        seen_segments: set[int] = set()
        for row in csv_rows:
            segment_id = int(row["segment_id"])
            if segment_id in reviewed or segment_id in seen_segments:
                continue
            selected_rows.append(row)
            seen_segments.add(segment_id)
            if len(selected_rows) >= total:
                break
        first_lote = args.start_lote or next_lote_number(conn)

    batches = []
    for offset in range(0, len(selected_rows), args.batch_size):
        batch_rows = selected_rows[offset : offset + args.batch_size]
        if not batch_rows:
            continue
        lote_number = first_lote + len(batches)
        focus_group = batch_rows[0].get("group_name") or "group_candidate"
        batches.append(
            {
                "lote_number": lote_number,
                "source_section": "ML group candidate queue",
                "focus_group": focus_group,
                "queue_source": "ml_group_candidate_queue",
                "candidates": [group_candidate_payload(row) for row in batch_rows],
            }
        )

    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_group_candidate_queue",
        "score_run_id": None,
        "source_report": str(queue_path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "batch_size": args.batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": sorted(VALID_LABELS),
            "recommended_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "semantic_error",
                "structure_error",
                "token_mismatch",
                "rejected_suggestion",
            ],
            "label_guidance": {
                "correct": "Use quando o candidate_text pode virar aprendizado positivo sem ajuste.",
                "contextual_exception": "Use quando a escolha foge do espelho, mas e aceitavel por contexto/jogabilidade.",
                "minor_fix": "Use quando a ideia esta boa, mas precisa de pequeno ajuste em corrected_text.",
                "semantic_error": "Use quando a traducao muda sentido, fica literal demais ou nao serve em PT-BR natural.",
                "structure_error": "Use para perda de espaco, token, marcador, ordem estrutural ou comando CK3.",
            },
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else reports_dir / f"{timestamp()}_group_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[parallel_review_loop] Source queue: {queue_path}")
    print(f"[parallel_review_loop] Prepared group batches: {len(batches)}")
    print(f"[parallel_review_loop] Candidates: {sum(len(batch['candidates']) for batch in batches)}")
    print(f"[parallel_review_loop] Decision template: {output_path}")


def prepare_policy(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    queue_path = Path(args.queue) if args.queue else latest_policy_audit_queue(settings)
    if queue_path is None:
        raise RuntimeError("No ml_policy_audit_queue CSV found. Run ml-policy-audit-queue first.")
    if not queue_path.is_absolute():
        queue_path = db.project_path(str(queue_path))

    with queue_path.open("r", encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    total = args.batches * args.batch_size
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        reviewed_rows = conn.execute(
            """
            SELECT segment_id
            FROM local_learning_candidates
            WHERE queue_source = 'ml_policy_audit_queue'
              AND local_status = 'reviewed_human'
            """
        ).fetchall()
        reviewed = {int(row["segment_id"]) for row in reviewed_rows}
        selected_rows = []
        seen_segments: set[int] = set()
        for row in csv_rows:
            segment_id = int(row["segment_id"])
            if segment_id in reviewed or segment_id in seen_segments:
                continue
            selected_rows.append(row)
            seen_segments.add(segment_id)
            if len(selected_rows) >= total:
                break
        first_lote = args.start_lote or next_lote_number(conn)

    batches = []
    for offset in range(0, len(selected_rows), args.batch_size):
        batch_rows = selected_rows[offset : offset + args.batch_size]
        if not batch_rows:
            continue
        lote_number = first_lote + len(batches)
        focus_group = batch_rows[0].get("audit_kind") or "policy_audit"
        batches.append(
            {
                "lote_number": lote_number,
                "source_section": "ML policy audit queue",
                "focus_group": focus_group,
                "queue_source": "ml_policy_audit_queue",
                "candidates": [policy_candidate_payload(row) for row in batch_rows],
            }
        )

    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_policy_audit_queue",
        "score_run_id": None,
        "source_report": str(queue_path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "batch_size": args.batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": sorted(VALID_LABELS),
            "recommended_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "semantic_error",
                "structure_error",
                "token_mismatch",
                "rejected_suggestion",
            ],
            "label_guidance": {
                "new_safe": "Use correct/contextual_exception only when policy auto_safe is acceptable; otherwise use semantic_error/minor_fix/token_mismatch/etc.",
                "demoted_safe": "Use correct/contextual_exception if the old auto_safe is truly acceptable; use a negative label if this exposes a real false-safe risk.",
            },
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else reports_dir / f"{timestamp()}_policy_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[parallel_review_loop] Source queue: {queue_path}")
    print(f"[parallel_review_loop] Prepared policy batches: {len(batches)}")
    print(f"[parallel_review_loop] Candidates: {sum(len(batch['candidates']) for batch in batches)}")
    print(f"[parallel_review_loop] Decision template: {output_path}")


def prepare_specialist_auditor(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    queue_path = Path(args.queue) if args.queue else latest_specialist_auditor_queue(settings)
    if queue_path is None:
        raise RuntimeError("No ml_specialist_auditor CSV found. Run ml-specialist-auditor first.")
    if not queue_path.is_absolute():
        queue_path = db.project_path(str(queue_path))

    requested_actions = set(args.actions or ["specialist_new_safe_review"])
    with queue_path.open("r", encoding="utf-8-sig", newline="") as handle:
        csv_rows = [
            row
            for row in csv.DictReader(handle)
            if not requested_actions or row.get("auditor_action") in requested_actions
        ]
    divergence_actions = {"specialist_new_safe_review", "specialist_demoted_review"}
    csv_rows = [
        row
        for row in csv_rows
        if row.get("auditor_action") not in divergence_actions
        or str(row.get("requires_human_review") or "0") == "1"
    ]
    action_priority = {
        "specialist_new_safe_review": 0,
        "specialist_demoted_review": 1,
        "auto_safe_agree": 2,
        "needs_human_agree": 3,
    }
    csv_rows.sort(
        key=lambda row: (
            action_priority.get(row.get("auditor_action") or "", 99),
            -float(row.get("specialist_safe_probability") or 0),
            row.get("relative_path") or "",
            row.get("source_key") or "",
        )
    )

    total = args.batches * args.batch_size
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        reviewed_rows = conn.execute(
            """
            SELECT segment_id
            FROM local_learning_candidates
            WHERE queue_source IN ('ml_specialist_auditor', 'ml_specialist_scope_review')
              AND local_status = 'reviewed_human'
            """
        ).fetchall()
        reviewed = {int(row["segment_id"]) for row in reviewed_rows}
        selected_rows = []
        seen_segments: set[int] = set()
        for row in csv_rows:
            segment_id = int(row["segment_id"])
            if segment_id in reviewed or segment_id in seen_segments:
                continue
            segment = conn.execute(
                """
                SELECT
                    s.source_line_number,
                    s.english_text,
                    s.spanish_text,
                    s.old_text,
                    o.portuguese_text AS output_text
                FROM source_segments s
                LEFT JOIN output_segments o ON o.segment_id = s.id
                WHERE s.id = ?
                """,
                (segment_id,),
            ).fetchone()
            if segment is None:
                continue
            row.update(dict(segment))
            selected_rows.append(row)
            seen_segments.add(segment_id)
            if len(selected_rows) >= total:
                break
        first_lote = args.start_lote or next_lote_number(conn)

    batches = []
    for offset in range(0, len(selected_rows), args.batch_size):
        batch_rows = selected_rows[offset : offset + args.batch_size]
        if not batch_rows:
            continue
        lote_number = first_lote + len(batches)
        focus_group = batch_rows[0].get("auditor_action") or "specialist_auditor"
        batches.append(
            {
                "lote_number": lote_number,
                "source_section": "ML specialist auditor queue",
                "focus_group": focus_group,
                "queue_source": "ml_specialist_auditor",
                "candidates": [specialist_auditor_candidate_payload(row) for row in batch_rows],
            }
        )

    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_specialist_auditor",
        "score_run_id": None,
        "source_report": str(queue_path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "batch_size": args.batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": sorted(VALID_LABELS),
            "recommended_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "semantic_error",
                "structure_error",
                "token_mismatch",
                "rejected_suggestion",
            ],
            "label_guidance": {
                "specialist_new_safe_review": "Use correct/contextual_exception when the specialist can safely recover coverage; use a negative label when the general model was right to block.",
                "specialist_demoted_review": "Use correct/contextual_exception if the general auto_safe is safe; use a negative label if the specialist exposed a false-safe risk.",
                "contextual_exception": "Use for intentional non-mirror localization choices that are safe in game context.",
            },
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    queue_stem = queue_path.stem.replace("ml_specialist_auditor_", "specialist_auditor_")
    output_path = (
        Path(args.output)
        if args.output
        else reports_dir / f"{timestamp()}_{queue_stem}_review_decisions_template.json"
    )
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[parallel_review_loop] Source queue: {queue_path}")
    print(f"[parallel_review_loop] Actions: {', '.join(sorted(requested_actions))}")
    print(f"[parallel_review_loop] Prepared specialist auditor batches: {len(batches)}")
    print(f"[parallel_review_loop] Candidates: {sum(len(batch['candidates']) for batch in batches)}")
    print(f"[parallel_review_loop] Decision template: {output_path}")


def validate_decision(candidate: dict[str, Any]) -> None:
    label = candidate.get("human_label")
    if label not in VALID_LABELS:
        raise ValueError(f"Invalid human_label for segment {candidate.get('segment_id')}: {label}")
    if label in EDIT_LABELS | {"semantic_error", "major_fix", "structure_error", "residual_spanish"}:
        if candidate.get("corrected_text") in {None, ""} and label in {"minor_fix", "major_fix", "semantic_error"}:
            raise ValueError(f"corrected_text required for {label} on segment {candidate.get('segment_id')}")
    if not str(candidate.get("reason") or "").strip():
        raise ValueError(f"reason required for segment {candidate.get('segment_id')}")


def create_review_run(
    conn,
    lote_number: int,
    candidate_count: int,
    source_report: str | None,
    score_run_id: int | None,
    source_type: str,
) -> int:
    current = now()
    mode_sources = {
        "ml_holdout_review_queue": "holdout",
        "ml_group_candidate_queue": "ml_group_candidate_queue",
        "ml_policy_audit_queue": "ml_policy_audit_queue",
        "ml_specialist_auditor": "ml_specialist_auditor",
        "ml_specialist_scope_review": "ml_specialist_scope_review",
    }
    mode_source = mode_sources.get(source_type, "ml_score_audit")
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode,
            limit_count,
            auto_confidence_threshold,
            candidate_count,
            high_confidence_count,
            pending_human_count,
            status,
            notes,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"human_review_{mode_source}_lote{lote_number}",
            candidate_count,
            1.0,
            candidate_count,
            0,
            0,
            "completed",
            (
                f"Manual review from {source_report or source_type}; "
                f"parallel review loop source={source_type}; "
                f"ml_score_run={score_run_id or 'none'}. No output changes applied."
            ),
            current,
            current,
            current,
        ),
    )
    return int(cursor.lastrowid)


def insert_review_candidate(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    score_run_id: int,
    source_report: str | None,
) -> None:
    current = now()
    row = conn.execute(
        """
        SELECT
            m.*,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM ml_score_items m
        JOIN source_segments s ON s.id = m.segment_id
        LEFT JOIN output_segments o ON o.segment_id = m.segment_id
        WHERE m.run_id = ? AND m.segment_id = ?
        """,
        (score_run_id, candidate["segment_id"]),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment not found in ml_score_items: {candidate['segment_id']}")
    conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            current_output_text,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            suggestion_status,
            local_confidence_score,
            local_status,
            human_label,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            reasons_json,
            created_at,
            updated_at,
            queue_source,
            focus_group
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["source_line_number"],
            row["english_text"],
            row["spanish_text"],
            row["old_text"],
            row["current_output_text"],
            row["candidate_text"],
            None,
            "ml_score_candidate",
            "ml_score_audit_loop",
            row["final_action"],
            row["model_safe_probability"],
            row["token_status"],
            row["final_action"],
            1.0,
            "reviewed_human",
            candidate["human_label"],
            candidate.get("corrected_text"),
            candidate["reason"],
            "Codex",
            current,
            json.dumps(
                [
                    f"source_report:{source_report or 'unknown'}",
                    f"ml_score_run:{score_run_id}",
                    f"source_section:{candidate.get('source_section') or 'unknown'}",
                    f"final_action:{row['final_action']}",
                    f"risk_class:{row['risk_class']}",
                    f"model_safe_probability:{float(row['model_safe_probability'] or 0):.4f}",
                    f"issue_count:{row['issue_count']}",
                    f"token_status:{row['token_status']}",
                ],
                ensure_ascii=True,
            ),
            current,
            current,
            "ml_score_audit",
            candidate.get("focus_group") or "ml_score_audit",
        ),
    )


def insert_holdout_review_candidate(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    source_report: str | None,
) -> None:
    current = now()
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (candidate["segment_id"],),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment not found: {candidate['segment_id']}")
    suggested_text = candidate.get("suggested_text")
    conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            current_output_text,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            suggestion_status,
            local_confidence_score,
            local_status,
            human_label,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            reasons_json,
            created_at,
            updated_at,
            queue_source,
            focus_group
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["source_line_number"],
            row["english_text"],
            row["spanish_text"],
            row["old_text"],
            row["current_output_text"],
            suggested_text,
            None,
            "ml_holdout_candidate",
            "ml_holdout_review_queue",
            candidate.get("final_action") or "auto_safe_false_positive",
            candidate.get("model_safe_probability") or 0,
            candidate.get("token_status") or "unknown",
            candidate.get("final_action") or "auto_safe_false_positive",
            1.0,
            "reviewed_human",
            candidate["human_label"],
            candidate.get("corrected_text"),
            candidate["reason"],
            "Codex",
            current,
            json.dumps(
                [
                    f"source_report:{source_report or 'unknown'}",
                    "source_queue:ml_holdout_review_queue",
                    f"focus_group:{candidate.get('focus_group') or 'unknown'}",
                    f"risk_class:{candidate.get('risk_class') or 'unknown'}",
                    f"model_safe_probability:{float(candidate.get('model_safe_probability') or 0):.4f}",
                    f"token_status:{candidate.get('token_status') or 'unknown'}",
                ],
                ensure_ascii=True,
            ),
            current,
            current,
            "ml_holdout_review_queue",
            candidate.get("focus_group") or "holdout_false_safe",
        ),
    )


def insert_group_candidate_review(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    source_report: str | None,
) -> None:
    current = now()
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (candidate["segment_id"],),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment not found: {candidate['segment_id']}")
    suggested_text = candidate.get("candidate_text") or candidate.get("suggested_text") or row["current_output_text"]
    if suggested_text in {None, ""}:
        suggested_text = row["spanish_text"] or ""
    conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            current_output_text,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            suggestion_status,
            local_confidence_score,
            local_status,
            human_label,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            reasons_json,
            created_at,
            updated_at,
            queue_source,
            focus_group
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["source_line_number"],
            row["english_text"],
            row["spanish_text"],
            row["old_text"],
            row["current_output_text"],
            suggested_text,
            None,
            "ml_group_candidate",
            "ml_group_candidate_queue",
            candidate.get("candidate_kind") or "group_candidate",
            candidate.get("model_safe_probability") or 0,
            candidate.get("token_status") or "unknown",
            candidate.get("candidate_kind") or "group_candidate",
            1.0,
            "reviewed_human",
            candidate["human_label"],
            candidate.get("corrected_text"),
            candidate["reason"],
            "Codex",
            current,
            json.dumps(
                [
                    f"source_report:{source_report or 'unknown'}",
                    "source_queue:ml_group_candidate_queue",
                    f"group_name:{candidate.get('group_name') or 'unknown'}",
                    f"candidate_kind:{candidate.get('candidate_kind') or 'unknown'}",
                    f"model_safe_probability:{float(candidate.get('model_safe_probability') or 0):.6f}",
                ],
                ensure_ascii=True,
            ),
            current,
            current,
            "ml_group_candidate_queue",
            candidate.get("group_name") or candidate.get("focus_group") or "group_candidate",
        ),
    )


def insert_policy_audit_review(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    source_report: str | None,
) -> None:
    current = now()
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (candidate["segment_id"],),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment not found: {candidate['segment_id']}")
    suggested_text = candidate.get("suggested_text") or row["current_output_text"] or row["spanish_text"] or ""
    conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            current_output_text,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            suggestion_status,
            local_confidence_score,
            local_status,
            human_label,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            reasons_json,
            created_at,
            updated_at,
            queue_source,
            focus_group
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["source_line_number"],
            row["english_text"],
            row["spanish_text"],
            row["old_text"],
            row["current_output_text"],
            suggested_text,
            None,
            "ml_policy_candidate",
            "ml_policy_audit_queue",
            candidate.get("audit_kind") or "policy_review",
            candidate.get("model_safe_probability") or 0,
            candidate.get("token_status") or "unknown",
            candidate.get("policy_action") or candidate.get("final_action") or "policy_review",
            1.0,
            "reviewed_human",
            candidate["human_label"],
            candidate.get("corrected_text"),
            candidate["reason"],
            "Codex",
            current,
            json.dumps(
                [
                    f"source_report:{source_report or 'unknown'}",
                    "source_queue:ml_policy_audit_queue",
                    f"audit_kind:{candidate.get('audit_kind') or 'unknown'}",
                    f"policy_group:{candidate.get('policy_group') or 'unknown'}",
                    f"score_final_action:{candidate.get('score_final_action') or 'unknown'}",
                    f"policy_action:{candidate.get('policy_action') or 'unknown'}",
                    f"learned_positive:{candidate.get('learned_positive') or 0}",
                    f"learned_negative:{candidate.get('learned_negative') or 0}",
                    f"model_safe_probability:{float(candidate.get('model_safe_probability') or 0):.6f}",
                ],
                ensure_ascii=True,
            ),
            current,
            current,
            "ml_policy_audit_queue",
            candidate.get("policy_group") or candidate.get("focus_group") or "policy_audit",
        ),
    )


def insert_specialist_auditor_review(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    source_report: str | None,
) -> None:
    insert_specialist_review_candidate(
        conn,
        run_id,
        candidate,
        source_report,
        queue_source="ml_specialist_auditor",
        origin="ml_specialist_auditor",
    )


def insert_specialist_scope_review(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    source_report: str | None,
) -> None:
    insert_specialist_review_candidate(
        conn,
        run_id,
        candidate,
        source_report,
        queue_source="ml_specialist_scope_review",
        origin="ml_specialist_scope_review",
    )


def insert_specialist_review_candidate(
    conn,
    run_id: int,
    candidate: dict[str, Any],
    source_report: str | None,
    queue_source: str,
    origin: str,
) -> None:
    current = now()
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (candidate["segment_id"],),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment not found: {candidate['segment_id']}")
    suggested_text = candidate.get("candidate_text") or candidate.get("suggested_text") or row["current_output_text"]
    if suggested_text in {None, ""}:
        suggested_text = row["spanish_text"] or ""
    focus_group = (
        candidate.get("focus_group")
        if queue_source == "ml_specialist_scope_review"
        else candidate.get("agent_key")
        or candidate.get("auditor_action")
        or candidate.get("focus_group")
        or "specialist_auditor"
    )
    conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            current_output_text,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            suggestion_status,
            local_confidence_score,
            local_status,
            human_label,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            reasons_json,
            created_at,
            updated_at,
            queue_source,
            focus_group
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["source_line_number"],
            row["english_text"],
            row["spanish_text"],
            row["old_text"],
            row["current_output_text"],
            suggested_text,
            None,
            "ml_specialist_candidate",
            origin,
            candidate.get("auditor_action") or "specialist_auditor",
            candidate.get("model_safe_probability") or candidate.get("specialist_safe_probability") or 0,
            candidate.get("token_status") or "unknown",
            candidate.get("specialist_action") or candidate.get("final_action") or "specialist_auditor",
            1.0,
            "reviewed_human",
            candidate["human_label"],
            candidate.get("corrected_text"),
            candidate["reason"],
            "Codex",
            current,
            json.dumps(
                [
                    f"source_report:{source_report or 'unknown'}",
                    f"source_queue:{queue_source}",
                    f"agent_key:{candidate.get('agent_key') or 'unknown'}",
                    f"route_status:{candidate.get('route_status') or 'unknown'}",
                    f"auditor_action:{candidate.get('auditor_action') or 'unknown'}",
                    f"general_action:{candidate.get('general_action') or 'unknown'}",
                    f"specialist_action:{candidate.get('specialist_action') or 'unknown'}",
                    f"general_safe_probability:{float(candidate.get('general_safe_probability') or 0):.6f}",
                    f"specialist_safe_probability:{float(candidate.get('specialist_safe_probability') or 0):.6f}",
                    f"token_status:{candidate.get('token_status') or 'unknown'}",
                ],
                ensure_ascii=True,
            ),
            current,
            current,
            queue_source,
            focus_group,
        ),
    )


def batch_counts(candidates: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(candidate["human_label"]) for candidate in candidates)


def positive_count(counts: Counter[str]) -> int:
    return sum(counts[label] for label in POSITIVE_LABELS | CONTEXTUAL_LABELS)


def negative_count(counts: Counter[str]) -> int:
    return sum(counts[label] for label in NEGATIVE_LABELS)


def write_batch_report(
    settings: dict,
    batch: dict[str, Any],
    run_id: int,
    score_run_id: int | None,
    source_report: str | None,
) -> Path:
    counts = batch_counts(batch["candidates"])
    positive = positive_count(counts)
    negative = negative_count(counts)
    edited = sum(counts[label] for label in EDIT_LABELS)
    lines = [
        "Human review batch report",
        f"Reviewed at: {now()}",
        "Reviewer: Codex",
        "",
        "Scope:",
        f"- Source queue: {source_report or 'unknown'}",
        f"- Source sections: {batch.get('source_section') or 'mixed'}",
        f"- Score run id: {score_run_id or 'none'}",
        f"- Inserted local learning run id: {run_id}",
        f"- Queue source: {batch.get('queue_source') or 'ml_score_audit'}",
        f"- Focus group: {batch.get('focus_group') or 'mixed'}",
        f"- Candidates reviewed: {len(batch['candidates'])}",
        "",
        "Summary:",
        f"- Positive examples: {positive}",
        f"- Negative examples: {negative}",
        f"- Corrections/edited: {edited}",
        f"- Label counts: {dict(sorted(counts.items()))}",
        "- Output files changed: no",
        "- ML consolidation/training/dataset rebuild: no",
        f"- Last queue/report used: {source_report or 'unknown'}",
        "",
        "Reviewed candidates:",
    ]
    for candidate in batch["candidates"]:
        corrected = f"; corrected_text={candidate.get('corrected_text')}" if candidate.get("corrected_text") else ""
        source_key = candidate.get("source_key") or "unknown_key"
        lines.append(
            f"- segment {candidate['segment_id']} / {source_key}: "
            f"{candidate['human_label']}{corrected}. {candidate['reason']}"
        )
    report_suffix = batch.get("queue_source") or "ml_score_audit"
    return db.write_report(settings, f"human_review_lote{batch['lote_number']}_{report_suffix}", lines)


def reviewed_totals(conn) -> Counter[str]:
    rows = conn.execute(
        """
        SELECT c.human_label, COUNT(*) AS total
        FROM local_learning_candidates c
        JOIN local_learning_runs r ON r.id = c.run_id
        WHERE r.mode LIKE 'human_review%'
        GROUP BY c.human_label
        """
    ).fetchall()
    return Counter({row["human_label"]: int(row["total"]) for row in rows})


def reviewed_cycle_count(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM local_learning_runs
        WHERE mode LIKE 'human_review%'
          AND candidate_count > 0
        """
    ).fetchone()
    return int(row["total"] or 0)


def progress_summary_lines(conn, source_report: str | None) -> list[str]:
    totals = reviewed_totals(conn)
    positive = positive_count(totals)
    negative = negative_count(totals)
    edited = sum(totals[label] for label in EDIT_LABELS)
    total = positive + negative
    return [
        "## Resumo acumulado",
        f"- Ciclos revisados: {reviewed_cycle_count(conn)}",
        f"- Segmentos revisados: {total}",
        f"- Positivos: {positive}",
        f"- Negativos: {negative}",
        f"- Correcoes/editados: {edited}",
        f"- Rejeitados: {negative}",
        f"- Structure errors: {totals['structure_error']}",
        f"- Residual Spanish: {totals['residual_spanish']}",
        f"- Semantic errors: {totals['semantic_error']}",
        f"- Major fixes: {totals['major_fix']}",
        f"- Ultimo relatorio de auditoria ML usado: {source_report or 'unknown'}",
        "- Observacoes para a frente principal: revisao paralela registrada no banco; nenhum lote alterou output, treinou modelo ou regenerou dataset.",
    ]


def progress_cycle_lines(batch: dict[str, Any], report_path: Path) -> list[str]:
    counts = batch_counts(batch["candidates"])
    positive = positive_count(counts)
    negative = negative_count(counts)
    edited = sum(counts[label] for label in EDIT_LABELS)
    critical = [
        str(candidate["segment_id"])
        for candidate in batch["candidates"]
        if candidate["human_label"] != "correct"
    ]
    paths = Counter(candidate.get("relative_path") or "unknown_path" for candidate in batch["candidates"])
    label_bits = ", ".join(f"{label}={count}" for label, count in sorted(counts.items()))
    return [
        f"## Ciclo {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- Fonte da fila: {batch.get('queue_source') or 'ml_score_audit'}",
        f"- Relatorio usado: {batch.get('source_report') or 'unknown'}",
        f"- Segmentos revisados: {len(batch['candidates'])}",
        f"- Positivos: {positive}",
        f"- Negativos: {negative}",
        f"- Correcoes/editados: {edited}",
        f"- Rejeitados: {negative}",
        f"- Principais tipos de erro: {label_bits}",
        f"- Segmentos criticos: {', '.join(critical) if critical else 'nenhum'}",
        f"- Arquivos/chaves mais recorrentes: {'; '.join(path for path, _ in paths.most_common(4))}",
        f"- Relatorios gerados: {str(report_path.relative_to(db.PROJECT_ROOT)).replace(chr(92), '/')}; reports/parallel_review_progress.md",
        "- Pendencias/observacoes: lote gerado pelo parallel_review_loop; sem output, treino ou consolidacao ML.",
    ]


def update_progress(settings: dict, conn, new_cycles: list[tuple[dict[str, Any], Path]], source_report: str | None) -> None:
    progress_path = db.project_path(settings["reports_dir"]) / "parallel_review_progress.md"
    existing = progress_path.read_text(encoding="utf-8") if progress_path.exists() else "# Progresso da Revisao Paralela\n"
    lines = existing.splitlines()
    cycle_start = next((idx for idx, line in enumerate(lines) if line.startswith("## Ciclo ")), len(lines))
    header = ["# Progresso da Revisao Paralela", ""]
    summary = progress_summary_lines(conn, source_report)
    cycle_lines: list[str] = []
    for batch, report_path in reversed(new_cycles):
        batch["source_report"] = source_report
        cycle_lines.extend(["", *progress_cycle_lines(batch, report_path)])
    tail = lines[cycle_start:] if cycle_start < len(lines) else []
    updated = header + summary + cycle_lines
    if tail:
        updated.extend(["", *tail])
    progress_path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def apply_decisions(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    path = Path(args.decisions)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    source_type = payload.get("source_type") or "ml_score_audit"
    score_run_id = int(payload["score_run_id"]) if payload.get("score_run_id") is not None else None
    source_report = payload.get("source_report") or (
        latest_audit_report(settings) if source_type == "ml_score_audit" else None
    )
    created: list[tuple[dict[str, Any], Path]] = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        with conn:
            for batch in payload["batches"]:
                batch["queue_source"] = source_type
                candidates = batch["candidates"]
                for candidate in candidates:
                    validate_decision(candidate)
                run_id = create_review_run(
                    conn,
                    int(batch["lote_number"]),
                    len(candidates),
                    source_report,
                    score_run_id,
                    source_type,
                )
                for candidate in candidates:
                    if source_type == "ml_holdout_review_queue":
                        insert_holdout_review_candidate(conn, run_id, candidate, source_report)
                    elif source_type == "ml_group_candidate_queue":
                        insert_group_candidate_review(conn, run_id, candidate, source_report)
                    elif source_type == "ml_policy_audit_queue":
                        insert_policy_audit_review(conn, run_id, candidate, source_report)
                    elif source_type == "ml_specialist_auditor":
                        insert_specialist_auditor_review(conn, run_id, candidate, source_report)
                    elif source_type == "ml_specialist_scope_review":
                        insert_specialist_scope_review(conn, run_id, candidate, source_report)
                    else:
                        if score_run_id is None:
                            raise RuntimeError("score_run_id is required for ml_score_audit decisions.")
                        insert_review_candidate(conn, run_id, candidate, score_run_id, source_report)
                report_path = write_batch_report(settings, batch, run_id, score_run_id, source_report)
                created.append((batch, report_path))
            update_progress(settings, conn, created, source_report)
    print(f"[parallel_review_loop] Applied batches: {len(created)}")
    for _, report_path in created:
        print(f"[parallel_review_loop] Report: {report_path}")


def load_jsonl_decisions(path: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        candidate = json.loads(line)
        candidate["segment_id"] = int(candidate["segment_id"])
        if candidate.get("corrected_text") == "":
            candidate["corrected_text"] = None
        try:
            candidate["model_safe_probability"] = float(candidate.get("model_safe_probability") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid model_safe_probability on line {line_number}.") from exc
        validate_decision(candidate)
        candidates.append(candidate)
    if not candidates:
        raise ValueError(f"No review decisions found in {path}.")
    return candidates


def filter_unreviewed_candidates(
    conn,
    candidates: list[dict[str, Any]],
    queue_source: str,
) -> tuple[list[dict[str, Any]], int]:
    segment_ids = [int(candidate["segment_id"]) for candidate in candidates]
    if not segment_ids:
        return [], 0
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id
        FROM local_learning_candidates
        WHERE queue_source = ?
          AND local_status = 'reviewed_human'
          AND segment_id IN ({placeholders})
        """,
        (queue_source, *segment_ids),
    ).fetchall()
    reviewed = {int(row["segment_id"]) for row in rows}
    return [candidate for candidate in candidates if int(candidate["segment_id"]) not in reviewed], len(reviewed)


def apply_group_jsonl(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    path = Path(args.jsonl)
    candidates = load_jsonl_decisions(path)
    lote_number = args.lote_number
    source_report = args.source_report or normalize_project_relative(path)
    latest_queue = latest_group_candidate_queue(settings)
    latest_queue_rel = normalize_project_relative(latest_queue)
    source_report_rel = normalize_project_relative(source_report)
    if (
        latest_queue_rel
        and source_report_rel
        and source_report_rel.endswith("_ml_group_candidate_queue.csv")
        and source_report_rel != latest_queue_rel
        and not args.allow_stale_source
    ):
        raise RuntimeError(
            "Refusing to apply stale ml_group_candidate_queue review. "
            f"source_report={source_report_rel}; latest_queue={latest_queue_rel}. "
            "Regenerate/review the latest queue or pass --allow-stale-source deliberately."
        )
    batch = {
        "lote_number": lote_number,
        "source_section": "ML group candidate queue structured review",
        "focus_group": "mixed",
        "queue_source": "ml_group_candidate_queue",
        "candidates": candidates,
    }
    created: list[tuple[dict[str, Any], Path]] = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        candidates, skipped = filter_unreviewed_candidates(conn, candidates, "ml_group_candidate_queue")
        if not candidates:
            print("[parallel_review_loop] Applied group JSONL candidates: 0")
            print(f"[parallel_review_loop] Skipped already reviewed candidates: {skipped}")
            print("[parallel_review_loop] No local learning run created.")
            return
        batch["candidates"] = candidates
        if lote_number is None:
            lote_number = next_lote_number(conn)
            batch["lote_number"] = lote_number
        with conn:
            run_id = create_review_run(
                conn,
                int(lote_number),
                len(candidates),
                source_report_rel,
                None,
                "ml_group_candidate_queue",
            )
            for candidate in candidates:
                insert_group_candidate_review(conn, run_id, candidate, source_report_rel)
            report_path = write_batch_report(settings, batch, run_id, None, source_report_rel)
            created.append((batch, report_path))
            update_progress(settings, conn, created, source_report_rel)
    print(f"[parallel_review_loop] Applied group JSONL candidates: {len(candidates)}")
    print(f"[parallel_review_loop] Skipped already reviewed candidates: {skipped}")
    print(f"[parallel_review_loop] Lote: {lote_number}")
    for _, report_path in created:
        print(f"[parallel_review_loop] Report: {report_path}")


def apply_policy_jsonl(args: argparse.Namespace) -> None:
    settings = db.load_settings()
    path = Path(args.jsonl)
    candidates = load_jsonl_decisions(path)
    lote_number = args.lote_number
    source_report = args.source_report or normalize_project_relative(path)
    latest_queue = latest_policy_audit_queue(settings)
    latest_queue_rel = normalize_project_relative(latest_queue)
    source_report_rel = normalize_project_relative(source_report)
    if (
        latest_queue_rel
        and source_report_rel
        and source_report_rel.endswith("_ml_policy_audit_queue.csv")
        and source_report_rel != latest_queue_rel
        and not args.allow_stale_source
    ):
        raise RuntimeError(
            "Refusing to apply stale ml_policy_audit_queue review. "
            f"source_report={source_report_rel}; latest_queue={latest_queue_rel}. "
            "Regenerate/review the latest queue or pass --allow-stale-source deliberately."
        )
    batch = {
        "lote_number": lote_number,
        "source_section": "ML policy audit queue structured review",
        "focus_group": "policy_audit",
        "queue_source": "ml_policy_audit_queue",
        "candidates": candidates,
    }
    created: list[tuple[dict[str, Any], Path]] = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        candidates, skipped = filter_unreviewed_candidates(conn, candidates, "ml_policy_audit_queue")
        if not candidates:
            print("[parallel_review_loop] Applied policy JSONL candidates: 0")
            print(f"[parallel_review_loop] Skipped already reviewed candidates: {skipped}")
            print("[parallel_review_loop] No local learning run created.")
            return
        batch["candidates"] = candidates
        if lote_number is None:
            lote_number = next_lote_number(conn)
            batch["lote_number"] = lote_number
        with conn:
            run_id = create_review_run(
                conn,
                int(lote_number),
                len(candidates),
                source_report_rel,
                None,
                "ml_policy_audit_queue",
            )
            for candidate in candidates:
                insert_policy_audit_review(conn, run_id, candidate, source_report_rel)
            report_path = write_batch_report(settings, batch, run_id, None, source_report_rel)
            created.append((batch, report_path))
            update_progress(settings, conn, created, source_report_rel)
    print(f"[parallel_review_loop] Applied policy JSONL candidates: {len(candidates)}")
    print(f"[parallel_review_loop] Skipped already reviewed candidates: {skipped}")
    print(f"[parallel_review_loop] Lote: {lote_number}")
    for _, report_path in created:
        print(f"[parallel_review_loop] Report: {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare/apply parallel human review batches from ML score audit rows.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Prepare a multi-batch JSON decision template.")
    prepare_parser.add_argument("--batches", type=int, default=3)
    prepare_parser.add_argument("--batch-size", type=int, default=25)
    prepare_parser.add_argument("--score-run-id", type=int)
    prepare_parser.add_argument("--start-lote", type=int)
    prepare_parser.add_argument("--sections", nargs="+", choices=sorted(SECTION_CONFIG))
    prepare_parser.add_argument("--output")
    prepare_parser.set_defaults(func=prepare)

    holdout_parser = subparsers.add_parser("prepare-holdout", help="Prepare review batches from latest holdout false-safe CSV.")
    holdout_parser.add_argument("--batches", type=int, default=3)
    holdout_parser.add_argument("--batch-size", type=int, default=25)
    holdout_parser.add_argument("--start-lote", type=int)
    holdout_parser.add_argument("--queue", help="Optional ml_holdout_review_queue CSV path. Defaults to latest report.")
    holdout_parser.add_argument("--output")
    holdout_parser.set_defaults(func=prepare_holdout)

    group_parser = subparsers.add_parser("prepare-group", help="Prepare review batches from latest group candidate CSV.")
    group_parser.add_argument("--batches", type=int, default=3)
    group_parser.add_argument("--batch-size", type=int, default=25)
    group_parser.add_argument("--start-lote", type=int)
    group_parser.add_argument("--queue", help="Optional ml_group_candidate_queue CSV path. Defaults to latest report.")
    group_parser.add_argument("--output")
    group_parser.set_defaults(func=prepare_group)

    policy_parser = subparsers.add_parser("prepare-policy", help="Prepare review batches from latest policy audit CSV.")
    policy_parser.add_argument("--batches", type=int, default=2)
    policy_parser.add_argument("--batch-size", type=int, default=20)
    policy_parser.add_argument("--start-lote", type=int)
    policy_parser.add_argument("--queue", help="Optional ml_policy_audit_queue CSV path. Defaults to latest report.")
    policy_parser.add_argument("--output")
    policy_parser.set_defaults(func=prepare_policy)

    specialist_parser = subparsers.add_parser(
        "prepare-specialist-auditor",
        help="Prepare review batches from latest specialist auditor CSV.",
    )
    specialist_parser.add_argument("--batches", type=int, default=3)
    specialist_parser.add_argument("--batch-size", type=int, default=30)
    specialist_parser.add_argument("--start-lote", type=int)
    specialist_parser.add_argument("--queue", help="Optional ml_specialist_auditor CSV path. Defaults to latest report.")
    specialist_parser.add_argument(
        "--actions",
        nargs="+",
        default=["specialist_new_safe_review"],
        choices=[
            "specialist_new_safe_review",
            "specialist_demoted_review",
            "auto_safe_agree",
            "needs_human_agree",
        ],
        help="Auditor actions to include in the review template.",
    )
    specialist_parser.add_argument("--output")
    specialist_parser.set_defaults(func=prepare_specialist_auditor)

    apply_parser = subparsers.add_parser("apply", help="Apply a filled decision JSON template.")
    apply_parser.add_argument("decisions")
    apply_parser.set_defaults(func=apply_decisions)

    group_jsonl_parser = subparsers.add_parser(
        "apply-group-jsonl",
        help="Apply structured JSONL reviews from ml_group_candidate_queue.",
    )
    group_jsonl_parser.add_argument("jsonl")
    group_jsonl_parser.add_argument("--lote-number", type=int)
    group_jsonl_parser.add_argument("--source-report")
    group_jsonl_parser.add_argument(
        "--allow-stale-source",
        action="store_true",
        help="Allow applying a review tied to an older ml_group_candidate_queue CSV.",
    )
    group_jsonl_parser.set_defaults(func=apply_group_jsonl)

    policy_jsonl_parser = subparsers.add_parser(
        "apply-policy-jsonl",
        help="Apply structured JSONL reviews from ml_policy_audit_queue.",
    )
    policy_jsonl_parser.add_argument("jsonl")
    policy_jsonl_parser.add_argument("--lote-number", type=int)
    policy_jsonl_parser.add_argument("--source-report")
    policy_jsonl_parser.add_argument(
        "--allow-stale-source",
        action="store_true",
        help="Allow applying a review tied to an older ml_policy_audit_queue CSV.",
    )
    policy_jsonl_parser.set_defaults(func=apply_policy_jsonl)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
