from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "build_offline_review_queue_v1"
DEFAULT_STATUSES = ("needs_review",)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_run_id(conn) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM offline_proposal_runs").fetchone()
    if not row or row["id"] is None:
        return None
    return int(row["id"])


def create_run(conn, limit: int, offline_run_id: int) -> int:
    timestamp = now()
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode,
            limit_count,
            auto_confidence_threshold,
            status,
            notes,
            started_at,
            updated_at
        )
        VALUES (?, ?, 1.0, 'running', ?, ?, ?)
        """,
        (
            "offline-review:all",
            limit,
            f"offline_proposal_run:{offline_run_id}",
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def fetch_candidates(
    conn,
    offline_run_id: int,
    limit: int,
    path_like: str | None,
    statuses: tuple[str, ...],
    reason_like: str | None = None,
    issue_code: str | None = None,
    proposal_source: str | None = None,
) -> list[dict]:
    placeholders = ",".join("?" for _ in statuses)
    params: list[object] = [offline_run_id, *statuses]
    path_sql = ""
    if path_like:
        path_sql = "AND op.relative_path LIKE ?"
        params.append(path_like)
    reason_sql = ""
    if reason_like:
        reason_sql = "AND op.reasons_json LIKE ?"
        params.append(f"%{reason_like}%")
    issue_sql = ""
    if issue_code:
        issue_sql = "AND op.issues_json LIKE ?"
        params.append(f"%\"code\": \"{issue_code}\"%")
    source_sql = ""
    if proposal_source:
        source_sql = "AND op.proposal_source = ?"
        params.append(proposal_source)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            op.*,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM offline_proposals op
        JOIN source_segments s ON s.id = op.segment_id
        LEFT JOIN output_segments o ON o.segment_id = op.segment_id
        LEFT JOIN local_learning_candidates c ON c.offline_proposal_id = op.id
        WHERE op.run_id = ?
          AND op.status IN ({placeholders})
          AND c.id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM local_learning_candidates previous
              WHERE previous.segment_id = op.segment_id
                AND previous.suggested_text = op.proposed_text
                AND previous.human_label <> 'pending'
          )
          {path_sql}
          {reason_sql}
          {issue_sql}
          {source_sql}
        ORDER BY
            CASE
                WHEN op.proposal_source LIKE '%inline_literal%' THEN 0
                WHEN op.proposal_source LIKE '%visible_word%' THEN 1
                WHEN op.proposal_source LIKE '%normalize_spanish_punctuation%' THEN 2
                ELSE 3
            END,
            op.high_issue_count DESC,
            op.medium_issue_count DESC,
            op.confidence_score DESC,
            op.segment_id ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def reasons_for_candidate(row: dict) -> list[str]:
    reasons = [
        f"offline_run:{row['run_id']}",
        f"offline_status:{row['status']}",
        f"offline_source:{row['proposal_source']}",
        f"offline_bucket:{row['candidate_bucket']}",
        f"offline_token_status:{row['token_status']}",
        f"offline_score:{float(row['confidence_score'] or 0):.3f}",
    ]
    for field, prefix in (
        ("rules_json", "offline_rule"),
        ("reasons_json", "offline_reason"),
        ("issues_json", "offline_issue"),
    ):
        try:
            values = json.loads(row[field] or "[]")
        except json.JSONDecodeError:
            values = []
        if field == "issues_json":
            for issue in values[:12]:
                if isinstance(issue, dict):
                    reasons.append(f"{prefix}:{issue.get('code', 'unknown')}")
        else:
            for value in values[:12]:
                reasons.append(f"{prefix}:{value}")
    return reasons


def insert_candidates(conn, local_run_id: int, rows: list[dict]) -> int:
    timestamp = now()
    inserted = 0
    for row in rows:
        suggested_text = row["proposed_text"] or ""
        suggested_hash = sha256_text(suggested_text)
        conn.execute(
            """
            INSERT INTO local_learning_candidates (
                run_id,
                feedback_id,
                suggestion_id,
                offline_proposal_id,
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
                queue_source,
                focus_group,
                source_language,
                origin,
                match_type,
                match_score,
                token_status,
                suggestion_status,
                local_confidence_score,
                local_status,
                reasons_json,
                created_at,
                updated_at
            )
            VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'offline-review', 'offline', 'offline', ?, ?, ?, ?, ?, ?, 'pending_human', ?, ?, ?)
            """,
            (
                local_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["english_text"],
                row["spanish_text"],
                row["old_text"],
                row["current_output_text"],
                suggested_text,
                suggested_hash,
                f"offline_proposals:{row['proposal_source']}",
                row["candidate_bucket"],
                float(row["confidence_score"] or 0),
                row["token_status"],
                row["status"],
                float(row["confidence_score"] or 0),
                json.dumps(reasons_for_candidate(row), ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        inserted += 1
    return inserted


def update_run(conn, local_run_id: int, inserted: int) -> None:
    timestamp = now()
    conn.execute(
        """
        UPDATE local_learning_runs
        SET candidate_count = ?,
            pending_human_count = ?,
            status = 'completed',
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (inserted, inserted, timestamp, timestamp, local_run_id),
    )


def build_report(
    started_at: datetime,
    offline_run_id: int,
    local_run_id: int,
    rows: list[dict],
    inserted: int,
    limit: int,
    path_like: str | None,
    reason_like: str | None,
    issue_code: str | None,
    proposal_source: str | None,
) -> list[str]:
    elapsed = datetime.now() - started_at
    status_counts = Counter(row["status"] for row in rows)
    source_counts = Counter(row["proposal_source"] for row in rows)
    bucket_counts = Counter(row["candidate_bucket"] for row in rows)
    package_counts = Counter(row["relative_path"] for row in rows)
    lines = [
        "Build offline review queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Offline proposal run id: {offline_run_id}",
        f"Local learning run id: {local_run_id}",
        f"Limit: {limit}",
        f"Path filter: {path_like or 'none'}",
        f"Reason filter: {reason_like or 'none'}",
        f"Issue filter: {issue_code or 'none'}",
        f"Proposal source filter: {proposal_source or 'none'}",
        "",
        "Summary:",
        f"- Candidates selected: {len(rows)}",
        f"- Candidates inserted: {inserted}",
        "",
        "Statuses:",
        *[f"- {key}: {count}" for key, count in status_counts.most_common()],
        "",
        "Proposal sources:",
        *[f"- {key}: {count}" for key, count in source_counts.most_common(20)],
        "",
        "Buckets:",
        *[f"- {key}: {count}" for key, count in bucket_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {key}: {count}" for key, count in package_counts.most_common(30)],
        "",
        "Review SQL examples:",
        f"SELECT id, segment_id, source_key, old_text, suggested_text, human_label, reason",
        f"FROM local_learning_candidates",
        f"WHERE run_id = {local_run_id}",
        f"ORDER BY id;",
        "",
        "Label guidance:",
        "- correct: proposta pronta como está.",
        "- minor_fix: quase pronta, ajuste superficial pequeno.",
        "- major_fix: ideia ajuda, mas precisa reescrita relevante/corrected_text.",
        "- residual_spanish: ainda há espanhol demais.",
        "- structure_error: token, literal, macro ou markup quebrado.",
        "- semantic_error: português fluente, sentido errado.",
        "- wrong: não ajuda.",
        "- harmful: piora um texto bom ou quebra algo importante.",
        "",
        "Preview:",
    ]
    for row in rows[:60]:
        before = (row["original_text"] or "").replace("\n", "\\n")
        after = (row["proposed_text"] or "").replace("\n", "\\n")
        if len(before) > 180:
            before = before[:180] + "..."
        if len(after) > 180:
            after = after[:180] + "..."
        lines.extend(
            [
                f"- segment {row['segment_id']} | {row['proposal_source']} | {row['relative_path']}::{row['source_key']}",
                f"  before: {before}",
                f"  after:  {after}",
            ]
        )
    return lines


def main(
    offline_run_id: int | None = None,
    limit: int | None = None,
    path_like: str | None = None,
    reason_like: str | None = None,
    issue_code: str | None = None,
    proposal_source: str | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    limit = limit or 50
    print("[build_offline_review_queue] Starting offline review queue")
    print(f"[build_offline_review_queue] Rule version: {RULE_VERSION}")
    print(f"[build_offline_review_queue] Limit: {limit}")
    print(f"[build_offline_review_queue] Path filter: {path_like or 'none'}")
    print(f"[build_offline_review_queue] Reason filter: {reason_like or 'none'}")
    print(f"[build_offline_review_queue] Issue filter: {issue_code or 'none'}")
    print(f"[build_offline_review_queue] Proposal source filter: {proposal_source or 'none'}")
    print(f"[build_offline_review_queue] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = offline_run_id if offline_run_id is not None else latest_run_id(conn)
        if selected_run_id is None:
            raise RuntimeError("No offline_proposal_runs found. Run offline-proposals first.")
        print(f"[build_offline_review_queue] Offline proposal run id: {selected_run_id}")
        local_run_id = create_run(conn, limit, selected_run_id)
        rows = fetch_candidates(
            conn,
            selected_run_id,
            limit,
            path_like,
            DEFAULT_STATUSES,
            reason_like=reason_like,
            issue_code=issue_code,
            proposal_source=proposal_source,
        )
        inserted = insert_candidates(conn, local_run_id, rows)
        update_run(conn, local_run_id, inserted)
        conn.commit()

    report_lines = build_report(
        started_at,
        selected_run_id,
        local_run_id,
        rows,
        inserted,
        limit,
        path_like,
        reason_like,
        issue_code,
        proposal_source,
    )
    report_path = db.write_report(settings, "offline_review_queue", report_lines)
    print(f"[build_offline_review_queue] Local learning run id: {local_run_id}")
    print(f"[build_offline_review_queue] Candidates inserted: {inserted}")
    print(f"[build_offline_review_queue] Report: {report_path}")
    print("[build_offline_review_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Queue offline proposals for human learning review.")
    parser.add_argument("--offline-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--reason-like", default=None)
    parser.add_argument("--issue-code", default=None)
    parser.add_argument("--proposal-source", default=None)
    args = parser.parse_args()
    main(
        offline_run_id=args.offline_run_id,
        limit=args.limit,
        path_like=args.path_like,
        reason_like=args.reason_like,
        issue_code=args.issue_code,
        proposal_source=args.proposal_source,
    )
