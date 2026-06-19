from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from typing import Any

import db


RULE_VERSION = "ml_build_dataset_v3_issue_review_bridge"
DATASET_VERSION = "supervised_bootstrap_v3"


POSITIVE_LOCAL_LABELS = {
    "auto_confirmed": ("positive", "accepted_local", None, 4),
    "correct": ("positive", "accepted_local", None, 4),
    "minor_fix": ("positive", "human_corrected", "minor_fix", 4),
    "contextual_exception": ("positive", "human_corrected", "contextual_exception", 4),
}

NEGATIVE_LOCAL_LABELS = {
    "residual_spanish": ("negative", "needs_autofix", "residual_spanish", 4),
    "structure_error": ("negative", "blocked_structure", "structure_error", 5),
    "semantic_error": ("negative", "needs_human", "semantic_error", 4),
    "major_fix": ("negative", "needs_human", "major_fix", 5),
    "negative": ("negative", "needs_human", "negative_feedback", 4),
    "harmful": ("negative", "blocked_structure", "harmful_feedback", 5),
    "rejected": ("negative", "needs_human", "rejected_candidate", 4),
    "rejected_suggestion": ("negative", "needs_human", "rejected_suggestion", 4),
    "token_mismatch": ("negative", "blocked_structure", "token_mismatch", 5),
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def limited_clause(limit: int | None) -> str:
    return "LIMIT ?" if limit else ""


def limited_params(limit: int | None) -> tuple:
    return (limit,) if limit else ()


def text_len(value: str | None) -> int:
    return len(value or "")


def insert_run(conn, source_scope: str, limit: int | None, started_at: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_dataset_runs (
            rule_version,
            dataset_version,
            source_scope,
            limit_count,
            notes,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            DATASET_VERSION,
            source_scope,
            limit,
            "Bootstrap dataset from human feedback, local learning labels, and locked human confirmations.",
            started_at,
            started_at,
        ),
    )
    return int(cursor.lastrowid)


def common_reason(evidence: str, details: dict[str, Any]) -> str:
    payload = {
        "rule_version": RULE_VERSION,
        "dataset_version": DATASET_VERSION,
        "evidence": evidence,
        **details,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def fetch_feedback_examples(conn, limit: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        WITH token_counts AS (
            SELECT segment_id, COUNT(*) AS token_count
            FROM protected_tokens
            GROUP BY segment_id
        )
        SELECT
            f.id AS evidence_id,
            f.segment_id,
            f.decision,
            f.suggested_text AS feedback_suggested_text,
            f.corrected_text,
            f.reason,
            f.reviewer,
            f.reviewed_at,
            ts.suggested_text AS suggestion_text,
            ts.origin AS suggestion_origin,
            ts.match_type,
            ts.match_score,
            ts.token_status,
            sc.id AS superseded_confirmation_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.has_english,
            s.has_old,
            o.portuguese_text AS output_text,
            coalesce(tc.token_count, 0) AS token_count
        FROM suggestion_feedback f
        JOIN source_segments s ON s.id = f.segment_id
        LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
        LEFT JOIN segment_confirmations sc
            ON sc.segment_id = f.segment_id
           AND sc.confirmed_text = coalesce(f.suggested_text, ts.suggested_text)
           AND sc.confirmation_level IN ('human_confirmed', 'human')
           AND sc.locked = 1
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN token_counts tc ON tc.segment_id = s.id
        WHERE s.is_active = 1
          AND f.decision IN ('accepted', 'edited', 'accepted_old', 'rejected')
        ORDER BY f.updated_at DESC, f.id DESC
        {limited_clause(limit)}
        """,
        limited_params(limit),
    ).fetchall()

    examples: list[dict[str, Any]] = []
    for row in rows:
        decision = row["decision"]
        candidate = row["feedback_suggested_text"] or row["suggestion_text"]
        if decision == "rejected" and row["superseded_confirmation_id"]:
            continue
        final_text = candidate
        label = "positive"
        action_label = "accept_suggestion"
        issue_label = None
        trust_level = 4
        if decision == "edited":
            final_text = row["corrected_text"]
            action_label = "human_corrected"
            issue_label = "manual_edit"
            trust_level = 5
        elif decision == "accepted_old":
            final_text = row["old_text"]
            action_label = "prefer_old"
            issue_label = "accepted_old"
            trust_level = 5
        elif decision == "rejected":
            final_text = None
            label = "negative"
            action_label = "reject_suggestion"
            issue_label = "rejected_suggestion"
            trust_level = 4

        examples.append(
            {
                **dict(row),
                "candidate_text": candidate,
                "final_text": final_text,
                "label": label,
                "action_label": action_label,
                "issue_label": issue_label,
                "trust_level": trust_level,
                "evidence_source": "suggestion_feedback",
                "confidence_score": row["match_score"],
                "locked": 0,
                "reasons_json": common_reason(
                    "suggestion_feedback",
                    {
                        "decision": decision,
                        "suggestion_origin": row["suggestion_origin"],
                        "match_type": row["match_type"],
                        "token_status": row["token_status"],
                    },
                ),
            }
        )
    return examples


def fetch_local_learning_examples(conn, limit: int | None) -> list[dict[str, Any]]:
    labels = sorted(set(POSITIVE_LOCAL_LABELS) | set(NEGATIVE_LOCAL_LABELS))
    placeholders = ",".join("?" for _ in labels)
    rows = conn.execute(
        f"""
        WITH token_counts AS (
            SELECT segment_id, COUNT(*) AS token_count
            FROM protected_tokens
            GROUP BY segment_id
        )
        SELECT
            c.id AS evidence_id,
            c.segment_id,
            c.human_label,
            c.corrected_text,
            c.suggested_text,
            c.local_confidence_score,
            c.queue_source,
            c.focus_group,
            c.local_status,
            c.reason,
            c.reviewer,
            c.reviewed_at,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.has_english,
            s.has_old,
            o.portuguese_text AS output_text,
            coalesce(tc.token_count, 0) AS token_count
        FROM local_learning_candidates c
        JOIN source_segments s ON s.id = c.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN token_counts tc ON tc.segment_id = s.id
        WHERE s.is_active = 1
          AND c.human_label IN ({placeholders})
        ORDER BY c.updated_at DESC, c.id DESC
        {limited_clause(limit)}
        """,
        (*labels, *limited_params(limit)),
    ).fetchall()

    examples: list[dict[str, Any]] = []
    for row in rows:
        label_info = POSITIVE_LOCAL_LABELS.get(row["human_label"]) or NEGATIVE_LOCAL_LABELS[row["human_label"]]
        label, action_label, issue_label, trust_level = label_info
        final_text = row["corrected_text"] or row["suggested_text"]
        if label == "negative" and not row["corrected_text"]:
            final_text = None
        examples.append(
            {
                **dict(row),
                "candidate_text": row["suggested_text"],
                "final_text": final_text,
                "label": label,
                "action_label": action_label,
                "issue_label": issue_label,
                "trust_level": trust_level,
                "evidence_source": "local_learning_candidate",
                "confidence_score": row["local_confidence_score"],
                "locked": 0,
                "reasons_json": common_reason(
                    "local_learning_candidate",
                    {
                        "human_label": row["human_label"],
                        "queue_source": row["queue_source"],
                        "focus_group": row["focus_group"],
                        "local_status": row["local_status"],
                    },
                ),
            }
        )
    return examples


def fetch_locked_confirmation_examples(conn, limit: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        WITH token_counts AS (
            SELECT segment_id, COUNT(*) AS token_count
            FROM protected_tokens
            GROUP BY segment_id
        )
        SELECT
            sc.id AS evidence_id,
            sc.segment_id,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.confirmed_text,
            sc.confidence_score,
            sc.locked,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.has_english,
            s.has_old,
            o.portuguese_text AS output_text,
            coalesce(tc.token_count, 0) AS token_count
        FROM segment_confirmations sc
        JOIN source_segments s ON s.id = sc.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN token_counts tc ON tc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.confirmation_level IN ('human_confirmed', 'human')
          AND sc.locked = 1
        ORDER BY sc.updated_at DESC, sc.id DESC
        {limited_clause(limit)}
        """,
        limited_params(limit),
    ).fetchall()

    examples: list[dict[str, Any]] = []
    for row in rows:
        examples.append(
            {
                **dict(row),
                "candidate_text": row["confirmed_text"],
                "final_text": row["confirmed_text"],
                "label": "positive",
                "action_label": "locked_human_confirmation",
                "issue_label": None,
                "trust_level": 5,
                "evidence_source": "segment_confirmation",
                "confidence_score": row["confidence_score"],
                "locked": row["locked"],
                "reasons_json": common_reason(
                    "segment_confirmation",
                    {
                        "confirmation_level": row["confirmation_level"],
                        "confirmation_source": row["confirmation_source"],
                        "confirmation_label": row["confirmation_label"],
                    },
                ),
            }
        )
    return examples


def issue_review_issue_label(row: dict[str, Any]) -> str:
    normalized_decision = row["normalized_decision"]
    notes = row["notes"] or ""
    if normalized_decision == "needs_repair" and "spanish_residual" in notes:
        return "residual_spanish"
    if normalized_decision == "needs_repair":
        return "issue_review_needs_repair"
    if normalized_decision == "false_positive_reopen":
        return "issue_review_false_positive_reopen"
    if normalized_decision == "safe_short_label":
        return "issue_review_safe_short_label"
    if normalized_decision == "needs_new_microagent":
        return "issue_review_needs_new_microagent"
    if normalized_decision == "needs_domain_context":
        return "issue_review_needs_domain_context"
    return f"issue_review_{normalized_decision}"


def fetch_issue_review_examples(conn, limit: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        WITH token_counts AS (
            SELECT segment_id, COUNT(*) AS token_count
            FROM protected_tokens
            GROUP BY segment_id
        )
        SELECT
            d.id AS evidence_id,
            d.segment_id,
            d.normalized_decision,
            d.evidence_label,
            d.corrected_text,
            d.notes,
            d.reviewer,
            d.updated_at AS reviewed_at,
            d.agent_key,
            d.issue_family,
            d.issue_kind,
            d.queue_bucket,
            q.confirmed_text,
            q.suggested_decision,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.has_english,
            s.has_old,
            o.portuguese_text AS output_text,
            coalesce(tc.token_count, 0) AS token_count
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        JOIN source_segments s ON s.id = d.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN token_counts tc ON tc.segment_id = s.id
        WHERE s.is_active = 1
          AND d.valid = 1
        ORDER BY d.updated_at DESC, d.id DESC
        {limited_clause(limit)}
        """,
        limited_params(limit),
    ).fetchall()

    examples: list[dict[str, Any]] = []
    for sqlite_row in rows:
        row = dict(sqlite_row)
        normalized_decision = row["normalized_decision"]
        candidate_text = row["confirmed_text"]
        final_text = candidate_text
        label = "neutral"
        action_label = "context_router"
        trust_level = 3

        if normalized_decision in {"safe_short_label", "false_positive_reopen"}:
            label = "positive"
            action_label = "accepted_local"
            trust_level = 5 if normalized_decision == "false_positive_reopen" else 4
        elif normalized_decision == "needs_repair":
            label = "negative"
            action_label = "needs_autofix"
            final_text = row["corrected_text"] or None
            trust_level = 5
        elif normalized_decision == "needs_new_microagent":
            action_label = "create_subagent"
            candidate_text = None
            final_text = None
            trust_level = 4
        elif normalized_decision == "needs_domain_context":
            action_label = "context_router"
            candidate_text = None
            final_text = None
            trust_level = 3

        examples.append(
            {
                **row,
                "candidate_text": candidate_text,
                "final_text": final_text,
                "label": label,
                "action_label": action_label,
                "issue_label": issue_review_issue_label(row),
                "trust_level": trust_level,
                "evidence_source": "issue_review_decision",
                "confidence_score": None,
                "locked": 0,
                "reasons_json": common_reason(
                    "issue_review_decision",
                    {
                        "normalized_decision": normalized_decision,
                        "evidence_label": row["evidence_label"],
                        "agent_key": row["agent_key"],
                        "issue_family": row["issue_family"],
                        "issue_kind": row["issue_kind"],
                        "queue_bucket": row["queue_bucket"],
                        "suggested_decision": row["suggested_decision"],
                        "reviewer": row["reviewer"],
                        "notes": row["notes"],
                        "macro_training_candidate": bool(candidate_text),
                    },
                ),
            }
        )
    return examples


def normalize_example(run_id: int, example: dict[str, Any], created_at: str) -> tuple:
    final_text = example.get("final_text")
    candidate_text = example.get("candidate_text")
    return (
        run_id,
        example["segment_id"],
        example["relative_path"],
        example["source_key"],
        example["source_line_number"],
        example["english_text"],
        example["spanish_text"],
        example["old_text"],
        example["output_text"],
        candidate_text,
        final_text,
        example["label"],
        example["action_label"],
        example["issue_label"],
        example["trust_level"],
        example["evidence_source"],
        example["evidence_id"],
        example["confidence_score"],
        int(example.get("locked") or 0),
        int(example.get("token_count") or 0),
        int(example.get("has_english") or 0),
        int(example.get("has_old") or 0),
        text_len(final_text or candidate_text),
        example["reasons_json"],
        created_at,
    )


def insert_examples(conn, run_id: int, examples: list[dict[str, Any]], created_at: str) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO ml_training_examples (
            run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            output_text,
            candidate_text,
            final_text,
            label,
            action_label,
            issue_label,
            trust_level,
            evidence_source,
            evidence_id,
            confidence_score,
            locked,
            token_count,
            has_english,
            has_old,
            text_length,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [normalize_example(run_id, example, created_at) for example in examples],
    )


def update_run_counts(conn, run_id: int, finished_at: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT label, trust_level, COUNT(*) AS total
        FROM ml_training_examples
        WHERE run_id = ?
        GROUP BY label, trust_level
        """,
        (run_id,),
    ).fetchall()
    label_counts: Counter[str] = Counter()
    strong_positive = 0
    strong_negative = 0
    for row in rows:
        label = row["label"]
        trust_level = int(row["trust_level"] or 0)
        total = int(row["total"] or 0)
        label_counts[label] += total
        if label == "positive" and trust_level >= 5:
            strong_positive += total
        if label == "negative" and trust_level >= 5:
            strong_negative += total
    total_count = sum(label_counts.values())
    conn.execute(
        """
        UPDATE ml_dataset_runs
        SET
            positive_count = ?,
            negative_count = ?,
            neutral_count = ?,
            total_count = ?,
            strong_positive_count = ?,
            strong_negative_count = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            label_counts["positive"],
            label_counts["negative"],
            label_counts["neutral"],
            total_count,
            strong_positive,
            strong_negative,
            finished_at,
            finished_at,
            run_id,
        ),
    )
    return {
        "positive": label_counts["positive"],
        "negative": label_counts["negative"],
        "neutral": label_counts["neutral"],
        "total": total_count,
        "strong_positive": strong_positive,
        "strong_negative": strong_negative,
    }


def report_rows(conn, run_id: int, column: str) -> list:
    return conn.execute(
        f"""
        SELECT {column} AS key, COUNT(*) AS total
        FROM ml_training_examples
        WHERE run_id = ?
        GROUP BY {column}
        ORDER BY total DESC, key
        """,
        (run_id,),
    ).fetchall()


def format_rows(rows) -> list[str]:
    if not rows:
        return ["- none: 0"]
    return [f"- {row['key'] or 'none'}: {row['total']}" for row in rows]


def main(limit: int | None = None) -> None:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    print("[ml_build_dataset] Starting supervised dataset build")
    print(f"[ml_build_dataset] Rule version: {RULE_VERSION}")
    print(f"[ml_build_dataset] Dataset version: {DATASET_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        run_id = insert_run(conn, "feedback+local_learning+locked_human+issue_review", limit, started_at)
        print(f"[ml_build_dataset] Run id: {run_id}")

        feedback_examples = fetch_feedback_examples(conn, limit)
        local_examples = fetch_local_learning_examples(conn, limit)
        confirmation_examples = fetch_locked_confirmation_examples(conn, limit)
        issue_review_examples = fetch_issue_review_examples(conn, limit)

        insert_examples(conn, run_id, feedback_examples, started_at)
        insert_examples(conn, run_id, local_examples, started_at)
        insert_examples(conn, run_id, confirmation_examples, started_at)
        insert_examples(conn, run_id, issue_review_examples, started_at)

        finished_at = now()
        counts = update_run_counts(conn, run_id, finished_at)
        conn.commit()

        source_rows = report_rows(conn, run_id, "evidence_source")
        label_rows = report_rows(conn, run_id, "label")
        action_rows = report_rows(conn, run_id, "action_label")
        issue_rows = report_rows(conn, run_id, "issue_label")
        trust_rows = report_rows(conn, run_id, "trust_level")

    elapsed = datetime.now() - started_at_dt
    report_lines = [
        "ML supervised dataset build report",
        f"Started at: {started_at}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Dataset version: {DATASET_VERSION}",
        f"Run id: {run_id}",
        "",
        "Summary:",
        f"- Total examples: {counts['total']}",
        f"- Positive examples: {counts['positive']} ({percent(counts['positive'], counts['total'])})",
        f"- Negative examples: {counts['negative']} ({percent(counts['negative'], counts['total'])})",
        f"- Neutral examples: {counts['neutral']} ({percent(counts['neutral'], counts['total'])})",
        f"- Strong positive examples: {counts['strong_positive']}",
        f"- Strong negative examples: {counts['strong_negative']}",
        "",
        "By evidence source:",
        *format_rows(source_rows),
        "",
        "By label:",
        *format_rows(label_rows),
        "",
        "By action label:",
        *format_rows(action_rows),
        "",
        "By issue label:",
        *format_rows(issue_rows),
        "",
        "By trust level:",
        *format_rows(trust_rows),
        "",
        "Interpretation:",
        "- This dataset is intentionally conservative and auditable.",
        "- Locked human confirmations are positive anchors.",
        "- Edited/corrected/rejected rows teach the classifier what human review changed.",
        "- Issue review decisions bridge agent queues into supervised evidence.",
        "- Context/delegation issue-review rows are stored as neutral evidence without candidate_text, so they do not train the macro classifier as hard negatives.",
        "- Negative examples are still scarce, so the first classifier must be evaluated with extra caution.",
    ]
    report_path = db.write_report(settings, "ml_build_dataset", report_lines)

    print(f"[ml_build_dataset] Total examples: {counts['total']}")
    print(
        "[ml_build_dataset] Positive/negative: "
        f"{counts['positive']}/{counts['negative']} "
        f"({percent(counts['negative'], counts['total'])} negative)"
    )
    print(f"[ml_build_dataset] Strong positives: {counts['strong_positive']}")
    print(f"[ml_build_dataset] Strong negatives: {counts['strong_negative']}")
    print(f"[ml_build_dataset] Report: {report_path}")
    print("[ml_build_dataset] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a curated supervised ML dataset.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional per-source limit for development runs.",
    )
    parsed = parser.parse_args()
    main(limit=parsed.limit)
