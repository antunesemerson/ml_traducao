from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "apply_local_learning_feedback_v1"

POSITIVE_LABELS = {"correct"}
NEAR_POSITIVE_LABELS = {"minor_fix"}
PARTIAL_LABELS = {"major_fix"}
NEGATIVE_LABELS = {"residual_spanish", "structure_error", "semantic_error", "wrong"}
HARMFUL_LABELS = {"harmful"}
LEARNABLE_LABELS = POSITIVE_LABELS | NEAR_POSITIVE_LABELS | PARTIAL_LABELS | NEGATIVE_LABELS | HARMFUL_LABELS
CONFIRMABLE_LABELS = POSITIVE_LABELS | NEAR_POSITIVE_LABELS | PARTIAL_LABELS


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_reasons(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed]


def pattern_keys(row) -> list[str]:
    keys = [
        f"origin:{row['origin'] or 'unknown'}",
        f"match_type:{row['match_type'] or 'unknown'}",
        f"suggestion_status:{row['suggestion_status'] or 'unknown'}",
        f"token_status:{row['token_status'] or 'unknown'}",
        f"source_language:{row['source_language'] or 'unknown'}",
        f"queue_source:{row['queue_source'] or 'unknown'}",
        f"focus_group:{row['focus_group'] or 'unknown'}",
        (
            "combo:"
            f"{row['origin'] or 'unknown'}|"
            f"{row['match_type'] or 'unknown'}|"
            f"{row['suggestion_status'] or 'unknown'}"
        ),
    ]
    for reason in parse_reasons(row["reasons_json"]):
        if reason.startswith("validator_issues:"):
            issue_list = reason.split(":", 1)[1]
            for issue in issue_list.split(","):
                issue = issue.strip()
                if issue:
                    keys.append(f"validator_issue:{issue}")
        elif reason.startswith("validator_words:"):
            try:
                words = int(reason.split(":", 1)[1])
            except ValueError:
                continue
            if words >= 70:
                keys.append("length:long")
            elif words >= 30:
                keys.append("length:medium")
            else:
                keys.append("length:short")
    return sorted(set(keys))


def label_bucket(label: str) -> str:
    if label in POSITIVE_LABELS:
        return "positive"
    if label in NEAR_POSITIVE_LABELS:
        return "near_positive"
    if label in PARTIAL_LABELS:
        return "partial"
    if label in HARMFUL_LABELS:
        return "harmful"
    return "negative"


def compute_weight_adjustment(
    positive_count: int,
    near_positive_count: int,
    partial_count: int,
    negative_count: int,
    harmful_count: int,
) -> float:
    total = positive_count + near_positive_count + partial_count + negative_count + harmful_count
    if total == 0:
        return 0.0
    raw = (
        positive_count * 1.0
        + near_positive_count * 0.35
        - partial_count * 0.25
        - negative_count * 0.85
        - harmful_count * 1.5
    ) / total
    confidence = min(1.0, total / 12)
    adjustment = raw * confidence * 0.18
    return max(-0.22, min(0.18, adjustment))


def upsert_pattern(conn, key: str, label: str, candidate_id: int) -> None:
    timestamp = now()
    existing = conn.execute(
        """
        SELECT *
        FROM local_learning_pattern_stats
        WHERE pattern_key = ?
        """,
        (key,),
    ).fetchone()
    bucket = label_bucket(label)
    increments = {
        "positive": (1, 0, 0, 0, 0),
        "near_positive": (0, 1, 0, 0, 0),
        "partial": (0, 0, 1, 0, 0),
        "negative": (0, 0, 0, 1, 0),
        "harmful": (0, 0, 0, 0, 1),
    }[bucket]

    if existing:
        positive = int(existing["positive_count"] or 0) + increments[0]
        near_positive = int(existing["near_positive_count"] or 0) + increments[1]
        partial = int(existing["partial_count"] or 0) + increments[2]
        negative = int(existing["negative_count"] or 0) + increments[3]
        harmful = int(existing["harmful_count"] or 0) + increments[4]
        total = positive + near_positive + partial + negative + harmful
        conn.execute(
            """
            UPDATE local_learning_pattern_stats
            SET
                positive_count = ?,
                near_positive_count = ?,
                partial_count = ?,
                negative_count = ?,
                harmful_count = ?,
                total_count = ?,
                weight_adjustment = ?,
                last_label = ?,
                last_candidate_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                positive,
                near_positive,
                partial,
                negative,
                harmful,
                total,
                compute_weight_adjustment(positive, near_positive, partial, negative, harmful),
                label,
                candidate_id,
                timestamp,
                existing["id"],
            ),
        )
        return

    positive, near_positive, partial, negative, harmful = increments
    total = positive + near_positive + partial + negative + harmful
    conn.execute(
        """
        INSERT INTO local_learning_pattern_stats (
            pattern_key,
            positive_count,
            near_positive_count,
            partial_count,
            negative_count,
            harmful_count,
            total_count,
            weight_adjustment,
            last_label,
            last_candidate_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            positive,
            near_positive,
            partial,
            negative,
            harmful,
            total,
            compute_weight_adjustment(positive, near_positive, partial, negative, harmful),
            label,
            candidate_id,
            timestamp,
            timestamp,
        ),
    )


def confirmation_text(row) -> str | None:
    label = row["human_label"]
    if label == "correct":
        return row["suggested_text"]
    corrected = (row["corrected_text"] or "").strip()
    if label in {"minor_fix", "major_fix"} and corrected:
        return row["corrected_text"]
    return None


def sync_human_confirmation(conn, row, timestamp: str) -> str:
    text = confirmation_text(row)
    if not text:
        return "skipped_missing_text"

    existing = conn.execute(
        """
        SELECT id, confirmation_level, locked
        FROM segment_confirmations
        WHERE segment_id = ?
        """,
        (row["segment_id"],),
    ).fetchone()

    if existing and int(existing["locked"] or 0) == 1:
        return "skipped_locked"

    if existing:
        conn.execute(
            """
            UPDATE segment_confirmations
            SET
                confirmation_level = 'human_confirmed',
                confirmed_text = ?,
                confirmation_source = 'local_learning',
                confirmation_label = ?,
                locked = 1,
                confidence_score = ?,
                candidate_id = ?,
                feedback_id = ?,
                reviewer = ?,
                confirmed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                text,
                row["human_label"],
                row["local_confidence_score"],
                row["id"],
                row["feedback_id"],
                row["reviewer"],
                row["reviewed_at"] or timestamp,
                timestamp,
                existing["id"],
            ),
        )
        return "upgraded"

    conn.execute(
        """
        INSERT INTO segment_confirmations (
            segment_id,
            confirmation_level,
            confirmed_text,
            confirmation_source,
            confirmation_label,
            locked,
            confidence_score,
            candidate_id,
            feedback_id,
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, 'human_confirmed', ?, 'local_learning', ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["segment_id"],
            text,
            row["human_label"],
            row["local_confidence_score"],
            row["id"],
            row["feedback_id"],
            row["reviewer"],
            row["reviewed_at"] or timestamp,
            timestamp,
        ),
    )
    return "inserted"


def fetch_confirmation_metrics(conn) -> dict[str, int | float]:
    total_segments = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM source_segments
            WHERE is_active = 1
            """
        ).fetchone()[0]
        or 0
    )
    rows = conn.execute(
        """
        SELECT confirmation_level, locked, COUNT(*) AS total
        FROM segment_confirmations
        GROUP BY confirmation_level, locked
        """
    ).fetchall()
    human = 0
    auto = 0
    locked = 0
    for row in rows:
        total = int(row["total"] or 0)
        if row["confirmation_level"] == "human_confirmed":
            human += total
        elif row["confirmation_level"] == "auto_confirmed":
            auto += total
        if int(row["locked"] or 0) == 1:
            locked += total
    return {
        "total_segments": total_segments,
        "human_confirmed": human,
        "auto_confirmed": auto,
        "locked": locked,
        "total_confirmed": human + auto,
        "pending_confirmation": max(total_segments - human - auto, 0),
    }


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_local_learning_feedback] Starting local learning feedback application")
    print(f"[apply_local_learning_feedback] Rule version: {RULE_VERSION}")
    print(f"[apply_local_learning_feedback] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = conn.execute(
            """
            SELECT
                id,
                human_label,
                origin,
                match_type,
                suggestion_status,
                token_status,
                source_language,
                queue_source,
                focus_group,
                reasons_json
            FROM local_learning_candidates
            WHERE human_label IN ({labels})
              AND learned_at IS NULL
            ORDER BY reviewed_at ASC, id ASC
            """.format(labels=",".join("?" for _ in sorted(LEARNABLE_LABELS))),
            tuple(sorted(LEARNABLE_LABELS)),
        ).fetchall()

        learned = 0
        patterns_updated = 0
        label_counts: Counter[str] = Counter()
        pattern_counts: Counter[str] = Counter()
        timestamp = now()
        for row in rows:
            keys = pattern_keys(row)
            for key in keys:
                upsert_pattern(conn, key, row["human_label"], row["id"])
                patterns_updated += 1
                pattern_counts[key] += 1
            conn.execute(
                """
                UPDATE local_learning_candidates
                SET learned_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, row["id"]),
            )
            learned += 1
            label_counts[row["human_label"]] += 1

        confirmation_rows = conn.execute(
            """
            SELECT
                id,
                feedback_id,
                segment_id,
                human_label,
                suggested_text,
                corrected_text,
                local_confidence_score,
                reviewer,
                reviewed_at
            FROM local_learning_candidates
            WHERE human_label IN ({labels})
              AND confirmation_synced_at IS NULL
            ORDER BY reviewed_at ASC, id ASC
            """.format(labels=",".join("?" for _ in sorted(CONFIRMABLE_LABELS))),
            tuple(sorted(CONFIRMABLE_LABELS)),
        ).fetchall()

        confirmation_counts: Counter[str] = Counter()
        for row in confirmation_rows:
            result = sync_human_confirmation(conn, row, timestamp)
            confirmation_counts[result] += 1
            conn.execute(
                """
                UPDATE local_learning_candidates
                SET confirmation_synced_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, row["id"]),
            )

        conn.commit()

        top_positive = conn.execute(
            """
            SELECT pattern_key, weight_adjustment, total_count
            FROM local_learning_pattern_stats
            WHERE weight_adjustment > 0
            ORDER BY weight_adjustment DESC, total_count DESC
            LIMIT 10
            """
        ).fetchall()
        top_negative = conn.execute(
            """
            SELECT pattern_key, weight_adjustment, total_count
            FROM local_learning_pattern_stats
            WHERE weight_adjustment < 0
            ORDER BY weight_adjustment ASC, total_count DESC
            LIMIT 10
            """
        ).fetchall()
        metrics = fetch_confirmation_metrics(conn)

    elapsed = datetime.now() - started_at
    report_lines = [
        "Apply local learning feedback report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Candidates learned: {learned}",
        f"- Pattern updates: {patterns_updated}",
        f"- Confirmations synced: {sum(confirmation_counts.values())}",
        "",
        "Labels:",
        *[f"- {label}: {total}" for label, total in sorted(label_counts.items())],
        "",
        "Confirmation sync:",
        *[f"- {status}: {total}" for status, total in sorted(confirmation_counts.items())],
        "",
        "Confirmation coverage:",
        f"- Active segments: {metrics['total_segments']}",
        (
            f"- Human confirmed: {metrics['human_confirmed']} "
            f"({percent(int(metrics['human_confirmed']), int(metrics['total_segments'])):.4f}%)"
        ),
        (
            f"- Auto confirmed: {metrics['auto_confirmed']} "
            f"({percent(int(metrics['auto_confirmed']), int(metrics['total_segments'])):.4f}%)"
        ),
        (
            f"- Total confirmed: {metrics['total_confirmed']} "
            f"({percent(int(metrics['total_confirmed']), int(metrics['total_segments'])):.4f}%)"
        ),
        f"- Human locked: {metrics['locked']}",
        f"- Pending confirmation: {metrics['pending_confirmation']}",
        "",
        "Top positive patterns:",
        *[
            f"- {row['pattern_key']}: {row['weight_adjustment']:.4f} ({row['total_count']})"
            for row in top_positive
        ],
        "",
        "Top negative patterns:",
        *[
            f"- {row['pattern_key']}: {row['weight_adjustment']:.4f} ({row['total_count']})"
            for row in top_negative
        ],
    ]
    if not label_counts:
        report_lines.append("- No reviewed local learning candidates found")
    report_path = db.write_report(settings, "apply_local_learning_feedback", report_lines)
    print(f"[apply_local_learning_feedback] Candidates learned: {learned}")
    print(f"[apply_local_learning_feedback] Pattern updates: {patterns_updated}")
    print(f"[apply_local_learning_feedback] Confirmations synced: {sum(confirmation_counts.values())}")
    print(
        "[apply_local_learning_feedback] Confirmation coverage: "
        f"{metrics['total_confirmed']}/{metrics['total_segments']} "
        f"({percent(int(metrics['total_confirmed']), int(metrics['total_segments'])):.4f}%)"
    )
    print(f"[apply_local_learning_feedback] Report: {report_path}")
    print("[apply_local_learning_feedback] Done")


if __name__ == "__main__":
    main()
