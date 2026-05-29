from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from ml_composite_guarded_overlay_checkpoint import (
    TARGET_ACTIONS,
    TARGET_PROFILES,
    text_hygiene_flags,
)
from segment_token_overlay_review_queue import DEFAULT_ACTIVE_GATE_KEY, slugify


RULE_VERSION = "ml_composite_guarded_overlay_shadow_queue_v1"
SOURCE_MODE = "guarded_overlay_shadow_validation"
SHADOW_QUEUE_KIND = "guarded_release_shadow"
SUGGESTED_ROUTE = "guarded_release_shadow_validation"
HYGIENE_PRIORITY = "text_hygiene_shadow_review"
CLEAN_PRIORITY = "clean_token_shadow_review"


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


def latest_checkpoint_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_guarded_overlay_checkpoints
        WHERE promotion_status IN ('ready_for_shadow_validation', 'ready_for_guarded_gate_candidate')
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No guarded overlay checkpoint ready for shadow validation was found.")
    return int(row["id"])


def fetch_checkpoint(conn, checkpoint_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_guarded_overlay_checkpoints
        WHERE id = ?
        """,
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded overlay checkpoint {checkpoint_id} was not found.")
    return dict(row)


def fetch_active_gate(conn, gate_key: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_gate_registry
        WHERE gate_key = ?
        """,
        (gate_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Active composite gate {gate_key!r} was not found.")
    return dict(row)


def fetch_parent_release_policy_item_ids(conn, parent_overlay_run_id: int | None) -> set[int]:
    if parent_overlay_run_id is None:
        return set()
    placeholders = ", ".join("?" for _ in TARGET_ACTIONS)
    rows = conn.execute(
        f"""
        SELECT source_policy_item_id
        FROM segment_token_policy_overlay_items
        WHERE run_id = ?
          AND overlay_action IN ({placeholders})
        """,
        (parent_overlay_run_id, *sorted(TARGET_ACTIONS)),
    ).fetchall()
    return {int(row["source_policy_item_id"]) for row in rows}


def fetch_shadow_rows(conn, *, checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in TARGET_ACTIONS)
    parent_policy_item_ids = fetch_parent_release_policy_item_ids(
        conn,
        int(checkpoint["active_gate_overlay_run_id"]) if checkpoint.get("active_gate_overlay_run_id") else None,
    )
    rows = conn.execute(
        f"""
        SELECT
            oi.run_id AS overlay_run_id,
            oi.source_policy_run_id,
            oi.source_policy_item_id AS policy_item_id,
            oi.state_run_id,
            oi.segment_id,
            oi.relative_path,
            oi.source_key,
            oi.source_line_number,
            oi.original_policy_bucket,
            oi.original_risk_level,
            oi.overlay_policy_bucket,
            oi.overlay_risk_level,
            oi.overlay_action,
            oi.overlay_agent_key,
            oi.apply_allowed,
            oi.decision AS overlay_decision,
            oi.rule_key,
            oi.reasons_json,
            i.review_state,
            i.diff_kind,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked
        FROM segment_token_policy_overlay_items oi
        JOIN segment_token_policy_items i ON i.id = oi.source_policy_item_id
        JOIN source_segments s ON s.id = oi.segment_id
        LEFT JOIN output_segments o ON o.segment_id = oi.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = oi.segment_id
        WHERE oi.run_id = ?
          AND oi.overlay_action IN ({placeholders})
        ORDER BY oi.relative_path, oi.source_line_number, oi.source_key
        """,
        (checkpoint["overlay_run_id"], *sorted(TARGET_ACTIONS)),
    ).fetchall()
    enriched: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        profile = TARGET_PROFILES.get(row.get("overlay_action")) or {}
        row["release_origin"] = (
            "inherited_release"
            if int(row["policy_item_id"]) in parent_policy_item_ids
            else "new_release"
        )
        row["shadow_profile_agent"] = profile.get("target_agent") or row.get("overlay_agent_key")
        row["shadow_profile_bucket"] = profile.get("target_bucket") or row.get("overlay_policy_bucket")
        row["missing_tokens"] = parse_json_list(row.get("missing_tokens_json"))
        row["extra_tokens"] = parse_json_list(row.get("extra_tokens_json"))
        row["issue_flags"] = parse_json_list(row.get("issue_flags_json"))
        row["overlay_reasons"] = parse_json_list(row.get("reasons_json"))
        row["hygiene_flags"] = text_hygiene_flags(row.get("confirmed_text"))
        row["priority_bucket"] = HYGIENE_PRIORITY if row["hygiene_flags"] else CLEAN_PRIORITY
        row["suggested_route"] = SUGGESTED_ROUTE
        row["suggested_decision"] = (
            "encoding_cleanup_required" if row["hygiene_flags"] else "accept_policy_candidate"
        )
        row["default_decision"] = "needs_subpolicy"
        row["suggested_review_labels"] = (
            [
                "encoding_cleanup_required",
                "fix_confirmed_text",
                "reject_policy_candidate",
                "accept_policy_candidate",
                "needs_subpolicy",
            ]
            if row["hygiene_flags"]
            else [
                "accept_policy_candidate",
                "keep_manual_exception_only",
                "reject_policy_candidate",
                "needs_subpolicy",
            ]
        )
        enriched.append(row)
    return enriched


def fetch_reviewed_policy_item_ids(conn, *, policy_run_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT policy_item_id
        FROM segment_token_policy_decisions
        WHERE policy_run_id = ?
        """,
        (policy_run_id,),
    ).fetchall()
    return {int(row["policy_item_id"]) for row in rows}


def select_rows(
    rows: list[dict[str, Any]],
    *,
    priority: str,
    release_scope: str,
    limit: int | None,
    reviewed_policy_item_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    reviewed_policy_item_ids = reviewed_policy_item_ids or set()
    selected = []
    for row in rows:
        if int(row["policy_item_id"]) in reviewed_policy_item_ids:
            continue
        if priority == "hygiene" and row["priority_bucket"] != HYGIENE_PRIORITY:
            continue
        if priority == "clean" and row["priority_bucket"] != CLEAN_PRIORITY:
            continue
        if release_scope == "new" and row.get("release_origin") != "new_release":
            continue
        if release_scope == "inherited" and row.get("release_origin") != "inherited_release":
            continue
        selected.append(row)
    selected.sort(
        key=lambda row: (
            0 if row["priority_bucket"] == HYGIENE_PRIORITY else 1,
            0 if row.get("release_origin") == "new_release" else 1,
            row.get("overlay_action") or "",
            row.get("rule_key") or "",
            row["relative_path"],
            int(row.get("source_line_number") or 0),
            row["source_key"],
        )
    )
    if limit is not None:
        return selected[:limit]
    return selected


def validate_checkpoint(checkpoint: dict[str, Any], active_gate: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    try:
        checkpoint_blockers = json.loads(checkpoint.get("blockers_json") or "[]")
    except json.JSONDecodeError:
        checkpoint_blockers = ["checkpoint_blockers_json_invalid"]
    if checkpoint_blockers:
        blockers.append("checkpoint_has_blockers")
    if checkpoint["promotion_status"] not in {"ready_for_shadow_validation", "ready_for_guarded_gate_candidate"}:
        blockers.append("checkpoint_not_ready_for_shadow_validation")
    if int(active_gate.get("auto_apply_allowed") or 0) != 0:
        blockers.append("active_gate_auto_apply_must_remain_disabled")
    if int(active_gate["active_overlay_run_id"]) != int(checkpoint["active_gate_overlay_run_id"]):
        blockers.append("active_gate_overlay_changed_since_checkpoint")
    if int(active_gate["active_policy_run_id"]) != int(checkpoint["source_policy_run_id"]):
        blockers.append("active_gate_policy_changed_since_checkpoint")
    return blockers


def write_outputs(
    settings: dict[str, Any],
    *,
    checkpoint: dict[str, Any],
    active_gate: dict[str, Any],
    rows: list[dict[str, Any]],
    priority: str,
    release_scope: str,
    limit: int | None,
    plan_only: bool,
    skip_reviewed: bool,
    started_at: datetime,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
    slug = slugify(f"{SHADOW_QUEUE_KIND}_{priority}_limit_{limit if limit is not None else 'all'}")
    base = reports_dir / f"{timestamp}_ml_composite_{slug}_queue"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    decisions_template_path = base.with_name(base.name + "_decisions_template").with_suffix(".jsonl")

    fieldnames = [
        "guarded_checkpoint_id",
        "overlay_run_id",
        "source_policy_run_id",
        "policy_item_id",
        "segment_id",
        "priority_bucket",
        "suggested_route",
        "suggested_decision",
        "default_decision",
        "rule_key",
        "release_origin",
        "hygiene_flags",
        "original_policy_bucket",
        "original_risk_level",
        "overlay_policy_bucket",
        "overlay_risk_level",
        "overlay_action",
        "overlay_agent_key",
        "apply_allowed",
        "relative_path",
        "source_line_number",
        "source_key",
        "missing_tokens",
        "extra_tokens",
        "issue_flags",
        "confirmed_text",
        "output_text",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {key: row.get(key) for key in fieldnames}
            payload["guarded_checkpoint_id"] = checkpoint["id"]
            for key in {"missing_tokens", "extra_tokens", "issue_flags", "hygiene_flags"}:
                payload[key] = json.dumps(payload.get(key) or [], ensure_ascii=False)
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = dict(row)
            payload["guarded_checkpoint_id"] = checkpoint["id"]
            payload["shadow_queue_kind"] = SHADOW_QUEUE_KIND
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "decision": row["default_decision"],
                        "suggested_decision": row["suggested_decision"],
                        "corrected_text": "",
                        "reviewer": "",
                        "notes": (
                            f"{RULE_VERSION}; checkpoint={checkpoint['id']}; overlay={checkpoint['overlay_run_id']}; "
                            f"priority={row['priority_bucket']}; release_origin={row['release_origin']}; "
                            f"rule_key={row['rule_key']}; "
                            "default is conservative. Use accept_policy_candidate only when the guarded specialist release is safe "
                            "and the confirmed text has no unrelated cleanup blocker."
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    priority_counts = Counter(row["priority_bucket"] for row in rows)
    rule_counts = Counter(row.get("rule_key") or "missing_rule_key" for row in rows)
    hygiene_counts = Counter(flag for row in rows for flag in row["hygiene_flags"])
    lines = [
        "ML composite guarded overlay shadow queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Plan only: {plan_only}",
        "",
        "Scope:",
        f"- Gate key: {active_gate['gate_key']}",
        f"- Active checkpoint id: {active_gate['active_checkpoint_id']}",
        f"- Active overlay run id: {active_gate['active_overlay_run_id']}",
        f"- Guarded checkpoint id: {checkpoint['id']}",
        f"- Guarded overlay run id: {checkpoint['overlay_run_id']}",
        f"- Source policy run id: {checkpoint['source_policy_run_id']}",
        f"- Source mode: {SOURCE_MODE}",
        "",
        "Settings:",
        f"- Priority filter: {priority}",
        f"- Release scope: {release_scope}",
        f"- Limit: {limit if limit is not None else 'none'}",
        f"- Skip reviewed: {skip_reviewed}",
        "",
        "Rows:",
        f"- Selected rows: {len(rows)}",
        *[f"- {key}: {value}" for key, value in priority_counts.most_common()],
        "",
        "Rules:",
        *[f"- {key}: {value}" for key, value in rule_counts.most_common()],
        "",
        "Text hygiene flags:",
        *([f"- {key}: {value}" for key, value in hygiene_counts.most_common()] or ["- none"]),
        "",
        "Review guidance:",
        "- This queue validates a guarded overlay, not the active gate.",
        "- Clean token rows may be accepted only if the specialist release is structurally and semantically safe.",
        "- Hygiene rows should normally stay out of apply approval until encoding/mojibake/text cleanup is handled.",
        "- This command does not ingest decisions, promote policies, train models, or update output files.",
        "",
        "Review sample:",
    ]
    for row in rows[:90]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | {row['priority_bucket']} | {row['release_origin']} | "
                    f"{row['rule_key']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  suggested/default: {row['suggested_decision']} / {row['default_decision']}",
                f"  hygiene: {json.dumps(row['hygiene_flags'], ensure_ascii=False)}",
                f"  missing: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  extra: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  confirmed: {short(row.get('confirmed_text'), 300)}",
                f"  english: {short(row.get('english_text'), 240)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, decisions_template_path


def record_shadow_queue_run(
    settings: dict[str, Any],
    *,
    checkpoint: dict[str, Any],
    active_gate: dict[str, Any],
    rows: list[dict[str, Any]],
    priority: str,
    limit: int | None,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
) -> int | None:
    if not rows:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    route_counts = Counter(row["suggested_route"] for row in rows)
    bucket_counts = Counter(row["overlay_policy_bucket"] for row in rows)
    risk_counts = Counter(row["overlay_risk_level"] for row in rows)
    priority_counts = Counter(row["priority_bucket"] for row in rows)
    route_bucket_counts = Counter(
        (row["suggested_route"], row["overlay_policy_bucket"], row["overlay_risk_level"]) for row in rows
    )
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        cursor = conn.execute(
            """
            INSERT INTO ml_composite_gate_queue_runs (
                rule_version,
                gate_key,
                checkpoint_id,
                overlay_run_id,
                source_policy_run_id,
                guarded_checkpoint_id,
                source_mode,
                shadow_queue_kind,
                route_filter_csv,
                risk_filter_csv,
                critical_only,
                limit_count,
                total_rows,
                critical_rows,
                high_rows,
                medium_rows,
                low_rows,
                route_counts_json,
                bucket_counts_json,
                priority_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                active_gate["gate_key"],
                active_gate["active_checkpoint_id"],
                checkpoint["overlay_run_id"],
                checkpoint["source_policy_run_id"],
                checkpoint["id"],
                SOURCE_MODE,
                SHADOW_QUEUE_KIND,
                SUGGESTED_ROUTE,
                "low",
                0,
                limit,
                len(rows),
                risk_counts.get("critical", 0),
                risk_counts.get("high", 0),
                risk_counts.get("medium", 0),
                risk_counts.get("low", 0),
                json.dumps(dict(route_counts), ensure_ascii=False),
                json.dumps(dict(bucket_counts), ensure_ascii=False),
                json.dumps(dict(priority_counts), ensure_ascii=False),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                now,
                now,
                now,
            ),
        )
        queue_run_id = int(cursor.lastrowid)
        for (route, bucket, risk), total in sorted(route_bucket_counts.items()):
            conn.execute(
                """
                INSERT INTO ml_composite_gate_queue_routes (
                    queue_run_id,
                    suggested_route,
                    overlay_policy_bucket,
                    overlay_risk_level,
                    total,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (queue_run_id, route, bucket, risk, total, now),
            )
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO ml_composite_gate_queue_items (
                    queue_run_id,
                    gate_key,
                    checkpoint_id,
                    overlay_run_id,
                    source_policy_run_id,
                    guarded_checkpoint_id,
                    policy_item_id,
                    segment_id,
                    suggested_route,
                    overlay_policy_bucket,
                    overlay_risk_level,
                    priority_bucket,
                    rule_key,
                    hygiene_flags_json,
                    relative_path,
                    source_key,
                    source_line_number,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_run_id,
                    active_gate["gate_key"],
                    active_gate["active_checkpoint_id"],
                    checkpoint["overlay_run_id"],
                    checkpoint["source_policy_run_id"],
                    checkpoint["id"],
                    row["policy_item_id"],
                    row["segment_id"],
                    row["suggested_route"],
                    row["overlay_policy_bucket"],
                    row["overlay_risk_level"],
                    row["priority_bucket"],
                    row.get("rule_key"),
                    json.dumps(row["hygiene_flags"], ensure_ascii=False),
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    now,
                ),
            )
        conn.commit()
    return queue_run_id


def main(
    *,
    checkpoint_id: int | None = None,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    priority: str = "all",
    release_scope: str = "new",
    limit: int | None = None,
    skip_reviewed: bool = False,
    plan_only: bool = False,
) -> dict[str, Any]:
    if priority not in {"all", "hygiene", "clean"}:
        raise RuntimeError("priority must be one of: all, hygiene, clean.")
    if release_scope not in {"new", "inherited", "all"}:
        raise RuntimeError("release_scope must be one of: new, inherited, all.")
    if limit is not None and limit <= 0:
        raise RuntimeError("limit must be positive when provided.")

    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_id = checkpoint_id or latest_checkpoint_id(conn)
        checkpoint = fetch_checkpoint(conn, selected_checkpoint_id)
        active_gate = fetch_active_gate(conn, gate_key)
        blockers = validate_checkpoint(checkpoint, active_gate)
        if blockers:
            raise RuntimeError(f"Refusing to build shadow queue: {', '.join(blockers)}")
        all_rows = fetch_shadow_rows(conn, checkpoint=checkpoint)
        reviewed_policy_item_ids = (
            fetch_reviewed_policy_item_ids(conn, policy_run_id=int(checkpoint["source_policy_run_id"]))
            if skip_reviewed
            else set()
        )

    rows = select_rows(
        all_rows,
        priority=priority,
        release_scope=release_scope,
        limit=limit,
        reviewed_policy_item_ids=reviewed_policy_item_ids,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = write_outputs(
        settings,
        checkpoint=checkpoint,
        active_gate=active_gate,
        rows=rows,
        priority=priority,
        release_scope=release_scope,
        limit=limit,
        plan_only=plan_only,
        skip_reviewed=skip_reviewed,
        started_at=started_at,
    )
    queue_run_id = None
    if not plan_only:
        queue_run_id = record_shadow_queue_run(
            settings,
            checkpoint=checkpoint,
            active_gate=active_gate,
            rows=rows,
            priority=priority,
            limit=limit,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
        )

    priority_counts = Counter(row["priority_bucket"] for row in rows)
    print("[ml_composite_guarded_overlay_shadow_queue] Queue generated")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Checkpoint id: {checkpoint['id']}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Overlay run id: {checkpoint['overlay_run_id']}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Release scope: {release_scope}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Rows selected: {len(rows)}")
    for key, value in priority_counts.most_common():
        print(f"[ml_composite_guarded_overlay_shadow_queue] {key}: {value}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Queue run id: {queue_run_id}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Report: {txt_path}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] CSV: {csv_path}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] JSONL: {jsonl_path}")
    print(f"[ml_composite_guarded_overlay_shadow_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "rows_selected": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a shadow review queue for guarded composite overlay releases.")
    parser.add_argument("--checkpoint-id", type=int, default=None)
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--priority", choices=["all", "hygiene", "clean"], default="all")
    parser.add_argument("--release-scope", choices=["new", "inherited", "all"], default="new")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-reviewed", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parsed = parser.parse_args()
    main(
        checkpoint_id=parsed.checkpoint_id,
        gate_key=parsed.gate_key,
        priority=parsed.priority,
        release_scope=parsed.release_scope,
        limit=parsed.limit,
        skip_reviewed=parsed.skip_reviewed,
        plan_only=parsed.plan_only,
    )
