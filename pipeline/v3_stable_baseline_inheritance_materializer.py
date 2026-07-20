from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import pending_architecture_diagnostic as pending_classifier
from apply_safe_output_updates import escape_localization_value
from source_tree_snapshot import inspect_tree


RULE_VERSION = "v3_stable_baseline_inheritance_materializer_v1"
POLICY_NAME = "v3_stable_baseline_inheritance"
POLICY_ACTION = "close_reopen_stable_baseline_inherited_v3"
EXPECTED_FINAL_STATE = "closed_stable_baseline_inherited_v3"
LABEL_FAMILY = "stable_baseline_inherited_v3"
OUTPUT_MATCH_KIND = "frozen_v2_old_output_exact_confirmation_canonical"
BASELINE_VERSION_NUMBER = 2
SOURCE_DELTA_STATUS = "unknown_pre_v3"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(mode: str) -> dict[str, Path]:
    root = db.project_path(db.load_settings()["reports_dir"])
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"{stamp()}_v3_stable_baseline_inheritance_materializer_{mode}"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "csv": base.with_suffix(".csv"),
        "summary": base.with_name(base.name + "_summary.json"),
    }


def package_tree_hash(root: Path) -> tuple[str, int]:
    inspected, _ = inspect_tree(root)
    return str(inspected["tree_hash"]), int(inspected["file_count"])


def canonical(value: Any) -> str:
    return escape_localization_value("" if value is None else str(value))


def text_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def learning_lane(primary_family: str) -> str:
    if primary_family in {
        "dynamic_ck3_expression_microagent",
        "gender_token_microagent",
        "structural_token_gate",
        "long_text_composer",
    }:
        return "parser_or_architecture"
    if primary_family in {
        "autofix_unknown_microagent",
        "short_label_style_microagent",
        "spanish_residual_microagent",
        "surface_boundary_microagent",
        "title_policy_microagent",
        "nickname_name_policy",
    }:
        return "rule_or_specialist_learning"
    return "semantic_or_human_review"


def latest_finished_state(conn: sqlite3.Connection, requested_id: int | None) -> dict[str, Any]:
    if requested_id is None:
        row = conn.execute(
            """
            SELECT *
            FROM segment_state_runs
            WHERE finished_at IS NOT NULL AND total_segments > 1000
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM segment_state_runs
            WHERE id = ? AND finished_at IS NOT NULL
            """,
            (requested_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run was found.")
    return dict(row)


def global_context(conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    settings = db.load_settings()
    old_root = db.project_path(settings["spanish_traduzido_old"])
    output_root = db.project_path(settings["output_spanish"])
    english_root = db.project_path(settings["english_source"])
    spanish_root = db.project_path(settings["spanish_source"])
    old_hash, old_file_count = package_tree_hash(old_root)
    output_hash, output_file_count = package_tree_hash(output_root)
    english, _ = inspect_tree(english_root)
    spanish, _ = inspect_tree(spanish_root)

    frozen = conn.execute(
        "SELECT * FROM package_versions WHERE version_number = ?",
        (BASELINE_VERSION_NUMBER,),
    ).fetchone()
    active_score = conn.execute(
        "SELECT * FROM ml_score_runs WHERE id = ?",
        (int(state.get("active_score_run_id") or 0),),
    ).fetchone()
    candidate_score = conn.execute(
        "SELECT * FROM ml_score_runs WHERE id = ?",
        (int(state.get("candidate_score_run_id") or 0),),
    ).fetchone()
    active_score = dict(active_score) if active_score else {}
    candidate_score = dict(candidate_score) if candidate_score else {}
    snapshot_id = int(candidate_score.get("source_snapshot_id") or 0)
    snapshot = conn.execute(
        "SELECT * FROM source_tree_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    snapshot = dict(snapshot) if snapshot else {}
    new_segment_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM source_segments WHERE is_active = 1 AND COALESCE(has_old, 0) = 0"
        ).fetchone()[0]
    )
    old_text_segment_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM source_segments
            WHERE is_active = 1
              AND COALESCE(old_text, '') <> ''
            """
        ).fetchone()[0]
    )
    output_text_segment_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM source_segments source
            JOIN output_segments output ON output.segment_id = source.id
            WHERE source.is_active = 1
              AND COALESCE(output.portuguese_text, '') <> ''
            """
        ).fetchone()[0]
    )
    existing_policy = conn.execute(
        """
        SELECT id, released_count
        FROM auto_confirmation_reopen_lifecycle_policy_runs
        WHERE policy_name = ? AND policy_status = 'active' AND released_count > 0
        ORDER BY id DESC LIMIT 1
        """,
        (POLICY_NAME,),
    ).fetchone()
    frozen_hash = str(frozen["package_hash"] if frozen else "")
    guards = {
        "state_finished": bool(state.get("finished_at")),
        "state_has_pending": int(state.get("pending_count") or 0) > 0,
        "state_needs_apply_zero": int(state.get("output_apply_pending_count") or 0) == 0,
        "frozen_v2_present": bool(frozen),
        "frozen_v2_status_frozen": bool(frozen and frozen["status"] == "frozen"),
        "old_tree_matches_frozen_v2": bool(frozen_hash and old_hash == frozen_hash),
        "output_tree_matches_frozen_v2": bool(frozen_hash and output_hash == frozen_hash),
        "old_output_tree_match": old_hash == output_hash,
        "old_output_file_count_match": old_file_count == output_file_count,
        "no_new_active_segments": new_segment_count == 0,
        "paired_score_runs_present": bool(active_score and candidate_score),
        "paired_score_model_match": bool(
            active_score
            and candidate_score
            and active_score.get("model_run_id") == candidate_score.get("model_run_id")
        ),
        "paired_score_rule_match": bool(
            active_score
            and candidate_score
            and active_score.get("rule_version") == candidate_score.get("rule_version")
        ),
        "paired_score_snapshot_match": bool(
            snapshot_id
            and int(active_score.get("source_snapshot_id") or 0) == snapshot_id
        ),
        "paired_score_sources_correct": bool(
            active_score.get("candidate_text_source") == "old"
            and candidate_score.get("candidate_text_source") == "output"
        ),
        "paired_score_runs_full": bool(
            active_score.get("path_filter") is None
            and candidate_score.get("path_filter") is None
            and active_score.get("limit_count") is None
            and candidate_score.get("limit_count") is None
            and int(active_score.get("scored_count") or 0) == old_text_segment_count
            and int(candidate_score.get("scored_count") or 0) == output_text_segment_count
        ),
        "source_snapshot_present": bool(snapshot),
        "source_snapshot_matches_files": bool(
            snapshot
            and snapshot.get("english_tree_hash") == english["tree_hash"]
            and snapshot.get("spanish_tree_hash") == spanish["tree_hash"]
        ),
        "no_existing_active_policy": existing_policy is None,
    }
    return {
        "frozen": dict(frozen) if frozen else None,
        "active_score": active_score,
        "candidate_score": candidate_score,
        "source_snapshot": snapshot,
        "old_tree_hash": old_hash,
        "output_tree_hash": output_hash,
        "old_file_count": old_file_count,
        "output_file_count": output_file_count,
        "new_segment_count": new_segment_count,
        "old_text_segment_count": old_text_segment_count,
        "output_text_segment_count": output_text_segment_count,
        "existing_policy_run_id": int(existing_policy["id"]) if existing_policy else None,
        "guards": guards,
        "global_allowed": all(guards.values()),
    }


def fetch_rows(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    active_score_run_id = int(state.get("active_score_run_id") or 0)
    candidate_score_run_id = int(state.get("candidate_score_run_id") or 0)
    existing_policy_segments = {
        int(row[0])
        for row in conn.execute(
            """
            SELECT item.segment_id
            FROM auto_confirmation_reopen_lifecycle_policy_items item
            JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
            WHERE run.policy_name = ? AND run.policy_status = 'active'
              AND item.policy_action = ? AND item.policy_allowed = 1
            """,
            (POLICY_NAME, POLICY_ACTION),
        ).fetchall()
    }
    source_rows = conn.execute(
        """
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.final_state,
            state.state_group,
            state.review_state,
            state.locked,
            state.active_action,
            state.candidate_action,
            state.policy_action,
            state.reasons_json AS state_reasons_json,
            state.has_output,
            state.confirmed_matches_output,
            state.needs_output_apply,
            source.is_active,
            source.has_old,
            source.old_text,
            source.old_hash,
            source.spanish_text,
            source.english_text,
            output.portuguese_text,
            output.portuguese_hash,
            confirmation.confirmed_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked AS confirmation_locked,
            active.model_safe_probability AS old_score,
            active.final_action AS active_final_action,
            active.risk_class AS active_risk_class,
            active.token_status AS active_token_status,
            active.issue_count AS active_issue_count,
            active.high_issue_count AS active_high_issue_count,
            active.medium_issue_count AS active_medium_issue_count,
            active.reasons_json AS active_reasons_json,
            active.issues_json AS active_issues_json,
            candidate.candidate_text,
            candidate.model_safe_probability AS candidate_score,
            candidate.final_action AS candidate_final_action,
            candidate.risk_class AS candidate_risk_class,
            candidate.token_status AS candidate_token_status,
            candidate.issue_count AS candidate_issue_count,
            candidate.high_issue_count AS candidate_high_issue_count,
            candidate.medium_issue_count AS candidate_medium_issue_count,
            candidate.reasons_json AS candidate_reasons_json,
            candidate.issues_json AS candidate_issues_json,
            policy.policy_group,
            policy.reasons_json AS policy_reasons_json
        FROM segment_state_items state
        JOIN source_segments source ON source.id = state.segment_id
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = state.segment_id
        LEFT JOIN ml_score_items active
          ON active.run_id = ? AND active.segment_id = state.segment_id
        LEFT JOIN ml_score_items candidate
          ON candidate.run_id = ? AND candidate.segment_id = state.segment_id
        LEFT JOIN ml_policy_items policy
          ON policy.run_id = ? AND policy.segment_id = state.segment_id
        WHERE state.run_id = ? AND state.state_group = 'pending'
        ORDER BY state.segment_id
        """,
        (
            active_score_run_id,
            candidate_score_run_id,
            int(state.get("policy_run_id") or 0),
            int(state["id"]),
        ),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for raw in source_rows:
        row = dict(raw)
        families = pending_classifier.micro_families(row)
        primary_family = pending_classifier.primary_family(families)
        lane = learning_lane(primary_family)
        local_guards = {
            "pending_reopen_state": (
                row.get("state_group") == "pending"
                and row.get("final_state") == "reopen_auto_confirmed_autofix"
            ),
            "source_active": int(row.get("is_active") or 0) == 1,
            "source_has_old": int(row.get("has_old") or 0) == 1,
            "output_present": bool(str(row.get("portuguese_text") or "").strip()),
            "output_equals_old_exact": row.get("portuguese_text") == row.get("old_text"),
            "confirmation_present": row.get("confirmed_text") is not None,
            "confirmation_matches_output_flag": int(row.get("confirmed_matches_output") or 0) == 1,
            "confirmation_matches_output_canonical": (
                canonical(row.get("confirmed_text")) == canonical(row.get("portuguese_text"))
            ),
            "needs_output_apply_zero": int(row.get("needs_output_apply") or 0) == 0,
            "not_already_materialized": int(row["segment_id"]) not in existing_policy_segments,
        }
        allowed = bool(context["global_allowed"] and all(local_guards.values()))
        block_reasons = [name for name, value in context["guards"].items() if not value]
        block_reasons.extend(name for name, value in local_guards.items() if not value)
        old_score = row.get("old_score")
        priority = 1.0 - float(old_score if old_score is not None else 0.0)
        row.update(
            {
                "families": families,
                "primary_family": primary_family,
                "learning_lane": lane,
                "local_guards": local_guards,
                "policy_allowed": int(allowed),
                "block_reason": ";".join(block_reasons),
                "policy_action": POLICY_ACTION,
                "expected_final_state": EXPECTED_FINAL_STATE if allowed else None,
                "review_priority": round(priority, 6),
                "baseline_text_hash": row.get("old_hash") or text_hash(row.get("old_text")),
                "output_text_hash": row.get("portuguese_hash") or text_hash(row.get("portuguese_text")),
                "confirmation_text_hash": text_hash(row.get("confirmed_text")),
                "source_delta_status": SOURCE_DELTA_STATUS,
            }
        )
        rows.append(row)
    return rows


def insert_policy_run(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    paths: dict[str, Path],
    now: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version, queue_run_id, audit_run_id, policy_name, label_family,
            policy_status, candidate_count, released_count, blocked_count,
            manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
            started_at, finished_at, updated_at
        ) VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            POLICY_NAME,
            LABEL_FAMILY,
            len(rows),
            len(rows),
            str(paths["markdown"]),
            str(paths["csv"]),
            str(paths["jsonl"]),
            now,
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
            run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
            segment_id, relative_path, source_key, source_line_number,
            label_family, confirmation_label, policy_action, policy_allowed,
            block_reason, output_match_kind, token_status, issue_count,
            high_issue_count, model_safe_probability, review_priority,
            reasons_json, created_at
        ) VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, 1, '', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                LABEL_FAMILY,
                row.get("confirmation_label"),
                POLICY_ACTION,
                OUTPUT_MATCH_KIND,
                row.get("candidate_token_status") or "not_measured",
                int(row.get("candidate_issue_count") or 0),
                int(row.get("candidate_high_issue_count") or 0),
                row.get("candidate_score"),
                float(row["review_priority"]),
                json.dumps(
                    {
                        "source": RULE_VERSION,
                        "operational_resolution": "inherit_frozen_v2_baseline",
                        "improvement_backlog_retained": True,
                        "source_delta_status": SOURCE_DELTA_STATUS,
                        "expected_final_state": EXPECTED_FINAL_STATE,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            )
            for row in rows
        ],
    )
    return run_id


def insert_backlog(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    state: dict[str, Any],
    context: dict[str, Any],
    policy_run_id: int,
    paths: dict[str, Path],
    now: str,
) -> int:
    lanes = Counter(row["learning_lane"] for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO v3_improvement_backlog_runs (
            rule_version, source_segment_state_run_id, active_score_run_id,
            candidate_score_run_id, source_snapshot_id, baseline_package_version_id,
            lifecycle_policy_run_id, backlog_status, candidate_count,
            parser_architecture_count, rule_specialist_count, semantic_human_count,
            guard_status, guards_json, report_path, jsonl_path, csv_path,
            started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 'passed', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            int(state["id"]),
            int(state.get("active_score_run_id") or 0),
            int(state.get("candidate_score_run_id") or 0),
            int(context["source_snapshot"].get("id") or 0),
            int(context["frozen"]["id"]),
            policy_run_id,
            len(rows),
            int(lanes.get("parser_or_architecture", 0)),
            int(lanes.get("rule_or_specialist_learning", 0)),
            int(lanes.get("semantic_or_human_review", 0)),
            json.dumps(context["guards"], ensure_ascii=False, sort_keys=True),
            str(paths["markdown"]),
            str(paths["jsonl"]),
            str(paths["csv"]),
            now,
            now,
            now,
        ),
    )
    backlog_run_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO v3_improvement_backlog_items (
            run_id, segment_id, relative_path, source_key, source_line_number,
            baseline_text_hash, output_text_hash, confirmation_text_hash,
            captured_final_state, captured_review_state, confirmation_locked,
            confirmed_matches_output, output_equals_baseline, needs_output_apply,
            primary_family, families_json, learning_lane, old_score, candidate_score,
            backlog_status, review_priority, source_delta_status, reasons_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
        """,
        [
            (
                backlog_run_id,
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row.get("baseline_text_hash"),
                row.get("output_text_hash"),
                row.get("confirmation_text_hash"),
                row.get("final_state"),
                row.get("review_state"),
                int(row.get("confirmation_locked") or row.get("locked") or 0),
                int(row.get("confirmed_matches_output") or 0),
                int(row.get("needs_output_apply") or 0),
                row["primary_family"],
                json.dumps(row["families"], ensure_ascii=False, sort_keys=True),
                row["learning_lane"],
                row.get("old_score"),
                row.get("candidate_score"),
                float(row["review_priority"]),
                SOURCE_DELTA_STATUS,
                json.dumps(
                    {
                        "operational_state_closed_by_baseline_inheritance": True,
                        "quality_work_remains_open": True,
                        "candidate_action": row.get("candidate_action"),
                        "candidate_risk_class": row.get("candidate_risk_class"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                now,
            )
            for row in rows
        ],
    )
    return backlog_run_id


def serializable_row(row: dict[str, Any]) -> dict[str, Any]:
    omitted = {
        "old_text",
        "spanish_text",
        "english_text",
        "portuguese_text",
        "confirmed_text",
        "candidate_reasons_json",
        "candidate_issues_json",
        "active_reasons_json",
        "active_issues_json",
        "policy_reasons_json",
        "state_reasons_json",
    }
    return {key: value for key, value in row.items() if key not in omitted}


def write_reports(
    *,
    mode: str,
    paths: dict[str, Path],
    state: dict[str, Any],
    context: dict[str, Any],
    rows: list[dict[str, Any]],
    policy_run_id: int | None,
    backlog_run_id: int | None,
) -> dict[str, Any]:
    released = sum(int(row["policy_allowed"]) for row in rows)
    lanes = Counter(row["learning_lane"] for row in rows)
    families = Counter(row["primary_family"] for row in rows)
    blocked_reasons = Counter(
        reason
        for row in rows
        if not row["policy_allowed"]
        for reason in str(row["block_reason"] or "unknown").split(";")
        if reason
    )
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "expected_final_state": EXPECTED_FINAL_STATE,
        "policy_run_id": policy_run_id,
        "backlog_run_id": backlog_run_id,
        "segment_state_run_id": int(state["id"]),
        "candidate_count": len(rows),
        "released_count": released,
        "blocked_count": len(rows) - released,
        "pending_count_at_capture": int(state.get("pending_count") or 0),
        "expected_delta_after_segment_state": {
            "closed": released,
            "pending": -released,
            "reopen": -released,
            "needs_output_apply": 0,
        },
        "global_allowed": context["global_allowed"],
        "global_guards": context["guards"],
        "blocked_reason_counts": dict(blocked_reasons),
        "learning_lane_counts": dict(lanes),
        "primary_family_counts": dict(families.most_common()),
        "writes_source": False,
        "writes_output": False,
        "writes_confirmations": False,
        "writes_lifecycle_policy": mode == "apply",
        "writes_improvement_backlog": mode == "apply",
        "runs_segment_state": False,
        "source_delta_status": SOURCE_DELTA_STATUS,
    }
    paths["summary"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(serializable_row(row), ensure_ascii=False, sort_keys=True) + "\n")
    with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "segment_id",
                "policy_allowed",
                "block_reason",
                "learning_lane",
                "primary_family",
                "old_score",
                "candidate_score",
                "relative_path",
                "source_key",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["segment_id"],
                    row["policy_allowed"],
                    row["block_reason"],
                    row["learning_lane"],
                    row["primary_family"],
                    row.get("old_score"),
                    row.get("candidate_score"),
                    row["relative_path"],
                    row["source_key"],
                ]
            )
    lines = [
        "# V3 stable baseline inheritance materializer",
        "",
        f"- Mode: `{mode}`",
        f"- Source segment-state: `{state['id']}`",
        f"- Policy run: `{policy_run_id}`",
        f"- Improvement backlog run: `{backlog_run_id}`",
        f"- Candidates: `{len(rows)}`",
        f"- Released: `{released}`",
        f"- Blocked: `{len(rows) - released}`",
        f"- Expected final state: `{EXPECTED_FINAL_STATE}`",
        f"- Source delta status: `{SOURCE_DELTA_STATUS}`",
        "",
        "## Meaning",
        "",
        "Operational closure inherits the frozen V2 package because current `spanish_old`, current output and V2 are identical. ",
        "It does not declare the text semantically perfect. Every released segment remains open in the V3 improvement backlog.",
        "",
        "## Global guards",
        "",
    ]
    lines.extend(f"- `{name}`: `{value}`" for name, value in context["guards"].items())
    lines.extend(["", "## Backlog lanes", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in lanes.most_common())
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "No source, output, confirmation, reindex or production-full write is performed. ",
            "Apply mode writes only the lifecycle policy and the durable V3 improvement backlog.",
        ]
    )
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Materialize V2 stable-baseline inheritance and retain V3 improvement work."
    )
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    paths = report_paths(mode)
    settings = db.load_settings()

    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            state = latest_finished_state(conn, args.segment_state_run_id)
            context = global_context(conn, state)
            rows = fetch_rows(conn, state, context)
            if not context["global_allowed"]:
                write_reports(
                    mode=mode,
                    paths=paths,
                    state=state,
                    context=context,
                    rows=rows,
                    policy_run_id=None,
                    backlog_run_id=None,
                )
                raise SystemExit("apply blocked: one or more global guards failed")
            if len(rows) != int(state.get("pending_count") or 0) or any(
                not row["policy_allowed"] for row in rows
            ):
                write_reports(
                    mode=mode,
                    paths=paths,
                    state=state,
                    context=context,
                    rows=rows,
                    policy_run_id=None,
                    backlog_run_id=None,
                )
                raise SystemExit("apply blocked: not every pending segment passed local guards")
            now = db.utc_now()
            policy_run_id = insert_policy_run(conn, rows, paths, now)
            backlog_run_id = insert_backlog(
                conn, rows, state, context, policy_run_id, paths, now
            )
            summary = write_reports(
                mode=mode,
                paths=paths,
                state=state,
                context=context,
                rows=rows,
                policy_run_id=policy_run_id,
                backlog_run_id=backlog_run_id,
            )
            conn.commit()
    else:
        database_path = db.get_database_path(settings)
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            state = latest_finished_state(conn, args.segment_state_run_id)
            context = global_context(conn, state)
            rows = fetch_rows(conn, state, context)
        summary = write_reports(
            mode=mode,
            paths=paths,
            state=state,
            context=context,
            rows=rows,
            policy_run_id=None,
            backlog_run_id=None,
        )

    print("[v3-baseline] Materializer completed")
    print(f"[v3-baseline] Mode: {mode}")
    print(f"[v3-baseline] Segment-state: {summary['segment_state_run_id']}")
    print(f"[v3-baseline] Candidates: {summary['candidate_count']}")
    print(f"[v3-baseline] Released: {summary['released_count']}")
    print(f"[v3-baseline] Blocked: {summary['blocked_count']}")
    print(f"[v3-baseline] Policy run: {summary['policy_run_id']}")
    print(f"[v3-baseline] Backlog run: {summary['backlog_run_id']}")
    print(f"[v3-baseline] Summary: {paths['summary']}")
    print(f"[v3-baseline] Markdown: {paths['markdown']}")
    print("[v3-baseline] Writes source/output: false")
    return summary


if __name__ == "__main__":
    main()
