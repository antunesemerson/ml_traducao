from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import issue_partial_coverage_report
import issue_review_queue


RULE_VERSION = "issue_composition_queue_v1"
DEFAULT_AGENT_KEY = "micro_dynamic_ck3_expression"
DEFAULT_LIMIT = 60
DEFAULT_PER_BUCKET = 15


def latest_id(conn, table_name: str, where_sql: str = "1 = 1") -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return float(part) / float(total) * 100


def short(value: str | None, limit: int = 240) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def report_paths(settings: dict[str, Any], agent_key: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_agent = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in agent_key)
    base = reports_dir / f"{stamp}_issue_composition_queue_{safe_agent}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def latest_partial_coverage_run(conn, *, partial_run_id: int | None) -> dict[str, Any]:
    if partial_run_id is None:
        partial_run_id = latest_id(conn, "ml_issue_partial_coverage_runs", "finished_at IS NOT NULL")
    if partial_run_id is None:
        raise RuntimeError("No finished ml_issue_partial_coverage_runs found. Run issue_partial_coverage_report first.")
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (partial_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_run_id}")
    return dict(row)


def composition_scope_sql(scope: str) -> str:
    if scope == "partial":
        return "pci.coverage_state = 'partial'"
    if scope == "full":
        return "pci.coverage_state = 'full'"
    if scope == "partial_or_reviewed":
        return "(pci.coverage_state = 'partial' OR pci.review_state <> 'review_none')"
    if scope == "not_uncovered":
        return "pci.coverage_state <> 'none'"
    raise ValueError(f"Unsupported composition scope: {scope}")


def fetch_candidates(
    conn,
    *,
    partial_run: dict[str, Any],
    agent_key: str,
    issue_family: str | None,
    scope: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    ledger_run_id = int(partial_run["ledger_run_id"])
    covered_evidence, blocked_evidence = issue_partial_coverage_report.fetch_exact_policy_evidence(
        conn,
        ledger_run_id=ledger_run_id,
    )
    covered_ids = set(covered_evidence)
    blocked_ids = set(blocked_evidence)
    existing_sql = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.ledger_item_id = item.id
                AND queued.agent_key = item.agent_key
          )
        """
    )
    family_sql = "AND item.issue_family = ?" if issue_family else ""
    params: list[Any] = [int(partial_run["id"]), ledger_run_id, agent_key]
    if issue_family:
        params.append(issue_family)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            pci.coverage_state AS composition_coverage_state,
            pci.review_state AS composition_review_state,
            pci.total_issue_count AS composition_total_issue_count,
            pci.covered_issue_count AS composition_covered_issue_count,
            pci.reviewed_issue_count AS composition_reviewed_issue_count,
            pci.open_issue_count AS composition_open_issue_count,
            pci.coverage_ratio AS composition_coverage_ratio,
            pci.issue_families_json AS composition_issue_families_json,
            pci.covered_families_json AS composition_covered_families_json,
            pci.open_families_json AS composition_open_families_json,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_partial_coverage_items pci
        JOIN ml_issue_ledger_items item
          ON item.run_id = ?
         AND item.segment_id = pci.segment_id
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.id = (
            SELECT c.id
            FROM segment_confirmations c
            WHERE c.segment_id = item.segment_id
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT 1
        )
        WHERE pci.run_id = ?
          AND item.agent_key = ?
          AND item.status = 'open'
          AND {composition_scope_sql(scope)}
          {family_sql}
          {existing_sql}
        ORDER BY pci.coverage_state, pci.open_issue_count, item.segment_id, item.issue_family, item.issue_kind
        """,
        tuple([params[1], params[0], *params[2:]]),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        ledger_item_id = int(payload["id"])
        if ledger_item_id in covered_ids or ledger_item_id in blocked_ids:
            continue
        open_families = parse_json(payload.get("composition_open_families_json"), {})
        if payload["issue_family"] not in open_families:
            continue
        evidence = parse_json(payload.get("evidence_json"), {})
        evidence["_composition"] = {
            "partial_coverage_run_id": int(partial_run["id"]),
            "coverage_state": payload.get("composition_coverage_state"),
            "review_state": payload.get("composition_review_state"),
            "total_issue_count": int(payload.get("composition_total_issue_count") or 0),
            "covered_issue_count": int(payload.get("composition_covered_issue_count") or 0),
            "open_issue_count": int(payload.get("composition_open_issue_count") or 0),
            "coverage_ratio": float(payload.get("composition_coverage_ratio") or 0.0),
            "issue_families": parse_json(payload.get("composition_issue_families_json"), {}),
            "covered_families": parse_json(payload.get("composition_covered_families_json"), {}),
            "open_families": open_families,
        }
        payload["evidence_json"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        candidates.append(payload)
    return candidates


def composition_priority(row: dict[str, Any]) -> float:
    base = float(issue_review_queue.priority_score(row))
    total = int(row.get("composition_total_issue_count") or 0)
    covered = int(row.get("composition_covered_issue_count") or 0)
    open_count = int(row.get("composition_open_issue_count") or 0)
    ratio = float(row.get("composition_coverage_ratio") or 0.0)
    score = base
    score += ratio * 600
    score += max(0, 8 - open_count) * 90
    score += covered * 60
    score += total * 15
    if open_count == 1:
        score += 450
    elif open_count == 2:
        score += 300
    elif open_count <= 4:
        score += 150
    score += int(row.get("segment_id") or 0) % 19 / 100
    return round(score, 4)


def select_rows(candidates: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    for row in candidates:
        row["queue_bucket"] = issue_review_queue.queue_bucket(row)
        row["priority_score"] = composition_priority(row)
        row["suggested_decision"] = issue_review_queue.suggested_decision(row)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(
            key=lambda item: (
                -float(item["priority_score"]),
                int(item.get("composition_open_issue_count") or 999),
                item["relative_path"],
                item["source_key"],
            )
        )

    bucket_order = issue_review_queue.DYNAMIC_BUCKET_ORDER.copy()
    bucket_order = [bucket for bucket in bucket_order if grouped.get(bucket)]
    bucket_order.extend(sorted(bucket for bucket in grouped if bucket not in set(bucket_order)))

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for index in range(per_bucket):
        for bucket in bucket_order:
            rows = grouped.get(bucket, [])
            if index >= len(rows):
                continue
            if len(selected) >= limit:
                break
            row = rows[index]
            selected.append(row)
            selected_ids.add(int(row["id"]))
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        remaining = [row for row in candidates if int(row["id"]) not in selected_ids]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"], item["relative_path"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
            selected_ids.add(int(row["id"]))
    return selected


def insert_queue_run(
    conn,
    *,
    partial_run: dict[str, Any],
    agent_key: str,
    issue_family: str | None,
    scope: str,
    limit: int,
    per_bucket: int,
    candidates_count: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    return int(
        conn.execute(
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
                int(partial_run["ledger_run_id"]),
                agent_key,
                issue_family,
                "partial_coverage_composition",
                limit,
                per_bucket,
                len(selected),
                len(selected),
                json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_template_path),
                now,
                now,
                now,
            ),
        ).lastrowid
    )


def insert_queue_items(conn, *, queue_run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    issue_review_queue.insert_queue_items(conn, queue_run_id, ledger_run_id, rows)


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    partial_run: dict[str, Any],
    agent_key: str,
    rows: list[dict[str, Any]],
    candidates_count: int,
    limit: int,
    per_bucket: int,
    scope: str,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    open_combo_counts = Counter(row.get("composition_open_families_json") or "{}" for row in rows)
    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "issue_family",
        "issue_kind",
        "composition_total_issue_count",
        "composition_covered_issue_count",
        "composition_open_issue_count",
        "composition_coverage_ratio",
        "composition_open_families_json",
        "token_impact",
        "token_status",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "ledger_item_id": row["id"],
                    **{name: row.get(name) for name in fieldnames if name not in {"queue_run_id", "ledger_item_id"}},
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_run_id": int(partial_run["ledger_run_id"]),
                "partial_coverage_run_id": int(partial_run["id"]),
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": agent_key,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "composition": {
                    "coverage_state": row.get("composition_coverage_state"),
                    "review_state": row.get("composition_review_state"),
                    "total_issue_count": row.get("composition_total_issue_count"),
                    "covered_issue_count": row.get("composition_covered_issue_count"),
                    "open_issue_count": row.get("composition_open_issue_count"),
                    "coverage_ratio": row.get("composition_coverage_ratio"),
                    "open_families": parse_json(row.get("composition_open_families_json"), {}),
                    "covered_families": parse_json(row.get("composition_covered_families_json"), {}),
                },
                "evidence": parse_json(row.get("evidence_json"), {}),
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "evidence_text": row.get("evidence_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "decision": "",
                "decision_options": issue_review_queue.decision_options_for_agent(agent_key),
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue composition queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Partial coverage run id: {partial_run['id']}",
        f"Ledger run id: {partial_run['ledger_run_id']}",
        f"Agent: {agent_key}",
        f"Scope: {scope}",
        "",
        "Selection:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(rows):,} ({percent(len(rows), candidates_count):.2f}%)",
        f"- Limit: {limit:,}",
        f"- Per bucket: {per_bucket:,}",
        "",
        "Composition context:",
        f"- Partial coverage segments in source run: {int(partial_run['partially_covered_segments'] or 0):,}",
        f"- Fully covered segments in source run: {int(partial_run['fully_covered_segments'] or 0):,}",
        f"- Uncovered segments in source run: {int(partial_run['uncovered_segments'] or 0):,}",
        "",
        "Buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Open-family combos:",
        *[f"- {combo}: {count:,}" for combo, count in open_combo_counts.most_common(12)],
        "",
        "Review guidance:",
        "- Review this as a composition queue: the segment already has at least one covered issue.",
        "- Focus on whether this microagent can classify or repair its own pattern, not the whole segment.",
        "- Mark `needs_new_microagent` when the issue is too broad and a narrower reusable neuron is visible.",
        "- Do not apply output from this queue; it is evidence for the learning front.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in rows[:25]:
        lines.append(
            (
                f"- ledger {row['id']} | segment {row['segment_id']} | {row['queue_bucket']} | "
                f"coverage={int(row.get('composition_covered_issue_count') or 0)}/"
                f"{int(row.get('composition_total_issue_count') or 0)} | "
                f"open={int(row.get('composition_open_issue_count') or 0)} | "
                f"{row['relative_path']}::{row['source_key']}"
            )
        )
        lines.append(f"  open_families: {row.get('composition_open_families_json')}")
        lines.append(f"  evidence: {short(row.get('evidence_text'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    agent_key: str = DEFAULT_AGENT_KEY,
    issue_family: str | None = None,
    partial_run_id: int | None = None,
    scope: str = "partial",
    limit: int | None = None,
    per_bucket: int = DEFAULT_PER_BUCKET,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    selected_limit = limit or DEFAULT_LIMIT
    paths = report_paths(settings, agent_key)
    print("[issue_composition_queue] Starting issue composition queue")
    print(f"[issue_composition_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_composition_queue] Agent: {agent_key}")
    print(f"[issue_composition_queue] Scope: {scope}")
    print(f"[issue_composition_queue] Limit: {selected_limit}")
    print(f"[issue_composition_queue] Per bucket: {per_bucket}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        partial_run = latest_partial_coverage_run(conn, partial_run_id=partial_run_id)
        candidates = fetch_candidates(
            conn,
            partial_run=partial_run,
            agent_key=agent_key,
            issue_family=issue_family,
            scope=scope,
            include_existing=include_existing,
        )
        selected = select_rows(candidates, limit=selected_limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            partial_run=partial_run,
            agent_key=agent_key,
            issue_family=issue_family,
            scope=scope,
            limit=selected_limit,
            per_bucket=per_bucket,
            candidates_count=len(candidates),
            selected=selected,
            paths=paths,
        )
        insert_queue_items(conn, queue_run_id=queue_run_id, ledger_run_id=int(partial_run["ledger_run_id"]), rows=selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        partial_run=partial_run,
        agent_key=agent_key,
        rows=selected,
        candidates_count=len(candidates),
        limit=selected_limit,
        per_bucket=per_bucket,
        scope=scope,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print(f"[issue_composition_queue] Queue run id: {queue_run_id}")
    print(f"[issue_composition_queue] Partial coverage run id: {partial_run['id']}")
    print(f"[issue_composition_queue] Candidates: {len(candidates)}")
    print(f"[issue_composition_queue] Selected: {len(selected)}")
    print(f"[issue_composition_queue] Report: {txt_path}")
    print(f"[issue_composition_queue] CSV: {csv_path}")
    print(f"[issue_composition_queue] JSONL: {jsonl_path}")
    print(f"[issue_composition_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "partial_coverage_run_id": int(partial_run["id"]),
        "ledger_run_id": int(partial_run["ledger_run_id"]),
        "agent_key": agent_key,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a composition review queue from partial issue coverage.")
    parser.add_argument("--agent-key", default=DEFAULT_AGENT_KEY)
    parser.add_argument("--issue-family", default=None)
    parser.add_argument("--partial-run-id", type=int, default=None)
    parser.add_argument(
        "--scope",
        choices=["partial", "full", "partial_or_reviewed", "not_uncovered"],
        default="partial",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        agent_key=args.agent_key,
        issue_family=args.issue_family,
        partial_run_id=args.partial_run_id,
        scope=args.scope,
        limit=args.limit,
        per_bucket=args.per_bucket,
        include_existing=args.include_existing,
    )
