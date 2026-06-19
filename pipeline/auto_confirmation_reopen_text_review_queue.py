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


RULE_VERSION = "auto_confirmation_reopen_text_review_queue_v1"
DEFAULT_AGENT_KEY = "nickname_select_cstring_literal_specialist"
DEFAULT_SUBFAMILY = "nickname_dynamic_literal_replacement"


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_diagnostic_runs
        WHERE finished_at IS NOT NULL
          AND total_items > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No text diagnostic run found.")
    return int(row["id"])


def slug(text: str) -> str:
    allowed = []
    for char in text.lower():
        if char.isalnum():
            allowed.append(char)
        elif char in {"_", "-", " "}:
            allowed.append("_")
    result = "".join(allowed).strip("_")
    while "__" in result:
        result = result.replace("__", "_")
    return result or "text_review"


def report_paths(settings: dict[str, Any], *, agent_key: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_review_queue_{slug(agent_key)}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template.jsonl"),
    )


def risk_rank_sql() -> str:
    return """
        CASE item.risk_level
            WHEN 'critical' THEN 0
            WHEN 'high' THEN 1
            WHEN 'medium' THEN 2
            WHEN 'low' THEN 3
            ELSE 4
        END
    """


def total_candidates(
    conn,
    *,
    diagnostic_run_id: int,
    agent_key: str,
    text_subfamily: str | None,
) -> int:
    params: list[Any] = [diagnostic_run_id, agent_key]
    subfamily_sql = ""
    if text_subfamily:
        subfamily_sql = "AND item.text_subfamily = ?"
        params.append(text_subfamily)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM auto_confirmation_reopen_text_diagnostic_items item
        WHERE item.run_id = ?
          AND item.suggested_agent_key = ?
          {subfamily_sql}
        """,
        params,
    ).fetchone()
    return int(row["total"] or 0)


def fetch_candidates(
    conn,
    *,
    diagnostic_run_id: int,
    agent_key: str,
    text_subfamily: str | None,
    limit: int | None,
    skip_previously_queued: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = [diagnostic_run_id, agent_key]
    subfamily_sql = ""
    if text_subfamily:
        subfamily_sql = "AND item.text_subfamily = ?"
        params.append(text_subfamily)
    skip_sql = ""
    if skip_previously_queued:
        skip_sql = """
          AND NOT EXISTS (
              SELECT 1
              FROM auto_confirmation_reopen_text_review_queue_items queued
              JOIN auto_confirmation_reopen_text_review_queue_runs run
                ON run.id = queued.run_id
              WHERE queued.diagnostic_item_id = item.id
                AND run.diagnostic_run_id = item.run_id
                AND run.agent_key = item.suggested_agent_key
          )
        """
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_diagnostic_items item
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
          AND item.suggested_agent_key = ?
          {subfamily_sql}
          {skip_sql}
        ORDER BY
          {risk_rank_sql()},
          item.spanish_literal_hint_count DESC,
          item.issue_count DESC,
          item.review_priority DESC,
          item.relative_path,
          item.source_line_number
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def suggested_review_label(row: dict[str, Any]) -> str:
    if int(row.get("issue_count") or 0) > 0:
        return "manual_boundary"
    if int(row.get("spanish_literal_hint_count") or 0) > 0:
        return "likely_needs_fix"
    if int(row.get("select_cstring_count") or 0) >= 3:
        return "dynamic_context_review"
    return "needs_review"


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    queue_run_id: int,
    diagnostic_run_id: int,
    agent_key: str,
    text_subfamily: str | None,
    rows: list[dict[str, Any]],
    total_count: int,
    skipped_count: int,
    started_at: datetime,
) -> None:
    fieldnames = [
        "queue_item_id",
        "queue_rank",
        "diagnostic_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "confirmation_label",
        "text_subfamily",
        "suggested_agent_key",
        "risk_level",
        "select_cstring_count",
        "spanish_literal_hint_count",
        "issue_count",
        "model_safe_probability",
        "review_priority",
        "suggested_review_label",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "reasons": json.loads(row.get("reasons_json") or "[]"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "diagnostic_item_id": row["diagnostic_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": agent_key,
                "text_subfamily": row["text_subfamily"],
                "risk_level": row["risk_level"],
                "suggested_review_label": row["suggested_review_label"],
                "decision": "pending",
                "human_label": "",
                "corrected_text": "",
                "notes": "",
                "english_text": row.get("english_text") or "",
                "spanish_text": row.get("spanish_text") or "",
                "current_output_text": row.get("portuguese_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_risk = Counter(row["risk_level"] for row in rows)
    by_label = Counter(row["suggested_review_label"] for row in rows)
    lines = [
        "Auto-confirmation text review queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Agent: {agent_key}",
        f"Subfamily: {text_subfamily or 'all'}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Total candidates in diagnostic scope: {total_count:,}",
        f"- Selected for this queue: {len(rows):,}",
        f"- Skipped previously queued: {skipped_count:,}",
        "",
        "By risk:",
        *[f"- {key}: {value:,}" for key, value in by_risk.most_common()],
        "",
        "By suggested review label:",
        *[f"- {key}: {value:,}" for key, value in by_label.most_common()],
        "",
        "Priority samples:",
    ]
    for row in rows[:25]:
        lines.extend(
            [
                (
                    f"- #{row['queue_rank']} {row['risk_level']} | "
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                ),
                (
                    "  "
                    f"label={row['suggested_review_label']}; "
                    f"select={row['select_cstring_count']}; "
                    f"hints={row['spanish_literal_hint_count']}; "
                    f"issue={row['issue_count']}"
                ),
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This queue is for human/learning review only.",
            "- It does not change confirmations, train models, promote policies, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    diagnostic_run_id: int | None = None,
    agent_key: str = DEFAULT_AGENT_KEY,
    text_subfamily: str | None = DEFAULT_SUBFAMILY,
    limit: int | None = 25,
    skip_previously_queued: bool = True,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        total_count = total_candidates(
            conn,
            diagnostic_run_id=selected_diagnostic_run_id,
            agent_key=agent_key,
            text_subfamily=text_subfamily,
        )
        rows = fetch_candidates(
            conn,
            diagnostic_run_id=selected_diagnostic_run_id,
            agent_key=agent_key,
            text_subfamily=text_subfamily,
            limit=limit,
            skip_previously_queued=skip_previously_queued,
        )
        available_count = len(
            fetch_candidates(
                conn,
                diagnostic_run_id=selected_diagnostic_run_id,
                agent_key=agent_key,
                text_subfamily=text_subfamily,
                limit=None,
                skip_previously_queued=skip_previously_queued,
            )
        )
        skipped_count = max(total_count - available_count, 0) if skip_previously_queued else 0
        now = datetime.now().isoformat(timespec="seconds")
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings, agent_key=agent_key)
        high_count = sum(1 for row in rows if row["risk_level"] == "high")
        medium_count = sum(1 for row in rows if row["risk_level"] == "medium")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_review_queue_runs (
                rule_version,
                diagnostic_run_id,
                agent_key,
                text_subfamily,
                total_candidates,
                selected_count,
                high_risk_count,
                medium_risk_count,
                skipped_previously_queued_count,
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_diagnostic_run_id,
                agent_key,
                text_subfamily,
                total_count,
                len(rows),
                high_count,
                medium_count,
                skipped_count,
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        queue_run_id = int(cursor.lastrowid)
        for rank, row in enumerate(rows, start=1):
            row["queue_rank"] = rank
            row["diagnostic_item_id"] = row["id"]
            row["suggested_review_label"] = suggested_review_label(row)
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_review_queue_items (
                    run_id,
                    diagnostic_run_id,
                    diagnostic_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    confirmation_label,
                    text_subfamily,
                    suggested_agent_key,
                    risk_level,
                    select_cstring_count,
                    spanish_literal_hint_count,
                    issue_count,
                    model_safe_probability,
                    review_priority,
                    queue_rank,
                    suggested_review_label,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_run_id,
                    selected_diagnostic_run_id,
                    row["diagnostic_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row.get("confirmation_label"),
                    row["text_subfamily"],
                    row["suggested_agent_key"],
                    row["risk_level"],
                    int(row.get("select_cstring_count") or 0),
                    int(row.get("spanish_literal_hint_count") or 0),
                    int(row.get("issue_count") or 0),
                    row.get("model_safe_probability"),
                    float(row.get("review_priority") or 0),
                    rank,
                    row["suggested_review_label"],
                    now,
                ),
            )
            row["queue_item_id"] = int(item_cursor.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            queue_run_id=queue_run_id,
            diagnostic_run_id=selected_diagnostic_run_id,
            agent_key=agent_key,
            text_subfamily=text_subfamily,
            rows=rows,
            total_count=total_count,
            skipped_count=skipped_count,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_review_queue] Queue generated")
    print(f"[auto_confirmation_reopen_text_review_queue] Queue run id: {queue_run_id}")
    print(f"[auto_confirmation_reopen_text_review_queue] Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"[auto_confirmation_reopen_text_review_queue] Agent: {agent_key}")
    print(f"[auto_confirmation_reopen_text_review_queue] Subfamily: {text_subfamily or 'all'}")
    print(f"[auto_confirmation_reopen_text_review_queue] Total candidates: {total_count:,}")
    print(f"[auto_confirmation_reopen_text_review_queue] Selected: {len(rows):,}")
    print(f"[auto_confirmation_reopen_text_review_queue] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_review_queue] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_review_queue] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_text_review_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "agent_key": agent_key,
        "text_subfamily": text_subfamily,
        "total_candidates": total_count,
        "selected_count": len(rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a focused text-replacement review queue.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default=DEFAULT_AGENT_KEY)
    parser.add_argument("--text-subfamily", default=DEFAULT_SUBFAMILY)
    parser.add_argument("--all-subfamilies", action="store_true")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--include-previously-queued", action="store_true")
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        agent_key=args.agent_key,
        text_subfamily=None if args.all_subfamilies else args.text_subfamily,
        limit=args.limit if args.limit > 0 else None,
        skip_previously_queued=not args.include_previously_queued,
    )
