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


RULE_VERSION = "auto_confirmation_reopen_text_boundary_repair_queue_v1"
DEFAULT_SCOPE = "all"

SCOPE_STATUS = {
    "same-token": {"repair_candidate_same_tokens"},
    "token-change": {"repair_candidate_token_change"},
    "all": {"repair_candidate_same_tokens", "repair_candidate_token_change"},
}

RISK_RANK = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value).strip("_") or "queue"


def latest_boundary_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_policy_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete auto_confirmation_reopen_text_boundary_policy_runs entry found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], *, scope: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_repair_queue_{slug(scope)}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def parse_reasons(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def repair_route(row: dict[str, Any]) -> str:
    if row["boundary_status"] == "repair_candidate_same_tokens":
        return "same_token_shadow_repair"
    if row["boundary_status"] == "repair_candidate_token_change":
        return "token_policy_repair_review"
    return "manual_boundary_review"


def risk_level(row: dict[str, Any]) -> str:
    if row["token_status"] == "structural_token_change_review_required":
        return "critical"
    if row["boundary_policy"] in {
        "weak_auto_visible_runtime_spanish_verb",
        "weak_auto_embedded_select_cstring_spanish_literal",
        "weak_auto_custom_loc_es_helper",
    }:
        return "high"
    if row["boundary_policy"] in {
        "weak_auto_visible_semantic_sentence_loss",
        "weak_auto_visible_sentence_collapse",
        "weak_auto_visible_copula_token_form",
    }:
        return "high"
    return "medium"


def suggested_review_labels(row: dict[str, Any]) -> list[str]:
    if row["boundary_status"] == "repair_candidate_same_tokens":
        return [
            "accept_same_token_repair_shadow",
            "keep_boundary_only",
            "needs_more_context",
        ]
    return [
        "accept_repair_after_token_policy",
        "keep_manual_exception_only",
        "needs_subpolicy",
        "reject_repair_candidate",
    ]


def fetch_rows(
    conn,
    *,
    boundary_policy_run_id: int,
    scope: str,
    include_existing: bool,
    limit: int | None,
) -> tuple[int, list[dict[str, Any]]]:
    statuses = SCOPE_STATUS[scope]
    placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = [boundary_policy_run_id, *sorted(statuses)]
    skip_sql = ""
    if not include_existing:
        skip_sql = """
          AND NOT EXISTS (
              SELECT 1
              FROM auto_confirmation_reopen_text_boundary_repair_queue_items queued
              WHERE queued.boundary_policy_item_id = item.id
          )
        """
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_policy_items item
        WHERE item.run_id = ?
          AND item.boundary_status IN ({placeholders})
        """,
        (boundary_policy_run_id, *sorted(statuses)),
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT
            item.id AS boundary_policy_item_id,
            item.run_id AS boundary_policy_run_id,
            item.review_decision_id,
            item.diagnostic_run_id,
            item.diagnostic_item_id,
            item.queue_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.source_agent_key,
            item.source_text_subfamily,
            item.boundary_agent_key,
            item.boundary_policy,
            item.boundary_status,
            item.boundary_action,
            item.token_status,
            item.block_reason,
            item.issue_count,
            item.select_cstring_count,
            item.concept_link_count,
            item.spanish_literal_hint_count,
            item.current_confirmed_text_hash,
            item.corrected_text_hash,
            item.reasons_json,
            decision.corrected_text,
            decision.notes,
            confirmation.confirmed_text,
            source.english_text,
            source.spanish_text
        FROM auto_confirmation_reopen_text_boundary_policy_items item
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
          AND item.boundary_status IN ({placeholders})
          {skip_sql}
        ORDER BY
            CASE item.token_status
                WHEN 'structural_token_change_review_required' THEN 0
                WHEN 'same_structural_tokens' THEN 1
                ELSE 2
            END,
            item.boundary_policy,
            item.relative_path,
            item.source_line_number,
            item.segment_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return int(total_row["count"] or 0), [enrich(dict(row)) for row in rows]


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    row["repair_route"] = repair_route(row)
    row["risk_level"] = risk_level(row)
    row["boundary_reasons"] = parse_reasons(row.get("reasons_json"))
    row["suggested_review_labels"] = suggested_review_labels(row)
    return row


def insert_queue(
    conn,
    *,
    boundary_policy_run_id: int,
    scope: str,
    rows: list[dict[str, Any]],
    candidate_count: int,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    token_counts = Counter(row["token_status"] for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_boundary_repair_queue_runs (
            rule_version,
            boundary_policy_run_id,
            queue_scope,
            candidate_count,
            selected_count,
            same_token_count,
            token_change_count,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            boundary_policy_run_id,
            scope,
            candidate_count,
            len(rows),
            token_counts["same_structural_tokens"],
            token_counts["structural_token_change_review_required"],
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for rank, row in enumerate(rows, start=1):
        row["queue_rank"] = rank
        item_cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_repair_queue_items (
                run_id,
                boundary_policy_run_id,
                boundary_policy_item_id,
                review_decision_id,
                diagnostic_run_id,
                diagnostic_item_id,
                queue_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                boundary_agent_key,
                boundary_policy,
                boundary_status,
                token_status,
                repair_route,
                risk_level,
                queue_rank,
                current_confirmed_text_hash,
                corrected_text_hash,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                boundary_policy_run_id,
                row["boundary_policy_item_id"],
                row["review_decision_id"],
                row.get("diagnostic_run_id"),
                row.get("diagnostic_item_id"),
                row.get("queue_item_id"),
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["boundary_agent_key"],
                row["boundary_policy"],
                row["boundary_status"],
                row["token_status"],
                row["repair_route"],
                row["risk_level"],
                rank,
                row.get("current_confirmed_text_hash"),
                row.get("corrected_text_hash"),
                json.dumps(row["boundary_reasons"], ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        row["repair_queue_item_id"] = int(item_cursor.lastrowid)
    return run_id


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    repair_queue_run_id: int,
    boundary_policy_run_id: int,
    scope: str,
    rows: list[dict[str, Any]],
    candidate_count: int,
    started_at: datetime,
) -> None:
    fieldnames = [
        "repair_queue_item_id",
        "queue_rank",
        "boundary_policy_item_id",
        "review_decision_id",
        "diagnostic_run_id",
        "diagnostic_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "boundary_agent_key",
        "boundary_policy",
        "boundary_status",
        "token_status",
        "repair_route",
        "risk_level",
        "suggested_review_labels",
        "boundary_reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "suggested_review_labels": json.dumps(row["suggested_review_labels"], ensure_ascii=False),
                    "boundary_reasons": json.dumps(row["boundary_reasons"], ensure_ascii=False),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "corrected_preview": short(row.get("corrected_text")),
                "notes": row.get("notes"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "repair_queue_item_id": row["repair_queue_item_id"],
                "boundary_policy_item_id": row["boundary_policy_item_id"],
                "review_decision_id": row["review_decision_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "boundary_policy": row["boundary_policy"],
                "repair_route": row["repair_route"],
                "risk_level": row["risk_level"],
                "suggested_review_labels": row["suggested_review_labels"],
                "decision": "pending",
                "human_label": "",
                "approved_for_repair_shadow": 0,
                "approved_for_token_policy_review": 0,
                "corrected_text": row.get("corrected_text") or "",
                "notes": "",
                "english_text": row.get("english_text") or "",
                "spanish_text": row.get("spanish_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_policy = Counter(row["boundary_policy"] for row in rows)
    by_route = Counter(row["repair_route"] for row in rows)
    by_risk = Counter(row["risk_level"] for row in rows)
    by_token = Counter(row["token_status"] for row in rows)
    lines = [
        "Auto-confirmation text boundary repair queue",
        f"Rule version: {RULE_VERSION}",
        f"Repair queue run id: {repair_queue_run_id}",
        f"Boundary policy run id: {boundary_policy_run_id}",
        f"Queue scope: {scope}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates in boundary policy scope: {candidate_count:,}",
        f"- Selected for repair review: {len(rows):,}",
        f"- By route: {json.dumps(dict(by_route), ensure_ascii=False, sort_keys=True)}",
        f"- By risk: {json.dumps(dict(by_risk), ensure_ascii=False, sort_keys=True)}",
        f"- By token status: {json.dumps(dict(by_token), ensure_ascii=False, sort_keys=True)}",
        f"- By boundary policy: {json.dumps(dict(by_policy), ensure_ascii=False, sort_keys=True)}",
        "",
        "Review labels:",
        "- accept_same_token_repair_shadow: corrected text preserves structural tokens and can feed a repair shadow.",
        "- accept_repair_after_token_policy: correction is useful but token structure must be governed first.",
        "- keep_boundary_only: keep as learned blocker without using the corrected text yet.",
        "- keep_manual_exception_only: valid human case, too contextual to generalize.",
        "- needs_subpolicy: split into a smaller neurônio before repair/promotion.",
        "- reject_repair_candidate: correction should not be used.",
        "",
        "Priority sample:",
    ]
    for row in rows[:50]:
        lines.extend(
            [
                (
                    f"- #{row['queue_rank']} {row['risk_level']} | {row['repair_route']} | "
                    f"{row['boundary_policy']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                f"  confirmed={short(row.get('confirmed_text'))}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Learning queue only: no source/output file reads, no model promotion, no confirmations changed, and no output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    boundary_policy_run_id: int | None = None,
    scope: str = DEFAULT_SCOPE,
    limit: int | None = None,
    include_existing: bool = False,
) -> dict[str, Any]:
    if scope not in SCOPE_STATUS:
        expected = ", ".join(sorted(SCOPE_STATUS))
        raise ValueError(f"Unknown scope={scope!r}. Expected one of: {expected}")
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_boundary_policy_run_id = boundary_policy_run_id or latest_boundary_policy_run_id(conn)
        candidate_count, rows = fetch_rows(
            conn,
            boundary_policy_run_id=selected_boundary_policy_run_id,
            scope=scope,
            include_existing=include_existing,
            limit=limit,
        )
        rows.sort(
            key=lambda row: (
                RISK_RANK.get(row["risk_level"], 9),
                row["repair_route"],
                row["boundary_policy"],
                row["relative_path"],
                int(row.get("source_line_number") or 0),
                int(row.get("segment_id") or 0),
            )
        )
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings, scope=scope)
        run_id = insert_queue(
            conn,
            boundary_policy_run_id=selected_boundary_policy_run_id,
            scope=scope,
            rows=rows,
            candidate_count=candidate_count,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            started_at=started_at,
        )
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            repair_queue_run_id=run_id,
            boundary_policy_run_id=selected_boundary_policy_run_id,
            scope=scope,
            rows=rows,
            candidate_count=candidate_count,
            started_at=started_at,
        )
        conn.commit()

    route_counts = Counter(row["repair_route"] for row in rows)
    policy_counts = Counter(row["boundary_policy"] for row in rows)
    print("[auto_confirmation_reopen_text_boundary_repair_queue] Queue generated")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Rule version: {RULE_VERSION}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Queue run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Boundary policy run id: {selected_boundary_policy_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Scope: {scope}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Candidates: {candidate_count:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Selected: {len(rows):,}")
    for key, value in route_counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_repair_queue] {key}: {value:,}")
    for key, value in policy_counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_repair_queue] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": run_id,
        "boundary_policy_run_id": selected_boundary_policy_run_id,
        "scope": scope,
        "candidate_count": candidate_count,
        "selected_count": len(rows),
        "route_counts": dict(route_counts),
        "policy_counts": dict(policy_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build learning-only repair queues from reviewed weak-auto boundary policies.")
    parser.add_argument("--boundary-policy-run-id", type=int, default=None)
    parser.add_argument("--scope", choices=sorted(SCOPE_STATUS), default=DEFAULT_SCOPE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        boundary_policy_run_id=args.boundary_policy_run_id,
        scope=args.scope,
        limit=args.limit,
        include_existing=args.include_existing,
    )
