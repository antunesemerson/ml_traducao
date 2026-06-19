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
from auto_confirmation_reopen_text_diagnostic import classify_weak_auto_row


RULE_VERSION = "auto_confirmation_reopen_weak_boundary_queue_v1"
LABEL_FAMILY = "weak_auto_confirmation"

BOUNDARIES = {
    "source_visible": {
        "agent_keys": {"weak_auto_token_surface_semantic_boundary"},
        "subfamilies": {"weak_auto_token_surface_semantic_delta"},
        "recommendation": "weak_auto_source_visible_boundary_evidence",
        "suggested_decision": "review_source_visible_semantic_delta",
        "review_guidance": (
            "The candidate/output surface is token-only, but English or Spanish still has visible semantic text. "
            "Decide whether the visible text is intentionally moved into CK3 tokens, safely redundant, or a semantic deletion."
        ),
    },
    "embedded_literal": {
        "agent_keys": {"weak_auto_embedded_literal_token_specialist"},
        "subfamilies": {"weak_auto_embedded_literal_token_exact", "weak_auto_embedded_spanish_literal_token"},
        "recommendation": "weak_auto_embedded_literal_boundary_evidence",
        "suggested_decision": "review_embedded_literal_token_payload",
        "review_guidance": (
            "The surface has no letters outside CK3 tokens, but Select_CString/LocalPlayerString/Concept payloads may contain visible grammar. "
            "Inspect literals inside token constructors before marking it safe."
        ),
    },
    "custom_loc": {
        "agent_keys": {"weak_auto_custom_loc_helper_boundary"},
        "subfamilies": {"weak_auto_custom_loc_helper_token"},
        "recommendation": "weak_auto_custom_loc_boundary_evidence",
        "suggested_decision": "review_es_custom_loc_runtime_helper",
        "review_guidance": (
            "The surface is token-only but depends on ES custom-localization helpers. "
            "Decide whether the runtime helper is safe in PT-BR or requires a separate manual exception."
        ),
    },
}


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete auto_confirmation_reopen_audit_runs entry found.")
    return int(row["id"])


def slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value).strip("_") or "weak_boundary"


def report_paths(settings: dict[str, Any], *, boundary: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_weak_boundary_queue_{slug(boundary)}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def fetch_rows(conn, *, audit_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_audit_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.label_family = ?
        ORDER BY
            item.review_priority DESC,
            item.relative_path,
            item.source_line_number,
            item.segment_id
        """,
        (audit_run_id, LABEL_FAMILY),
    ).fetchall()
    return [dict(row) for row in rows]


def reviewed_or_queued_segment_ids(conn, *, agent_keys: set[str], subfamilies: set[str]) -> set[int]:
    agent_params = ",".join("?" for _ in agent_keys)
    subfamily_params = ",".join("?" for _ in subfamilies)
    params = tuple(sorted(agent_keys)) + tuple(sorted(subfamilies))
    rows = conn.execute(
        f"""
        SELECT DISTINCT segment_id
        FROM auto_confirmation_reopen_text_review_decisions
        WHERE agent_key IN ({agent_params})
          AND text_subfamily IN ({subfamily_params})
        UNION
        SELECT DISTINCT item.segment_id
        FROM auto_confirmation_reopen_text_review_queue_items item
        JOIN auto_confirmation_reopen_text_review_queue_runs run ON run.id = item.run_id
        WHERE run.agent_key IN ({agent_params})
          AND item.text_subfamily IN ({subfamily_params})
        """,
        params + params,
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    subfamily, agent, recommendation, risk, reasons = classify_weak_auto_row(row)
    row["text_subfamily"] = subfamily
    row["suggested_agent_key"] = agent
    row["diagnostic_recommendation"] = recommendation
    row["risk_level"] = risk
    row["split_reasons"] = reasons
    return row


def select_rows(
    rows: list[dict[str, Any]],
    *,
    boundary: str,
    limit: int | None,
    skip_existing: bool,
    existing_segment_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    spec = BOUNDARIES[boundary]
    candidates = [
        row
        for row in rows
        if row["suggested_agent_key"] in spec["agent_keys"]
        and row["text_subfamily"] in spec["subfamilies"]
        and (not skip_existing or int(row["segment_id"]) not in existing_segment_ids)
    ]
    candidates.sort(
        key=lambda item: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item["risk_level"], 4),
            -float(item.get("review_priority") or 0),
            item.get("relative_path") or "",
            int(item.get("source_line_number") or 0),
            int(item.get("segment_id") or 0),
        )
    )
    selected = candidates if limit is None else candidates[:limit]
    for rank, row in enumerate(selected, start=1):
        row["queue_rank"] = rank
        row["recommendation"] = spec["recommendation"]
        row["suggested_decision"] = spec["suggested_decision"]
        row["policy_candidate"] = 0
        row["manual_boundary"] = 1
        row["reasons_json"] = json.dumps(
            {
                "rule_version": RULE_VERSION,
                "boundary": boundary,
                "text_subfamily": row["text_subfamily"],
                "suggested_agent_key": row["suggested_agent_key"],
                "risk_level": row["risk_level"],
                "split_reasons": row["split_reasons"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return candidates, selected


def insert_queue(
    conn,
    *,
    audit_run_id: int,
    boundary: str,
    rows: list[dict[str, Any]],
    candidate_total: int,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    match_counts = Counter(row.get("output_match_kind") or "" for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_guarded_queue_runs (
            rule_version,
            audit_run_id,
            label_family,
            recommendation_filter,
            candidate_count,
            selected_count,
            guarded_policy_candidate_count,
            manual_boundary_count,
            exact_output_match_count,
            display_equivalent_count,
            text_delta_count,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            audit_run_id,
            LABEL_FAMILY,
            BOUNDARIES[boundary]["recommendation"],
            candidate_total,
            len(rows),
            0,
            len(rows),
            match_counts["exact_match"],
            match_counts["display_equivalent_escape_delta"],
            match_counts["text_delta"],
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
    for row in rows:
        item_cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_guarded_queue_items (
                run_id,
                audit_run_id,
                audit_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                confirmation_label,
                label_family,
                recommendation,
                suggested_decision,
                policy_candidate,
                manual_boundary,
                output_match_kind,
                token_status,
                issue_count,
                high_issue_count,
                word_count,
                model_safe_probability,
                model_confidence,
                review_priority,
                queue_rank,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                audit_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row.get("confirmation_label"),
                LABEL_FAMILY,
                row["recommendation"],
                row["suggested_decision"],
                int(row["policy_candidate"]),
                int(row["manual_boundary"]),
                row.get("output_match_kind"),
                row.get("token_status"),
                int(row.get("issue_count") or 0),
                int(row.get("high_issue_count") or 0),
                int(row.get("word_count") or 0),
                row.get("model_safe_probability"),
                row.get("model_confidence"),
                float(row.get("review_priority") or 0),
                int(row["queue_rank"]),
                row["reasons_json"],
                now,
            ),
        )
        row["queue_item_id"] = int(item_cursor.lastrowid)
    return run_id


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    run_id: int,
    audit_run_id: int,
    boundary: str,
    candidates: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    spec = BOUNDARIES[boundary]
    fieldnames = [
        "queue_item_id",
        "queue_rank",
        "audit_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "confirmation_label",
        "label_family",
        "recommendation",
        "suggested_decision",
        "text_subfamily",
        "suggested_agent_key",
        "risk_level",
        "output_match_kind",
        "token_status",
        "issue_count",
        "high_issue_count",
        "word_count",
        "model_safe_probability",
        "review_priority",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["audit_item_id"] = row.get("id")
            writer.writerow(payload)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "audit_item_id": row.get("id"),
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "reasons": row["split_reasons"],
                "queue_guidance": spec["review_guidance"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": row["suggested_agent_key"],
                "text_subfamily": row["text_subfamily"],
                "risk_level": row["risk_level"],
                "suggested_decision": row["suggested_decision"],
                "decision": "pending",
                "allowed_decisions": [
                    "positive_evidence",
                    "negative_boundary",
                    "needs_more_context",
                    "manual_exception",
                ],
                "corrected_text": "",
                "notes": "",
                "english_text": row.get("english_text") or "",
                "spanish_text": row.get("spanish_text") or "",
                "current_output_text": row.get("portuguese_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_subfamily = Counter(row["text_subfamily"] for row in candidates)
    by_path = Counter(row["relative_path"] for row in candidates)
    selected_by_path = Counter(row["relative_path"] for row in rows)
    lines = [
        "Weak auto boundary review queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {run_id}",
        f"Audit run id: {audit_run_id}",
        f"Boundary: {boundary}",
        f"Agent(s): {', '.join(sorted(spec['agent_keys']))}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Boundary candidates: {len(candidates):,}",
        f"- Selected rows: {len(rows):,}",
        f"- Recommendation: {spec['recommendation']}",
        f"- Suggested decision: {spec['suggested_decision']}",
        "",
        "Candidate subfamilies:",
        *[f"- {key}: {value:,}" for key, value in by_subfamily.most_common()],
        "",
        "Top candidate paths:",
        *[f"- {key}: {value:,}" for key, value in by_path.most_common(20)],
        "",
        "Top selected paths:",
        *[f"- {key}: {value:,}" for key, value in selected_by_path.most_common(20)],
        "",
        "Review guidance:",
        f"- {spec['review_guidance']}",
        "",
        "Review sample:",
    ]
    for row in rows[:40]:
        lines.extend(
            [
                f"- #{row['queue_rank']} | {row['risk_level']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  subfamily={row['text_subfamily']}; reasons={', '.join(row['split_reasons'])}",
                f"  EN: {short(row.get('english_text'))}",
                f"  ES: {short(row.get('spanish_text'))}",
                f"  OUT: {short(row.get('portuguese_text'))}",
                f"  CONF: {short(row.get('confirmed_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This queue records review evidence only.",
            "- It does not close lifecycle states, alter confirmations, train models, promote policies, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    audit_run_id: int | None = None,
    boundary: str = "source_visible",
    limit: int | None = None,
    skip_existing: bool = True,
) -> dict[str, Any]:
    if boundary not in BOUNDARIES:
        expected = ", ".join(sorted(BOUNDARIES))
        raise ValueError(f"Unknown boundary={boundary!r}. Expected one of: {expected}")
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        spec = BOUNDARIES[boundary]
        existing_segment_ids = reviewed_or_queued_segment_ids(
            conn,
            agent_keys=spec["agent_keys"],
            subfamilies=spec["subfamilies"],
        )
        enriched_rows = [enrich(row) for row in fetch_rows(conn, audit_run_id=selected_audit_run_id)]
        candidates, rows = select_rows(
            enriched_rows,
            boundary=boundary,
            limit=limit,
            skip_existing=skip_existing,
            existing_segment_ids=existing_segment_ids,
        )
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings, boundary=boundary)
        run_id = insert_queue(
            conn,
            audit_run_id=selected_audit_run_id,
            boundary=boundary,
            rows=rows,
            candidate_total=len(candidates),
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
            run_id=run_id,
            audit_run_id=selected_audit_run_id,
            boundary=boundary,
            candidates=candidates,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_weak_boundary_queue] Queue generated")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Boundary: {boundary}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Audit run id: {selected_audit_run_id}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Candidates: {len(candidates):,}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Selected: {len(rows):,}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_weak_boundary_queue] Decisions template: {decisions_template_path}")
    return {
        "run_id": run_id,
        "audit_run_id": selected_audit_run_id,
        "boundary": boundary,
        "candidate_total": len(candidates),
        "selected_count": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a focused review queue for weak-auto boundary subagents.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--boundary", choices=sorted(BOUNDARIES), default="source_visible")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        audit_run_id=args.audit_run_id,
        boundary=args.boundary,
        limit=args.limit,
        skip_existing=not args.include_existing,
    )
