from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_segment_lifecycle_bridge_proposal_v1"
BRIDGE_NAME = "select_cstring_segment_lifecycle_bridge_v1"
BRIDGE_STATUS = "proposal_shadow"
BRIDGE_ACTION = "propose_segment_level_lifecycle_bridge"
PRODUCTION_RELEASE_ALLOWED = 0


def latest_maturity_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_segment_composition_maturity_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed Select_CString segment composition maturity audit found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_segment_lifecycle_bridge_proposal_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            source_maturity_audit_run_id INTEGER NOT NULL,
            source_segment_overlay_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            review_required_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            bridge_candidate INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            source_counts_json TEXT,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_segment_lifecycle_bridge_proposal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_maturity_audit_run_id INTEGER NOT NULL,
            source_segment_overlay_run_id INTEGER NOT NULL,
            segment_overlay_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            composition_source TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            bridge_action TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            blocking_reason TEXT,
            source_refs_json TEXT NOT NULL,
            guardrails_json TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_segment_lifecycle_bridge_proposal_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_segment_bridge_items_run
        ON ml_issue_select_cstring_segment_lifecycle_bridge_proposal_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_segment_bridge_items_segment
        ON ml_issue_select_cstring_segment_lifecycle_bridge_proposal_items(segment_id, bridge_status)
        """
    )


def report_paths(settings: dict[str, Any], audit_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_segment_lifecycle_bridge_proposal_audit_run_{audit_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_maturity_audit_run(conn, audit_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_select_cstring_segment_composition_maturity_audit_runs WHERE id = ?",
        (audit_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment composition maturity audit run not found: {audit_run_id}")
    return dict(row)


def fetch_overlay_items(conn, overlay_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_segment_composition_overlay_items
        WHERE run_id = ?
        ORDER BY segment_id, id
        """,
        (overlay_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_item(row: dict[str, Any], *, audit: dict[str, Any]) -> dict[str, Any]:
    blocking_reason = ""
    status = "ready_for_lifecycle_bridge"
    risk_level = "low"
    if int(audit.get("bridge_candidate") or 0) != 1:
        status = "blocked"
        blocking_reason = "source_maturity_audit_not_bridge_candidate"
        risk_level = "high"
    elif str(audit.get("maturity_status") or "") != "bridge_candidate_shadow":
        status = "blocked"
        blocking_reason = f"unexpected_maturity_status:{audit.get('maturity_status')}"
        risk_level = "high"
    elif int(row.get("composed_allowed") or 0) != 1:
        status = "blocked"
        blocking_reason = row.get("block_reason") or "composition_not_allowed"
        risk_level = "high"
    elif int(row.get("production_release_allowed") or 0) != 0:
        status = "blocked"
        blocking_reason = "source_overlay_has_production_authority"
        risk_level = "high"
    elif not row.get("segment_id"):
        status = "blocked"
        blocking_reason = "missing_segment_id"
        risk_level = "high"
    source_refs = {
        "source_final_overlay_run_id": row.get("source_final_overlay_run_id"),
        "source_final_overlay_item_id": row.get("source_final_overlay_item_id"),
        "source_preterite_checkpoint_run_id": row.get("source_preterite_checkpoint_run_id"),
        "source_preterite_checkpoint_item_id": row.get("source_preterite_checkpoint_item_id"),
    }
    guardrails = {
        "shadow_only": 1,
        "no_output_write": 1,
        "no_production_release": 1,
        "requires_future_segment_state_bridge": 1,
        "requires_future_production_flow": 1,
    }
    return {
        "segment_overlay_item_id": int(row["id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "composition_source": str(row.get("composition_source") or ""),
        "bridge_status": status,
        "bridge_action": BRIDGE_ACTION,
        "risk_level": risk_level,
        "blocking_reason": blocking_reason,
        "source_refs_json": json.dumps(source_refs, ensure_ascii=False, sort_keys=True),
        "guardrails_json": json.dumps(guardrails, ensure_ascii=False, sort_keys=True),
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    audit_run: dict[str, Any],
    rows: list[dict[str, Any]],
    status_counts: Counter[str],
    source_counts: Counter[str],
    block_counts: Counter[str],
) -> None:
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "composition_source",
        "bridge_status",
        "risk_level",
        "blocking_reason",
        "production_release_allowed",
        "source_refs_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({"run_id": run_id, **row}, ensure_ascii=False, sort_keys=True) + "\n")
    ready = status_counts.get("ready_for_lifecycle_bridge", 0)
    blocked = status_counts.get("blocked", 0)
    review = sum(value for key, value in status_counts.items() if key not in {"ready_for_lifecycle_bridge", "blocked"})
    lines = [
        "Select_CString segment lifecycle bridge proposal",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Bridge status: {BRIDGE_STATUS}",
        f"Bridge run id: {run_id}",
        f"Source maturity audit run id: {audit_run['id']}",
        f"Source segment overlay run id: {audit_run['source_segment_overlay_run_id']}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready: {ready:,}",
        f"- Review required: {review:,}",
        f"- Blocked: {blocked:,}",
        f"- Bridge candidate: {1 if ready == len(rows) and blocked == 0 and review == 0 else 0}",
        "",
        "Composition sources:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Bridge statuses:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in block_counts.most_common()] or ["- none"]),
        "",
        "Safety:",
        "- Proposal shadow only.",
        "- This does not read source files, write output, or release production authority.",
        "- Future segment_state/lifecycle integration must consume this proposal explicitly.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, audit_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        ensure_tables(conn)
        selected_audit_run_id = audit_run_id or latest_maturity_audit_run_id(conn)
        audit = fetch_maturity_audit_run(conn, selected_audit_run_id)
        overlay_run_id = int(audit["source_segment_overlay_run_id"])
        overlay_rows = fetch_overlay_items(conn, overlay_run_id)
        rows = [classify_item(row, audit=audit) for row in overlay_rows]
        status_counts = Counter(row["bridge_status"] for row in rows)
        source_counts = Counter(row["composition_source"] for row in rows)
        block_counts = Counter(row["blocking_reason"] for row in rows if row["blocking_reason"])
        ready = status_counts.get("ready_for_lifecycle_bridge", 0)
        blocked = status_counts.get("blocked", 0)
        review = sum(value for key, value in status_counts.items() if key not in {"ready_for_lifecycle_bridge", "blocked"})
        bridge_candidate = 1 if rows and ready == len(rows) and blocked == 0 and review == 0 else 0
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_audit_run_id)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_segment_lifecycle_bridge_proposal_runs (
                started_at, finished_at, rule_version, bridge_name, bridge_status,
                source_maturity_audit_run_id, source_segment_overlay_run_id,
                total_candidates, ready_count, review_required_count, blocked_count,
                production_release_allowed, bridge_candidate, status_counts_json,
                source_counts_json, block_counts_json, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                RULE_VERSION,
                BRIDGE_NAME,
                BRIDGE_STATUS,
                selected_audit_run_id,
                overlay_run_id,
                len(rows),
                ready,
                review,
                blocked,
                bridge_candidate,
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_segment_lifecycle_bridge_proposal_items (
                run_id, source_maturity_audit_run_id, source_segment_overlay_run_id,
                segment_overlay_item_id, segment_id, relative_path, source_key,
                composition_source, bridge_status, bridge_action, risk_level,
                blocking_reason, source_refs_json, guardrails_json,
                production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    selected_audit_run_id,
                    overlay_run_id,
                    row["segment_overlay_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["composition_source"],
                    row["bridge_status"],
                    row["bridge_action"],
                    row["risk_level"],
                    row["blocking_reason"],
                    row["source_refs_json"],
                    row["guardrails_json"],
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
        audit_run=audit,
        rows=rows,
        status_counts=status_counts,
        source_counts=source_counts,
        block_counts=block_counts,
    )
    payload = {
        "run_id": run_id,
        "source_maturity_audit_run_id": selected_audit_run_id,
        "source_segment_overlay_run_id": overlay_run_id,
        "total_candidates": len(rows),
        "ready_count": ready,
        "blocked_count": blocked,
        "bridge_candidate": bridge_candidate,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a shadow lifecycle bridge proposal for mature Select_CString segment composition.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    args = parser.parse_args()
    run(audit_run_id=args.audit_run_id)
