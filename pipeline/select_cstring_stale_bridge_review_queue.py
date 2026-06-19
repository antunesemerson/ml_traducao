from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import structural_tokens
from apply_safe_output_updates import escape_localization_value


RULE_VERSION = "select_cstring_stale_bridge_review_queue_v1"
AGENT_KEY = "select_cstring_stale_bridge_microagent"
ISSUE_FAMILY = "select_cstring_stale_governed_bridge"
QUEUE_STRATEGY = "stale_select_cstring_bridge_pending"
BRIDGE_CLOSED_STATE = "closed_auto_confirmed_select_cstring_governed_bridge"


def canonical(value: Any) -> str:
    return escape_localization_value("" if value is None else str(value))


def short(value: Any, limit: int = 240) -> str:
    text = ("" if value is None else str(value)).replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def latest_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run found.")
    return int(row["id"])


def latest_bridge_proposal_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_governed_bridge_proposal_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed Select_CString bridge proposal run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = reports_dir / f"{stamp}_select_cstring_stale_bridge_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
        base.with_name(base.name + "_codex_decisions").with_suffix(".jsonl"),
    )


def classify_issue_kind(row: dict[str, Any]) -> str:
    text = f"{row.get('confirmed_text') or ''} {row.get('corrected_text') or ''}".casefold()
    if "nicknames_l_spanish.yml" in str(row.get("relative_path") or ""):
        if any(marker in text for marker in ("'puedes'", "'portas'", "'consideras'", "'te tomas'", "'eres'")):
            return "nickname_select_cstring_second_person_verb_residual"
        if any(marker in text for marker in ("'tú'", "'ti'", "'te'")):
            return "nickname_select_cstring_second_person_pronoun_residual"
        return "nickname_select_cstring_context_rewrite"
    if "interactions_l_spanish.yml" in str(row.get("relative_path") or ""):
        return "interaction_select_cstring_second_person_rewrite"
    return "select_cstring_stale_bridge_rewrite"


def classify_bucket(row: dict[str, Any]) -> str:
    issue_kind = classify_issue_kind(row)
    if issue_kind == "nickname_select_cstring_second_person_verb_residual":
        return "nickname_second_person_verb_residual"
    if issue_kind == "nickname_select_cstring_second_person_pronoun_residual":
        return "nickname_second_person_pronoun_residual"
    if issue_kind == "interaction_select_cstring_second_person_rewrite":
        return "interaction_second_person_rewrite"
    return "select_cstring_context_rewrite"


def contains_spanish_second_person(value: str | None) -> bool:
    text = (value or "").casefold()
    markers = (
        "'tú'",
        "'tu'",
        "'ti'",
        "'te'",
        "'le'",
        "'puedes'",
        "'portas'",
        "'consideras'",
        "'te tomas'",
        "'se toma'",
        "'eres'",
        " demasiado",
    )
    return any(marker in text for marker in markers)


def validate_candidate(row: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    current = row.get("confirmed_text") or ""
    corrected = row.get("corrected_text") or ""
    reasons: list[str] = []

    current_tokens = structural_tokens(current)
    corrected_tokens = structural_tokens(corrected)
    token_status = "same_structural_tokens" if current_tokens == corrected_tokens else "token_mismatch"
    if token_status != "same_structural_tokens":
        reasons.append("structural_tokens_changed")

    current_has_residue = contains_spanish_second_person(current)
    corrected_has_residue = contains_spanish_second_person(corrected)
    if current_has_residue:
        reasons.append("current_select_cstring_second_person_residue")
    if corrected_has_residue:
        reasons.append("corrected_still_has_second_person_residue")

    validation = local_quality_validator.validate_text(corrected)
    blocking = [
        issue
        for issue in (validation.get("issues") or [])
        if issue.get("severity") == "high"
        or issue.get("code")
        in {
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
    ]
    if blocking:
        reasons.append("corrected_text_has_blocking_validation_issue")

    return token_status, reasons, blocking


def fetch_rows(conn, *, segment_state_run_id: int, bridge_proposal_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS bridge_item_id,
            item.run_id AS bridge_proposal_run_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.composition_source,
            item.bridge_status,
            item.bridge_action,
            item.token_status AS bridge_token_status,
            item.risk_level,
            item.corrected_text,
            source.english_text,
            source.spanish_text,
            source.old_text,
            confirmation.confirmed_text,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked,
            output.portuguese_text AS output_text,
            state.id AS state_item_id,
            state.final_state,
            state.state_group,
            state.confirmed_matches_output,
            state.lifecycle_policy_allowed
        FROM ml_issue_select_cstring_governed_bridge_proposal_items item
        JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        JOIN source_segments source
          ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND state.final_state != ?
          AND state.state_group = 'pending'
        ORDER BY item.id
        """,
        (segment_state_run_id, bridge_proposal_run_id, BRIDGE_CLOSED_STATE),
    ).fetchall()
    return [dict(row) for row in rows]


def make_ledger_item(row: dict[str, Any], run_id: int, created_at: str) -> dict[str, Any]:
    token_status, reasons, blocking = validate_candidate(row)
    issue_kind = classify_issue_kind(row)
    bucket = classify_bucket(row)
    evidence = {
        "rule_version": RULE_VERSION,
        "bridge_item_id": row["bridge_item_id"],
        "bridge_proposal_run_id": row["bridge_proposal_run_id"],
        "composition_source": row.get("composition_source"),
        "bridge_status": row.get("bridge_status"),
        "bridge_action": row.get("bridge_action"),
        "bridge_token_status": row.get("bridge_token_status"),
        "risk_level": row.get("risk_level"),
        "queue_bucket": bucket,
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "confirmed_matches_corrected": int(canonical(row.get("confirmed_text")) == canonical(row.get("corrected_text"))),
        "output_matches_corrected": int(canonical(row.get("output_text")) == canonical(row.get("corrected_text"))),
        "current_has_second_person_residue": contains_spanish_second_person(row.get("confirmed_text")),
        "corrected_has_second_person_residue": contains_spanish_second_person(row.get("corrected_text")),
        "token_status": token_status,
        "validation_reasons": reasons,
        "blocking_validation_count": len(blocking),
        "blocking_validation_codes": [issue.get("code") for issue in blocking],
    }
    return {
        "run_id": run_id,
        "state_item_id": row.get("state_item_id"),
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "final_state": row.get("final_state"),
        "state_group": row.get("state_group"),
        "active_action": None,
        "candidate_action": "needs_autofix",
        "policy_action": "needs_autofix",
        "confirmation_level": None,
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "issue_family": ISSUE_FAMILY,
        "issue_kind": issue_kind,
        "issue_role": "repair",
        "issue_severity": "high" if contains_spanish_second_person(row.get("confirmed_text")) else "medium",
        "agent_key": AGENT_KEY,
        "route_status": "candidate",
        "proposed_action": "review_stale_bridge_corrected_text",
        "proposed_repair_text": row.get("corrected_text"),
        "token_impact": "same_structural_tokens" if token_status == "same_structural_tokens" else "token_sensitive",
        "token_status": token_status,
        "confidence_score": 0.91 if token_status == "same_structural_tokens" and not blocking else 0.62,
        "evidence_text": short(row.get("confirmed_text"), 360),
        "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        "validation_status": "not_validated",
        "status": "open",
        "created_at": created_at,
    }


def insert_ledger(conn, *, segment_state_run_id: int, rows: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    started_at = db.utc_now()
    primary_counts = Counter(classify_bucket(row) for row in rows)
    cur = conn.execute(
        """
        INSERT INTO ml_issue_ledger_runs (
            rule_version,
            segment_state_run_id,
            active_score_run_id,
            candidate_score_run_id,
            policy_run_id,
            source_scope,
            pending_segments_count,
            ledger_segment_count,
            ledger_item_count,
            actionable_item_count,
            blocked_item_count,
            primary_family_counts_json,
            notes_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, NULL, NULL, NULL, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            segment_state_run_id,
            "select_cstring_stale_bridge_pending",
            len(rows),
            json.dumps(dict(primary_counts.most_common()), ensure_ascii=False, sort_keys=True),
            json.dumps({"bridge_pending_rows": len(rows)}, ensure_ascii=False, sort_keys=True),
            started_at,
            started_at,
        ),
    )
    ledger_run_id = int(cur.lastrowid)
    items = [make_ledger_item(row, ledger_run_id, started_at) for row in rows]
    if items:
        columns = list(items[0].keys())
        conn.executemany(
            f"""
            INSERT INTO ml_issue_ledger_items ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            """,
            [tuple(item[column] for column in columns) for item in items],
        )
    inserted = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM ml_issue_ledger_items
            WHERE run_id = ?
            ORDER BY id
            """,
            (ledger_run_id,),
        ).fetchall()
    ]
    finished_at = db.utc_now()
    conn.execute(
        """
        UPDATE ml_issue_ledger_runs
        SET ledger_segment_count = ?,
            ledger_item_count = ?,
            actionable_item_count = ?,
            family_counts_json = ?,
            agent_counts_json = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len({item["segment_id"] for item in inserted}),
            len(inserted),
            len(inserted),
            json.dumps({ISSUE_FAMILY: len(inserted)}, ensure_ascii=False, sort_keys=True),
            json.dumps({AGENT_KEY: len(inserted)}, ensure_ascii=False, sort_keys=True),
            finished_at,
            finished_at,
            ledger_run_id,
        ),
    )
    return ledger_run_id, inserted


def write_queue_artifacts(
    *,
    rows: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path, Path],
) -> None:
    report_path, csv_path, jsonl_path, decisions_template_path, codex_decisions_path = paths
    bucket_counts = Counter(item["queue_bucket"] for item in queue_items)
    lines = [
        "Select_CString stale bridge review queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue items: {len(queue_items)}",
        "",
        "Interpretation:",
        "- These rows were blocked from governed bridge closure because confirmed/output text differs from the proposed corrected text.",
        "- They are learning items, not production writes.",
        "- Recommended review path: accept corrected_text only when it removes Spanish second-person residue and keeps structural tokens stable.",
        "",
        "Buckets:",
    ]
    for bucket, count in bucket_counts.most_common():
        lines.append(f"- {bucket}: {count}")
    lines.extend(["", "Rows:"])
    by_segment = {int(row["segment_id"]): row for row in rows}
    for item in queue_items:
        source = by_segment[int(item["segment_id"])]
        evidence = json.loads(item["evidence_json"])
        item_token_status = evidence.get("token_status") or item.get("token_status") or "unknown"
        lines.extend(
            [
                f"- queue_item: {item['id']}",
                f"  segment_id: {item['segment_id']}",
                f"  source: {item['relative_path']}:{item['source_line_number']}::{item['source_key']}",
                f"  bucket: {item['queue_bucket']}",
                f"  issue_kind: {item['issue_kind']}",
                f"  token_status: {item_token_status}",
                f"  validation_reasons: {', '.join(evidence.get('validation_reasons') or []) or 'none'}",
                f"  english: {short(source.get('english_text'))}",
                f"  confirmed: {short(source.get('confirmed_text'))}",
                f"  corrected: {short(source.get('corrected_text'))}",
            ]
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "queue_item_id",
        "queue_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "queue_bucket",
        "issue_kind",
        "token_status",
        "suggested_decision",
        "english_text",
        "spanish_text",
        "confirmed_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in queue_items:
            source = by_segment[int(item["segment_id"])]
            evidence = json.loads(item["evidence_json"])
            item_token_status = evidence.get("token_status") or item.get("token_status") or "unknown"
            writer.writerow(
                {
                    "queue_item_id": item["id"],
                    "queue_run_id": item["run_id"],
                    "segment_id": item["segment_id"],
                    "relative_path": item["relative_path"],
                    "source_line_number": item["source_line_number"],
                    "source_key": item["source_key"],
                    "queue_bucket": item["queue_bucket"],
                    "issue_kind": item["issue_kind"],
                    "token_status": item_token_status,
                    "suggested_decision": item["suggested_decision"],
                    "english_text": source.get("english_text"),
                    "spanish_text": source.get("spanish_text"),
                    "confirmed_text": source.get("confirmed_text"),
                    "corrected_text": source.get("corrected_text"),
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as queue_handle, decisions_template_path.open(
        "w", encoding="utf-8"
    ) as template_handle, codex_decisions_path.open("w", encoding="utf-8") as decisions_handle:
        for item in queue_items:
            source = by_segment[int(item["segment_id"])]
            evidence = json.loads(item["evidence_json"])
            item_token_status = evidence.get("token_status") or item.get("token_status") or "unknown"
            queue_payload = {
                "queue_item_id": item["id"],
                "queue_run_id": item["run_id"],
                "ledger_item_id": item["ledger_item_id"],
                "ledger_run_id": item["ledger_run_id"],
                "segment_id": item["segment_id"],
                "relative_path": item["relative_path"],
                "source_line_number": item["source_line_number"],
                "source_key": item["source_key"],
                "agent_key": item["agent_key"],
                "issue_family": item["issue_family"],
                "issue_kind": item["issue_kind"],
                "queue_bucket": item["queue_bucket"],
                "token_status": item_token_status,
                "suggested_decision": item["suggested_decision"],
                "english_text": source.get("english_text"),
                "spanish_text": source.get("spanish_text"),
                "old_text": source.get("old_text"),
                "confirmed_text": source.get("confirmed_text"),
                "output_text": source.get("output_text"),
                "corrected_text": source.get("corrected_text"),
                "evidence_json": evidence,
            }
            queue_handle.write(json.dumps(queue_payload, ensure_ascii=False, sort_keys=True) + "\n")

            template_payload = {
                "queue_item_id": item["id"],
                "queue_run_id": item["run_id"],
                "segment_id": item["segment_id"],
                "decision": "pending",
                "corrected_text": source.get("corrected_text"),
                "notes": "",
            }
            template_handle.write(json.dumps(template_payload, ensure_ascii=False, sort_keys=True) + "\n")

            decision_payload = {
                "queue_item_id": item["id"],
                "queue_run_id": item["run_id"],
                "segment_id": item["segment_id"],
                "decision": "needs_repair",
                "corrected_text": source.get("corrected_text"),
                "notes": "stale_select_cstring_bridge: current confirmed/output keeps Spanish second-person residue; corrected_text preserves structural tokens and is the learning target.",
                "reviewer": "codex_learning_front",
            }
            decisions_handle.write(json.dumps(decision_payload, ensure_ascii=False, sort_keys=True) + "\n")


def insert_queue(
    conn,
    *,
    ledger_run_id: int,
    ledger_items: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    by_segment = {int(row["segment_id"]): row for row in source_rows}
    bucket_counts = Counter(
        (json.loads(item["evidence_json"]).get("queue_bucket") or "select_cstring_context_rewrite")
        for item in ledger_items
    )
    report_path, csv_path, jsonl_path, decisions_template_path, _ = paths
    cur = conn.execute(
        """
        INSERT INTO ml_issue_review_queue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            issue_family,
            queue_strategy,
            limit_count,
            per_bucket,
            selected_count,
            open_count,
            reviewed_count,
            bucket_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            ledger_run_id,
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            len(ledger_items),
            len(ledger_items),
            len(ledger_items),
            len(ledger_items),
            json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
            str(report_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    queue_run_id = int(cur.lastrowid)

    values = []
    for ledger_item in ledger_items:
        source = by_segment[int(ledger_item["segment_id"])]
        evidence = json.loads(ledger_item["evidence_json"])
        bucket = evidence.get("queue_bucket") or "select_cstring_context_rewrite"
        values.append(
            (
                queue_run_id,
                ledger_run_id,
                int(ledger_item["id"]),
                int(ledger_item["segment_id"]),
                ledger_item["relative_path"],
                ledger_item["source_key"],
                ledger_item["source_line_number"],
                ISSUE_FAMILY,
                ledger_item["issue_kind"],
                AGENT_KEY,
                bucket,
                1000.0 + int(ledger_item["segment_id"]) % 17 / 100,
                "open",
                "decide_stale_bridge_repair",
                ledger_item["evidence_text"],
                ledger_item["evidence_json"],
                source.get("english_text"),
                source.get("spanish_text"),
                source.get("confirmed_text"),
                None,
                None,
                ledger_item["proposed_repair_text"],
                now,
                None,
            )
        )
    conn.executemany(
        """
        INSERT INTO ml_issue_review_queue_items (
            run_id,
            ledger_run_id,
            ledger_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            issue_family,
            issue_kind,
            agent_key,
            queue_bucket,
            priority_score,
            review_status,
            suggested_decision,
            evidence_text,
            evidence_json,
            english_text,
            spanish_text,
            confirmed_text,
            reviewer_decision,
            reviewer_notes,
            corrected_text,
            created_at,
            reviewed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return queue_run_id


def hydrate_queue_items(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            item.id AS queue_item_id,
            item.run_id AS queue_run_id,
            item.ledger_item_id,
            item.ledger_run_id
        FROM ml_issue_review_queue_items item
        WHERE item.run_id = ?
        ORDER BY item.id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_rows_for_queue(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            source.english_text,
            source.spanish_text,
            source.old_text,
            item.confirmed_text,
            output.portuguese_text AS output_text,
            item.corrected_text
        FROM ml_issue_review_queue_items item
        JOIN source_segments source
          ON source.id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE item.run_id = ?
        ORDER BY item.id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def refresh_existing_queue_evidence(conn, queue_run_id: int) -> None:
    rows = fetch_rows_for_queue(conn, queue_run_id)
    by_segment = {int(row["segment_id"]): row for row in rows}
    queue_items = hydrate_queue_items(conn, queue_run_id)
    for item in queue_items:
        source = by_segment[int(item["segment_id"])]
        token_status, reasons, blocking = validate_candidate(source)
        evidence = json.loads(item["evidence_json"] or "{}")
        evidence.update(
            {
                "current_has_second_person_residue": contains_spanish_second_person(source.get("confirmed_text")),
                "corrected_has_second_person_residue": contains_spanish_second_person(source.get("corrected_text")),
                "token_status": token_status,
                "validation_reasons": reasons,
                "blocking_validation_count": len(blocking),
                "blocking_validation_codes": [issue.get("code") for issue in blocking],
            }
        )
        payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            UPDATE ml_issue_review_queue_items
            SET evidence_json = ?
            WHERE id = ?
            """,
            (payload, int(item["id"])),
        )
        if item.get("ledger_item_id") is not None:
            conn.execute(
                """
                UPDATE ml_issue_ledger_items
                SET evidence_json = ?,
                    token_status = ?
                WHERE id = ?
                """,
                (payload, token_status, int(item["ledger_item_id"])),
            )


def update_queue_artifact_paths(
    conn,
    *,
    queue_run_id: int,
    paths: tuple[Path, Path, Path, Path, Path],
) -> None:
    report_path, csv_path, jsonl_path, decisions_template_path, _ = paths
    now = db.utc_now()
    conn.execute(
        """
        UPDATE ml_issue_review_queue_runs
        SET report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            decisions_template_path = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (str(report_path), str(csv_path), str(jsonl_path), str(decisions_template_path), now, queue_run_id),
    )


def main(
    *,
    segment_state_run_id: int | None = None,
    bridge_proposal_run_id: int | None = None,
    queue_run_id: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run = segment_state_run_id
        selected_bridge_run = bridge_proposal_run_id
        ledger_run_id: int | None = None
        if queue_run_id is not None:
            queue_run = conn.execute(
                """
                SELECT *
                FROM ml_issue_review_queue_runs
                WHERE id = ?
                """,
                (queue_run_id,),
            ).fetchone()
            if not queue_run:
                raise RuntimeError(f"Queue run not found: {queue_run_id}")
            ledger_run_id = int(queue_run["ledger_run_id"])
            refresh_existing_queue_evidence(conn, queue_run_id)
            rows = fetch_rows_for_queue(conn, queue_run_id)
            queue_items = hydrate_queue_items(conn, queue_run_id)
            update_queue_artifact_paths(conn, queue_run_id=queue_run_id, paths=paths)
        else:
            selected_state_run = segment_state_run_id or latest_segment_state_run_id(conn)
            selected_bridge_run = bridge_proposal_run_id or latest_bridge_proposal_run_id(conn)
            rows = fetch_rows(
                conn,
                segment_state_run_id=selected_state_run,
                bridge_proposal_run_id=selected_bridge_run,
            )
            ledger_run_id, ledger_items = insert_ledger(conn, segment_state_run_id=selected_state_run, rows=rows)
            queue_run_id = insert_queue(
                conn,
                ledger_run_id=ledger_run_id,
                ledger_items=ledger_items,
                source_rows=rows,
                paths=paths,
            )
            queue_items = hydrate_queue_items(conn, queue_run_id)
        conn.commit()

    write_queue_artifacts(rows=rows, queue_items=queue_items, paths=paths)
    report_path, csv_path, jsonl_path, decisions_template_path, codex_decisions_path = paths
    result = {
        "rule_version": RULE_VERSION,
        "segment_state_run_id": selected_state_run,
        "bridge_proposal_run_id": selected_bridge_run,
        "ledger_run_id": ledger_run_id,
        "queue_run_id": queue_run_id,
        "selected_count": len(queue_items),
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
        "codex_decisions_path": str(codex_decisions_path),
    }
    print(f"[select_cstring_stale_bridge_review_queue] Segment-state run id: {selected_state_run}")
    print(f"[select_cstring_stale_bridge_review_queue] Bridge proposal run id: {selected_bridge_run}")
    print(f"[select_cstring_stale_bridge_review_queue] Ledger run id: {ledger_run_id}")
    print(f"[select_cstring_stale_bridge_review_queue] Queue run id: {queue_run_id}")
    print(f"[select_cstring_stale_bridge_review_queue] Selected: {len(queue_items)}")
    print(f"[select_cstring_stale_bridge_review_queue] Report: {report_path}")
    print(f"[select_cstring_stale_bridge_review_queue] JSONL: {jsonl_path}")
    print(f"[select_cstring_stale_bridge_review_queue] Decisions: {codex_decisions_path}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create learning queue for stale Select_CString governed bridge proposals.")
    parser.add_argument("--segment-state-run-id", type=int, default=None)
    parser.add_argument("--bridge-proposal-run-id", type=int, default=None)
    parser.add_argument("--queue-run-id", type=int, default=None, help="Rewrite artifacts for an existing queue run.")
    args = parser.parse_args()
    main(
        segment_state_run_id=args.segment_state_run_id,
        bridge_proposal_run_id=args.bridge_proposal_run_id,
        queue_run_id=args.queue_run_id,
    )
