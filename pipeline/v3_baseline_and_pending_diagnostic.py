from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import pending_architecture_diagnostic as pending_classifier


RULE_VERSION = "v3_baseline_and_pending_diagnostic_v1"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def tree_fingerprint(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*.yml") if path.is_file())
    newest_mtime = 0.0
    total_bytes = 0
    for path in files:
        relative_path = path.relative_to(root).as_posix()
        stat = path.stat()
        newest_mtime = max(newest_mtime, stat.st_mtime)
        total_bytes += stat.st_size
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(db.file_hash(path).encode("ascii"))
        digest.update(b"\n")
    return {
        "path": str(root.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "tree_hash": digest.hexdigest(),
        "newest_mtime": (
            datetime.fromtimestamp(newest_mtime).isoformat(timespec="seconds")
            if newest_mtime
            else None
        ),
    }


def latest_finished_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 1000
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed operational segment-state run was found.")
    return dict(row)


def score_metadata(conn: sqlite3.Connection, run_id: int | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    row = conn.execute(
        """
        SELECT id, rule_version, model_run_id, model_version, scored_count,
               started_at, finished_at, path_filter, limit_count,
               source_snapshot_id, candidate_text_source, candidate_tree_hash
        FROM ml_score_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def package_version_metadata(conn: sqlite3.Connection, version_number: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, version_number, version_label, package_role, source_path,
               package_hash, segment_count, frozen_at
        FROM package_versions
        WHERE version_number = ?
        """,
        (version_number,),
    ).fetchone()
    return dict(row) if row else None


def latest_source_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM source_tree_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def fetch_pending_rows(conn: sqlite3.Connection, state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            state.*,
            source.spanish_text,
            source.english_text,
            source.old_text,
            source.has_old,
            output.portuguese_text,
            confirmation.confirmed_text,
            confirmation.confirmation_source,
            active.final_action AS active_final_action,
            active.model_safe_probability AS active_model_safe_probability,
            active.risk_class AS active_risk_class,
            active.token_status AS active_token_status,
            active.issue_count AS active_issue_count,
            active.high_issue_count AS active_high_issue_count,
            active.medium_issue_count AS active_medium_issue_count,
            active.word_count AS active_word_count,
            active.reasons_json AS active_reasons_json,
            active.issues_json AS active_issues_json,
            candidate.candidate_text,
            candidate.final_action AS candidate_final_action,
            candidate.model_safe_probability AS candidate_model_safe_probability,
            candidate.risk_class AS candidate_risk_class,
            candidate.token_status AS candidate_token_status,
            candidate.issue_count AS candidate_issue_count,
            candidate.high_issue_count AS candidate_high_issue_count,
            candidate.medium_issue_count AS candidate_medium_issue_count,
            candidate.word_count AS candidate_word_count,
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
        WHERE state.run_id = ?
          AND state.state_group = 'pending'
        ORDER BY state.priority_score DESC, state.segment_id
        """,
        (
            int(state.get("active_score_run_id") or 0),
            int(state.get("candidate_score_run_id") or 0),
            int(state.get("policy_run_id") or 0),
            int(state["id"]),
        ),
    ).fetchall()
    return [dict(row) for row in rows]


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


def analyze_rows(
    rows: list[dict[str, Any]],
    *,
    stable_hash_matches_old: bool,
    stable_hash_matches_output: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    primary_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    overlap_counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records: list[dict[str, Any]] = []

    operational_inherit_candidates = 0
    human_evidence_count = 0
    output_differs_old_count = 0
    confirmation_mismatch_count = 0
    needs_apply_count = 0

    for row in rows:
        families = pending_classifier.micro_families(row)
        primary_family = pending_classifier.primary_family(families)
        lane = learning_lane(primary_family)
        output_text = str(row.get("portuguese_text") or "")
        old_text = str(row.get("old_text") or "")
        output_equals_old = output_text == old_text
        confirmation_matches_output = int(row.get("confirmed_matches_output") or 0) == 1
        needs_output_apply = int(row.get("needs_output_apply") or 0) == 1
        has_output = int(row.get("has_output") or 0) == 1
        human_evidence = str(row.get("review_state") or "") in {"human_locked", "human_confirmed"}
        inherit_candidate = bool(
            stable_hash_matches_old
            and stable_hash_matches_output
            and output_equals_old
            and confirmation_matches_output
            and not needs_output_apply
            and has_output
        )

        primary_counts[primary_family] += 1
        lane_counts[lane] += 1
        review_counts[str(row.get("review_state") or "unknown")] += 1
        action_counts[
            "|".join(
                str(row.get(name) or "")
                for name in ("active_action", "candidate_action", "policy_action")
            )
        ] += 1
        for family in families:
            overlap_counts[family] += 1
        operational_inherit_candidates += int(inherit_candidate)
        human_evidence_count += int(human_evidence)
        output_differs_old_count += int(not output_equals_old)
        confirmation_mismatch_count += int(not confirmation_matches_output)
        needs_apply_count += int(needs_output_apply)

        record = {
            "segment_id": int(row["segment_id"]),
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "source_line_number": row.get("source_line_number"),
            "final_state": row.get("final_state"),
            "review_state": row.get("review_state"),
            "locked": int(row.get("locked") or 0),
            "confirmed_matches_output": int(confirmation_matches_output),
            "needs_output_apply": int(needs_output_apply),
            "output_equals_old": output_equals_old,
            "operational_inherit_candidate": inherit_candidate,
            "operational_resolution": (
                "inherit_stable_v2_after_source_guard"
                if inherit_candidate
                else "retain_operational_pending"
            ),
            "primary_family": primary_family,
            "families": families,
            "learning_lane": lane,
            "active_action": row.get("active_action"),
            "candidate_action": row.get("candidate_action"),
            "policy_action": row.get("policy_action"),
            "old_score": row.get("active_model_safe_probability"),
            "candidate_score": row.get("candidate_model_safe_probability"),
            "output_text": output_text,
        }
        records.append(record)
        if len(examples[primary_family]) < 5:
            examples[primary_family].append(record)

    summary = {
        "pending_count": len(rows),
        "operational_inherit_candidate_count": operational_inherit_candidates,
        "improvement_backlog_count": len(rows),
        "human_evidence_count": human_evidence_count,
        "output_differs_old_count": output_differs_old_count,
        "confirmation_mismatch_count": confirmation_mismatch_count,
        "needs_output_apply_count": needs_apply_count,
        "primary_family_counts": dict(primary_counts.most_common()),
        "learning_lane_counts": dict(lane_counts.most_common()),
        "review_state_counts": dict(review_counts.most_common()),
        "action_combo_counts": dict(action_counts.most_common()),
        "overlap_family_counts": dict(overlap_counts.most_common()),
        "examples": dict(examples),
    }
    return summary, records


def write_reports(payload: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, str]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_v3_baseline_and_pending_diagnostic_readonly"
    summary_path = base.with_name(base.name + "_summary.json")
    jsonl_path = base.with_suffix(".jsonl")
    markdown_path = base.with_suffix(".md")

    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    state = payload["segment_state"]
    analysis = payload["analysis"]
    score_freshness = payload["score_freshness"]
    lines = [
        "# V3 baseline and pending diagnostic",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Segment-state: `{state['id']}`",
        f"- Total: `{state['total_segments']}`",
        f"- Closed: `{state['closed_count']}`",
        f"- Pending operational: `{state['pending_count']}`",
        f"- Needs output apply: `{state['output_apply_pending_count']}`",
        "",
        "## Baseline inheritance",
        "",
        f"- Current `spanish_old` matches frozen V2: `{payload['baseline_guard']['old_matches_v2']}`",
        f"- Current `output/spanish` matches frozen V2: `{payload['baseline_guard']['output_matches_v2']}`",
        f"- Current source snapshot: `{(payload.get('current_source_snapshot') or {}).get('id')}`",
        f"- Source snapshot matches current files: `{payload['baseline_guard']['current_source_snapshot_matches_files']}`",
        f"- Pending rows with output equal to old: `{analysis['pending_count'] - analysis['output_differs_old_count']}`",
        f"- Pending rows confirmed equal to output: `{analysis['pending_count'] - analysis['confirmation_mismatch_count']}`",
        f"- Conditional V2 inheritance candidates: `{analysis['operational_inherit_candidate_count']}`",
        "",
        "These rows may be operationally closed by inheriting V2 while remaining in a separate improvement backlog. "
        "Mass closure must wait for a source-delta guard because the pre-update source fingerprint was not frozen.",
        "",
        "## Score freshness",
        "",
        f"- Active score run: `{score_freshness['active_score_run_id']}`",
        f"- Candidate score run: `{score_freshness['candidate_score_run_id']}`",
        f"- Scores newer than current sources: `{score_freshness['scores_are_current_for_sources']}`",
        f"- Reason: `{score_freshness['reason']}`",
        "",
        "Zero promotions therefore means that the current queue found no promotion with the referenced runs; "
        "it does not prove that all 288,100 segments were rescored after the game update.",
        "",
        "## Improvement backlog by lane",
        "",
    ]
    for lane, count in analysis["learning_lane_counts"].items():
        lines.append(f"- `{lane}`: `{count}`")
    lines.extend(["", "## Primary families", ""])
    for family, count in list(analysis["primary_family_counts"].items())[:20]:
        lines.append(f"- `{family}`: `{count}`")
    lines.extend(
        [
            "",
            "## Recommended order",
            "",
            "1. Materialize stable-baseline inheritance as operational closure, without deleting the improvement backlog.",
            "2. Audit the high-ROI `autofix_unknown` and `short_label_style` lanes under the fresh score pair.",
            "3. Route semantic/religion/culture cases to small human-review packets with explicit final text.",
            "4. Keep dynamic/gender/structural families in parser or architecture lanes until their metadata is explicit.",
            "5. Re-run Evaluation after each learned rule so old and output are rescored under the same contract.",
            "",
            "## Safety",
            "",
            "Read-only diagnostic. No source/output write, apply, confirmation, lifecycle, segment-state, reindex, or training was executed.",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "summary": str(summary_path),
        "jsonl": str(jsonl_path),
        "markdown": str(markdown_path),
    }


def main() -> dict[str, Any]:
    settings = db.load_settings()
    roots = {
        "english_source": db.project_path(settings["english_source"]),
        "spanish_source": db.project_path(settings["spanish_source"]),
        "spanish_old": db.project_path(settings["spanish_traduzido_old"]),
        "output_spanish": db.project_path(settings["output_spanish"]),
    }
    fingerprints = {name: tree_fingerprint(path) for name, path in roots.items()}

    with connect_readonly() as conn:
        state = latest_finished_state(conn)
        active_score = score_metadata(conn, int(state.get("active_score_run_id") or 0))
        candidate_score = score_metadata(conn, int(state.get("candidate_score_run_id") or 0))
        frozen_v2 = package_version_metadata(conn, 2)
        source_snapshot = latest_source_snapshot(conn)
        rows = fetch_pending_rows(conn, state)
        new_segments = int(
            conn.execute(
                "SELECT COUNT(*) FROM source_segments WHERE is_active = 1 AND COALESCE(has_old, 0) = 0"
            ).fetchone()[0]
        )
        valid_blank_segments = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM segment_state_items
                WHERE run_id = ? AND final_state = 'closed_valid_blank'
                """,
                (int(state["id"]),),
            ).fetchone()[0]
        )

    frozen_hash = str((frozen_v2 or {}).get("package_hash") or "")
    old_matches_v2 = bool(frozen_hash and fingerprints["spanish_old"]["tree_hash"] == frozen_hash)
    output_matches_v2 = bool(frozen_hash and fingerprints["output_spanish"]["tree_hash"] == frozen_hash)
    analysis, records = analyze_rows(
        rows,
        stable_hash_matches_old=old_matches_v2,
        stable_hash_matches_output=output_matches_v2,
    )

    source_times = [
        value["newest_mtime"]
        for key, value in fingerprints.items()
        if key in {"english_source", "spanish_source"} and value.get("newest_mtime")
    ]
    newest_source_time = max(source_times) if source_times else None
    score_times = [
        str(score.get("finished_at") or score.get("started_at") or "")
        for score in (active_score, candidate_score)
        if score
    ]
    timestamp_order_would_look_current = bool(
        newest_source_time
        and score_times
        and all(score_time >= newest_source_time for score_time in score_times)
    )
    current_snapshot_id = int((source_snapshot or {}).get("id") or 0)
    score_snapshot_ids = {
        int(score.get("source_snapshot_id") or 0)
        for score in (active_score, candidate_score)
        if score
    }
    source_snapshot_matches_files = bool(
        source_snapshot
        and source_snapshot.get("english_tree_hash") == fingerprints["english_source"]["tree_hash"]
        and source_snapshot.get("spanish_tree_hash") == fingerprints["spanish_source"]["tree_hash"]
    )
    # Game archives can preserve original file mtimes when copied into source/.
    # A score is fresh only when its run references the exact source snapshot.
    scores_are_current = bool(
        current_snapshot_id
        and source_snapshot_matches_files
        and score_snapshot_ids == {current_snapshot_id}
    )
    score_reason = (
        "Both score runs reference the exact current source snapshot."
        if scores_are_current
        else "The current sources are fingerprinted, but the referenced score runs predate source-snapshot linkage."
    )

    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "segment_state": state,
        "frozen_v2": frozen_v2,
        "current_source_snapshot": source_snapshot,
        "fingerprints": fingerprints,
        "baseline_guard": {
            "old_matches_v2": old_matches_v2,
            "output_matches_v2": output_matches_v2,
            "source_delta_comparability": "missing_pre_update_source_fingerprint",
            "current_source_snapshot_matches_files": source_snapshot_matches_files,
            "new_segment_count": new_segments,
            "valid_blank_segment_count": valid_blank_segments,
        },
        "score_freshness": {
            "active_score_run_id": (active_score or {}).get("id"),
            "candidate_score_run_id": (candidate_score or {}).get("id"),
            "active_score": active_score,
            "candidate_score": candidate_score,
            "newest_source_mtime": newest_source_time,
            "timestamp_order_would_look_current": timestamp_order_would_look_current,
            "scores_are_current_for_sources": scores_are_current,
            "freshness_status": "current_exact_source_snapshot" if scores_are_current else "stale_or_unlinked",
            "reason": score_reason,
        },
        "analysis": analysis,
    }
    paths = write_reports(payload, records)
    payload["reports"] = paths

    print("[v3-pending] Read-only diagnostic completed")
    print(f"[v3-pending] Segment-state run: {state['id']}")
    print(f"[v3-pending] Pending: {analysis['pending_count']}")
    print(f"[v3-pending] Conditional V2 inheritance: {analysis['operational_inherit_candidate_count']}")
    print(f"[v3-pending] Scores current for sources: {scores_are_current}")
    print(f"[v3-pending] Markdown: {paths['markdown']}")
    print(f"[v3-pending] Summary: {paths['summary']}")
    print(f"[v3-pending] JSONL: {paths['jsonl']}")
    return payload


if __name__ == "__main__":
    main()
