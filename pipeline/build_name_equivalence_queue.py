from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db
import review_name_disagreements


RULE_VERSION = "build_name_equivalence_queue_v1"
SOURCE_BUCKET = "candidate_accept_portuguese_historical_name"
QUEUE_REASON = "softening_candidate_exact_variant"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def name_family(source_name: str, portuguese_name: str) -> str:
    source_key = review_name_disagreements.ascii_key(source_name)
    return source_key


def upsert_equivalence(conn, row: dict, proposed_text: str, timestamp: str) -> str:
    source_name = str(row["english_text"] or "").strip()
    portuguese_name = proposed_text.strip()
    family = name_family(source_name, portuguese_name)
    existing = conn.execute(
        """
        SELECT id, status, evidence_count
        FROM name_equivalences
        WHERE source_name = ?
          AND portuguese_name = ?
          AND source_kind = 'character_name'
        """,
        (source_name, portuguese_name),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE name_equivalences
            SET
                name_family = ?,
                confidence_score = ?,
                last_segment_id = ?,
                reason = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'pending'
            """,
            (family, 0.55, row["segment_id"], QUEUE_REASON, timestamp, existing["id"]),
        )
        if existing["status"] == "pending":
            return "updated_pending"
        return f"kept_{existing['status']}"

    conn.execute(
        """
        INSERT INTO name_equivalences (
            source_name,
            portuguese_name,
            name_family,
            source_kind,
            status,
            confidence_score,
            evidence_count,
            first_segment_id,
            last_segment_id,
            reason,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, 'character_name', 'pending', ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            source_name,
            portuguese_name,
            family,
            0.55,
            row["segment_id"],
            row["segment_id"],
            QUEUE_REASON,
            timestamp,
            timestamp,
        ),
    )
    return "inserted_pending"


def fetch_candidates(conn, limit: int) -> list[tuple[dict, str]]:
    rows = review_name_disagreements.fetch_rows(conn)
    candidates: list[tuple[dict, str]] = []
    for row in rows:
        bucket, proposed_text = review_name_disagreements.classify(row)
        if bucket != SOURCE_BUCKET or not proposed_text:
            continue
        existing = conn.execute(
            """
            SELECT status
            FROM name_equivalences
            WHERE source_name = ?
              AND portuguese_name = ?
              AND source_kind = 'character_name'
            """,
            (str(row["english_text"] or "").strip(), proposed_text.strip()),
        ).fetchone()
        if existing and existing["status"] != "pending":
            continue
        candidates.append((row, proposed_text))
        if len(candidates) >= limit:
            break
    return candidates


def main(limit: int | None = None) -> None:
    settings = db.load_settings()
    limit = limit if limit is not None else int(settings.get("name_equivalences", {}).get("queue_limit", 200))
    started_at = datetime.now()
    timestamp = now()

    print("[build_name_equivalence_queue] Starting name equivalence queue")
    print(f"[build_name_equivalence_queue] Rule version: {RULE_VERSION}")
    print(f"[build_name_equivalence_queue] Limit: {limit}")
    print(f"[build_name_equivalence_queue] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        candidates = fetch_candidates(conn, limit)
        result_counts: Counter[str] = Counter()
        for row, proposed_text in candidates:
            result_counts[upsert_equivalence(conn, row, proposed_text, timestamp)] += 1
        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM name_equivalences
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        pending_rows = conn.execute(
            """
            SELECT *
            FROM name_equivalences
            WHERE status = 'pending'
            ORDER BY evidence_count DESC, id ASC
            LIMIT 80
            """
        ).fetchall()
        conn.commit()

    status_counts = {row["status"]: int(row["total"] or 0) for row in status_rows}
    elapsed = datetime.now() - started_at
    report_lines = [
        "Name equivalence queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Candidates inspected: {len(candidates)}",
        *[f"- {key}: {value}" for key, value in result_counts.most_common()],
        "",
        "Statuses:",
        *[f"- {status}: {total}" for status, total in status_counts.items()],
        "",
        "Pending review sample:",
    ]
    for row in pending_rows:
        report_lines.extend(
            [
                (
                    f"- id {row['id']} | {row['source_name']} -> {row['portuguese_name']} "
                    f"| family={row['name_family']} | evidence={row['evidence_count']}"
                ),
                f"  first_segment_id: {row['first_segment_id']}",
            ]
        )
    if not pending_rows:
        report_lines.append("- No pending rows")

    report_path = db.write_report(settings, "build_name_equivalence_queue", report_lines)
    print(f"[build_name_equivalence_queue] Candidates inspected: {len(candidates)}")
    for key, value in result_counts.most_common():
        print(f"[build_name_equivalence_queue] {key}: {value}")
    print(f"[build_name_equivalence_queue] Pending: {status_counts.get('pending', 0)}")
    print(f"[build_name_equivalence_queue] Report: {report_path}")
    print("[build_name_equivalence_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a human-review queue for historical name equivalences.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidates to queue.")
    args = parser.parse_args()
    main(limit=args.limit)
