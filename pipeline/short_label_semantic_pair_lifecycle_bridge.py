from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "short_label_semantic_pair_lifecycle_bridge_v1"
BRIDGE_NAME = "short_label_semantic_pair_lifecycle_bridge_v1"
READY_STATUS = "ready_for_lifecycle_bridge"
EXPECTED_LEDGER_RUN_ID = 70
EXPECTED_SEGMENT_STATE_RUN_ID = 353
ALLOWED_DECISIONS = {
    "lifecycle_ready_quote_fragment",
    "lifecycle_ready_nominal_label",
    "lifecycle_ready_short_phrase",
    "lifecycle_ready_compact_option",
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_short_label_semantic_pair_lifecycle_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            reviewed_jsonl TEXT NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            block_counts_json TEXT,
            decision_counts_json TEXT,
            report_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_short_label_semantic_pair_lifecycle_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            reviewed_jsonl TEXT NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            decision TEXT NOT NULL,
            suggested_subpolicy TEXT,
            surface_bucket TEXT,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            lifecycle_candidate INTEGER NOT NULL DEFAULT 0,
            requires_apply_later INTEGER NOT NULL DEFAULT 0,
            tokens_preserved INTEGER NOT NULL DEFAULT 0,
            corrected_text TEXT,
            confidence REAL,
            current_text TEXT,
            spanish_text TEXT,
            english_text TEXT,
            ledger_item_ids_json TEXT,
            issue_families_json TEXT,
            issue_tags_json TEXT,
            rationale TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_short_label_semantic_pair_lifecycle_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_semantic_pair_bridge_items_run_status
        ON ml_short_label_semantic_pair_lifecycle_bridge_items(run_id, bridge_status, decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_semantic_pair_bridge_items_segment
        ON ml_short_label_semantic_pair_lifecycle_bridge_items(segment_id, run_id)
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_semantic_pair_lifecycle_bridge"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def load_review_rows(review_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with review_jsonl.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_number"] = line_number
            rows.append(row)
    return rows


def infer_source_ids(rows: list[dict[str, Any]]) -> tuple[int, int]:
    ledger_ids = {int(row.get("ledger_run_id") or 0) for row in rows if row.get("lifecycle_candidate") is True}
    state_ids = {int(row.get("segment_state_run_id") or 0) for row in rows if row.get("lifecycle_candidate") is True}
    if len(ledger_ids) != 1 or 0 in ledger_ids:
        raise RuntimeError(f"Expected exactly one nonzero ledger_run_id among lifecycle candidates, got {sorted(ledger_ids)}")
    if len(state_ids) != 1 or 0 in state_ids:
        raise RuntimeError(
            f"Expected exactly one nonzero segment_state_run_id among lifecycle candidates, got {sorted(state_ids)}"
        )
    return ledger_ids.pop(), state_ids.pop()


def classify_review_row(
    row: dict[str, Any],
    seen_segments: set[int],
    *,
    expected_ledger_run_id: int,
    expected_segment_state_run_id: int,
) -> tuple[str, str | None]:
    reasons: list[str] = []
    segment_id = int(row.get("segment_id") or 0)
    if int(row.get("ledger_run_id") or 0) != expected_ledger_run_id:
        reasons.append("ledger_run_id_mismatch")
    if int(row.get("segment_state_run_id") or 0) != expected_segment_state_run_id:
        reasons.append("segment_state_run_id_mismatch")
    if row.get("lifecycle_candidate") is not True:
        reasons.append("not_lifecycle_candidate")
    if row.get("requires_apply_later") is not False:
        reasons.append("requires_apply_later")
    if row.get("tokens_preserved") is not True:
        reasons.append("tokens_not_preserved")
    if row.get("decision") not in ALLOWED_DECISIONS:
        reasons.append("decision_not_lifecycle_ready")
    if as_text(row.get("corrected_text")).strip():
        reasons.append("corrected_text_present")
    if segment_id in seen_segments:
        reasons.append("duplicate_segment_id")
    if reasons:
        return "blocked", ";".join(reasons)
    seen_segments.add(segment_id)
    return READY_STATUS, None


def build_items(
    rows: list[dict[str, Any]],
    *,
    run_id: int,
    reviewed_jsonl: Path,
    expected_ledger_run_id: int,
    expected_segment_state_run_id: int,
) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    seen_segments: set[int] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        if row.get("lifecycle_candidate") is not True:
            continue
        status, block_reason = classify_review_row(
            row,
            seen_segments,
            expected_ledger_run_id=expected_ledger_run_id,
            expected_segment_state_run_id=expected_segment_state_run_id,
        )
        items.append(
            {
                "run_id": run_id,
                "reviewed_jsonl": str(reviewed_jsonl),
                "source_ledger_run_id": int(row.get("ledger_run_id") or 0),
                "source_segment_state_run_id": int(row.get("segment_state_run_id") or 0),
                "segment_id": int(row.get("segment_id") or 0),
                "relative_path": as_text(row.get("relative_path")),
                "source_key": as_text(row.get("source_key")),
                "decision": as_text(row.get("decision")),
                "suggested_subpolicy": as_text(row.get("suggested_subpolicy")),
                "surface_bucket": as_text(row.get("surface_bucket")),
                "bridge_status": status,
                "block_reason": block_reason,
                "lifecycle_candidate": 1 if row.get("lifecycle_candidate") is True else 0,
                "requires_apply_later": 1 if row.get("requires_apply_later") is True else 0,
                "tokens_preserved": 1 if row.get("tokens_preserved") is True else 0,
                "corrected_text": as_text(row.get("corrected_text")),
                "confidence": float(row.get("confidence") or 0),
                "current_text": as_text(row.get("current_text")),
                "spanish_text": as_text(row.get("spanish_text")),
                "english_text": as_text(row.get("english_text")),
                "ledger_item_ids_json": json.dumps(row.get("ledger_item_ids", []), ensure_ascii=False, sort_keys=True),
                "issue_families_json": json.dumps(row.get("issue_families", []), ensure_ascii=False, sort_keys=True),
                "issue_tags_json": json.dumps(row.get("issue_tags", []), ensure_ascii=False, sort_keys=True),
                "rationale": as_text(row.get("rationale")),
                "created_at": created_at,
            }
        )
    return items


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = list(items[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"""
        INSERT INTO ml_short_label_semantic_pair_lifecycle_bridge_items (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        """,
        [tuple(item[column] for column in columns) for item in items],
    )


def write_outputs(txt_path: Path, jsonl_path: Path, *, run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["bridge_status"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
    decision_counts = Counter(item["decision"] for item in items)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Short-label semantic pair lifecycle bridge",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {run_id}",
        "",
        "Summary:",
        f"- candidates: {len(items):,}",
        f"- ready_for_lifecycle_bridge: {status_counts.get(READY_STATUS, 0):,}",
        f"- blocked: {len(items) - status_counts.get(READY_STATUS, 0):,}",
        f"- estimated_closed_gain: {status_counts.get(READY_STATUS, 0):,}",
        "- production_release_allowed: 0",
        "",
        "Decisions:",
    ]
    lines.extend(f"- {name}: {count:,}" for name, count in decision_counts.most_common()) if decision_counts else lines.append("- none")
    lines.extend(["", "Block reasons:"])
    lines.extend(f"- {name}: {count:,}" for name, count in block_counts.most_common()) if block_counts else lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Bridge only; no output/source writes.",
            "- No confirmations created or promoted.",
            "- Segment-state must still re-check the live guards before closing anything.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, review_jsonl: str) -> dict[str, Any]:
    settings = db.load_settings()
    review_path = Path(review_jsonl)
    if not review_path.exists():
        raise FileNotFoundError(review_path)
    rows = load_review_rows(review_path)
    source_ledger_run_id, source_segment_state_run_id = infer_source_ids(rows)
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        now = db.utc_now()
        run_id = int(
            conn.execute(
                """
                INSERT INTO ml_short_label_semantic_pair_lifecycle_bridge_runs (
                    rule_version,
                    bridge_name,
                    reviewed_jsonl,
                    source_ledger_run_id,
                    source_segment_state_run_id,
                    candidate_count,
                    ready_count,
                    blocked_count,
                    estimated_closed_gain,
                    production_release_allowed,
                    status_counts_json,
                    block_counts_json,
                    decision_counts_json,
                    report_path,
                    jsonl_path,
                    started_at,
                    finished_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '{}', '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    BRIDGE_NAME,
                    str(review_path),
                    source_ledger_run_id,
                    source_segment_state_run_id,
                    str(txt_path),
                    str(jsonl_path),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        items = build_items(
            rows,
            run_id=run_id,
            reviewed_jsonl=review_path,
            expected_ledger_run_id=source_ledger_run_id,
            expected_segment_state_run_id=source_segment_state_run_id,
        )
        insert_items(conn, items)
        status_counts = Counter(item["bridge_status"] for item in items)
        block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
        decision_counts = Counter(item["decision"] for item in items)
        ready_count = status_counts.get(READY_STATUS, 0)
        blocked_count = len(items) - ready_count
        conn.execute(
            """
            UPDATE ml_short_label_semantic_pair_lifecycle_bridge_runs
            SET candidate_count = ?,
                ready_count = ?,
                blocked_count = ?,
                estimated_closed_gain = ?,
                status_counts_json = ?,
                block_counts_json = ?,
                decision_counts_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                ready_count,
                blocked_count,
                ready_count,
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        conn.commit()

    write_outputs(txt_path, jsonl_path, run_id=run_id, items=items)
    print("[short_label_semantic_pair_lifecycle_bridge] Bridge generated")
    print(f"[short_label_semantic_pair_lifecycle_bridge] Bridge run id: {run_id}")
    print(f"[short_label_semantic_pair_lifecycle_bridge] Candidates: {len(items):,}")
    print(f"[short_label_semantic_pair_lifecycle_bridge] Ready: {ready_count:,}")
    print(f"[short_label_semantic_pair_lifecycle_bridge] Blocked: {blocked_count:,}")
    print(f"[short_label_semantic_pair_lifecycle_bridge] Report: {txt_path}")
    return {
        "run_id": run_id,
        "candidates": len(items),
        "ready": ready_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build read-only lifecycle bridge for reviewed short-label semantic pairs.")
    parser.add_argument("--review-jsonl", required=True)
    args = parser.parse_args()
    main(review_jsonl=args.review_jsonl)
