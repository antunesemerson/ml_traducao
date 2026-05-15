from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db
import local_learning_cycle
import local_quality_validator


RULE_VERSION = "triage_positive_core_v1"
CONFIRMATION_SOURCE = "codex_positive_core_triage"
AUTO_LABEL = "auto_triage_positive_core"
REVIEWER = "codex"

SENSITIVE_TERMS = {
    "accolade",
    "accolades",
    "average",
    "breach",
    "breaches",
    "breed",
    "counter",
    "counters",
    "day-to-day",
    "disease",
    "diseases",
    "drift",
    "drifted",
    "flavor",
    "frivolous",
    "hegemony",
    "hide ui",
    "hooked",
    "landed",
    "lash out",
    "libationer",
    "prowess",
    "raiders",
    "rank",
    "ranks",
    "retract",
    "retracted",
    "retracting",
    "retraction",
    "shunned",
    "station",
    "steward",
    "tour",
    "tours",
    "unlanded",
    "wandering",
}

SENSITIVE_KEYS = {
    "game_concept_accolade",
    "game_concept_accolades",
    "game_concept_prowess",
    "game_concept_rank",
    "game_concept_ranks",
    "game_concept_title_rank",
    "game_concept_title_ranks",
    "game_concept_de_jure_drift",
    "game_concept_scheme_breach",
    "game_concept_board_game_type_counter",
    "game_concept_board_game_type_counters",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def is_sensitive(row) -> tuple[bool, str | None]:
    haystack = " ".join(
        [
            normalize(row["source_key"]),
            normalize(row["english_text"]),
            normalize(row["spanish_text"]),
            normalize(row["suggested_text"]),
        ]
    )
    key = normalize(row["source_key"])
    for sensitive_key in SENSITIVE_KEYS:
        if key.startswith(sensitive_key):
            return True, f"sensitive_key:{sensitive_key}"
    for term in SENSITIVE_TERMS:
        if term in haystack:
            return True, f"sensitive_term:{term}"
    return False, None


def approval_blocker(row) -> str | None:
    if row["human_label"] != "pending":
        return "already_labeled"
    if row["local_status"] != "high_confidence":
        return f"local_status:{row['local_status']}"
    if float(row["local_confidence_score"] or 0) < 0.99:
        return f"confidence:{row['local_confidence_score']}"
    validation = local_quality_validator.validate_text(row["suggested_text"])
    if validation["auto_approval_blocked"] or validation["issue_count"]:
        codes = ",".join(issue["code"] for issue in validation["issues"])
        return f"validator:{codes or 'blocked'}"
    sensitive, reason = is_sensitive(row)
    if sensitive:
        return reason
    return None


def upsert_auto_confirmation(conn, row, timestamp: str) -> str:
    existing = conn.execute(
        """
        SELECT id, locked
        FROM segment_confirmations
        WHERE segment_id = ?
        """,
        (row["segment_id"],),
    ).fetchone()
    if existing and int(existing["locked"] or 0) == 1:
        return "skipped_locked"

    params = (
        row["suggested_text"],
        AUTO_LABEL,
        row["local_confidence_score"],
        row["id"],
        row["feedback_id"],
        REVIEWER,
        timestamp,
        timestamp,
    )
    if existing:
        conn.execute(
            """
            UPDATE segment_confirmations
            SET
                confirmation_level = 'auto_confirmed',
                confirmed_text = ?,
                confirmation_source = ?,
                confirmation_label = ?,
                locked = 0,
                confidence_score = ?,
                candidate_id = ?,
                feedback_id = ?,
                reviewer = ?,
                confirmed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (params[0], CONFIRMATION_SOURCE, *params[1:], existing["id"]),
        )
        return "updated"

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
        VALUES (?, 'auto_confirmed', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """,
        (row["segment_id"], params[0], CONFIRMATION_SOURCE, *params[1:]),
    )
    return "inserted"


def fetch_run_rows(conn, run_id: int):
    return conn.execute(
        """
        SELECT *
        FROM local_learning_candidates
        WHERE run_id = ?
        ORDER BY id
        """,
        (run_id,),
    ).fetchall()


def triage_run(conn, run_id: int) -> tuple[Counter, list[tuple[int, int, str, str, str]]]:
    timestamp = now()
    counts: Counter[str] = Counter()
    review_rows: list[tuple[int, int, str, str, str]] = []
    rows = fetch_run_rows(conn, run_id)
    for row in rows:
        blocker = approval_blocker(row)
        if blocker:
            counts["needs_review"] += 1
            review_rows.append(
                (
                    row["id"],
                    row["segment_id"],
                    row["source_key"],
                    row["suggested_text"],
                    blocker,
                )
            )
            continue

        result = upsert_auto_confirmation(conn, row, timestamp)
        conn.execute(
            """
            UPDATE local_learning_candidates
            SET
                human_label = 'auto_confirmed',
                reason = ?,
                reviewer = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (CONFIRMATION_SOURCE, REVIEWER, timestamp, timestamp, row["id"]),
        )
        counts[f"auto_{result}"] += 1
    conn.commit()
    return counts, review_rows


def main(limit: int = 500, batch_size: int = 50, review_limit: int = 80) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[triage_positive_core] Starting positive core triage")
    print(f"[triage_positive_core] Rule version: {RULE_VERSION}")
    print(f"[triage_positive_core] Limit: {limit}")
    print(f"[triage_positive_core] Batch size: {batch_size}")

    total_counts: Counter[str] = Counter()
    review_rows: list[tuple[int, int, str, str, str]] = []
    processed = 0
    run_ids: list[int] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        while processed < limit:
            run_id = local_learning_cycle.create_run(conn, batch_size, 0.98, "positive", "core")
            pattern_weights = local_learning_cycle.load_pattern_weights(conn)
            candidates = local_learning_cycle.fetch_positive_candidates(conn, batch_size, 0.98, "core")
            if not candidates:
                conn.execute(
                    """
                    UPDATE local_learning_runs
                    SET status = 'completed', finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now(), now(), run_id),
                )
                conn.commit()
                break

            status_counts: Counter[str] = Counter()
            inserted = 0
            for candidate in candidates:
                confidence, reasons, local_status = local_learning_cycle.score_candidate(
                    candidate,
                    pattern_weights,
                )
                if local_learning_cycle.insert_candidate(
                    conn,
                    run_id,
                    candidate,
                    confidence,
                    reasons,
                    local_status,
                ):
                    inserted += 1
                    status_counts[local_status] += 1
            finished_at = now()
            conn.execute(
                """
                UPDATE local_learning_runs
                SET
                    candidate_count = ?,
                    high_confidence_count = ?,
                    pending_human_count = ?,
                    status = 'completed',
                    finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    inserted,
                    status_counts["high_confidence"],
                    status_counts["pending_human"],
                    finished_at,
                    finished_at,
                    run_id,
                ),
            )
            conn.commit()
            if inserted == 0:
                break

            counts, review = triage_run(conn, run_id)
            total_counts.update(counts)
            review_rows.extend(review)
            processed += inserted
            run_ids.append(run_id)
            print(
                "[triage_positive_core] "
                f"Run {run_id}: inserted={inserted}, "
                f"auto={counts['auto_inserted'] + counts['auto_updated']}, "
                f"review={counts['needs_review']}"
            )

    elapsed = datetime.now() - started_at
    report_lines = [
        "Positive core triage report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Runs: {', '.join(str(item) for item in run_ids) or '(none)'}",
        "",
        "Summary:",
        f"- Processed: {processed}",
        f"- Auto inserted: {total_counts['auto_inserted']}",
        f"- Auto updated: {total_counts['auto_updated']}",
        f"- Auto skipped locked: {total_counts['auto_skipped_locked']}",
        f"- Needs review: {total_counts['needs_review']}",
        "",
        "Review sample:",
    ]
    for candidate_id, segment_id, key, text, reason in review_rows[:review_limit]:
        safe_text = (text or "").replace("\n", "\\n")
        if len(safe_text) > 160:
            safe_text = safe_text[:157] + "..."
        report_lines.append(
            f"- candidate={candidate_id} segment={segment_id} key={key} reason={reason} text={safe_text}"
        )
    if len(review_rows) > review_limit:
        report_lines.append(f"- ... {len(review_rows) - review_limit} more review candidates")

    report_path = db.write_report(settings, "triage_positive_core", report_lines)
    print(f"[triage_positive_core] Processed: {processed}")
    print(f"[triage_positive_core] Auto inserted: {total_counts['auto_inserted']}")
    print(f"[triage_positive_core] Auto updated: {total_counts['auto_updated']}")
    print(f"[triage_positive_core] Needs review: {total_counts['needs_review']}")
    print(f"[triage_positive_core] Report: {report_path}")
    print("[triage_positive_core] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-triage obvious positive core candidates.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--review-limit", type=int, default=80)
    args = parser.parse_args()
    main(limit=args.limit, batch_size=args.batch_size, review_limit=args.review_limit)
