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
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_v1"
AUDIT_NAME = "boundary_token_subpolicy_controlled_production_readiness_v1"


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_yml_escaped_text_for_compare(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace('\\"', '"')


def latest_lifecycle_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs
        WHERE finished_at IS NOT NULL
          AND policy_status = 'shadow'
          AND released_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed boundary token subpolicy lifecycle run found.")
    return int(row["id"])


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed segment_state run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
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


def fetch_rows(conn, *, lifecycle_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            life.id AS lifecycle_item_id,
            life.run_id AS lifecycle_run_id,
            life.segment_id,
            life.relative_path,
            life.source_key,
            life.source_line_number,
            life.subpolicy_name,
            life.policy_bucket,
            life.boundary_policy,
            life.policy_allowed,
            life.current_confirmed_text_hash,
            life.corrected_text_hash,
            life.evidence_json,
            decision.corrected_text,
            confirmation.id AS confirmation_id,
            confirmation.confirmed_text,
            output.portuguese_text,
            state.id AS state_item_id,
            state.final_state AS current_final_state,
            state.apply_state AS current_apply_state,
            state.state_group AS current_state_group,
            state.review_state AS current_review_state,
            state.lifecycle_policy_allowed AS current_lifecycle_policy_allowed,
            state.needs_human,
            state.needs_output_apply,
            state.needs_reopen,
            state.is_closed
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items life
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = life.review_decision_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = life.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        LEFT JOIN output_segments output
          ON output.segment_id = life.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = life.segment_id
         AND state.run_id = ?
        WHERE life.run_id = ?
        ORDER BY life.policy_bucket, life.relative_path, life.source_line_number, life.source_key
        """,
        (state_run_id, lifecycle_run_id),
    ).fetchall()
    return [evaluate_row(dict(row), state_run_id=state_run_id) for row in rows]


def evaluate_row(row: dict[str, Any], *, state_run_id: int) -> dict[str, Any]:
    corrected_text = row.get("corrected_text")
    confirmed_text = row.get("confirmed_text")
    output_text = row.get("portuguese_text")
    corrected_compare = normalize_yml_escaped_text_for_compare(corrected_text)
    confirmed_compare = normalize_yml_escaped_text_for_compare(confirmed_text)
    output_compare = normalize_yml_escaped_text_for_compare(output_text)
    corrected_matches_output = corrected_compare == output_compare
    corrected_matches_confirmation = corrected_compare == confirmed_compare
    confirmation_matches_output = confirmed_compare == output_compare
    validation_issues = blocking_validation_issues(corrected_text)

    requires_confirmation_promotion = not corrected_matches_confirmation
    requires_output_apply = not corrected_matches_output
    # The current segment-state snapshot only reads the legacy auto_confirmation_reopen_lifecycle_policy_items
    # lifecycle table. This new shadow lifecycle needs production-side integration before it can close rows.
    requires_segment_state_lifecycle_integration = int(row.get("current_lifecycle_policy_allowed") or 0) != 1

    blockers: list[str] = []
    if int(row.get("policy_allowed") or 0) != 1:
        blockers.append("lifecycle_item_not_allowed")
    if row.get("state_item_id") is None:
        blockers.append("missing_latest_segment_state")
    if validation_issues:
        blockers.append("validation_issue")
    if not requires_confirmation_promotion and not requires_output_apply:
        blockers.append("already_applied")
    if not confirmation_matches_output:
        blockers.append("current_confirmation_output_mismatch")
    if not row.get("corrected_text_hash") or row.get("corrected_text_hash") != sha256_text(corrected_text):
        blockers.append("corrected_hash_mismatch")

    eligible = not blockers
    estimated_closed_gain = bool(
        eligible
        and requires_confirmation_promotion
        and requires_output_apply
        and row.get("current_state_group") == "pending"
        and row.get("current_final_state") == "reopen_auto_confirmed_autofix"
    )
    production_steps = []
    if requires_confirmation_promotion:
        production_steps.append("promote_corrected_text_to_confirmation")
    if requires_output_apply:
        production_steps.append("write_corrected_text_to_output")
    if requires_segment_state_lifecycle_integration:
        production_steps.append("include_token_subpolicy_lifecycle_in_segment_state")

    return {
        **row,
        "state_run_id": state_run_id,
        "corrected_matches_output": int(corrected_matches_output),
        "corrected_matches_confirmation": int(corrected_matches_confirmation),
        "confirmation_matches_output": int(confirmation_matches_output),
        "requires_confirmation_promotion": int(requires_confirmation_promotion),
        "requires_output_apply": int(requires_output_apply),
        "requires_segment_state_lifecycle_integration": int(requires_segment_state_lifecycle_integration),
        "eligible_controlled_production": int(eligible),
        "estimated_closed_gain": int(estimated_closed_gain),
        "block_reason": ",".join(blockers),
        "validation_issue_count": len(validation_issues),
        "production_steps": production_steps,
        "output_text_hash": sha256_text(output_text),
        "confirmed_text_hash": sha256_text(confirmed_text),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    audit_run_id: int,
    lifecycle_run_id: int,
    state_run_id: int,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "audit_item_id",
        "lifecycle_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "subpolicy_name",
        "policy_bucket",
        "boundary_policy",
        "current_final_state",
        "current_apply_state",
        "current_state_group",
        "current_review_state",
        "corrected_matches_output",
        "corrected_matches_confirmation",
        "confirmation_matches_output",
        "requires_confirmation_promotion",
        "requires_output_apply",
        "requires_segment_state_lifecycle_integration",
        "eligible_controlled_production",
        "estimated_closed_gain",
        "block_reason",
        "production_steps",
        "portuguese_text",
        "confirmed_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["production_steps"] = json.dumps(row["production_steps"], ensure_ascii=False)
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames if field != "production_steps"},
                "production_steps": row["production_steps"],
                "evidence": json.loads(row["evidence_json"]) if row.get("evidence_json") else {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counters = Counter()
    by_subpolicy = Counter()
    by_bucket = Counter()
    by_state = Counter()
    for row in rows:
        for key in [
            "requires_confirmation_promotion",
            "requires_output_apply",
            "requires_segment_state_lifecycle_integration",
            "eligible_controlled_production",
            "estimated_closed_gain",
        ]:
            counters[key] += int(row[key])
        counters["blocked"] += 1 if row["block_reason"] else 0
        by_subpolicy[row["subpolicy_name"]] += int(row["eligible_controlled_production"])
        by_bucket[row["policy_bucket"]] += int(row["eligible_controlled_production"])
        by_state[row["current_final_state"] or "<missing>"] += 1

    lines = [
        "Auto-confirmation boundary token subpolicy production audit",
        f"Rule version: {RULE_VERSION}",
        f"Audit name: {AUDIT_NAME}",
        f"Audit run id: {audit_run_id}",
        f"Lifecycle run id: {lifecycle_run_id}",
        f"Segment-state run id: {state_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Eligible for controlled production path: {counters['eligible_controlled_production']:,}",
        f"- Estimated closed-state gain after full integration: {counters['estimated_closed_gain']:,}",
        f"- Require confirmation promotion: {counters['requires_confirmation_promotion']:,}",
        f"- Require output apply: {counters['requires_output_apply']:,}",
        f"- Require segment-state lifecycle integration: {counters['requires_segment_state_lifecycle_integration']:,}",
        f"- Blocked by audit: {counters['blocked']:,}",
        "",
        "Eligible by subpolicy:",
        *[f"- {key}: {value:,}" for key, value in by_subpolicy.most_common()],
        "",
        "Eligible by bucket:",
        *[f"- {key}: {value:,}" for key, value in by_bucket.most_common()],
        "",
        "Current final states:",
        *[f"- {key}: {value:,}" for key, value in by_state.most_common()],
        "",
        "Interpretation:",
        "- These candidates are not currently applied: corrected_text differs from output for all eligible rows.",
        "- A safe production path must promote corrected_text to the trusted confirmation layer before/with output write.",
        "- Segment-state must also consume this lifecycle family, otherwise the same rows remain reopen_auto_confirmed_autofix after apply.",
        "",
        "Priority sample:",
    ]
    for row in rows[:30]:
        lines.extend(
            [
                (
                    f"- segment {row['segment_id']} | {row['subpolicy_name']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                f"  state={row.get('current_final_state')}; eligible={row['eligible_controlled_production']}; steps={', '.join(row['production_steps'])}",
                f"  current={short(row.get('portuguese_text'))}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Audit only: no confirmation promotion, no output writes, no segment-state updates.",
            "- Production should remain controlled by the production/front workflow, not this learning audit.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, lifecycle_run_id: int | None = None, state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_lifecycle_run_id = lifecycle_run_id or latest_lifecycle_run_id(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        rows = fetch_rows(conn, lifecycle_run_id=selected_lifecycle_run_id, state_run_id=selected_state_run_id)
        if not rows:
            raise RuntimeError(f"Lifecycle run {selected_lifecycle_run_id} has no audit rows.")

        counters = Counter()
        for row in rows:
            counters["policy_allowed"] += int(row.get("policy_allowed") or 0)
            counters["output_delta"] += 1 if not row["corrected_matches_output"] else 0
            counters["confirmation_delta"] += 1 if not row["corrected_matches_confirmation"] else 0
            counters["confirmation_matches_output"] += int(row["confirmation_matches_output"])
            counters["current_pending"] += 1 if row.get("current_state_group") == "pending" else 0
            counters["current_closed"] += 1 if row.get("current_state_group") == "closed" else 0
            counters["requires_confirmation_promotion"] += int(row["requires_confirmation_promotion"])
            counters["requires_output_apply"] += int(row["requires_output_apply"])
            counters["requires_segment_state_lifecycle_integration"] += int(row["requires_segment_state_lifecycle_integration"])
            counters["eligible_controlled_production"] += int(row["eligible_controlled_production"])
            counters["blocked"] += 1 if row["block_reason"] else 0
            counters["estimated_closed_gain"] += int(row["estimated_closed_gain"])

        audit_status = "ready_for_controlled_production_design" if counters["eligible_controlled_production"] else "blocked_or_no_gain"
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs (
                rule_version,
                lifecycle_run_id,
                state_run_id,
                audit_name,
                audit_status,
                candidate_count,
                policy_allowed_count,
                output_delta_count,
                confirmation_delta_count,
                confirmation_matches_output_count,
                current_pending_count,
                current_closed_count,
                requires_confirmation_promotion_count,
                requires_output_apply_count,
                requires_segment_state_lifecycle_integration_count,
                eligible_controlled_production_count,
                blocked_count,
                estimated_closed_gain_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_lifecycle_run_id,
                selected_state_run_id,
                AUDIT_NAME,
                audit_status,
                len(rows),
                counters["policy_allowed"],
                counters["output_delta"],
                counters["confirmation_delta"],
                counters["confirmation_matches_output"],
                counters["current_pending"],
                counters["current_closed"],
                counters["requires_confirmation_promotion"],
                counters["requires_output_apply"],
                counters["requires_segment_state_lifecycle_integration"],
                counters["eligible_controlled_production"],
                counters["blocked"],
                counters["estimated_closed_gain"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        audit_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items (
                    run_id,
                    lifecycle_run_id,
                    lifecycle_item_id,
                    state_run_id,
                    state_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    subpolicy_name,
                    policy_bucket,
                    boundary_policy,
                    current_final_state,
                    current_apply_state,
                    current_state_group,
                    current_review_state,
                    current_lifecycle_policy_allowed,
                    corrected_matches_output,
                    corrected_matches_confirmation,
                    confirmation_matches_output,
                    requires_confirmation_promotion,
                    requires_output_apply,
                    requires_segment_state_lifecycle_integration,
                    eligible_controlled_production,
                    estimated_closed_gain,
                    block_reason,
                    current_confirmed_text_hash,
                    corrected_text_hash,
                    output_text_hash,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_run_id,
                    selected_lifecycle_run_id,
                    row["lifecycle_item_id"],
                    selected_state_run_id,
                    row.get("state_item_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["subpolicy_name"],
                    row["policy_bucket"],
                    row["boundary_policy"],
                    row.get("current_final_state"),
                    row.get("current_apply_state"),
                    row.get("current_state_group"),
                    row.get("current_review_state"),
                    int(row.get("current_lifecycle_policy_allowed") or 0),
                    int(row["corrected_matches_output"]),
                    int(row["corrected_matches_confirmation"]),
                    int(row["confirmation_matches_output"]),
                    int(row["requires_confirmation_promotion"]),
                    int(row["requires_output_apply"]),
                    int(row["requires_segment_state_lifecycle_integration"]),
                    int(row["eligible_controlled_production"]),
                    int(row["estimated_closed_gain"]),
                    row["block_reason"],
                    row.get("current_confirmed_text_hash") or row.get("confirmed_text_hash"),
                    row.get("corrected_text_hash"),
                    row.get("output_text_hash"),
                    row.get("evidence_json"),
                    now,
                ),
            )
            row["audit_item_id"] = int(item_cursor.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            audit_run_id=audit_run_id,
            lifecycle_run_id=selected_lifecycle_run_id,
            state_run_id=selected_state_run_id,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Audit generated")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Audit run id: {audit_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Lifecycle run id: {selected_lifecycle_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] State run id: {selected_state_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Eligible controlled production: {counters['eligible_controlled_production']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Estimated closed gain: {counters['estimated_closed_gain']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Blocked: {counters['blocked']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit] JSONL: {jsonl_path}")
    return {
        "audit_run_id": audit_run_id,
        "lifecycle_run_id": selected_lifecycle_run_id,
        "state_run_id": selected_state_run_id,
        "eligible_controlled_production": counters["eligible_controlled_production"],
        "estimated_closed_gain": counters["estimated_closed_gain"],
        "blocked": counters["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit controlled production readiness for token-boundary subpolicy lifecycle rows.")
    parser.add_argument("--lifecycle-run-id", type=int, default=None)
    parser.add_argument("--state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(lifecycle_run_id=args.lifecycle_run_id, state_run_id=args.state_run_id)
