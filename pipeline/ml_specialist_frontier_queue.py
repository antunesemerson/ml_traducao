from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_specialist_models import SPECIALIST_GROUPS, SPECIALISTS


RULE_VERSION = "ml_specialist_frontier_queue_v1"
POLICY_GROUP = "specialist_ensemble:no_safe_vote"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_policy_runs
        WHERE scored_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_policy_runs found. Run ml-specialist-ensemble-policy first.")
    return int(row["id"])


def next_lote_number(conn) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review_%_lote%'
        """
    ).fetchall()
    lote_numbers: list[int] = []
    for row in rows:
        match = re.search(r"lote(\d+)", str(row["mode"] or ""))
        if match:
            lote_numbers.append(int(match.group(1)))
    return (max(lote_numbers) + 1) if lote_numbers else 1


def resolve_specialists(value: str) -> list[str]:
    if value in SPECIALIST_GROUPS:
        names = list(SPECIALIST_GROUPS[value])
    elif value in SPECIALISTS:
        names = [value]
    else:
        raise RuntimeError(f"Unknown specialist/group: {value}")
    return [name for name in names if name in SPECIALISTS and name != "titles"]


def latest_score_runs(conn, specialists: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for specialist in specialists:
        prefix = f"specialist_{specialist}_%"
        row = conn.execute(
            """
            SELECT id
            FROM ml_score_runs
            WHERE model_version LIKE ?
              AND scored_count > 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (prefix,),
        ).fetchone()
        if row is not None:
            result[specialist] = int(row["id"])
    return result


def policy_snapshot_score_runs(conn, policy_run_id: int, specialists: list[str]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in specialists)
    rows = conn.execute(
        f"""
        WITH policy AS (
            SELECT score_run_id AS general_score_run_id
            FROM ml_policy_runs
            WHERE id = ?
        ),
        latest AS (
            SELECT s.specialist_key, MAX(s.id) AS id
            FROM ml_specialist_policy_snapshots s
            JOIN policy p ON p.general_score_run_id = s.general_score_run_id
            WHERE s.specialist_key IN ({placeholders})
              AND s.status LIKE 'READY%'
              AND COALESCE(s.pending_real_count, 0) = 0
              AND COALESCE(s.threshold_below_policy, 0) = 0
              AND COALESCE(s.scope_delta_count, 0) = 0
            GROUP BY s.specialist_key
        )
        SELECT s.specialist_key, s.score_run_id
        FROM latest l
        JOIN ml_specialist_policy_snapshots s ON s.id = l.id
        WHERE s.score_run_id IS NOT NULL
        """,
        (policy_run_id, *specialists),
    ).fetchall()
    return {str(row["specialist_key"]): int(row["score_run_id"]) for row in rows}


def reviewed_segment_ids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT segment_id
        FROM local_learning_candidates
        WHERE local_status = 'reviewed_human'
        """
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def parse_specialist_votes(reasons_json: str | None) -> list[str]:
    votes: list[str] = []
    try:
        reasons = json.loads(reasons_json or "[]")
    except json.JSONDecodeError:
        return votes
    for reason in reasons:
        if not str(reason).startswith("specialist_votes:"):
            continue
        for vote in str(reason).split(":", 1)[1].split(","):
            vote = vote.strip()
            if vote:
                votes.append(vote)
    return votes


def load_policy_rows(
    conn,
    policy_run_id: int,
    include_reviewed: bool,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            mpi.*,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.source_line_number,
            o.portuguese_text AS current_output_text,
            msi.candidate_text AS general_candidate_text,
            msi.token_status AS general_token_status,
            msi.issue_count AS general_issue_count
        FROM ml_policy_items mpi
        JOIN source_segments s ON s.id = mpi.segment_id
        JOIN ml_score_items msi ON msi.id = mpi.score_item_id
        LEFT JOIN output_segments o ON o.segment_id = mpi.segment_id
        WHERE mpi.run_id = ?
          AND mpi.policy_group = ?
        """,
        (policy_run_id, POLICY_GROUP),
    ).fetchall()
    result = [dict(row) for row in rows]
    if include_reviewed:
        return result
    reviewed = reviewed_segment_ids(conn)
    return [row for row in result if int(row["segment_id"]) not in reviewed]


def load_specialist_scores(
    conn,
    score_runs: dict[str, int],
) -> dict[str, dict[int, dict[str, Any]]]:
    scores: dict[str, dict[int, dict[str, Any]]] = {}
    for specialist, score_run_id in score_runs.items():
        rows = conn.execute(
            """
            SELECT
                segment_id,
                final_action,
                risk_class,
                model_safe_probability,
                token_status,
                issue_count,
                high_issue_count,
                candidate_text
            FROM ml_score_items
            WHERE run_id = ?
            """,
            (score_run_id,),
        ).fetchall()
        scores[specialist] = {int(row["segment_id"]): dict(row) for row in rows}
    return scores


def choose_specialist_score(
    row: dict[str, Any],
    requested: set[str],
    scores: dict[str, dict[int, dict[str, Any]]],
) -> tuple[str | None, dict[str, Any] | None]:
    segment_id = int(row["segment_id"])
    candidates: list[tuple[str, dict[str, Any]]] = []
    for specialist in parse_specialist_votes(row.get("reasons_json")):
        if specialist not in requested:
            continue
        score = scores.get(specialist, {}).get(segment_id)
        if score is not None:
            candidates.append((specialist, score))
    if not candidates:
        return None, None
    candidates.sort(
        key=lambda item: (
            float(item[1].get("model_safe_probability") or 0.0),
            item[0],
        ),
        reverse=True,
    )
    return candidates[0]


def candidate_payload(row: dict[str, Any], specialist: str, score: dict[str, Any]) -> dict[str, Any]:
    candidate_text = (
        score.get("candidate_text")
        or row.get("general_candidate_text")
        or row.get("current_output_text")
        or row.get("old_text")
        or row.get("spanish_text")
        or ""
    )
    general_prob = float(row.get("model_safe_probability") or 0.0)
    specialist_prob = float(score.get("model_safe_probability") or 0.0)
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "source_section": "ML specialist no-safe frontier",
        "focus_group": f"{specialist}_no_safe_frontier",
        "auditor_action": "specialist_no_safe_frontier",
        "general_action": row.get("score_final_action"),
        "specialist_action": score.get("final_action"),
        "general_safe_probability": round(general_prob, 6),
        "specialist_safe_probability": round(specialist_prob, 6),
        "final_action": score.get("final_action"),
        "risk_class": score.get("risk_class") or "specialist_no_safe_frontier",
        "model_safe_probability": round(specialist_prob, 6),
        "issue_count": int(score.get("issue_count") or 0),
        "high_issue_count": int(score.get("high_issue_count") or 0),
        "token_status": score.get("token_status") or "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("current_output_text"),
        "suggested_text": candidate_text,
        "candidate_text": candidate_text,
        "policy_run_id": int(row["run_id"]),
        "policy_action": row.get("policy_action"),
        "policy_reasons_json": row.get("reasons_json") or "[]",
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def build_candidates(
    rows: list[dict[str, Any]],
    requested: set[str],
    scores: dict[str, dict[int, dict[str, Any]]],
    min_score: float,
    per_specialist_limit: int,
    per_path_limit: int,
    limit: int,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for row in rows:
        specialist, score = choose_specialist_score(row, requested, scores)
        if specialist is None or score is None:
            continue
        specialist_prob = float(score.get("model_safe_probability") or 0.0)
        if specialist_prob < min_score:
            continue
        payload = candidate_payload(row, specialist, score)
        raw.append(payload)
    raw.sort(
        key=lambda item: (
            float(item.get("model_safe_probability") or 0.0),
            -int(item.get("issue_count") or 0),
            str(item.get("relative_path") or ""),
            str(item.get("source_key") or ""),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    specialist_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    for item in raw:
        focus = str(item["focus_group"])
        path = str(item.get("relative_path") or "unknown")
        if per_specialist_limit and specialist_counts[focus] >= per_specialist_limit:
            continue
        if per_path_limit and path_counts[path] >= per_path_limit:
            continue
        selected.append(item)
        specialist_counts[focus] += 1
        path_counts[path] += 1
        if len(selected) >= limit:
            break
    return selected


def build_batches(candidates: list[dict[str, Any]], first_lote: int, batch_size: int) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    for offset in range(0, len(candidates), batch_size):
        batch_candidates = candidates[offset : offset + batch_size]
        if not batch_candidates:
            continue
        focus_counts = Counter(candidate["focus_group"] for candidate in batch_candidates)
        dominant_focus = focus_counts.most_common(1)[0][0] if focus_counts else "specialist_no_safe_frontier"
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "ML specialist no-safe frontier",
                "focus_group": dominant_focus,
                "queue_source": "ml_specialist_scope_review",
                "candidates": batch_candidates,
            }
        )
    return batches


def write_report(
    settings: dict,
    policy_run_id: int,
    specialist: str,
    candidates: list[dict[str, Any]],
    output_path: Path,
    min_score: float,
) -> Path:
    focus_counts = Counter(candidate["focus_group"] for candidate in candidates)
    path_counts = Counter(candidate.get("relative_path") or "unknown" for candidate in candidates)
    lines = [
        "ML specialist frontier queue",
        f"Started at: {now()}",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Requested specialist/group: {specialist}",
        f"Minimum specialist safe probability: {min_score:.4f}",
        f"Decision template: {output_path}",
        "",
        "Queue summary:",
        f"- candidates: {len(candidates)}",
        f"- focus groups: {dict(focus_counts.most_common())}",
        f"- paths: {dict(path_counts.most_common(12))}",
        "",
        "Top candidates:",
    ]
    for candidate in candidates[:40]:
        lines.append(
            "- "
            f"{candidate['focus_group']} | "
            f"p={float(candidate.get('model_safe_probability') or 0.0):.4f} | "
            f"{candidate.get('relative_path')}::{candidate.get('source_key')} | "
            f"candidate=\"{candidate.get('candidate_text') or ''}\""
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- These rows are still blocked by specialist no_safe_vote in the ensemble.",
            "- Review positives teach the specialist that this local pattern is safe.",
            "- Review negatives teach the specialist to keep the cautious boundary.",
            "- This queue does not alter output, train models, or change scores by itself.",
        ]
    )
    return db.write_report(settings, "ml_specialist_frontier_queue", lines)


def main(
    specialist: str,
    policy_run_id: int | None,
    limit: int,
    batch_size: int,
    min_score: float,
    per_specialist_limit: int,
    per_path_limit: int,
    output: str | None,
    include_reviewed: bool,
) -> None:
    started_at = datetime.now()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        resolved = resolve_specialists(specialist)
        policy_run_id = policy_run_id or latest_policy_run_id(conn)
        score_runs = policy_snapshot_score_runs(conn, policy_run_id, resolved)
        fallback_score_runs = latest_score_runs(conn, resolved)
        for key, score_run_id in fallback_score_runs.items():
            score_runs.setdefault(key, score_run_id)
        if not score_runs:
            raise RuntimeError(f"No specialist score runs found for: {', '.join(resolved)}")
        rows = load_policy_rows(conn, policy_run_id, include_reviewed=include_reviewed)
        scores = load_specialist_scores(conn, score_runs)
        first_lote = next_lote_number(conn)

    candidates = build_candidates(
        rows=rows,
        requested=set(score_runs),
        scores=scores,
        min_score=min_score,
        per_specialist_limit=per_specialist_limit,
        per_path_limit=per_path_limit,
        limit=limit,
    )
    batches = build_batches(candidates, first_lote=first_lote, batch_size=batch_size)

    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output) if output else reports_dir / f"{timestamp()}_ml_specialist_frontier_review_template.json"
    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_specialist_scope_review",
        "score_run_id": None,
        "source_report": f"specialist_frontier:policy_run_{policy_run_id}:{specialist}",
        "policy_run_id": policy_run_id,
        "specialist": specialist,
        "resolved_specialists": resolved,
        "min_score": min_score,
        "batch_size": batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "major_fix",
                "semantic_error",
                "residual_spanish",
                "structure_error",
                "token_mismatch",
                "rejected",
                "rejected_suggestion",
            ],
            "recommended_labels": [
                "correct",
                "minor_fix",
                "semantic_error",
                "residual_spanish",
                "contextual_exception",
            ],
            "label_guidance": {
                "correct": "Use when current/candidate output is safe and natural in this specialist frontier.",
                "minor_fix": "Use when only a small textual correction is needed.",
                "semantic_error": "Use when the meaning diverges from the English/context.",
                "residual_spanish": "Use when Spanish remains and should not be preserved.",
                "contextual_exception": "Use for intentional safe in-game localization choices.",
            },
            "do_not_run": ["apply output", "ml-score", "ml-train-risk"],
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = write_report(settings, policy_run_id, specialist, candidates, output_path, min_score)
    elapsed = datetime.now() - started_at
    print("[ml_specialist_frontier_queue] Starting frontier queue")
    print(f"[ml_specialist_frontier_queue] Rule version: {RULE_VERSION}")
    print(f"[ml_specialist_frontier_queue] Elapsed: {elapsed}")
    print(f"[ml_specialist_frontier_queue] Policy run id: {policy_run_id}")
    print(f"[ml_specialist_frontier_queue] Specialist/group: {specialist}")
    print(f"[ml_specialist_frontier_queue] Resolved: {', '.join(resolved)}")
    print(f"[ml_specialist_frontier_queue] Candidates: {len(candidates)}")
    print(f"[ml_specialist_frontier_queue] Batches: {len(batches)}")
    print(f"[ml_specialist_frontier_queue] Decision template: {output_path}")
    print(f"[ml_specialist_frontier_queue] Report: {report_path}")
    print("[ml_specialist_frontier_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare review batches from specialist ensemble no-safe frontier.")
    parser.add_argument("--specialist", default="operational_title_religion_v1")
    parser.add_argument("--policy-run-id", type=int)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=0.70)
    parser.add_argument("--per-specialist-limit", type=int, default=20)
    parser.add_argument("--per-path-limit", type=int, default=20)
    parser.add_argument("--output")
    parser.add_argument("--include-reviewed", action="store_true")
    args = parser.parse_args()
    main(
        specialist=args.specialist,
        policy_run_id=args.policy_run_id,
        limit=args.limit,
        batch_size=args.batch_size,
        min_score=args.min_score,
        per_specialist_limit=args.per_specialist_limit,
        per_path_limit=args.per_path_limit,
        output=args.output,
        include_reviewed=args.include_reviewed,
    )
