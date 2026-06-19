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
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens
from issue_dynamic_token_literal_repair_checkpoint import dynamic_payload_changes_only
from issue_select_cstring_same_token_lifecycle_policy import SOURCE_SPECS


RULE_VERSION = "issue_select_cstring_governed_bridge_proposal_v1"
BRIDGE_NAME = "select_cstring_final_composition_governed_bridge_v1"
BRIDGE_STATUS = "proposal_shadow"
PRODUCTION_RELEASE_ALLOWED = 0

BLOCKING_VALIDATION_CODES = {
    "spanish_punctuation",
    "mojibake_or_unexpected_script",
    "utf8_mojibake_sequence",
    "replacement_question_mark_mojibake",
    "spanish_residue",
    "spanish_residue_in_literal",
    "token_breakage",
    "placeholder_breakage",
    "gender_token_extra_suffix",
}

BASELINE_SOURCE_BY_FAMILY = {spec["family"]: spec for spec in SOURCE_SPECS}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_maturity_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_final_composition_maturity_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed final Select_CString maturity audit found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_governed_bridge_proposal_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            source_maturity_audit_run_id INTEGER NOT NULL,
            source_final_overlay_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            review_required_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            same_token_count INTEGER NOT NULL DEFAULT 0,
            dynamic_payload_delta_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            bridge_candidate INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            source_counts_json TEXT,
            token_status_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_governed_bridge_proposal_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_maturity_audit_run_id INTEGER NOT NULL,
            source_final_overlay_run_id INTEGER NOT NULL,
            final_overlay_item_id INTEGER NOT NULL,
            source_lifecycle_item_id INTEGER NOT NULL,
            source_checkpoint_table TEXT,
            source_checkpoint_run_id INTEGER,
            source_checkpoint_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            composition_source TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            bridge_action TEXT NOT NULL,
            token_status TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            validation_issue_count INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT,
            corrected_text_hash TEXT,
            corrected_text TEXT,
            blocking_reason TEXT,
            guardrails_json TEXT,
            reasons_json TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_governed_bridge_proposal_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], audit_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_governed_bridge_proposal_audit_run_{audit_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in BLOCKING_VALIDATION_CODES
    ]


def fetch_audit_run(conn, audit_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_select_cstring_final_composition_maturity_audit_runs WHERE id = ?",
        (audit_run_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"Final composition maturity audit run not found: {audit_run_id}")
    return dict(row)


def fetch_final_rows(conn, final_overlay_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_final_composition_overlay_items
        WHERE run_id = ?
        ORDER BY segment_id, id
        """,
        (final_overlay_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_source_text_for_row(conn, row: dict[str, Any]) -> dict[str, Any]:
    source = row["composition_source"]
    if source == "baseline_lifecycle":
        life = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_same_token_lifecycle_items
            WHERE id = ?
            """,
            (row["source_lifecycle_item_id"],),
        ).fetchone()
        if not life:
            return {"error": "missing_lifecycle_item"}
        life_payload = dict(life)
        spec = BASELINE_SOURCE_BY_FAMILY.get(life_payload["source_family"])
        if spec is None:
            return {"error": f"unknown_lifecycle_source_family:{life_payload['source_family']}"}
        item = conn.execute(
            f"""
            SELECT *
            FROM {spec['item_table']}
            WHERE id = ?
              AND {spec['run_ref_column']} = ?
            """,
            (life_payload["source_checkpoint_item_id"], life_payload["source_checkpoint_run_id"]),
        ).fetchone()
        if not item:
            return {"error": "missing_baseline_source_checkpoint_item"}
        payload = dict(item)
        return {
            "source_checkpoint_table": spec["item_table"],
            "source_checkpoint_run_id": life_payload["source_checkpoint_run_id"],
            "source_checkpoint_item_id": life_payload["source_checkpoint_item_id"],
            "current_text": payload.get("current_text"),
            "corrected_text": payload.get("corrected_text"),
            "source_token_status": payload.get("token_status"),
            "source_allowed": int(payload.get("checkpoint_allowed") or 0),
            "source_block_reason": payload.get("block_reason") or "",
            "source_reasons_json": payload.get("reasons_json"),
        }

    source_map = {
        "residual_literal_cleanup": (
            "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items",
            "source_cleanup_item_id",
            "source_cleanup_checkpoint_run_id",
        ),
        "dynamic_literal_payload_delta": (
            "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items",
            "source_dynamic_payload_item_id",
            "source_dynamic_payload_checkpoint_run_id",
        ),
        "local_player_pronoun_literal": (
            "ml_issue_select_cstring_local_player_pronoun_checkpoint_items",
            "source_local_pronoun_item_id",
            "source_local_pronoun_checkpoint_run_id",
        ),
    }
    spec = source_map.get(source)
    if spec is None:
        return {"error": f"unknown_composition_source:{source}"}
    table_name, item_column, run_column = spec
    item_id = row.get(item_column)
    run_id = row.get(run_column)
    if item_id is None or run_id is None:
        return {"error": f"missing_source_reference:{source}"}
    item = conn.execute(
        f"""
        SELECT *
        FROM {table_name}
        WHERE id = ?
          AND run_id = ?
        """,
        (item_id, run_id),
    ).fetchone()
    if not item:
        return {"error": f"missing_source_checkpoint_item:{source}"}
    payload = dict(item)
    return {
        "source_checkpoint_table": table_name,
        "source_checkpoint_run_id": run_id,
        "source_checkpoint_item_id": item_id,
        "current_text": payload.get("current_text"),
        "corrected_text": payload.get("corrected_text"),
        "source_token_status": payload.get("token_status"),
        "source_allowed": int(payload.get("checkpoint_allowed") or 0),
        "source_block_reason": payload.get("block_reason") or "",
        "source_reasons_json": payload.get("reasons_json"),
    }


def classify_token_status(current: str | None, corrected: str | None) -> str:
    current = current or ""
    corrected = corrected or ""
    if structural_tokens(current) == structural_tokens(corrected):
        return "same_structural_tokens"
    if dynamic_payload_changes_only(current, corrected):
        return "dynamic_literal_payload_only"
    return "structural_token_change_review_required"


def enrich_row(conn, row: dict[str, Any], *, audit_run: dict[str, Any]) -> dict[str, Any]:
    source = fetch_source_text_for_row(conn, row)
    current_text = source.get("current_text")
    corrected_text = source.get("corrected_text")
    validation_issues = blocking_validation_issues(corrected_text)
    token_status = classify_token_status(current_text, corrected_text)
    guardrails = [
        "learning_status.production_safe must be true before production run",
        "final composition maturity audit must be bridge_candidate_shadow=1",
        "source checkpoint item must remain allowed and unblocked",
        "local validation must have zero blocking issues",
        "current_text_hash and corrected_text_hash must match proposal at production bridge time",
        "no source/output write is allowed by this proposal",
    ]
    reasons: list[str] = []
    if source.get("source_reasons_json"):
        try:
            reasons.extend(json.loads(source["source_reasons_json"]))
        except json.JSONDecodeError:
            reasons.append(str(source["source_reasons_json"]))
    blocking_reason = ""
    bridge_status = "ready_for_governed_bridge_review"
    bridge_action = "propose_confirmed_text_promotion_after_production_bridge_checks"
    risk_level = "medium"

    if int(audit_run.get("bridge_candidate") or 0) != 1:
        bridge_status = "blocked_maturity_audit_not_bridge_candidate"
        bridge_action = "hold_for_maturity_audit"
        risk_level = "critical"
        blocking_reason = "maturity_audit_not_bridge_candidate"
    elif int(row.get("composition_allowed") or 0) != 1:
        bridge_status = "blocked_final_composition_item_not_allowed"
        bridge_action = "hold_for_composition_review"
        risk_level = "critical"
        blocking_reason = row.get("block_reason") or "composition_not_allowed"
    elif source.get("error"):
        bridge_status = "blocked_source_reference_error"
        bridge_action = "hold_for_source_reference_repair"
        risk_level = "critical"
        blocking_reason = source["error"]
    elif int(source.get("source_allowed") or 0) != 1:
        bridge_status = "blocked_source_checkpoint_not_allowed"
        bridge_action = "hold_for_source_checkpoint_review"
        risk_level = "critical"
        blocking_reason = "source_checkpoint_not_allowed"
    elif source.get("source_block_reason"):
        bridge_status = "blocked_source_checkpoint_has_block_reason"
        bridge_action = "hold_for_source_checkpoint_review"
        risk_level = "critical"
        blocking_reason = source["source_block_reason"]
    elif not (corrected_text or "").strip():
        bridge_status = "blocked_missing_corrected_text"
        bridge_action = "hold_for_correction_text"
        risk_level = "critical"
        blocking_reason = "missing_corrected_text"
    elif validation_issues:
        bridge_status = "blocked_validation_issue"
        bridge_action = "hold_for_quality_review"
        risk_level = "critical"
        blocking_reason = "blocking_validation_issue"
    elif token_status == "structural_token_change_review_required":
        bridge_status = "blocked_unexpected_token_delta"
        bridge_action = "hold_for_token_delta_review"
        risk_level = "critical"
        blocking_reason = "unexpected_token_delta"
    elif token_status == "dynamic_literal_payload_only":
        risk_level = "high"
    else:
        risk_level = "low"

    return {
        **row,
        "source_checkpoint_table": source.get("source_checkpoint_table"),
        "source_checkpoint_run_id": source.get("source_checkpoint_run_id"),
        "source_checkpoint_item_id": source.get("source_checkpoint_item_id"),
        "current_text": current_text,
        "corrected_text": corrected_text,
        "token_status": token_status,
        "bridge_status": bridge_status,
        "bridge_action": bridge_action,
        "risk_level": risk_level,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "current_text_hash": sha256_text(current_text),
        "corrected_text_hash": sha256_text(corrected_text),
        "blocking_reason": blocking_reason,
        "guardrails": guardrails,
        "reasons": reasons,
    }


def insert_run(
    conn,
    *,
    audit_run: dict[str, Any],
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    statuses = Counter(row["bridge_status"] for row in rows)
    sources = Counter(row["composition_source"] for row in rows)
    token_statuses = Counter(row["token_status"] for row in rows)
    ready_count = statuses["ready_for_governed_bridge_review"]
    blocked_count = sum(count for status, count in statuses.items() if status.startswith("blocked_"))
    review_required_count = len(rows) - ready_count - blocked_count
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO ml_issue_select_cstring_governed_bridge_proposal_runs (
            rule_version,
            bridge_name,
            bridge_status,
            source_maturity_audit_run_id,
            source_final_overlay_run_id,
            total_candidates,
            ready_count,
            review_required_count,
            blocked_count,
            same_token_count,
            dynamic_payload_delta_count,
            production_release_allowed,
            bridge_candidate,
            status_counts_json,
            source_counts_json,
            token_status_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            BRIDGE_NAME,
            "ready_for_governed_review" if ready_count and not blocked_count else "blocked_or_empty",
            audit_run["id"],
            audit_run["source_final_overlay_run_id"],
            len(rows),
            ready_count,
            review_required_count,
            blocked_count,
            token_statuses["same_structural_tokens"],
            token_statuses["dynamic_literal_payload_only"],
            PRODUCTION_RELEASE_ALLOWED,
            int(audit_run.get("bridge_candidate") or 0),
            json.dumps(dict(statuses), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(sources), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(token_statuses), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, *, run_id: int, audit_run: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_governed_bridge_proposal_items (
                run_id,
                source_maturity_audit_run_id,
                source_final_overlay_run_id,
                final_overlay_item_id,
                source_lifecycle_item_id,
                source_checkpoint_table,
                source_checkpoint_run_id,
                source_checkpoint_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                composition_source,
                bridge_status,
                bridge_action,
                token_status,
                risk_level,
                validation_issue_count,
                current_text_hash,
                corrected_text_hash,
                corrected_text,
                blocking_reason,
                guardrails_json,
                reasons_json,
                production_release_allowed,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                audit_run["id"],
                audit_run["source_final_overlay_run_id"],
                row["id"],
                row["source_lifecycle_item_id"],
                row.get("source_checkpoint_table"),
                row.get("source_checkpoint_run_id"),
                row.get("source_checkpoint_item_id"),
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["composition_source"],
                row["bridge_status"],
                row["bridge_action"],
                row["token_status"],
                row["risk_level"],
                row["validation_issue_count"],
                row.get("current_text_hash"),
                row.get("corrected_text_hash"),
                row.get("corrected_text"),
                row.get("blocking_reason"),
                json.dumps(row["guardrails"], ensure_ascii=False, sort_keys=True),
                json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                PRODUCTION_RELEASE_ALLOWED,
                now,
            ),
        )
        row["bridge_item_id"] = int(cursor.lastrowid)


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    run_id: int,
    audit_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "bridge_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "composition_source",
        "source_checkpoint_table",
        "source_checkpoint_run_id",
        "source_checkpoint_item_id",
        "bridge_status",
        "bridge_action",
        "token_status",
        "risk_level",
        "validation_issue_count",
        "current_text_hash",
        "corrected_text_hash",
        "corrected_text",
        "blocking_reason",
        "guardrails",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "guardrails": json.dumps(row["guardrails"], ensure_ascii=False),
                    "reasons": json.dumps(row["reasons"], ensure_ascii=False),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["guardrails"] = row["guardrails"]
            payload["reasons"] = row["reasons"]
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "bridge_item_id": row["bridge_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "composition_source": row["composition_source"],
                "suggested_decision": "accept_governed_select_cstring_bridge"
                if row["bridge_status"] == "ready_for_governed_bridge_review"
                else "hold_governed_select_cstring_bridge",
                "allowed_decisions": [
                    "accept_governed_select_cstring_bridge",
                    "hold_governed_select_cstring_bridge",
                    "reject_governed_select_cstring_bridge",
                ],
                "corrected_text": row.get("corrected_text"),
                "current_text_hash": row.get("current_text_hash"),
                "corrected_text_hash": row.get("corrected_text_hash"),
                "required_guardrails": row["guardrails"],
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    statuses = Counter(row["bridge_status"] for row in rows)
    sources = Counter(row["composition_source"] for row in rows)
    token_statuses = Counter(row["token_status"] for row in rows)
    lines = [
        "Issue Select_CString governed bridge proposal",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Bridge run id: {run_id}",
        f"Bridge status: {BRIDGE_STATUS}",
        f"Source maturity audit run id: {audit_run['id']}",
        f"Source final overlay run id: {audit_run['source_final_overlay_run_id']}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready for governed bridge review: {statuses['ready_for_governed_bridge_review']:,}",
        f"- Blocked: {sum(count for status, count in statuses.items() if status.startswith('blocked_')):,}",
        f"- Same-token candidates: {token_statuses['same_structural_tokens']:,}",
        f"- Dynamic literal payload candidates: {token_statuses['dynamic_literal_payload_only']:,}",
        "",
        "Composition sources:",
        *[f"- {key}: {value:,}" for key, value in sources.most_common()],
        "",
        "Bridge statuses:",
        *[f"- {key}: {value:,}" for key, value in statuses.most_common()],
        "",
        "Token statuses:",
        *[f"- {key}: {value:,}" for key, value in token_statuses.most_common()],
        "",
        "Required next step:",
        "- This proposal is not a production writer. Production integration must be implemented by a separate guarded bridge that rechecks all hashes, maturity status, validation, learning gate, and source references at runtime.",
        "",
        "Samples:",
    ]
    for row in rows[:20]:
        lines.extend(
            [
                (
                    f"- {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                    f"{row['composition_source']} status={row['bridge_status']} risk={row['risk_level']}"
                ),
                f"  corrected: {short(row.get('corrected_text'), 260)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Create a shadow governed bridge proposal for final Select_CString composition.")
    parser.add_argument("--source-maturity-audit-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        audit_run_id = args.source_maturity_audit_run_id or latest_maturity_audit_run_id(conn)
        audit_run = fetch_audit_run(conn, audit_run_id)
        raw_rows = fetch_final_rows(conn, int(audit_run["source_final_overlay_run_id"]))
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings, audit_run_id)
        started_at = datetime.now()
        rows = [enrich_row(conn, row, audit_run=audit_run) for row in raw_rows]
        run_id = insert_run(
            conn,
            audit_run=audit_run,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            started_at=started_at,
        )
        insert_items(conn, run_id=run_id, audit_run=audit_run, rows=rows)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            run_id=run_id,
            audit_run=audit_run,
            rows=rows,
        )
        conn.commit()

    statuses = Counter(row["bridge_status"] for row in rows)
    token_statuses = Counter(row["token_status"] for row in rows)
    payload = {
        "run_id": run_id,
        "source_maturity_audit_run_id": audit_run_id,
        "source_final_overlay_run_id": int(audit_run["source_final_overlay_run_id"]),
        "candidate_count": len(rows),
        "ready_count": statuses["ready_for_governed_bridge_review"],
        "blocked_count": sum(count for status, count in statuses.items() if status.startswith("blocked_")),
        "same_token_count": token_statuses["same_structural_tokens"],
        "dynamic_payload_delta_count": token_statuses["dynamic_literal_payload_only"],
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    main()
