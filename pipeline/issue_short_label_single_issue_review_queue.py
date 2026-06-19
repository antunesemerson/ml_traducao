from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_single_issue_review_queue_v1"
AGENT_KEY = "micro_short_label_style"
ISSUE_FAMILY = "short_label_style_microagent"
QUEUE_STRATEGY = "short_label_single_issue_stratified_sample"
DEFAULT_PER_PROFILE = 25

PROFILE_ORDER = [
    "single_no_token_short_label",
    "single_low_token_short_text",
    "single_medium_token_short_ui",
    "single_rules_tooltip_low_token",
    "single_activity_low_token",
]

DECISION_OPTIONS = [
    "safe_short_label",
    "needs_repair",
    "false_positive_reopen",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
]


def normalize_profiles(value: str | None) -> list[str]:
    if not value:
        return list(PROFILE_ORDER)
    requested = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in requested if item not in PROFILE_ORDER]
    if unknown:
        raise ValueError(f"Unknown profile(s): {', '.join(unknown)}")
    return requested


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_single_issue_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def latest_opportunity_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_single_issue_opportunity_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished ml_issue_short_label_single_issue_opportunity_runs found.")
    return int(row["id"])


def fetch_opportunity_run(conn, *, opportunity_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_single_issue_opportunity_runs
        WHERE id = ?
        """,
        (opportunity_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Opportunity run not found: {opportunity_run_id}")
    return dict(row)


def fetch_candidates(
    conn,
    *,
    opportunity_run_id: int,
    include_existing: bool,
    profiles: list[str],
) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.ledger_item_id = item.ledger_item_id
                AND queued.agent_key = ?
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.ledger_item_id = item.ledger_item_id
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
          )
        """
    )
    params: list[Any] = [opportunity_run_id, *profiles]
    if not include_existing:
        params.append(AGENT_KEY)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            ledger.issue_family,
            ledger.issue_kind,
            ledger.agent_key,
            ledger.active_action,
            ledger.candidate_action,
            ledger.policy_action,
            ledger.evidence_text,
            ledger.evidence_json AS ledger_evidence_json,
            source.english_text,
            source.spanish_text,
            COALESCE(
                confirmation.confirmed_text,
                output.portuguese_text,
                source.old_text,
                ''
            ) AS confirmed_text
        FROM ml_issue_short_label_single_issue_opportunity_items item
        JOIN ml_issue_ledger_items ledger ON ledger.id = item.ledger_item_id
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = item.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.review_bucket = 'reviewable'
          AND item.profile IN ({",".join("?" for _ in profiles)})
          {existing_filter}
        ORDER BY item.profile, item.package, item.text_length, item.token_count, item.segment_id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def priority_score(row: dict[str, Any]) -> float:
    profile_rank = PROFILE_ORDER.index(row["profile"]) if row["profile"] in PROFILE_ORDER else len(PROFILE_ORDER)
    score = 1000 - profile_rank * 50
    score += min(int(row.get("text_length") or 0), 140) / 10
    score += min(int(row.get("token_count") or 0), 8) * 5
    score += (int(row.get("segment_id") or 0) % 97) / 1000
    return round(score, 4)


def select_profile_rows(rows: list[dict[str, Any]], *, per_profile: int, profiles: list[str]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_profile[row["profile"]].append(row)

    for profile in profiles:
        profile_rows = by_profile.get(profile, [])
        by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in profile_rows:
            by_package[row["package"]].append(row)
        package_names = sorted(by_package, key=lambda key: (-len(by_package[key]), key))
        queues: dict[str, deque[dict[str, Any]]] = {}
        for package in package_names:
            package_rows = sorted(
                by_package[package],
                key=lambda item: (int(item.get("token_count") or 0), int(item.get("text_length") or 0), item["relative_path"], item["source_key"]),
            )
            queues[package] = deque(package_rows)

        picked = 0
        while picked < per_profile and any(queues.values()):
            for package in package_names:
                queue = queues[package]
                if not queue:
                    continue
                row = queue.popleft()
                if int(row["ledger_item_id"]) in selected_ids:
                    continue
                row["queue_bucket"] = profile
                row["priority_score"] = priority_score(row)
                row["suggested_decision"] = "classify_short_label_single_issue_profile"
                selected.append(row)
                selected_ids.add(int(row["ledger_item_id"]))
                picked += 1
                if picked >= per_profile:
                    break
    return selected


def insert_queue_run(
    conn,
    *,
    opportunity_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    per_profile: int,
    profiles: list[str],
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            int(opportunity_run["ledger_run_id"]),
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            per_profile * len(profiles),
            per_profile,
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
    )
    return int(cur.lastrowid)


def insert_queue_items(conn, *, queue_run_id: int, opportunity_run: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    for row in rows:
        cur = conn.execute(
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
                queue_run_id,
                int(opportunity_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row.get("issue_family") or ISSUE_FAMILY,
                row.get("issue_kind") or "",
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                row.get("evidence_text") or row.get("portuguese_preview") or row.get("confirmed_text"),
                json.dumps(
                    {
                        **parse_json(row.get("ledger_evidence_json"), {}),
                        "opportunity_run_id": int(opportunity_run["id"]),
                        "opportunity_item_id": int(row["id"]),
                        "profile": row["profile"],
                        "review_bucket": row["review_bucket"],
                        "domain": row["domain"],
                        "package": row["package"],
                        "queue_strategy": QUEUE_STRATEGY,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            ),
        )
        row["queue_item_id"] = int(cur.lastrowid)


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    opportunity_run: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    per_profile: int,
    profiles: list[str],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    profile_counts = Counter(row["profile"] for row in candidates)
    selected_profile_counts = Counter(row["queue_bucket"] for row in selected)
    selected_package_counts = Counter(row["package"] for row in selected)
    fieldnames = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "profile",
        "domain",
        "package",
        "text_length",
        "token_count",
        "word_count",
        "priority_score",
        "suggested_decision",
        "english_text",
        "spanish_text",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "queue_item_id": row["queue_item_id"],
                    **{field: row.get(field) for field in fieldnames if field not in {"queue_run_id", "queue_item_id"}},
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row["queue_item_id"],
                "ledger_run_id": int(opportunity_run["ledger_run_id"]),
                "ledger_item_id": int(row["ledger_item_id"]),
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row.get("source_line_number"),
                "agent_key": AGENT_KEY,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "issue_family": row.get("issue_family") or ISSUE_FAMILY,
                "issue_kind": row.get("issue_kind") or "",
                "profile": row["profile"],
                "domain": row["domain"],
                "package": row["package"],
                "text_length": row["text_length"],
                "token_count": row["token_count"],
                "word_count": row["word_count"],
                "texts": {
                    "english_text": row.get("english_text"),
                    "spanish_text": row.get("spanish_text"),
                    "confirmed_text": row.get("confirmed_text"),
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row["queue_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "profile": row["profile"],
                "decision": "",
                "decision_options": DECISION_OPTIONS,
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short-label single-issue stratified review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Opportunity run id: {opportunity_run['id']}",
        f"Partial coverage run id: {opportunity_run['partial_coverage_run_id']}",
        f"Ledger run id: {opportunity_run['ledger_run_id']}",
        f"Queue strategy: {QUEUE_STRATEGY}",
        "",
        "Selection:",
        f"- Reviewable candidates available after dedupe: {len(candidates):,}",
        f"- Selected: {len(selected):,}",
        f"- Per profile target: {per_profile:,}",
        f"- Target profiles: {', '.join(profiles)}",
        "",
        "Available by profile:",
        *[f"- {profile}: {count:,}" for profile, count in profile_counts.most_common()],
        "",
        "Selected by profile:",
        *[f"- {profile}: {count:,}" for profile, count in selected_profile_counts.most_common()],
        "",
        "Selected packages:",
        *[f"- {package}: {count:,}" for package, count in selected_package_counts.most_common(20)],
        "",
        "Review goal:",
        "- Estimate false-safe rate by profile before creating any broad checkpoint.",
        "- Mark safe only when PT-BR is natural, tokens are stable, and no semantic/context issue is visible.",
        "- Mark needs_repair for clear text problems; include corrected_text when obvious.",
        "- Mark needs_domain_context or needs_new_microagent when the row points to a reusable pattern we should split.",
        "- Do not apply output from this queue.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in selected[:25]:
        lines.append(
            f"- item {row['queue_item_id']} | ledger {row['ledger_item_id']} | {row['profile']} | "
            f"{row['relative_path']}::{row['source_key']}"
        )
        lines.append(f"  pt: {short(row.get('confirmed_text'))}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    opportunity_run_id: int | None = None,
    per_profile: int = DEFAULT_PER_PROFILE,
    include_existing: bool = False,
    profiles_value: str | None = None,
) -> dict[str, Any]:
    profiles = normalize_profiles(profiles_value)
    settings = db.load_settings()
    paths = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_opportunity_run_id = opportunity_run_id or latest_opportunity_run_id(conn)
        opportunity_run = fetch_opportunity_run(conn, opportunity_run_id=selected_opportunity_run_id)
        candidates = fetch_candidates(
            conn,
            opportunity_run_id=selected_opportunity_run_id,
            include_existing=include_existing,
            profiles=profiles,
        )
        selected = select_profile_rows(candidates, per_profile=per_profile, profiles=profiles)
        queue_run_id = insert_queue_run(
            conn,
            opportunity_run=opportunity_run,
            selected=selected,
            paths=paths,
            per_profile=per_profile,
            profiles=profiles,
        )
        insert_queue_items(conn, queue_run_id=queue_run_id, opportunity_run=opportunity_run, rows=selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        opportunity_run=opportunity_run,
        candidates=candidates,
        selected=selected,
        per_profile=per_profile,
        profiles=profiles,
    )
    print("[issue_short_label_single_issue_review_queue] Queue generated")
    print(f"[issue_short_label_single_issue_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_single_issue_review_queue] Opportunity run id: {opportunity_run['id']}")
    print(f"[issue_short_label_single_issue_review_queue] Candidates after dedupe: {len(candidates):,}")
    print(f"[issue_short_label_single_issue_review_queue] Selected: {len(selected):,}")
    print(f"[issue_short_label_single_issue_review_queue] Report: {paths[0]}")
    print(f"[issue_short_label_single_issue_review_queue] CSV: {paths[1]}")
    print(f"[issue_short_label_single_issue_review_queue] JSONL: {paths[2]}")
    print(f"[issue_short_label_single_issue_review_queue] Decisions template: {paths[3]}")
    return {
        "queue_run_id": queue_run_id,
        "opportunity_run_id": int(opportunity_run["id"]),
        "candidates": len(candidates),
        "selected": len(selected),
        "profiles": profiles,
        "report_path": str(paths[0]),
        "csv_path": str(paths[1]),
        "jsonl_path": str(paths[2]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a stratified review queue from short-label single-issue opportunities.")
    parser.add_argument("--opportunity-run-id", type=int, default=None)
    parser.add_argument("--per-profile", type=int, default=DEFAULT_PER_PROFILE)
    parser.add_argument("--profiles", default=None, help="Comma-separated subset of short-label profiles to sample.")
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        opportunity_run_id=args.opportunity_run_id,
        per_profile=args.per_profile,
        include_existing=args.include_existing,
        profiles_value=args.profiles,
    )
