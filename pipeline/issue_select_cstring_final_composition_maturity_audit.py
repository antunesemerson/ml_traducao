from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_final_composition_maturity_audit_v1"
AUDIT_NAME = "select_cstring_final_composition_maturity_audit_v1"
AUDIT_STATUS = "shadow_maturity_audit"
PRODUCTION_RELEASE_ALLOWED = 0


SOURCE_TABLES = {
    "baseline_lifecycle": (
        "ml_issue_select_cstring_same_token_lifecycle_items",
        "source_lifecycle_item_id",
        "policy_allowed",
        None,
    ),
    "residual_literal_cleanup": (
        "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items",
        "source_cleanup_item_id",
        "checkpoint_allowed",
        "source_cleanup_checkpoint_run_id",
    ),
    "dynamic_literal_payload_delta": (
        "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items",
        "source_dynamic_payload_item_id",
        "checkpoint_allowed",
        "source_dynamic_payload_checkpoint_run_id",
    ),
    "local_player_pronoun_literal": (
        "ml_issue_select_cstring_local_player_pronoun_checkpoint_items",
        "source_local_pronoun_item_id",
        "checkpoint_allowed",
        "source_local_pronoun_checkpoint_run_id",
    ),
}


def latest_final_overlay_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_final_composition_overlay_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed final Select_CString composition overlay run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_final_composition_maturity_audit_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            audit_name TEXT NOT NULL,
            audit_status TEXT NOT NULL,
            source_final_overlay_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            composed_released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            duplicate_segment_count INTEGER NOT NULL DEFAULT 0,
            missing_source_ref_count INTEGER NOT NULL DEFAULT 0,
            invalid_source_ref_count INTEGER NOT NULL DEFAULT 0,
            unexpected_source_count INTEGER NOT NULL DEFAULT 0,
            maturity_status TEXT NOT NULL,
            bridge_candidate INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            source_counts_json TEXT,
            issue_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_final_composition_maturity_audit_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_final_overlay_run_id INTEGER NOT NULL,
            final_overlay_item_id INTEGER,
            segment_id INTEGER,
            relative_path TEXT,
            source_key TEXT,
            composition_source TEXT,
            issue_code TEXT NOT NULL,
            issue_detail TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_final_composition_maturity_audit_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], overlay_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_final_composition_maturity_audit_overlay_run_{overlay_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_overlay_run(conn, overlay_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_select_cstring_final_composition_overlay_runs WHERE id = ?",
        (overlay_run_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"Final composition overlay run not found: {overlay_run_id}")
    return dict(row)


def fetch_overlay_items(conn, overlay_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_final_composition_overlay_items
        WHERE run_id = ?
        ORDER BY segment_id, id
        """,
        (overlay_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def source_ref_valid(conn, row: dict[str, Any]) -> tuple[bool, str]:
    source = row.get("composition_source")
    spec = SOURCE_TABLES.get(source)
    if spec is None:
        return False, f"unexpected_composition_source:{source}"
    table_name, id_column, allowed_column, run_column = spec
    source_id = row.get(id_column)
    if source_id is None:
        return False, f"missing_{id_column}"
    if run_column:
        source_run_id = row.get(run_column)
        if source_run_id is None:
            return False, f"missing_{run_column}"
        found = conn.execute(
            f"""
            SELECT 1
            FROM {table_name}
            WHERE id = ?
              AND run_id = ?
              AND {allowed_column} = 1
              AND COALESCE(block_reason, '') = ''
            """,
            (source_id, source_run_id),
        ).fetchone()
    else:
        found = conn.execute(
            f"""
            SELECT 1
            FROM {table_name}
            WHERE id = ?
              AND {allowed_column} = 1
              AND COALESCE(block_reason, '') = ''
            """,
            (source_id,),
        ).fetchone()
    if not found:
        return False, f"invalid_source_reference:{source}:{source_id}"
    return True, ""


def build_issues(conn, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_segment = Counter(int(row["segment_id"]) for row in rows)
    duplicate_segments = {segment_id for segment_id, count in by_segment.items() if count > 1}
    for row in rows:
        base = {
            "final_overlay_item_id": row["id"],
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "composition_source": row["composition_source"],
        }
        if int(row.get("composition_allowed") or 0) != 1:
            issues.append({**base, "issue_code": "composition_not_allowed", "issue_detail": row.get("block_reason") or ""})
        if int(row["segment_id"]) in duplicate_segments:
            issues.append({**base, "issue_code": "duplicate_segment", "issue_detail": str(row["segment_id"])})
        ok, detail = source_ref_valid(conn, row)
        if not ok:
            code = "unexpected_source" if detail.startswith("unexpected_composition_source") else "invalid_source_reference"
            if detail.startswith("missing_"):
                code = "missing_source_reference"
            issues.append({**base, "issue_code": code, "issue_detail": detail})
    return issues


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    audit_run_id: int,
    overlay_run: dict[str, Any],
    rows: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    source_counts: Counter[str],
    issue_counts: Counter[str],
    maturity_status: str,
    bridge_candidate: int,
) -> None:
    fields = [
        "issue_code",
        "issue_detail",
        "final_overlay_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "composition_source",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for issue in issues:
            writer.writerow(issue)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for issue in issues:
            handle.write(json.dumps(issue, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue Select_CString final composition maturity audit",
        f"Rule version: {RULE_VERSION}",
        f"Audit run id: {audit_run_id}",
        f"Source final overlay run id: {overlay_run['id']}",
        f"Audit status: {AUDIT_STATUS}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Composed released shadow: {int(overlay_run['composed_released_count'] or 0):,}",
        f"- Blocked: {int(overlay_run['blocked_count'] or 0):,}",
        f"- Issues: {len(issues):,}",
        f"- Maturity status: {maturity_status}",
        f"- Bridge candidate: {bridge_candidate}",
        "",
        "Composition sources:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Issue counts:",
        *([f"- {key}: {value:,}" for key, value in issue_counts.most_common()] or ["- none"]),
        "",
        "Safety:",
        "- Audit-only; no source/output files read and no output is written.",
        "- Bridge candidate means mechanically mature for a later governed bridge, not production-approved by this script.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Audit final Select_CString composition maturity.")
    parser.add_argument("--source-final-overlay-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        overlay_run_id = args.source_final_overlay_run_id or latest_final_overlay_run_id(conn)
        overlay_run = fetch_overlay_run(conn, overlay_run_id)
        rows = fetch_overlay_items(conn, overlay_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings, overlay_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_final_composition_maturity_audit_runs (
                rule_version,
                audit_name,
                audit_status,
                source_final_overlay_run_id,
                production_release_allowed,
                started_at,
                updated_at,
                maturity_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                AUDIT_NAME,
                AUDIT_STATUS,
                overlay_run_id,
                PRODUCTION_RELEASE_ALLOWED,
                now,
                now,
                "running",
            ),
        )
        audit_run_id = int(cursor.lastrowid)

        issues = build_issues(conn, rows)
        source_counts: Counter[str] = Counter(row["composition_source"] for row in rows)
        issue_counts: Counter[str] = Counter(issue["issue_code"] for issue in issues)
        duplicate_segment_count = issue_counts.get("duplicate_segment", 0)
        missing_source_ref_count = issue_counts.get("missing_source_reference", 0)
        invalid_source_ref_count = issue_counts.get("invalid_source_reference", 0)
        unexpected_source_count = issue_counts.get("unexpected_source", 0)
        bridge_candidate = int(
            len(rows) > 0
            and int(overlay_run["candidate_count"] or 0) == len(rows)
            and int(overlay_run["blocked_count"] or 0) == 0
            and int(overlay_run["composed_released_count"] or 0) == len(rows)
            and not issues
        )
        maturity_status = "bridge_candidate_shadow" if bridge_candidate else "needs_more_governance"

        for issue in issues:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_final_composition_maturity_audit_items (
                    run_id,
                    source_final_overlay_run_id,
                    final_overlay_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    composition_source,
                    issue_code,
                    issue_detail,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_run_id,
                    overlay_run_id,
                    issue.get("final_overlay_item_id"),
                    issue.get("segment_id"),
                    issue.get("relative_path"),
                    issue.get("source_key"),
                    issue.get("composition_source"),
                    issue["issue_code"],
                    issue.get("issue_detail"),
                    now,
                ),
            )

        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            audit_run_id=audit_run_id,
            overlay_run=overlay_run,
            rows=rows,
            issues=issues,
            source_counts=source_counts,
            issue_counts=issue_counts,
            maturity_status=maturity_status,
            bridge_candidate=bridge_candidate,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_final_composition_maturity_audit_runs
            SET candidate_count = ?,
                composed_released_count = ?,
                blocked_count = ?,
                duplicate_segment_count = ?,
                missing_source_ref_count = ?,
                invalid_source_ref_count = ?,
                unexpected_source_count = ?,
                maturity_status = ?,
                bridge_candidate = ?,
                source_counts_json = ?,
                issue_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                int(overlay_run["composed_released_count"] or 0),
                int(overlay_run["blocked_count"] or 0),
                duplicate_segment_count,
                missing_source_ref_count,
                invalid_source_ref_count,
                unexpected_source_count,
                maturity_status,
                bridge_candidate,
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(issue_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                audit_run_id,
            ),
        )
        conn.commit()

    payload = {
        "audit_run_id": audit_run_id,
        "source_final_overlay_run_id": overlay_run_id,
        "candidate_count": len(rows),
        "composed_released_count": int(overlay_run["composed_released_count"] or 0),
        "blocked_count": int(overlay_run["blocked_count"] or 0),
        "issue_count": len(issues),
        "maturity_status": maturity_status,
        "bridge_candidate": bridge_candidate,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    main()
