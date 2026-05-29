from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_specialist_policy import OPERATIONAL_SPECIALIST_KEYS, RULE_VERSION as SNAPSHOT_RULE_VERSION
from ml_specialist_models import SPECIALIST_GROUPS, SPECIALISTS


RULE_VERSION = "ml_specialist_ensemble_policy_v1"
POSITIVE_LABELS = {"correct", "contextual_exception"}
NEGATIVE_LABELS = {
    "major_fix",
    "minor_fix",
    "rejected",
    "rejected_suggestion",
    "residual_spanish",
    "semantic_error",
    "structure_error",
    "token_mismatch",
}


def percent(part: int, total: int) -> str:
    if total <= 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_general_score_run(conn) -> int:
    row = conn.execute(
        """
        SELECT r.id
        FROM ml_score_runs r
        JOIN ml_model_runs m ON m.id = r.model_run_id
        WHERE m.model_kind = 'risk_action_classifier'
          AND r.finished_at IS NOT NULL
          AND r.scored_count > 0
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished general risk_action_classifier score run found.")
    return int(row["id"])


def load_score_run(conn, score_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE id = ?
          AND finished_at IS NOT NULL
        """,
        (score_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Score run {score_run_id} is missing or unfinished.")
    return dict(row)


def resolve_specialist_keys(specialist: str | None) -> list[str]:
    if not specialist:
        return list(OPERATIONAL_SPECIALIST_KEYS)
    if specialist in SPECIALIST_GROUPS:
        keys = [key for key in SPECIALIST_GROUPS[specialist] if key in OPERATIONAL_SPECIALIST_KEYS]
    elif specialist in SPECIALISTS:
        keys = [specialist]
    else:
        raise ValueError(f"Unknown specialist or specialist group: {specialist}")
    if not keys:
        raise ValueError(f"No operational specialists selected by: {specialist}")
    return keys


def latest_ready_snapshots(conn, general_score_run_id: int, specialist_keys: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in specialist_keys)
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT specialist_key, MAX(id) AS id
            FROM ml_specialist_policy_snapshots
            WHERE general_score_run_id = ?
              AND specialist_key IN ({placeholders})
            GROUP BY specialist_key
        )
        SELECT s.*
        FROM latest l
        JOIN ml_specialist_policy_snapshots s ON s.id = l.id
        ORDER BY s.specialist_key
        """,
        (general_score_run_id, *specialist_keys),
    ).fetchall()
    snapshots = [dict(row) for row in rows]
    missing = sorted(set(specialist_keys) - {row["specialist_key"] for row in snapshots})
    if missing:
        raise RuntimeError(
            "Missing specialist policy snapshots for: "
            + ", ".join(missing)
            + ". Run ml-specialist-policy first."
        )
    blocked = [
        row
        for row in snapshots
        if not str(row["status"]).startswith("READY")
        or int(row.get("pending_real_count") or 0) > 0
        or int(row.get("threshold_below_policy") or 0) > 0
        or int(row.get("scope_delta_count") or 0) != 0
    ]
    if blocked:
        details = ", ".join(
            f"{row['specialist_key']}:{row['status']}/pending={row.get('pending_real_count')}"
            for row in blocked
        )
        raise RuntimeError(f"Specialist policy is not ready for ensemble dry-run: {details}")
    return snapshots


def fetch_general_items(conn, general_score_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            i.*,
            i.id AS score_item_id,
            o.portuguese_text AS output_text
        FROM ml_score_items i
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE i.run_id = ?
        ORDER BY i.segment_id
        """,
        (general_score_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_specialist_votes(conn, snapshots: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    votes_by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        score_run_id = snapshot.get("score_run_id")
        if score_run_id is None:
            continue
        rows = conn.execute(
            """
            SELECT
                i.*,
                i.id AS specialist_score_item_id,
                o.portuguese_text AS output_text
            FROM ml_score_items i
            LEFT JOIN output_segments o ON o.segment_id = i.segment_id
            WHERE i.run_id = ?
            ORDER BY i.segment_id
            """,
            (score_run_id,),
        ).fetchall()
        for row in rows:
            vote = dict(row)
            vote["specialist_key"] = snapshot["specialist_key"]
            vote["specialist_model_kind"] = snapshot["model_kind"]
            vote["specialist_score_run_id"] = score_run_id
            vote["specialist_model_run_id"] = snapshot["model_run_id"]
            vote["specialist_threshold"] = snapshot["operational_threshold"]
            votes_by_segment[int(vote["segment_id"])].append(vote)
    return votes_by_segment


def fetch_learning_labels(conn) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            segment_id,
            SUM(CASE WHEN human_label IN ({positive}) THEN 1 ELSE 0 END) AS positive_count,
            SUM(CASE WHEN queue_source = 'ml_specialist_auditor'
                      AND human_label IN ({positive})
                     THEN 1 ELSE 0 END) AS specialist_positive_count,
            SUM(CASE WHEN human_label IN ({negative}) THEN 1 ELSE 0 END) AS negative_count,
            GROUP_CONCAT(DISTINCT human_label) AS labels,
            MAX(reviewed_at) AS latest_reviewed_at
        FROM local_learning_candidates
        WHERE local_status = 'reviewed_human'
        GROUP BY segment_id
        """.format(
            positive=",".join("?" for _ in POSITIVE_LABELS),
            negative=",".join("?" for _ in NEGATIVE_LABELS),
        ),
        (*sorted(POSITIVE_LABELS), *sorted(POSITIVE_LABELS), *sorted(NEGATIVE_LABELS)),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def candidate_matches_output(row: dict[str, Any]) -> bool:
    output_text = row.get("output_text")
    return output_text is not None and (row.get("candidate_text") or "") == (output_text or "")


def vote_is_structurally_safe(vote: dict[str, Any]) -> bool:
    return (
        vote.get("final_action") == "auto_safe"
        and vote.get("token_status") == "ok"
        and int(vote.get("issue_count") or 0) == 0
        and int(vote.get("high_issue_count") or 0) == 0
        and int(vote.get("medium_issue_count") or 0) == 0
        and int(vote.get("deterministic_blocked") or 0) == 0
        and candidate_matches_output(vote)
    )


def choose_policy(
    general: dict[str, Any],
    votes: list[dict[str, Any]],
    learning: dict[str, Any],
    protect_general_safe: bool,
) -> dict[str, Any]:
    general_safe = general.get("final_action") == "auto_safe"
    positive = int(learning.get("positive_count") or 0)
    specialist_positive = int(learning.get("specialist_positive_count") or 0)
    negative = int(learning.get("negative_count") or 0)
    safe_votes = [vote for vote in votes if vote_is_structurally_safe(vote)]
    caution_votes = [vote for vote in votes if vote.get("final_action") != "auto_safe"]
    safe_keys = sorted({vote["specialist_key"] for vote in safe_votes})
    caution_keys = sorted({vote["specialist_key"] for vote in caution_votes})

    policy_action = general["final_action"]
    policy_group = "general_score"
    policy_threshold = 0.0
    require_positive = 0
    new_safe = 0
    demoted_safe = 0
    selected_vote = None
    reasons = [
        f"rule:{RULE_VERSION}",
        f"general_action:{general['final_action']}",
        f"general_safe_probability:{float(general.get('model_safe_probability') or 0.0):.4f}",
    ]

    if votes:
        reasons.append("specialist_votes:" + ",".join(sorted({vote["specialist_key"] for vote in votes})))
    if safe_keys:
        reasons.append("specialist_safe_votes:" + ",".join(safe_keys))
    if caution_keys:
        reasons.append("specialist_caution_votes:" + ",".join(caution_keys))
    if positive:
        reasons.append(f"learned_positive:{positive}")
    if specialist_positive:
        reasons.append(f"specialist_positive:{specialist_positive}")
    if negative:
        reasons.append(f"learned_negative:{negative}")

    if general_safe:
        if caution_votes:
            demoted_safe = 1
            policy_group = "specialist_ensemble:protected_demotion"
            selected_vote = max(
                caution_votes,
                key=lambda vote: float(vote.get("model_safe_probability") or 0.0),
            )
            policy_threshold = float(selected_vote.get("specialist_threshold") or 0.0)
            if not protect_general_safe:
                policy_action = "needs_human"
                reasons.append("policy:demoted_by_specialist")
            else:
                reasons.append("policy:protect_general_safe")
        return {
            "policy_action": policy_action,
            "policy_group": policy_group,
            "policy_threshold": policy_threshold,
            "policy_require_learned_positive": require_positive,
            "new_safe": new_safe,
            "demoted_safe": demoted_safe,
            "selected_vote": selected_vote,
            "learned_positive": int(positive > 0),
            "learned_negative": int(negative > 0),
            "reasons": reasons,
        }

    if not safe_votes:
        if votes:
            policy_group = "specialist_ensemble:no_safe_vote"
        return {
            "policy_action": policy_action,
            "policy_group": policy_group,
            "policy_threshold": policy_threshold,
            "policy_require_learned_positive": require_positive,
            "new_safe": new_safe,
            "demoted_safe": demoted_safe,
            "selected_vote": None,
            "learned_positive": int(positive > 0),
            "learned_negative": int(negative > 0),
            "reasons": reasons,
        }

    require_positive = 1
    selected_vote = max(safe_votes, key=lambda vote: float(vote.get("model_safe_probability") or 0.0))
    policy_group = f"specialist_ensemble:{selected_vote['specialist_key']}"
    policy_threshold = float(selected_vote.get("specialist_threshold") or 0.0)
    if negative:
        reasons.append("policy:block_learned_negative")
    elif caution_votes:
        reasons.append("policy:block_specialist_conflict")
    elif not specialist_positive:
        reasons.append("policy:block_missing_specialist_positive_review")
    else:
        policy_action = "auto_safe"
        new_safe = 1
        reasons.append("policy:new_safe_from_reviewed_specialist")

    return {
        "policy_action": policy_action,
        "policy_group": policy_group,
        "policy_threshold": policy_threshold,
        "policy_require_learned_positive": require_positive,
        "new_safe": new_safe,
        "demoted_safe": demoted_safe,
        "selected_vote": selected_vote,
        "learned_positive": int(positive > 0),
        "learned_negative": int(negative > 0),
        "reasons": reasons,
    }


def insert_policy_run(
    conn,
    general_score: dict[str, Any],
    started_at: datetime,
    protect_general_safe: bool,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_policy_runs (
            rule_version,
            score_run_id,
            model_run_id,
            model_version,
            protect_active_safe,
            notes,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            general_score["id"],
            general_score["model_run_id"],
            general_score["model_version"],
            int(protect_general_safe),
            "Specialist ensemble dry-run; requires ready specialist policy snapshots and positive divergence review. Does not update scores, confirmations, or output files.",
            started_at.isoformat(timespec="seconds"),
            started_at.isoformat(timespec="seconds"),
        ),
    )
    return int(cursor.lastrowid)


def insert_policy_items(
    conn,
    policy_run_id: int,
    general_score_run_id: int,
    rows: list[dict[str, Any]],
    created_at: str,
) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO ml_policy_items (
            run_id,
            score_item_id,
            score_run_id,
            segment_id,
            relative_path,
            source_key,
            policy_group,
            policy_threshold,
            policy_require_learned_positive,
            score_final_action,
            policy_action,
            new_safe,
            demoted_safe,
            learned_positive,
            learned_negative,
            model_safe_probability,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                policy_run_id,
                row["score_item_id"],
                general_score_run_id,
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["policy_group"],
                row["policy_threshold"],
                row["policy_require_learned_positive"],
                row["score_final_action"],
                row["policy_action"],
                row["new_safe"],
                row["demoted_safe"],
                row["learned_positive"],
                row["learned_negative"],
                row["model_safe_probability"],
                json.dumps(row["reasons"], ensure_ascii=False),
                created_at,
            )
            for row in rows
        ],
    )


def update_policy_run(
    conn,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    finished_at: str,
) -> None:
    total = len(rows)
    active_auto = sum(1 for row in rows if row["score_final_action"] == "auto_safe")
    policy_auto = sum(1 for row in rows if row["policy_action"] == "auto_safe")
    new_safe = sum(int(row["new_safe"]) for row in rows)
    demoted_safe = sum(int(row["demoted_safe"]) for row in rows)
    conn.execute(
        """
        UPDATE ml_policy_runs
        SET scored_count = ?,
            active_auto_safe_count = ?,
            policy_auto_safe_count = ?,
            new_safe_count = ?,
            demoted_safe_count = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (total, active_auto, policy_auto, new_safe, demoted_safe, finished_at, finished_at, policy_run_id),
    )


def write_csv(settings: dict[str, Any], rows: list[dict[str, Any]], started_at: datetime) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{started_at.strftime('%Y%m%d_%H%M%S')}_ml_specialist_ensemble_policy_changes.csv"
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "score_final_action",
        "policy_action",
        "new_safe",
        "demoted_safe",
        "policy_group",
        "model_safe_probability",
        "learned_positive",
        "learned_negative",
        "candidate_text",
        "output_text",
        "reasons_json",
    ]
    selected = [row for row in rows if row["new_safe"] or row["demoted_safe"]]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "score_final_action": row["score_final_action"],
                    "policy_action": row["policy_action"],
                    "new_safe": row["new_safe"],
                    "demoted_safe": row["demoted_safe"],
                    "policy_group": row["policy_group"],
                    "model_safe_probability": row["model_safe_probability"],
                    "learned_positive": row["learned_positive"],
                    "learned_negative": row["learned_negative"],
                    "candidate_text": row.get("candidate_text") or "",
                    "output_text": row.get("output_text") or "",
                    "reasons_json": json.dumps(row["reasons"], ensure_ascii=False),
                }
            )
    return path


def sample_lines(rows: list[dict[str, Any]], key: str, limit: int) -> list[str]:
    selected = [row for row in rows if row[key]]
    if not selected:
        return ["- none"]
    lines = []
    for row in selected[:limit]:
        lines.append(
            f"- {row['policy_group']} | prob={float(row.get('model_safe_probability') or 0.0):.4f} | "
            f"{row['relative_path']}::{row['source_key']} | {row.get('candidate_text') or ''}"
        )
    return lines


def main(
    general_score_run_id: int | None = None,
    sample_limit: int = 40,
    protect_general_safe: bool = True,
    specialist: str | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_specialist_ensemble_policy] Starting specialist ensemble policy dry-run")
    print(f"[ml_specialist_ensemble_policy] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        general_score_run_id = general_score_run_id or latest_general_score_run(conn)
        general_score = load_score_run(conn, general_score_run_id)
        specialist_keys = resolve_specialist_keys(specialist)
        snapshots = latest_ready_snapshots(conn, general_score_run_id, specialist_keys)
        general_items = fetch_general_items(conn, general_score_run_id)
        specialist_votes = fetch_specialist_votes(conn, snapshots)
        learning_labels = fetch_learning_labels(conn)

    policy_rows: list[dict[str, Any]] = []
    group_counts: Counter[str] = Counter()
    block_counts: Counter[str] = Counter()
    for general in general_items:
        segment_id = int(general["segment_id"])
        votes = specialist_votes.get(segment_id, [])
        learning = learning_labels.get(segment_id, {})
        decision = choose_policy(general, votes, learning, protect_general_safe)
        selected_vote = decision["selected_vote"]
        probability = (
            selected_vote.get("model_safe_probability")
            if selected_vote is not None
            else general.get("model_safe_probability")
        )
        policy_row = {
            "score_item_id": general["score_item_id"],
            "segment_id": segment_id,
            "relative_path": general["relative_path"],
            "source_key": general["source_key"],
            "score_final_action": general["final_action"],
            "policy_action": decision["policy_action"],
            "new_safe": int(decision["new_safe"]),
            "demoted_safe": int(decision["demoted_safe"]),
            "policy_group": decision["policy_group"],
            "policy_threshold": decision["policy_threshold"],
            "policy_require_learned_positive": int(decision["policy_require_learned_positive"]),
            "learned_positive": decision["learned_positive"],
            "learned_negative": decision["learned_negative"],
            "model_safe_probability": probability,
            "candidate_text": general.get("candidate_text"),
            "output_text": general.get("output_text"),
            "reasons": decision["reasons"],
        }
        policy_rows.append(policy_row)
        group_counts[policy_row["policy_group"]] += 1
        for reason in decision["reasons"]:
            if reason.startswith("policy:block_"):
                block_counts[reason] += 1

    new_safe_rows = sorted(
        [row for row in policy_rows if row["new_safe"]],
        key=lambda row: (row["policy_group"], -float(row.get("model_safe_probability") or 0.0), row["segment_id"]),
    )
    demoted_rows = sorted(
        [row for row in policy_rows if row["demoted_safe"]],
        key=lambda row: (row["policy_group"], -float(row.get("model_safe_probability") or 0.0), row["segment_id"]),
    )
    unchanged_safe = sum(
        1 for row in policy_rows if row["score_final_action"] == "auto_safe" and row["policy_action"] == "auto_safe"
    )
    policy_auto = unchanged_safe + len(new_safe_rows)
    active_auto = sum(1 for row in policy_rows if row["score_final_action"] == "auto_safe")
    finished_at = datetime.now().isoformat(timespec="seconds")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        policy_run_id = insert_policy_run(conn, general_score, started_at, protect_general_safe)
        insert_policy_items(conn, policy_run_id, general_score_run_id, policy_rows, finished_at)
        update_policy_run(conn, policy_run_id, policy_rows, finished_at)
        conn.commit()

    csv_path = write_csv(settings, policy_rows, started_at)
    elapsed = datetime.now() - started_at
    report_lines = [
        "ML specialist ensemble policy dry-run",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Snapshot rule version: {SNAPSHOT_RULE_VERSION}",
        f"Requested specialists: {specialist or 'all_operational'}",
        f"General score run id: {general_score_run_id}",
        f"General model run id: {general_score['model_run_id']}",
        f"General model version: {general_score['model_version']}",
        f"Policy run id: {policy_run_id}",
        f"Protect general safe: {protect_general_safe}",
        f"CSV changes: {csv_path}",
        "",
        "Summary:",
        f"- rows evaluated: {len(policy_rows)}",
        f"- active/general auto_safe: {active_auto} ({percent(active_auto, len(policy_rows))})",
        f"- ensemble policy auto_safe: {policy_auto} ({percent(policy_auto, len(policy_rows))})",
        f"- new safe from specialists: {len(new_safe_rows)}",
        f"- protected specialist demotions: {len(demoted_rows)}",
        f"- net coverage gain: {policy_auto - active_auto}",
        "",
        "Ready specialist snapshots:",
    ]
    for snapshot in snapshots:
        report_lines.append(
            f"- {snapshot['specialist_key']} | status={snapshot['status']} | "
            f"model={snapshot['model_run_id']} | score={snapshot['score_run_id']} | "
            f"auto={snapshot['final_auto_safe_count']}/{snapshot['scored_count']} "
            f"({percent(int(snapshot['final_auto_safe_count'] or 0), int(snapshot['scored_count'] or 0))}) | "
            f"pending={snapshot['pending_real_count']}"
        )
    report_lines.extend(
        [
            "",
            "Policy groups:",
            *[f"- {group}: {count}" for group, count in group_counts.most_common(30)],
            "",
            "Blocked specialist promotions:",
            *[f"- {reason}: {count}" for reason, count in block_counts.most_common()],
            "",
            f"Top {min(sample_limit, len(new_safe_rows))} new safe rows:",
            *sample_lines(new_safe_rows, "new_safe", sample_limit),
            "",
            f"Top {min(sample_limit, len(demoted_rows))} protected demotion rows:",
            *sample_lines(demoted_rows, "demoted_safe", sample_limit),
            "",
            "Interpretation:",
            "- This is a dry-run materialization only; it does not update ml_score_items, confirmations, models, or output files.",
            "- Specialist new-safe rows require a positive human review from the specialist auditor queue and no learned negative label.",
            "- Conflicting specialist votes block promotion.",
            "- General auto-safe rows are protected by default; specialist demotions are recorded as audit evidence.",
        ]
    )
    report_path = db.write_report(settings, "ml_specialist_ensemble_policy", report_lines)
    print(f"[ml_specialist_ensemble_policy] Policy run id: {policy_run_id}")
    print(f"[ml_specialist_ensemble_policy] Active auto_safe: {active_auto}")
    print(f"[ml_specialist_ensemble_policy] Ensemble auto_safe: {policy_auto}")
    print(f"[ml_specialist_ensemble_policy] New safe: {len(new_safe_rows)}")
    print(f"[ml_specialist_ensemble_policy] Protected demotions: {len(demoted_rows)}")
    print(f"[ml_specialist_ensemble_policy] CSV: {csv_path}")
    print(f"[ml_specialist_ensemble_policy] Report: {report_path}")
    print("[ml_specialist_ensemble_policy] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Materialize a dry-run policy combining the general score and ready specialists.")
    parser.add_argument("--general-score-run-id", type=int, default=None)
    parser.add_argument("--specialist", default=None)
    parser.add_argument("--sample-limit", type=int, default=40)
    parser.add_argument("--allow-demotion", action="store_true")
    args = parser.parse_args()
    main(
        general_score_run_id=args.general_score_run_id,
        sample_limit=args.sample_limit,
        protect_general_safe=not args.allow_demotion,
        specialist=args.specialist,
    )
