from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "auto_confirmation_reopen_text_boundary_repair_shadow_policy_v1"
POLICY_NAME = "weak_auto_same_token_boundary_repair_shadow_v1"
MERIT_RANK_PTBR_OVERLAP_LABELS = {
    "lower-upper": "inferior-alto",
    "upper-upper": "superior-alto",
}
SAFE_NOOP_BOUNDARY_POLICIES = {
    "weak_auto_custom_loc_es_helper",
    "weak_auto_short_custom_loc_article_helper",
    "weak_auto_short_dynamic_spanish_verb",
    "weak_auto_visible_copula_token_form",
    "weak_auto_visible_possessive_connector_loss",
    "weak_auto_visible_runtime_spanish_verb",
    "weak_auto_visible_sentence_collapse",
}
SAFE_GLOSSARY_NOOP_SOURCE_KEYS = {
    "HYPERPYRON",
    "mongol_invasion.1002.desc.genghis",
}
SAFE_GLOSSARY_NOOP_PREFIXES = (
    "merit_rank_",
    "chinese_students.0020.desc.",
)


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_repair_shadow_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_same_token_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_repair_queue_runs
        WHERE finished_at IS NOT NULL
          AND queue_scope = 'same-token'
          AND selected_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete same-token boundary repair queue found.")
    return int(row["id"])


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def text_delta_kind(confirmed_text: str | None, corrected_text: str | None) -> str:
    confirmed = (confirmed_text or "").strip()
    corrected = (corrected_text or "").strip()
    if not corrected:
        return "missing_correction"
    if confirmed == corrected:
        return "no_text_delta"
    if local_quality_validator.normalize(confirmed) == local_quality_validator.normalize(corrected):
        return "normalized_only_delta"
    return "visible_text_delta"


def glossary_visible_label(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(r"Glossary\('([^']+)'", value)
    return match.group(1).strip() if match else ""


def safe_merit_rank_ptbr_overlap_noop(row: dict[str, Any]) -> bool:
    if row.get("boundary_policy") != "weak_auto_embedded_glossary_visible_label":
        return False
    if not str(row.get("source_key") or "").startswith("merit_rank_"):
        return False
    english_label = glossary_visible_label(row.get("english_text"))
    corrected_label = glossary_visible_label(row.get("corrected_text"))
    if not english_label or not corrected_label:
        return False
    return MERIT_RANK_PTBR_OVERLAP_LABELS.get(english_label) == corrected_label


def safe_glossary_visible_label_noop(row: dict[str, Any]) -> bool:
    if row.get("boundary_policy") != "weak_auto_embedded_glossary_visible_label":
        return False
    key = str(row.get("source_key") or "")
    return (
        safe_merit_rank_ptbr_overlap_noop(row)
        or key in SAFE_GLOSSARY_NOOP_SOURCE_KEYS
        or any(key.startswith(prefix) for prefix in SAFE_GLOSSARY_NOOP_PREFIXES)
    )


def safe_same_token_noop_observation(row: dict[str, Any]) -> bool:
    policy = str(row.get("boundary_policy") or "")
    return policy in SAFE_NOOP_BOUNDARY_POLICIES or safe_glossary_visible_label_noop(row)


def blocking_validation_issues(corrected_text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(corrected_text)
    issues = validation.get("issues") or []
    blocked_codes = {
        "spanish_punctuation",
        "mojibake_or_unexpected_script",
        "utf8_mojibake_sequence",
        "replacement_question_mark_mojibake",
        "spanish_residue",
        "spanish_residue_in_literal",
        "gender_token_extra_suffix",
    }
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in blocked_codes
    ]


def evaluate_row(row: dict[str, Any]) -> dict[str, Any]:
    confirmed = row.get("confirmed_text")
    corrected = row.get("corrected_text")
    delta = text_delta_kind(confirmed, corrected)
    validation_issues = blocking_validation_issues(corrected)
    same_tokens = structural_tokens(confirmed) == structural_tokens(corrected)

    if delta == "missing_correction":
        status = "shadow_blocked_missing_correction"
        action = "hold_boundary_only"
        block_reason = "missing_correction"
    elif delta == "no_text_delta":
        if not same_tokens:
            status = "shadow_blocked_token_mismatch"
            action = "route_to_token_policy_review"
            block_reason = "structural_token_mismatch"
        elif validation_issues:
            status = "shadow_blocked_quality_issue"
            action = "hold_for_quality_review"
            block_reason = ",".join(sorted({str(issue.get("code")) for issue in validation_issues}))
        elif safe_same_token_noop_observation(row):
            status = "shadow_ready"
            action = "would_observe_same_token_noop_shadow"
            block_reason = ""
        else:
            status = "shadow_blocked_no_text_delta"
            action = "hold_boundary_only"
            block_reason = "no_text_delta"
    elif not same_tokens:
        status = "shadow_blocked_token_mismatch"
        action = "route_to_token_policy_review"
        block_reason = "structural_token_mismatch"
    elif validation_issues:
        status = "shadow_blocked_quality_issue"
        action = "hold_for_quality_review"
        block_reason = ",".join(sorted({str(issue.get("code")) for issue in validation_issues}))
    else:
        status = "shadow_ready"
        action = "would_stage_same_token_repair_shadow"
        block_reason = ""

    return {
        **row,
        "shadow_status": status,
        "shadow_action": action,
        "block_reason": block_reason,
        "text_delta_kind": delta,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "same_structural_tokens_rechecked": same_tokens,
        "current_confirmed_text_hash": sha256_text(confirmed),
        "corrected_text_hash": sha256_text(corrected),
    }


def fetch_rows(conn, *, repair_queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            decision.corrected_text,
            decision.notes,
            confirmation.confirmed_text,
            source.english_text,
            source.spanish_text
        FROM auto_confirmation_reopen_text_boundary_repair_queue_items item
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = item.review_decision_id
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.repair_route = 'same_token_shadow_repair'
        ORDER BY item.queue_rank, item.id
        """,
        (repair_queue_run_id,),
    ).fetchall()
    enriched = []
    for row in rows:
        payload = dict(row)
        payload["boundary_reasons"] = parse_json_list(payload.get("reasons_json"))
        enriched.append(evaluate_row(payload))
    return enriched


def insert_run(
    conn,
    *,
    repair_queue_run_id: int,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    statuses = Counter(row["shadow_status"] for row in rows)
    deltas = Counter(row["text_delta_kind"] for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_boundary_repair_shadow_runs (
            rule_version,
            repair_queue_run_id,
            policy_name,
            policy_status,
            total_candidates,
            shadow_ready_count,
            blocked_count,
            no_text_delta_count,
            validation_issue_count,
            same_token_count,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            repair_queue_run_id,
            POLICY_NAME,
            "shadow",
            len(rows),
            statuses["shadow_ready"],
            len(rows) - statuses["shadow_ready"],
            deltas["no_text_delta"],
            sum(1 for row in rows if row["validation_issue_count"] > 0),
            sum(1 for row in rows if row["same_structural_tokens_rechecked"]),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, *, run_id: int, repair_queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_repair_shadow_items (
                run_id,
                repair_queue_run_id,
                repair_queue_item_id,
                boundary_policy_item_id,
                review_decision_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                boundary_agent_key,
                boundary_policy,
                repair_route,
                token_status,
                shadow_status,
                shadow_action,
                block_reason,
                text_delta_kind,
                validation_issue_count,
                validation_issues_json,
                current_confirmed_text_hash,
                corrected_text_hash,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                repair_queue_run_id,
                row["id"],
                row["boundary_policy_item_id"],
                row["review_decision_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["boundary_agent_key"],
                row["boundary_policy"],
                row["repair_route"],
                row["token_status"],
                row["shadow_status"],
                row["shadow_action"],
                row["block_reason"],
                row["text_delta_kind"],
                row["validation_issue_count"],
                json.dumps(row["validation_issues"], ensure_ascii=False, sort_keys=True),
                row["current_confirmed_text_hash"],
                row["corrected_text_hash"],
                json.dumps(row["boundary_reasons"], ensure_ascii=False, sort_keys=True),
                now,
            ),
        )


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    repair_queue_run_id: int,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "shadow_item_id",
        "repair_queue_item_id",
        "queue_rank",
        "boundary_policy_item_id",
        "review_decision_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "boundary_agent_key",
        "boundary_policy",
        "shadow_status",
        "shadow_action",
        "block_reason",
        "text_delta_kind",
        "validation_issue_count",
        "validation_issues",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "repair_queue_item_id": row["id"],
                    "validation_issues": json.dumps(row["validation_issues"], ensure_ascii=False),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "repair_queue_item_id": row["id"],
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "corrected_preview": short(row.get("corrected_text")),
                "boundary_reasons": row["boundary_reasons"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    statuses = Counter(row["shadow_status"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows)
    deltas = Counter(row["text_delta_kind"] for row in rows)
    issues = Counter()
    for row in rows:
        for issue in row["validation_issues"]:
            issues[str(issue.get("code") or "unknown_issue")] += 1

    lines = [
        "Auto-confirmation boundary repair shadow policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Shadow run id: {run_id}",
        f"Repair queue run id: {repair_queue_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {statuses['shadow_ready']:,}",
        f"- Blocked: {len(rows) - statuses['shadow_ready']:,}",
        f"- No text delta: {deltas['no_text_delta']:,}",
        f"- Validation issue rows: {sum(1 for row in rows if row['validation_issue_count'] > 0):,}",
        f"- By status: {json.dumps(dict(statuses), ensure_ascii=False, sort_keys=True)}",
        f"- By policy: {json.dumps(dict(policies), ensure_ascii=False, sort_keys=True)}",
        f"- By text delta: {json.dumps(dict(deltas), ensure_ascii=False, sort_keys=True)}",
        f"- Validation issues: {json.dumps(dict(issues), ensure_ascii=False, sort_keys=True)}",
        "",
        "Shadow-ready sample:",
    ]
    for row in [item for item in rows if item["shadow_status"] == "shadow_ready"][:30]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} | {row['boundary_policy']}",
                f"  confirmed={short(row.get('confirmed_text'))}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["shadow_status"] == "shadow_ready" for item in rows):
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    for row in [item for item in rows if item["shadow_status"] != "shadow_ready"][:30]:
        lines.extend(
            [
                f"- {row['shadow_status']} | {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  confirmed={short(row.get('confirmed_text'))}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if all(item["shadow_status"] == "shadow_ready" for item in rows):
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow validation only: no source/output file reads, no confirmation updates, no production release, and no output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, repair_queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_repair_queue_run_id = repair_queue_run_id or latest_same_token_queue_run_id(conn)
        rows = fetch_rows(conn, repair_queue_run_id=selected_repair_queue_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        run_id = insert_run(
            conn,
            repair_queue_run_id=selected_repair_queue_run_id,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
        )
        insert_items(conn, run_id=run_id, repair_queue_run_id=selected_repair_queue_run_id, rows=rows)
        item_ids = conn.execute(
            """
            SELECT id, repair_queue_item_id
            FROM auto_confirmation_reopen_text_boundary_repair_shadow_items
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
        by_queue_item = {int(row["repair_queue_item_id"]): int(row["id"]) for row in item_ids}
        for row in rows:
            row["shadow_item_id"] = by_queue_item.get(int(row["id"]))
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            repair_queue_run_id=selected_repair_queue_run_id,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    statuses = Counter(row["shadow_status"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows)
    print("[auto_confirmation_reopen_text_boundary_repair_shadow_policy] Shadow policy generated")
    print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] Repair queue run id: {selected_repair_queue_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] Candidates: {len(rows):,}")
    for key, value in statuses.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] {key}: {value:,}")
    for key, value in policies.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_shadow_policy] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "repair_queue_run_id": selected_repair_queue_run_id,
        "candidates": len(rows),
        "status_counts": dict(statuses),
        "policy_counts": dict(policies),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate same-token boundary repair candidates in shadow mode.")
    parser.add_argument("--repair-queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(repair_queue_run_id=args.repair_queue_run_id)
