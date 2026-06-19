from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import issue_partial_coverage_report


RULE_VERSION = "issue_governed_blocker_queue_v1"
QUEUE_AGENT_KEY = "coordinator_governed_blocker_queue"
QUEUE_STRATEGY = "partial_coverage_governed_blockers"


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_partial_coverage_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished partial coverage run found.")
    return int(row["id"])


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_governed_blocker_queue_run_{partial_coverage_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def remaining_families(row: dict[str, Any]) -> dict[str, int]:
    issue_families = parse_json_dict(row.get("issue_families_json"))
    covered_families = parse_json_dict(row.get("covered_families_json"))
    remaining: dict[str, int] = {}
    for family, total in issue_families.items():
        count = int(total or 0) - int(covered_families.get(family) or 0)
        if count > 0:
            remaining[str(family)] = count
    return remaining


def classify_bucket(row: dict[str, Any]) -> tuple[str, str, float]:
    family = str(row.get("issue_family") or "")
    issue_kind = str(row.get("issue_kind") or "")
    source_key = str(row.get("source_key") or "")
    text = str(row.get("evidence_text") or "")
    relative_path = str(row.get("relative_path") or "")

    if family == "culture_semantic_microagent":
        return "culture_unclassified_context", "review_culture_context_policy_or_repair", 980.0
    if family == "high_issue_auditor":
        return "high_issue_followup", "audit_after_context_policy_decision", 860.0
    if family == "surface_boundary_microagent":
        if any(marker in text for marker in ("sois", "son", "encarcelad")):
            return "dynamic_token_literal_residual", "create_dynamic_literal_repair_subpolicy", 960.0
        return "surface_boundary_context", "review_surface_boundary_policy", 820.0
    if family == "nickname_name_policy":
        if relative_path == "nicknames_l_spanish.yml" and not source_key.endswith("_desc"):
            return "nickname_title_gender_boundary", "review_nickname_title_gender_token_policy", 940.0
        return "nickname_context_policy", "review_nickname_context_policy", 880.0
    if family == "semantic_review_router":
        if source_key == "MY_REALM_WINDOW_BOOKMARK_SUBJECTS_TT" or "Súbditos" in text:
            return "visible_ui_label_residual", "repair_visible_ui_label_ptbr", 970.0
        if issue_kind == "needs_human_or_semantic_conflict" and any(
            marker in text for marker in ("sois", "son", "encarcelad")
        ):
            return "semantic_dynamic_literal_residual", "route_to_dynamic_literal_repair", 950.0
        if relative_path == "nicknames_l_spanish.yml":
            return "nickname_semantic_title_policy", "review_nickname_semantic_title_policy", 930.0
        return "semantic_context_policy", "review_semantic_context_policy", 840.0
    return "governed_blocker_general", "review_governed_blocker", 700.0


def fetch_candidates(conn, *, partial_coverage_run_id: int) -> tuple[int, list[dict[str, Any]]]:
    run = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (partial_coverage_run_id,),
    ).fetchone()
    if row_missing := run is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_coverage_run_id}")
    ledger_run_id = int(run["ledger_run_id"])
    covered_evidence, _blocked_evidence = issue_partial_coverage_report.fetch_exact_policy_evidence(
        conn,
        ledger_run_id=ledger_run_id,
    )
    partial_rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND coverage_state = 'partial'
        ORDER BY open_issue_count DESC, coverage_ratio ASC, segment_id
        """,
        (partial_coverage_run_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for partial_row in partial_rows:
        partial = dict(partial_row)
        remaining = remaining_families(partial)
        if not remaining:
            continue
        ledger_items = conn.execute(
            """
            SELECT
                item.*,
                source.english_text,
                source.spanish_text,
                confirmation.confirmed_text
            FROM ml_issue_ledger_items item
            JOIN source_segments source ON source.id = item.segment_id
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.id = (
                  SELECT c2.id
                  FROM segment_confirmations c2
                  WHERE c2.segment_id = item.segment_id
                  ORDER BY c2.updated_at DESC, c2.id DESC
                  LIMIT 1
              )
            WHERE item.run_id = ?
              AND item.segment_id = ?
            ORDER BY item.issue_family, item.issue_kind, item.id
            """,
            (ledger_run_id, int(partial["segment_id"])),
        ).fetchall()
        emitted_by_family: Counter[str] = Counter()
        for item_row in ledger_items:
            item = dict(item_row)
            family = str(item["issue_family"])
            if family not in remaining:
                continue
            if emitted_by_family[family] >= int(remaining[family]):
                continue
            if int(item["id"]) in covered_evidence:
                continue
            bucket, suggested_decision, priority = classify_bucket(item)
            evidence = parse_json_dict(item.get("evidence_json"))
            evidence["_governed_blocker_queue"] = {
                "partial_coverage_run_id": partial_coverage_run_id,
                "partial_coverage_item_id": int(partial["id"]),
                "coverage_state": partial.get("coverage_state"),
                "review_state": partial.get("review_state"),
                "total_issue_count": int(partial.get("total_issue_count") or 0),
                "covered_issue_count": int(partial.get("covered_issue_count") or 0),
                "blocked_issue_count": int(partial.get("blocked_issue_count") or 0),
                "open_issue_count": int(partial.get("open_issue_count") or 0),
                "issue_families": parse_json_dict(partial.get("issue_families_json")),
                "covered_families": parse_json_dict(partial.get("covered_families_json")),
                "remaining_families": remaining,
            }
            item["queue_bucket"] = bucket
            item["suggested_decision"] = suggested_decision
            item["priority_score"] = priority + int(item.get("segment_id") or 0) % 23 / 100
            item["evidence_json"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            candidates.append(item)
            emitted_by_family[family] += 1
    candidates.sort(key=lambda row: (-float(row["priority_score"]), row["relative_path"], row["source_key"]))
    return ledger_run_id, candidates


def insert_queue(
    conn,
    *,
    ledger_run_id: int,
    selected: list[dict[str, Any]],
    limit: int,
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
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
        VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            ledger_run_id,
            QUEUE_AGENT_KEY,
            QUEUE_STRATEGY,
            limit,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    run_id = int(cur.lastrowid)
    for row in selected:
        conn.execute(
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
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ledger_run_id,
                int(row["id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["issue_family"],
                row["issue_kind"],
                row["agent_key"],
                row["queue_bucket"],
                float(row["priority_score"]),
                row["suggested_decision"],
                row.get("evidence_text") or "",
                row.get("evidence_json") or "{}",
                row.get("english_text") or "",
                row.get("spanish_text") or "",
                row.get("confirmed_text") or "",
                now,
            ),
        )
    return run_id


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    run_id: int,
    partial_coverage_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fields = [
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "issue_family",
        "issue_kind",
        "agent_key",
        "queue_bucket",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    queue_rows = []
    with db.connect() as conn:
        queue_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    id AS queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    issue_family,
                    issue_kind,
                    agent_key,
                    queue_bucket,
                    priority_score,
                    suggested_decision,
                    evidence_text,
                    english_text,
                    spanish_text,
                    confirmed_text
                FROM ml_issue_review_queue_items
                WHERE run_id = ?
                ORDER BY priority_score DESC, relative_path, source_key
                """,
                (run_id,),
            )
        ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in queue_rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in queue_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in queue_rows:
            template = {
                "queue_run_id": run_id,
                "queue_item_id": row["queue_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "decision": "pending",
                "corrected_text": "",
                "notes": (
                    f"Suggested: {row['suggested_decision']}. "
                    "Use needs_repair, needs_domain_context, needs_new_microagent, "
                    "manual_exception, false_positive or safe."
                ),
            }
            handle.write(json.dumps(template, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["queue_bucket"] for row in queue_rows)
    lines = [
        "Issue governed blocker queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {run_id}",
        f"Partial coverage run id: {partial_coverage_run_id}",
        f"Selected rows: {len(queue_rows)}",
        "",
        "Buckets:",
    ]
    for bucket, count in counts.most_common():
        lines.append(f"- {bucket}: {count}")
    lines.extend(["", "Rows:"])
    for row in queue_rows:
        lines.append(
            "- "
            f"{row['queue_item_id']} | segment={row['segment_id']} | {row['issue_family']} | "
            f"{row['queue_bucket']} | {row['source_key']} | {short(row.get('evidence_text'))}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(partial_run_id: int | None = None, limit: int | None = None) -> None:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        partial_coverage_run_id = partial_run_id or latest_partial_coverage_run_id(conn)
        ledger_run_id, candidates = fetch_candidates(conn, partial_coverage_run_id=partial_coverage_run_id)
        selected = candidates[: limit or len(candidates)]
        paths = report_paths(settings, partial_coverage_run_id)
        run_id = insert_queue(conn, ledger_run_id=ledger_run_id, selected=selected, limit=limit or len(selected), paths=paths)
        conn.commit()
    write_outputs(paths=paths, run_id=run_id, partial_coverage_run_id=partial_coverage_run_id, rows=selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_governed_blocker_queue] Queue generated")
    print(f"[issue_governed_blocker_queue] Queue run id: {run_id}")
    print(f"[issue_governed_blocker_queue] Partial coverage run id: {partial_coverage_run_id}")
    print(f"[issue_governed_blocker_queue] Selected: {len(selected)}")
    print(f"[issue_governed_blocker_queue] Report: {txt_path}")
    print(f"[issue_governed_blocker_queue] CSV: {csv_path}")
    print(f"[issue_governed_blocker_queue] JSONL: {jsonl_path}")
    print(f"[issue_governed_blocker_queue] Decisions template: {decisions_template_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a review queue for governed blockers in partial issue coverage.")
    parser.add_argument("--partial-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(partial_run_id=args.partial_run_id, limit=args.limit)
