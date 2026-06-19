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
from apply_segment_state_updates import short


RULE_VERSION = "issue_culture_semantic_pattern_shadow_v1"
POLICY_NAME = "culture_semantic_pattern_shadow"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_culture_semantic"
ISSUE_FAMILY = "culture_semantic_microagent"


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_culture_semantic_pattern_shadow"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE agent_key = ?
          AND issue_family = ?
          AND queue_strategy = 'partial_coverage_composition'
          AND finished_at IS NOT NULL
          AND selected_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (AGENT_KEY, ISSUE_FAMILY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed culture semantic composition queue found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_culture_semantic_pattern_shadow_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            blocker_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_culture_semantic_pattern_shadow_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            shadow_allowed INTEGER NOT NULL,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            queue_confirmed_text_hash TEXT,
            evidence_text_hash TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_culture_semantic_pattern_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_queue_run(conn, *, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_issue_review_queue_runs WHERE id = ?", (queue_run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"Issue review queue run not found: {queue_run_id}")
    return dict(row)


def fetch_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            q.*,
            l.issue_family AS ledger_issue_family,
            l.issue_kind AS ledger_issue_kind,
            c.confirmed_text AS current_confirmed_text,
            c.locked AS confirmation_locked
        FROM ml_issue_review_queue_items q
        JOIN ml_issue_ledger_items l ON l.id = q.ledger_item_id
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = q.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE q.run_id = ?
        ORDER BY q.relative_path, q.source_line_number, q.source_key, q.id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_pattern(row: dict[str, Any]) -> tuple[str, str]:
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    text = row.get("current_confirmed_text") or ""
    english = row.get("english_text") or ""
    bucket = row.get("queue_bucket") or ""

    if (
        bucket == "short_dynamic_expression"
        and relative_path == "culture/traditions/cultural_traditions_l_spanish.yml"
        and "Republican vassal" in english
        and "um[recipient.Custom('ES_XA')]" in text
        and "vassal[recipient.Custom('ES_OA')]" in text
        and "republicano[recipient.Custom('ES_OA')]" in text
    ):
        return (
            "culture_republican_vassal_role_gender_label",
            "would_cover_culture_republican_vassal_role_gender_label_shadow",
        )

    if bucket != "domain_culture":
        return "culture_semantic_unclassified", "hold_for_manual_culture_semantic_review"
    if len(text) > 280:
        return "culture_semantic_unclassified", "hold_for_manual_culture_semantic_review"
    if not (
        relative_path == "culture/traditions/cultural_traditions_l_spanish.yml"
        or relative_path == "culture/cultural_innovations_l_spanish.yml"
        or relative_path == "dlc/fp3/dlc_fp3_culture_l_spanish.yml"
    ):
        return "culture_semantic_unclassified", "hold_for_manual_culture_semantic_review"
    if not any(
        token in text
        for token in (
            "[culture|lE]",
            "[buildings|lE]",
            "[knights|lE]",
            "[prestige|lE]",
            "[scheme|lE]",
            "[casus_belli|lE]",
            "[traditions|lE]",
            "[decision|lE]",
            "[gold|lE]",
            "[interaction|lE]",
            "[holdings|lE]",
        )
    ):
        return "culture_semantic_unclassified", "hold_for_manual_culture_semantic_review"
    if not english:
        return "culture_semantic_unclassified", "hold_for_manual_culture_semantic_review"

    if "$building_type_" in text or "[buildings|lE]" in text:
        return "culture_building_effect_label", "would_cover_culture_building_effect_label_shadow"
    if "não possui" in text or source_key.endswith("_percentage_desc") or source_key.endswith("_desc"):
        return "culture_requirement_or_condition_label", "would_cover_culture_requirement_label_shadow"
    if "[GetScheme(" in text and "[success_chance|E]" in text:
        return "culture_scheme_success_effect_label", "would_cover_culture_scheme_success_label_shadow"
    if "Custo" in text or source_key.startswith("cb_discount_"):
        return "culture_cost_effect_label", "would_cover_culture_cost_effect_label_shadow"
    if source_key.startswith("culture_parameter_") or source_key.startswith("TRADITION_"):
        return "culture_parameter_effect_label", "would_cover_culture_parameter_effect_label_shadow"

    return "culture_semantic_unclassified", "hold_for_manual_culture_semantic_review"


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    subpolicy_name, shadow_action = classify_pattern(row)
    blockers = list(global_reasons)
    queue_text = row.get("confirmed_text") or ""
    current_text = row.get("current_confirmed_text") or ""
    evidence_text = row.get("evidence_text") or ""

    if row.get("ledger_issue_family") != ISSUE_FAMILY:
        blockers.append("ledger_family_mismatch")
    if row.get("issue_family") != ISSUE_FAMILY:
        blockers.append("queue_family_mismatch")
    if row.get("agent_key") != AGENT_KEY:
        blockers.append("queue_agent_mismatch")
    if int(row.get("confirmation_locked") or 0):
        blockers.append("locked_confirmation")
    if not current_text:
        blockers.append("missing_current_confirmation")
    if queue_text and current_text != queue_text:
        blockers.append("stale_confirmation_text_changed")
    if evidence_text and current_text != evidence_text:
        blockers.append("evidence_text_mismatch")
    if subpolicy_name == "culture_semantic_unclassified":
        blockers.append("unclassified_culture_semantic_pattern")

    shadow_allowed = 0 if blockers else 1
    shadow_status = "shadow_ready_pattern" if shadow_allowed else "shadow_blocked"
    return {
        **row,
        "subpolicy_name": subpolicy_name,
        "shadow_status": shadow_status,
        "shadow_action": shadow_action if shadow_allowed else "hold_for_manual_culture_semantic_review",
        "shadow_allowed": shadow_allowed,
        "block_reason": ",".join(blockers) if blockers else "",
        "current_confirmed_text_hash": stable_hash(current_text),
        "queue_confirmed_text_hash": stable_hash(queue_text),
        "evidence_text_hash": stable_hash(evidence_text),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "shadow_item_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "subpolicy_name",
        "shadow_status",
        "shadow_action",
        "shadow_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fields}
            payload["confirmed_preview"] = short(row.get("current_confirmed_text"))
            payload["english_preview"] = short(row.get("english_text"))
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["shadow_status"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["shadow_allowed"])
    blocker_counts = Counter(row["block_reason"] or "none" for row in rows)
    lines = [
        "Issue culture semantic pattern shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Run id: {run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready: {sum(1 for row in rows if row['shadow_allowed']):,}",
        f"- Blocked: {sum(1 for row in rows if not row['shadow_allowed']):,}",
        "",
        "Ready subpolicies:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Blockers:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Statuses:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Ready samples:",
    ]
    for row in [item for item in rows if item["shadow_allowed"]][:30]:
        lines.append(f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']}")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow only: no source/output writes, no confirmations, no production promotion.",
            "- This covers only recognized short culture effect/condition labels.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, queue_run_id=selected_queue_run_id)
        source_rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)
        global_reasons: list[str] = []
        if queue_run.get("agent_key") != AGENT_KEY:
            global_reasons.append("queue_run_agent_mismatch")
        if queue_run.get("issue_family") != ISSUE_FAMILY:
            global_reasons.append("queue_run_family_mismatch")
        if queue_run.get("queue_strategy") != "partial_coverage_composition":
            global_reasons.append("queue_run_not_partial_composition")
        if not source_rows:
            global_reasons.append("no_pattern_candidate_rows")
        rows = [evaluate_row(row, global_reasons=global_reasons) for row in source_rows]

        ready_count = sum(1 for row in rows if row["shadow_allowed"])
        blocked_count = len(rows) - ready_count
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["shadow_allowed"])
        action_counts = Counter(row["shadow_action"] for row in rows if row["shadow_allowed"])
        blocker_counts = Counter(row["block_reason"] or "none" for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_culture_semantic_pattern_shadow_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                queue_run_id,
                ledger_run_id,
                candidate_count,
                ready_count,
                blocked_count,
                subpolicy_counts_json,
                action_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                AGENT_KEY,
                selected_queue_run_id,
                queue_run["ledger_run_id"],
                len(rows),
                ready_count,
                blocked_count,
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_culture_semantic_pattern_shadow_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_bucket,
                    issue_family,
                    issue_kind,
                    subpolicy_name,
                    shadow_status,
                    shadow_action,
                    shadow_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    queue_confirmed_text_hash,
                    evidence_text_hash,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_queue_run_id,
                    row["id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["subpolicy_name"],
                    row["shadow_status"],
                    row["shadow_action"],
                    int(row["shadow_allowed"]),
                    row["block_reason"],
                    row["current_confirmed_text_hash"],
                    row["queue_confirmed_text_hash"],
                    row["evidence_text_hash"],
                    "",
                    now,
                ),
            )
            row["shadow_item_id"] = int(item_cursor.lastrowid)
        conn.commit()

    write_outputs(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, run_id=run_id, queue_run=queue_run, rows=rows)
    print("[issue_culture_semantic_pattern_shadow] Shadow generated")
    print(f"[issue_culture_semantic_pattern_shadow] Run id: {run_id}")
    print(f"[issue_culture_semantic_pattern_shadow] Queue run id: {selected_queue_run_id}")
    print(f"[issue_culture_semantic_pattern_shadow] Candidates: {len(rows):,}")
    print(f"[issue_culture_semantic_pattern_shadow] Ready: {ready_count:,}")
    print(f"[issue_culture_semantic_pattern_shadow] Blocked: {blocked_count:,}")
    print(f"[issue_culture_semantic_pattern_shadow] Report: {txt_path}")
    return {
        "run_id": run_id,
        "queue_run_id": selected_queue_run_id,
        "candidate_count": len(rows),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create shadow coverage for recognized culture semantic short labels.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
