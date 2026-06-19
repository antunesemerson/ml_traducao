from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "segment_token_overlay_review_queue_v1"
DEFAULT_ACTIVE_GATE_KEY = "segment_token_composite_review_gate"


def latest_overlay_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_overlay_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_overlay_runs entry found.")
    return int(row["id"])


def active_gate_overlay_run_id(conn, *, gate_key: str = DEFAULT_ACTIVE_GATE_KEY) -> tuple[int, dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_gate_registry
        WHERE gate_key = ?
        """,
        (gate_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No active composite gate found for {gate_key!r}.")
    gate = dict(row)
    if gate.get("operational_state") != "operational":
        raise RuntimeError(
            f"Composite gate {gate_key!r} is not operational: {gate.get('operational_state')!r}."
        )
    if int(gate.get("auto_apply_allowed") or 0) != 0:
        raise RuntimeError("Refusing active gate queue while auto_apply_allowed is not 0.")
    return int(gate["active_overlay_run_id"]), gate


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


def parse_csv_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned[:80] or "filter"


def suggested_labels(row: dict[str, Any]) -> list[str]:
    bucket = row["overlay_policy_bucket"]
    if bucket == "blocked_suspicious_confirmed_text":
        return ["fix_confirmed_text", "manual_token_rewrite_required", "needs_subpolicy"]
    if bucket == "blocked_variable_or_icon_change":
        if row["relative_path"].startswith("tutorial/"):
            return ["needs_subpolicy", "fix_confirmed_text", "reject_policy_candidate"]
        return ["needs_subpolicy", "manual_token_rewrite_required", "reject_policy_candidate"]
    if bucket == "candidate_tutorial_concept_exception":
        return ["needs_subpolicy", "accept_policy_candidate", "reject_policy_candidate"]
    if bucket.startswith("guarded_"):
        return ["accept_policy_candidate", "encoding_cleanup_required", "needs_subpolicy"]
    if bucket.startswith("review_"):
        return ["needs_subpolicy", "reject_policy_candidate", "keep_manual_exception_only"]
    return ["needs_subpolicy", "reject_policy_candidate", "manual_token_rewrite_required"]


def suggested_route(row: dict[str, Any]) -> tuple[str, str]:
    bucket = row["overlay_policy_bucket"]
    path = row["relative_path"]
    key = row["source_key"]
    missing = row["missing_tokens"]
    extra = row["extra_tokens"]
    text = row.get("confirmed_text") or ""

    if bucket == "blocked_suspicious_confirmed_text":
        if "[Select_CString(" in text:
            return (
                "manual_dynamic_token_rewrite",
                "Text still has mojibake inside dynamic Select_CString/string literal context; review manually before any policy.",
            )
        if missing or extra:
            return (
                "manual_text_and_token_rewrite",
                "Text needs linguistic cleanup and token/format decision; do not auto-fix by replacement table.",
            )
        return (
            "manual_text_cleanup",
            "Text has residual question-mark mojibake; prepare explicit corrected_text decision.",
        )

    if bucket == "blocked_variable_or_icon_change":
        if path.startswith("tutorial/") and any(token.startswith("$catholic_") for token in extra):
            return (
                "tutorial_religion_named_variable_subpolicy_candidate",
                "Tutorial row adds a religion-specific named variable plus game concepts; needs a narrower tutorial religion subpolicy.",
            )
        if key.startswith("game_concept_"):
            return (
                "game_concept_definition_subpolicy_candidate",
                "Game concept definition rewrites Concept() into direct links/game_concept variables outside tutorial.",
            )
        if path == "schemes_l_spanish.yml":
            return (
                "scheme_concept_definition_subpolicy_candidate",
                "Scheme concept definitions intentionally mix direct links/game_concept variables; review as scheme-definition specialist.",
            )
        if "danelaw" in text.lower() or any("danelaw" in token.lower() for token in extra):
            return (
                "danelaw_article_manual_exception",
                "Danelaw article variable is a known contextual article/fluency exception; needs explicit named exception.",
            )
        return (
            "manual_variable_rewrite_review",
            "Variable or icon change remains outside existing tutorial concept policy; review before promoting any exception.",
        )

    if bucket == "candidate_tutorial_concept_exception":
        return (
            "tutorial_concept_exception_subpolicy_review",
            "Tutorial concept/token exception released by the active composite gate; keep in specialist review before apply approval.",
        )
    if bucket == "guarded_pronoun_english_aligned_subpolicy":
        return (
            "gender_pronoun_english_aligned_subpolicy",
            "Pronoun token change accepted by guarded evidence; keep text-hygiene rows blocked from apply until cleanup.",
        )
    if bucket == "guarded_select_cstring_ui_subpolicy":
        return (
            "select_cstring_dynamic_context_review",
            "Select_CString change is already guarded by the active composite gate; keep routed to the dynamic Select_CString specialist.",
        )
    if bucket == "guarded_mixed_name_gender_subpolicy":
        return (
            "mixed_token_change_review",
            "Mixed name/gender token change is already guarded by the active composite gate; keep routed to the mixed-token specialist.",
        )
    if bucket == "guarded_glossary_label_translation_subpolicy":
        return (
            "mixed_token_change_review",
            "Glossary label translation is already guarded by same-key evidence; keep routed to mixed-token/glossary specialist evidence.",
        )
    if bucket == "guarded_tutorial_concept_exception_subpolicy":
        return (
            "tutorial_concept_exception_subpolicy_review",
            "Tutorial concept exception is already guarded by the active composite gate; keep routed to tutorial concept review.",
        )
    if bucket == "guarded_token_added_english_reference_subpolicy":
        return (
            "token_added_review",
            "Token addition is already guarded by English-reference evidence; keep routed to token-added review.",
        )
    if bucket in {
        "guarded_pronoun_form_swap_subpolicy",
        "guarded_gender_form_swap_subpolicy",
        "guarded_gender_article_removal_subpolicy",
    }:
        return (
            "gender_token_subspecialist_review",
            "Gender/pronoun token change is already guarded by the active composite gate; keep routed to gender-token specialist evidence.",
        )
    if bucket in {
        "guarded_name_form_style_subpolicy",
        "guarded_token_style_modifier_subpolicy",
    }:
        return (
            "mixed_token_change_review",
            "Name/style token change is already guarded by the active composite gate; keep routed to mixed-token specialist evidence.",
        )
    if bucket == "guarded_dynamic_scope_neutralization_subpolicy":
        return (
            "dynamic_scope_token_review",
            "Dynamic scope neutralization is already guarded by the active composite gate; keep routed to scope-aware token specialist review.",
        )
    if bucket == "review_gender_token_change":
        return (
            "gender_token_subspecialist_review",
            "Gender token variation was downgraded by the active composite gate; route to gender-token specialist evidence.",
        )
    if bucket == "review_select_cstring_change":
        return (
            "select_cstring_dynamic_context_review",
            "Select_CString or dynamic string context changed; route to dynamic-token specialist review.",
        )
    if bucket == "review_mixed_token_change":
        return (
            "mixed_token_change_review",
            "Multiple structural token classes changed together; review as mixed-token case before any exception policy.",
        )
    if bucket == "review_dynamic_scope_change":
        return (
            "dynamic_scope_token_review",
            "Dynamic scope token changed; route to scope-aware token specialist review.",
        )
    if bucket == "review_token_added":
        return (
            "token_added_review",
            "Token was added relative to the source mirror; review as explicit contextual exception.",
        )
    if bucket == "review_token_removed":
        return (
            "token_removed_review",
            "Token was removed relative to the source mirror; review as explicit contextual exception.",
        )

    return ("manual_review", "No overlay-specific route found.")


def fetch_rows(conn, *, overlay_run_id: int, critical_only: bool) -> list[dict[str, Any]]:
    where = ["oi.run_id = ?"]
    params: list[Any] = [overlay_run_id]
    if critical_only:
        where.append("oi.overlay_risk_level = 'critical'")
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
            oi.would_release_critical,
            oi.apply_allowed,
            oi.decision,
            oi.rule_key,
            oi.reasons_json AS overlay_reasons_json,
            i.review_state,
            i.diff_kind,
            i.policy_bucket AS base_policy_bucket,
            i.risk_level AS base_risk_level,
            i.recommendation AS base_recommendation,
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
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE oi.overlay_risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
            END,
            oi.overlay_policy_bucket,
            oi.relative_path,
            oi.source_line_number
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["missing_tokens"] = parse_json_list(row.get("missing_tokens_json"))
    payload["extra_tokens"] = parse_json_list(row.get("extra_tokens_json"))
    payload["issue_flags"] = parse_json_list(row.get("issue_flags_json"))
    payload["overlay_reasons"] = parse_json_list(row.get("overlay_reasons_json"))
    route, route_reason = suggested_route(payload)
    payload["suggested_route"] = route
    payload["suggested_route_reason"] = route_reason
    payload["suggested_review_labels"] = suggested_labels(payload)
    return payload


def filter_rows(
    rows: list[dict[str, Any]],
    *,
    route_filter: set[str],
    risk_filter: set[str],
    limit: int | None,
    excluded_policy_item_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    excluded_policy_item_ids = excluded_policy_item_ids or set()
    filtered = []
    for row in rows:
        if int(row["policy_item_id"]) in excluded_policy_item_ids:
            continue
        if route_filter and row["suggested_route"] not in route_filter:
            continue
        if risk_filter and row["overlay_risk_level"] not in risk_filter:
            continue
        filtered.append(row)
    if limit is not None:
        return filtered[:limit]
    return filtered


def write_outputs(
    settings: dict[str, Any],
    *,
    overlay_run_id: int,
    rows: list[dict[str, Any]],
    critical_only: bool,
    source_mode: str,
    active_gate: dict[str, Any] | None,
    route_filter: set[str],
    risk_filter: set[str],
    limit: int | None,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    slug_prefix = "active_gate" if active_gate else "latest"
    slug_parts = [slug_prefix, "critical" if critical_only else "all"]
    if route_filter:
        slug_parts.append("route_" + slugify("_".join(sorted(route_filter))))
    if risk_filter:
        slug_parts.append("risk_" + slugify("_".join(sorted(risk_filter))))
    if limit is not None:
        slug_parts.append(f"limit_{limit}")
    slug = "_".join(slug_parts)
    base = reports_dir / f"{timestamp}_segment_token_overlay_review_queue_{slug}"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    decisions_template_path = base.with_name(base.name + "_decisions_template").with_suffix(".jsonl")

    fieldnames = [
        "overlay_run_id",
        "source_policy_run_id",
        "policy_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "review_state",
        "diff_kind",
        "original_policy_bucket",
        "original_risk_level",
        "overlay_policy_bucket",
        "overlay_risk_level",
        "overlay_action",
        "suggested_route",
        "suggested_route_reason",
        "suggested_review_labels",
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
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"missing_tokens", "extra_tokens", "issue_flags", "suggested_review_labels"}
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    with decisions_template_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            label = row["suggested_review_labels"][0]
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "decision": label,
                        "corrected_text": "",
                        "notes": f"{RULE_VERSION}; {row['suggested_route']}; {row['suggested_route_reason']}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    bucket_counts = Counter(row["overlay_policy_bucket"] for row in rows)
    route_counts = Counter(row["suggested_route"] for row in rows)
    lines = [
        "Segment token overlay review queue",
        f"Rule version: {RULE_VERSION}",
        f"Source mode: {source_mode}",
        f"Overlay run id: {overlay_run_id}",
        f"Active gate: {active_gate.get('gate_key') if active_gate else 'none'}",
        f"Active checkpoint id: {active_gate.get('active_checkpoint_id') if active_gate else 'none'}",
        f"Auto-apply allowed: {active_gate.get('auto_apply_allowed') if active_gate else 'n/a'}",
        f"Critical only: {critical_only}",
        f"Route filter: {', '.join(sorted(route_filter)) if route_filter else 'none'}",
        f"Risk filter: {', '.join(sorted(risk_filter)) if risk_filter else 'none'}",
        f"Limit: {limit if limit is not None else 'none'}",
        f"Rows selected: {len(rows)}",
        "",
        "Overlay buckets:",
        *[f"- {key}: {value}" for key, value in bucket_counts.most_common()],
        "",
        "Suggested routes:",
        *[f"- {key}: {value}" for key, value in route_counts.most_common()],
        "",
        "Review sample:",
    ]
    for row in rows:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['overlay_policy_bucket']} | {row['suggested_route']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  labels: {', '.join(row['suggested_review_labels'])}",
                f"  reason: {row['suggested_route_reason']}",
                f"  missing: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  extra: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  confirmed: {short(row['confirmed_text'], 360)}",
                f"  output: {short(row['output_text'], 260)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, decisions_template_path


def record_active_gate_queue_run(
    settings: dict[str, Any],
    *,
    active_gate: dict[str, Any] | None,
    overlay_run_id: int,
    source_mode: str,
    rows: list[dict[str, Any]],
    critical_only: bool,
    route_filter: set[str],
    risk_filter: set[str],
    limit: int | None,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
) -> int | None:
    if not active_gate:
        return None

    now = datetime.now().isoformat(timespec="seconds")
    route_counts = Counter(row["suggested_route"] for row in rows)
    bucket_counts = Counter(row["overlay_policy_bucket"] for row in rows)
    risk_counts = Counter(row["overlay_risk_level"] for row in rows)
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
                source_mode,
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
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                active_gate["gate_key"],
                active_gate["active_checkpoint_id"],
                overlay_run_id,
                active_gate.get("active_policy_run_id"),
                source_mode,
                ",".join(sorted(route_filter)) if route_filter else None,
                ",".join(sorted(risk_filter)) if risk_filter else None,
                1 if critical_only else 0,
                limit,
                len(rows),
                risk_counts.get("critical", 0),
                risk_counts.get("high", 0),
                risk_counts.get("medium", 0),
                risk_counts.get("low", 0),
                json.dumps(dict(route_counts), ensure_ascii=False),
                json.dumps(dict(bucket_counts), ensure_ascii=False),
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
                    policy_item_id,
                    segment_id,
                    suggested_route,
                    overlay_policy_bucket,
                    overlay_risk_level,
                    relative_path,
                    source_key,
                    source_line_number,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_run_id,
                    active_gate["gate_key"],
                    active_gate["active_checkpoint_id"],
                    overlay_run_id,
                    active_gate.get("active_policy_run_id"),
                    row["policy_item_id"],
                    row["segment_id"],
                    row["suggested_route"],
                    row["overlay_policy_bucket"],
                    row["overlay_risk_level"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    now,
                ),
            )
        conn.commit()
    return queue_run_id


def fetch_excluded_policy_item_ids(
    settings: dict[str, Any],
    *,
    active_gate: dict[str, Any] | None,
    skip_reviewed: bool,
    skip_queued: bool,
) -> set[int]:
    if not active_gate or not (skip_reviewed or skip_queued):
        return set()
    excluded: set[int] = set()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if skip_reviewed:
            rows = conn.execute(
                """
                SELECT policy_item_id
                FROM segment_token_policy_decisions
                WHERE policy_run_id = ?
                """,
                (active_gate["active_policy_run_id"],),
            ).fetchall()
            excluded.update(int(row["policy_item_id"]) for row in rows)
        if skip_queued:
            rows = conn.execute(
                """
                SELECT qi.policy_item_id
                FROM ml_composite_gate_queue_items qi
                JOIN ml_composite_gate_queue_runs qr ON qr.id = qi.queue_run_id
                WHERE qi.gate_key = ?
                  AND qi.overlay_run_id = ?
                  AND (
                      qr.route_filter_csv IS NOT NULL
                      OR qr.risk_filter_csv IS NOT NULL
                      OR qr.limit_count IS NOT NULL
                  )
                """,
                (active_gate["gate_key"], active_gate["active_overlay_run_id"]),
            ).fetchall()
            excluded.update(int(row["policy_item_id"]) for row in rows)
    return excluded


def resolve_overlay_run(
    conn,
    *,
    overlay_run_id: int | None,
    use_active_gate: bool,
    gate_key: str,
) -> tuple[int, str, dict[str, Any] | None]:
    if overlay_run_id is not None and use_active_gate:
        raise RuntimeError("--overlay-run-id and --use-active-composite-gate are mutually exclusive.")
    if use_active_gate:
        selected_overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        return selected_overlay_run_id, "active_composite_gate", active_gate
    if overlay_run_id is not None:
        return overlay_run_id, "explicit_overlay_run", None
    return latest_overlay_run_id(conn), "latest_completed_overlay_run", None


def main(
    *,
    overlay_run_id: int | None = None,
    critical_only: bool = True,
    use_active_gate: bool = False,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    route: str | None = None,
    risk: str | None = None,
    limit: int | None = None,
    skip_reviewed: bool = False,
    skip_queued: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_overlay_run_id, source_mode, active_gate = resolve_overlay_run(
            conn,
            overlay_run_id=overlay_run_id,
            use_active_gate=use_active_gate,
            gate_key=gate_key,
        )
        raw_rows = fetch_rows(conn, overlay_run_id=selected_overlay_run_id, critical_only=critical_only)
    route_filter = parse_csv_filter(route)
    risk_filter = parse_csv_filter(risk)
    excluded_policy_item_ids = fetch_excluded_policy_item_ids(
        settings,
        active_gate=active_gate,
        skip_reviewed=skip_reviewed,
        skip_queued=skip_queued,
    )
    rows = filter_rows(
        [enrich(row) for row in raw_rows],
        route_filter=route_filter,
        risk_filter=risk_filter,
        limit=limit,
        excluded_policy_item_ids=excluded_policy_item_ids,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = write_outputs(
        settings,
        overlay_run_id=selected_overlay_run_id,
        rows=rows,
        critical_only=critical_only,
        source_mode=source_mode,
        active_gate=active_gate,
        route_filter=route_filter,
        risk_filter=risk_filter,
        limit=limit,
    )
    queue_run_id = record_active_gate_queue_run(
        settings,
        active_gate=active_gate,
        overlay_run_id=selected_overlay_run_id,
        source_mode=source_mode,
        rows=rows,
        critical_only=critical_only,
        route_filter=route_filter,
        risk_filter=risk_filter,
        limit=limit,
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        decisions_template_path=decisions_template_path,
    )
    bucket_counts = Counter(row["overlay_policy_bucket"] for row in rows)
    route_counts = Counter(row["suggested_route"] for row in rows)
    print("[segment_token_overlay_review_queue] Queue generated")
    print(f"[segment_token_overlay_review_queue] Rule version: {RULE_VERSION}")
    print(f"[segment_token_overlay_review_queue] Source mode: {source_mode}")
    print(f"[segment_token_overlay_review_queue] Overlay run id: {selected_overlay_run_id}")
    print(f"[segment_token_overlay_review_queue] Route filter: {', '.join(sorted(route_filter)) if route_filter else 'none'}")
    print(f"[segment_token_overlay_review_queue] Risk filter: {', '.join(sorted(risk_filter)) if risk_filter else 'none'}")
    print(f"[segment_token_overlay_review_queue] Limit: {limit if limit is not None else 'none'}")
    print(f"[segment_token_overlay_review_queue] Skip reviewed: {skip_reviewed}")
    print(f"[segment_token_overlay_review_queue] Skip queued: {skip_queued}")
    print(f"[segment_token_overlay_review_queue] Excluded policy items: {len(excluded_policy_item_ids)}")
    if active_gate:
        print(f"[segment_token_overlay_review_queue] Active gate: {active_gate['gate_key']}")
        print(f"[segment_token_overlay_review_queue] Active checkpoint id: {active_gate['active_checkpoint_id']}")
        print("[segment_token_overlay_review_queue] Auto-apply allowed: 0")
        print(f"[segment_token_overlay_review_queue] Active queue run id: {queue_run_id}")
    print(f"[segment_token_overlay_review_queue] Rows selected: {len(rows)}")
    for key, value in bucket_counts.most_common():
        print(f"[segment_token_overlay_review_queue] bucket {key}: {value}")
    for key, value in route_counts.most_common():
        print(f"[segment_token_overlay_review_queue] route {key}: {value}")
    print(f"[segment_token_overlay_review_queue] Report: {txt_path}")
    print(f"[segment_token_overlay_review_queue] CSV: {csv_path}")
    print(f"[segment_token_overlay_review_queue] JSONL: {jsonl_path}")
    print(f"[segment_token_overlay_review_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "overlay_run_id": selected_overlay_run_id,
        "source_mode": source_mode,
        "active_gate": active_gate,
        "route_filter": sorted(route_filter),
        "risk_filter": sorted(risk_filter),
        "limit": limit,
        "skip_reviewed": skip_reviewed,
        "skip_queued": skip_queued,
        "excluded_policy_items": len(excluded_policy_item_ids),
        "rows_selected": len(rows),
        "bucket_counts": dict(bucket_counts),
        "route_counts": dict(route_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a review queue from the latest segment token policy overlay.")
    parser.add_argument("--overlay-run-id", type=int, default=None)
    parser.add_argument("--all", action="store_true", help="Include all overlay rows instead of critical rows only.")
    parser.add_argument("--use-active-composite-gate", action="store_true")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--route", default=None, help="Comma-separated suggested_route filter.")
    parser.add_argument("--risk", default=None, help="Comma-separated overlay_risk_level filter.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-reviewed", action="store_true")
    parser.add_argument("--skip-queued", action="store_true")
    args = parser.parse_args()
    main(
        overlay_run_id=args.overlay_run_id,
        critical_only=not args.all,
        use_active_gate=args.use_active_composite_gate,
        gate_key=args.gate_key,
        route=args.route,
        risk=args.risk,
        limit=args.limit,
        skip_reviewed=args.skip_reviewed,
        skip_queued=args.skip_queued,
    )
