from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_v1"
BRIDGE_NAME = "short_label_pure_no_token_segment_lifecycle_bridge_v1"
BRIDGE_STATUS = "proposal_shadow"
BRIDGE_ACTION = "propose_short_label_pure_no_token_segment_lifecycle_bridge"
REQUIRED_SOURCE = "short_label_pure_no_token_checkpoint"
PRODUCTION_RELEASE_ALLOWED = 0


def canonical_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def sha1_text(value: str | None) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_coverage_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
          AND fully_covered_segments > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished issue partial coverage run found.")
    return int(row["id"])


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_allowed_count > 0
          AND production_release_allowed = 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished pure no-token checkpoint run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            source_coverage_run_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            review_required_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            bridge_candidate INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            block_counts_json TEXT,
            source_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_coverage_run_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_coverage_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            bridge_status TEXT NOT NULL,
            bridge_action TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            blocking_reason TEXT,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            pure_no_token_checkpoint_issue_count INTEGER NOT NULL DEFAULT 0,
            coverage_sources_json TEXT NOT NULL,
            guardrails_json TEXT NOT NULL,
            current_final_state TEXT,
            current_review_state TEXT,
            current_apply_state TEXT,
            current_is_closed INTEGER NOT NULL DEFAULT 0,
            confirmed_text_hash TEXT,
            output_text_hash TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_pure_segment_bridge_items_run
        ON ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items(run_id, bridge_status, segment_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_pure_segment_bridge_items_segment
        ON ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items(segment_id, bridge_status)
        """
    )


def fetch_coverage_run(conn, coverage_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (coverage_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Coverage run not found: {coverage_run_id}")
    return dict(row)


def fetch_checkpoint_run(conn, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, coverage_run_id: int, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH pure_checkpoint_segments AS (
            SELECT
                segment_id,
                COUNT(*) AS pure_no_token_checkpoint_issue_count
            FROM ml_issue_short_label_pure_no_token_checkpoint_items
            WHERE checkpoint_run_id = ?
              AND checkpoint_allowed = 1
            GROUP BY segment_id
        )
        SELECT
            cov.id AS coverage_item_id,
            cov.*,
            pure.pure_no_token_checkpoint_issue_count,
            state.final_state AS current_final_state,
            state.review_state AS current_review_state,
            state.apply_state AS current_apply_state,
            state.is_closed AS current_is_closed,
            state.locked AS current_locked,
            state.confirmed_matches_output AS current_confirmed_matches_output,
            confirmation.confirmed_text,
            confirmation.locked AS confirmation_locked,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            output.portuguese_text AS output_text
        FROM ml_issue_partial_coverage_items cov
        JOIN pure_checkpoint_segments pure
          ON pure.segment_id = cov.segment_id
        LEFT JOIN segment_state_items state
          ON state.run_id = cov.segment_state_run_id
         AND state.segment_id = cov.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = cov.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = cov.segment_id
        WHERE cov.run_id = ?
          AND cov.coverage_sources_json LIKE ?
        ORDER BY cov.relative_path, cov.source_line_number, cov.segment_id
        """,
        (checkpoint_run_id, coverage_run_id, f"%{REQUIRED_SOURCE}%"),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any], *, coverage_run: dict[str, Any], checkpoint_run: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    sources = parse_json_dict(row.get("coverage_sources_json"))
    confirmed = row.get("confirmed_text")
    output = row.get("output_text")

    if checkpoint_run.get("checkpoint_status") != "ready_for_lifecycle_policy":
        reasons.append("checkpoint_run_not_ready_for_lifecycle_policy")
    if int(checkpoint_run.get("production_release_allowed") or 0) != 0:
        reasons.append("checkpoint_run_has_production_authority")
    if int(coverage_run.get("id") or 0) != int(row.get("run_id") or 0):
        reasons.append("coverage_run_mismatch")
    if row.get("coverage_state") != "full":
        reasons.append("coverage_not_full")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_count_not_zero")
    if int(row.get("blocked_issue_count") or 0) != 0:
        reasons.append("blocked_issue_count_not_zero")
    if REQUIRED_SOURCE not in " ".join(sources):
        reasons.append("missing_required_pure_no_token_source")
    if int(row.get("pure_no_token_checkpoint_issue_count") or 0) <= 0:
        reasons.append("missing_pure_no_token_checkpoint_item")
    if row.get("current_final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("current_state_not_reopen_auto_confirmed_autofix")
    if row.get("current_review_state") != "auto_confirmed":
        reasons.append("current_review_state_not_auto_confirmed")
    if row.get("current_apply_state") != "needs_review":
        reasons.append("current_apply_state_not_needs_review")
    if int(row.get("current_is_closed") or 0) != 0 or int(row.get("is_closed") or 0) != 0:
        reasons.append("already_closed")
    if int(row.get("current_locked") or 0) != 0 or int(row.get("confirmation_locked") or 0) != 0:
        reasons.append("locked_state_or_confirmation")
    if not confirmed:
        reasons.append("missing_confirmed_text")
    if not output:
        reasons.append("missing_output_text")
    if canonical_text(confirmed) != canonical_text(output):
        reasons.append("confirmed_output_canonical_mismatch")
    if protected_tokens(confirmed) != protected_tokens(output):
        reasons.append("token_mismatch")
    if int(row.get("current_confirmed_matches_output") or 0) != 1:
        reasons.append("segment_state_confirmed_matches_output_false")

    status = "ready_for_lifecycle_bridge" if not reasons else "blocked"
    risk_level = "low" if not reasons else "high"
    guardrails = {
        "shadow_only": 1,
        "no_output_write": 1,
        "no_source_read": 1,
        "no_source_write": 1,
        "no_production_release": 1,
        "requires_future_segment_state_bridge": 1,
        "requires_future_production_flow": 1,
        "requires_full_issue_coverage": 1,
        "requires_confirmed_output_match": 1,
    }
    return {
        "source_coverage_item_id": int(row["coverage_item_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "bridge_status": status,
        "bridge_action": BRIDGE_ACTION,
        "risk_level": risk_level,
        "blocking_reason": ";".join(reasons),
        "total_issue_count": int(row.get("total_issue_count") or 0),
        "covered_issue_count": int(row.get("covered_issue_count") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "blocked_issue_count": int(row.get("blocked_issue_count") or 0),
        "pure_no_token_checkpoint_issue_count": int(row.get("pure_no_token_checkpoint_issue_count") or 0),
        "coverage_sources_json": row.get("coverage_sources_json") or "{}",
        "guardrails_json": json.dumps(guardrails, ensure_ascii=False, sort_keys=True),
        "current_final_state": row.get("current_final_state"),
        "current_review_state": row.get("current_review_state"),
        "current_apply_state": row.get("current_apply_state"),
        "current_is_closed": int(row.get("current_is_closed") or 0),
        "confirmed_text_hash": sha1_text(confirmed),
        "output_text_hash": sha1_text(output),
        "confirmed_text": confirmed,
        "output_text": output,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    coverage_run: dict[str, Any],
    checkpoint_run: dict[str, Any],
    rows: list[dict[str, Any]],
    status_counts: Counter[str],
    block_counts: Counter[str],
    source_counts: Counter[str],
) -> None:
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "bridge_status",
        "risk_level",
        "blocking_reason",
        "total_issue_count",
        "covered_issue_count",
        "open_issue_count",
        "blocked_issue_count",
        "pure_no_token_checkpoint_issue_count",
        "current_final_state",
        "current_review_state",
        "current_apply_state",
        "current_is_closed",
        "confirmed_text_hash",
        "output_text_hash",
        "coverage_sources_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {key: value for key, value in row.items() if key not in {"confirmed_text", "output_text"}}
            handle.write(json.dumps({"run_id": run_id, **payload}, ensure_ascii=False, sort_keys=True) + "\n")

    ready = status_counts.get("ready_for_lifecycle_bridge", 0)
    blocked = status_counts.get("blocked", 0)
    review_required = sum(value for key, value in status_counts.items() if key not in {"ready_for_lifecycle_bridge", "blocked"})
    ready_rows = [row for row in rows if row["bridge_status"] == "ready_for_lifecycle_bridge"]
    blocked_rows = [row for row in rows if row["bridge_status"] != "ready_for_lifecycle_bridge"]
    lines = [
        "Short-label pure no-token segment lifecycle bridge proposal",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Bridge status: {BRIDGE_STATUS}",
        f"Bridge run id: {run_id}",
        f"Source coverage run id: {coverage_run['id']}",
        f"Source ledger run id: {coverage_run['ledger_run_id']}",
        f"Source segment-state run id: {coverage_run['segment_state_run_id']}",
        f"Source checkpoint run id: {checkpoint_run['id']}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready for lifecycle bridge: {ready:,}",
        f"- Review required: {review_required:,}",
        f"- Blocked: {blocked:,}",
        f"- Estimated closed gain: {ready:,}",
        f"- Bridge candidate: {1 if ready > 0 and blocked == 0 and review_required == 0 else 0}",
        "",
        "Coverage sources:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Bridge statuses:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in block_counts.most_common(30)] or ["- none"]),
        "",
        "Ready sample:",
    ]
    if ready_rows:
        for row in ready_rows[:25]:
            lines.extend(
                [
                    f"- segment={row['segment_id']} | issues={row['covered_issue_count']}/{row['total_issue_count']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                    f"  state={row['current_final_state']}; text={short(row.get('confirmed_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    if blocked_rows:
        for row in blocked_rows[:25]:
            lines.extend(
                [
                    f"- {row['blocking_reason']} | segment={row['segment_id']} | issues={row['covered_issue_count']}/{row['total_issue_count']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                    f"  state={row['current_final_state']}; confirmed={short(row.get('confirmed_text'))}; output={short(row.get('output_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Proposal shadow only.",
            "- No source files are read or written.",
            "- No output is written.",
            "- No production authority is granted.",
            "- Future segment_state integration must consume this proposal explicitly.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, coverage_run_id: int | None = None, checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_coverage_run_id = coverage_run_id or latest_coverage_run_id(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        coverage_run = fetch_coverage_run(conn, selected_coverage_run_id)
        checkpoint_run = fetch_checkpoint_run(conn, selected_checkpoint_run_id)
        raw_rows = fetch_rows(
            conn,
            coverage_run_id=selected_coverage_run_id,
            checkpoint_run_id=selected_checkpoint_run_id,
        )
        rows = [classify(row, coverage_run=coverage_run, checkpoint_run=checkpoint_run) for row in raw_rows]
        status_counts = Counter(row["bridge_status"] for row in rows)
        block_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        for row in rows:
            for source, count in parse_json_dict(row["coverage_sources_json"]).items():
                source_counts[str(source)] += int(count or 0)
            if row["blocking_reason"]:
                for reason in row["blocking_reason"].split(";"):
                    block_counts[reason] += 1
        ready = status_counts.get("ready_for_lifecycle_bridge", 0)
        blocked = status_counts.get("blocked", 0)
        review_required = sum(value for key, value in status_counts.items() if key not in {"ready_for_lifecycle_bridge", "blocked"})
        bridge_candidate = 1 if ready > 0 and blocked == 0 and review_required == 0 else 0
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs (
                started_at, finished_at, rule_version, bridge_name, bridge_status,
                source_coverage_run_id, source_checkpoint_run_id, source_ledger_run_id,
                source_segment_state_run_id, total_candidates, ready_count,
                review_required_count, blocked_count, estimated_closed_gain,
                production_release_allowed, bridge_candidate, status_counts_json,
                block_counts_json, source_counts_json, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                now,
                RULE_VERSION,
                BRIDGE_NAME,
                BRIDGE_STATUS,
                selected_coverage_run_id,
                selected_checkpoint_run_id,
                int(coverage_run["ledger_run_id"]),
                int(coverage_run["segment_state_run_id"]),
                len(rows),
                ready,
                review_required,
                blocked,
                ready,
                bridge_candidate,
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items (
                run_id, source_coverage_run_id, source_checkpoint_run_id,
                source_coverage_item_id, segment_id, relative_path, source_key,
                source_line_number, bridge_status, bridge_action, risk_level,
                blocking_reason, total_issue_count, covered_issue_count,
                open_issue_count, blocked_issue_count,
                pure_no_token_checkpoint_issue_count, coverage_sources_json,
                guardrails_json, current_final_state, current_review_state,
                current_apply_state, current_is_closed, confirmed_text_hash,
                output_text_hash, production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    selected_coverage_run_id,
                    selected_checkpoint_run_id,
                    row["source_coverage_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["bridge_status"],
                    row["bridge_action"],
                    row["risk_level"],
                    row["blocking_reason"],
                    row["total_issue_count"],
                    row["covered_issue_count"],
                    row["open_issue_count"],
                    row["blocked_issue_count"],
                    row["pure_no_token_checkpoint_issue_count"],
                    row["coverage_sources_json"],
                    row["guardrails_json"],
                    row["current_final_state"],
                    row["current_review_state"],
                    row["current_apply_state"],
                    row["current_is_closed"],
                    row["confirmed_text_hash"],
                    row["output_text_hash"],
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()
    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        coverage_run=coverage_run,
        checkpoint_run=checkpoint_run,
        rows=rows,
        status_counts=status_counts,
        block_counts=block_counts,
        source_counts=source_counts,
    )
    payload = {
        "run_id": run_id,
        "source_coverage_run_id": selected_coverage_run_id,
        "source_checkpoint_run_id": selected_checkpoint_run_id,
        "total_candidates": len(rows),
        "ready_count": ready,
        "blocked_count": blocked,
        "estimated_closed_gain": ready,
        "bridge_candidate": bridge_candidate,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create a shadow segment lifecycle bridge proposal from pure no-token short-label checkpoint coverage."
    )
    parser.add_argument("--coverage-run-id", type=int, default=None)
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    run(coverage_run_id=args.coverage_run_id, checkpoint_run_id=args.checkpoint_run_id)
