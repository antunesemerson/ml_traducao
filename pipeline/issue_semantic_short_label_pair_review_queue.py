from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_semantic_short_label_pair_review_queue_v1"
AGENT_KEY = "coordinator_semantic_short_label_pair"
ISSUE_FAMILY = "semantic_review_router"
QUEUE_STRATEGY = "semantic_short_label_pair_stratified_sample"
DEFAULT_PER_PROFILE = 80

PROFILE_ORDER = [
    "pair_no_token_short_label",
    "pair_low_token_short_text",
    "pair_medium_token_short_ui",
]

DECISION_OPTIONS = [
    "safe_short_label",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_pair_review_queue"
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
        FROM ml_issue_semantic_short_label_pair_opportunity_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished semantic short-label pair opportunity run found.")
    return int(row["id"])


def fetch_opportunity_run(conn, *, opportunity_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_opportunity_runs
        WHERE id = ?
        """,
        (opportunity_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Opportunity run not found: {opportunity_run_id}")
    return dict(row)


def normalize_profiles(value: str | None) -> list[str]:
    if not value:
        return list(PROFILE_ORDER)
    requested = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in requested if item not in PROFILE_ORDER]
    if unknown:
        raise ValueError(f"Unknown profile(s): {', '.join(unknown)}")
    return requested


def package_name(relative_path: str | None) -> str:
    path = relative_path or "unknown"
    return path.split("/", 1)[0] if "/" in path else path


def short(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def fetch_candidates(
    conn,
    *,
    opportunity_run_id: int,
    profiles: list[str],
    include_existing: bool,
) -> list[dict[str, Any]]:
    existing_filter = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.segment_id = item.segment_id
                AND queued.agent_key = ?
                AND queued.queue_bucket = item.profile
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_decisions decision
              WHERE decision.segment_id = item.segment_id
                AND decision.valid = 1
                AND decision.validation_status = 'accepted'
                AND decision.reviewer IN ('codex_learning_front', 'human', 'codex')
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
            ledger.id AS ledger_item_id,
            ledger.issue_kind,
            ledger.evidence_json AS ledger_evidence_json,
            ledger.evidence_text AS ledger_evidence_text,
            confirmation.confirmed_text,
            output.portuguese_text
        FROM ml_issue_semantic_short_label_pair_opportunity_items item
        JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = item.ledger_run_id
         AND ledger.segment_id = item.segment_id
         AND ledger.issue_family = 'semantic_review_router'
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.review_bucket = 'reviewable'
          AND item.profile IN ({",".join("?" for _ in profiles)})
          {existing_filter}
        ORDER BY item.profile, item.relative_path, item.source_line_number, item.source_key
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def select_stratified(rows: list[dict[str, Any]], *, per_profile: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(lambda: defaultdict(deque))
    for row in rows:
        grouped[row["profile"]][package_name(row["relative_path"])].append(row)

    selected: list[dict[str, Any]] = []
    selected_segments: set[int] = set()
    for profile in PROFILE_ORDER:
        package_buckets = grouped.get(profile) or {}
        packages = deque(sorted(package_buckets, key=lambda key: (-len(package_buckets[key]), key)))
        picked = 0
        while packages and picked < per_profile:
            package = packages.popleft()
            bucket = package_buckets[package]
            while bucket:
                row = bucket.popleft()
                segment_id = int(row["segment_id"])
                if segment_id in selected_segments:
                    continue
                row["queue_bucket"] = profile
                row["package"] = package
                row["priority_score"] = priority_score(row)
                row["suggested_decision"] = "classify_semantic_short_label_pair"
                selected.append(row)
                selected_segments.add(segment_id)
                picked += 1
                break
            if bucket:
                packages.append(package)
    return selected


def priority_score(row: dict[str, Any]) -> float:
    profile_weight = {
        "pair_no_token_short_label": 10.0,
        "pair_low_token_short_text": 8.0,
        "pair_medium_token_short_ui": 6.0,
    }.get(row["profile"], 1.0)
    token_penalty = float(row.get("token_count") or 0) * 0.2
    length_penalty = min(float(row.get("char_count") or 0) / 200.0, 2.0)
    return max(profile_weight - token_penalty - length_penalty, 0.1)


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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                queue_run_id,
                int(opportunity_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                ISSUE_FAMILY,
                row.get("issue_kind") or "needs_human_or_semantic_conflict",
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                row.get("text_sample") or row.get("confirmed_text") or row.get("portuguese_text") or row.get("ledger_evidence_text"),
                json.dumps(
                    {
                        "opportunity_run_id": int(opportunity_run["id"]),
                        "opportunity_item_id": int(row["id"]),
                        "profile": row["profile"],
                        "pair_agent": AGENT_KEY,
                        "paired_families": ["semantic_review_router", "short_label_style_microagent"],
                        "queue_strategy": QUEUE_STRATEGY,
                        "char_count": int(row.get("char_count") or 0),
                        "token_count": int(row.get("token_count") or 0),
                        "package": row["package"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                row.get("confirmed_text") or row.get("portuguese_text"),
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
        "package",
        "char_count",
        "token_count",
        "priority_score",
        "suggested_decision",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "queue_item_id": row.get("queue_item_id"),
                    "ledger_item_id": row.get("ledger_item_id"),
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row.get("source_line_number"),
                    "queue_bucket": row["queue_bucket"],
                    "profile": row["profile"],
                    "package": row["package"],
                    "char_count": row.get("char_count"),
                    "token_count": row.get("token_count"),
                    "priority_score": row.get("priority_score"),
                    "suggested_decision": row.get("suggested_decision"),
                    "evidence_text": row.get("text_sample") or row.get("confirmed_text") or row.get("portuguese_text"),
                }
            )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row.get("source_line_number"),
                "queue_bucket": row["queue_bucket"],
                "profile": row["profile"],
                "package": row["package"],
                "char_count": row.get("char_count"),
                "token_count": row.get("token_count"),
                "suggested_decision": row.get("suggested_decision"),
                "evidence_text": row.get("text_sample") or row.get("confirmed_text") or row.get("portuguese_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row["segment_id"],
                "decision": "pending",
                "corrected_text": "",
                "notes": "",
                "decision_options": DECISION_OPTIONS,
                "review_hint": (
                    "Use safe_short_label only when current PT-BR is natural and semantic risk is a conservative false alarm; "
                    "use needs_repair for real wording/semantic issue; use needs_domain_context when a title/culture/religion/game context is required."
                ),
                "evidence_text": row.get("text_sample") or row.get("confirmed_text") or row.get("portuguese_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Semantic + short-label pair review queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Opportunity run id: {opportunity_run['id']}",
        f"Ledger run id: {opportunity_run['ledger_run_id']}",
        f"Agent key: {AGENT_KEY}",
        f"Queue strategy: {QUEUE_STRATEGY}",
        f"Profiles: {', '.join(profiles)}",
        f"Per profile: {per_profile}",
        "",
        "Summary:",
        f"- candidates after filters: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        "",
        "Candidate profiles:",
        *[f"- {key}: {value:,}" for key, value in profile_counts.most_common()],
        "",
        "Selected profiles:",
        *[f"- {key}: {value:,}" for key, value in selected_profile_counts.most_common()],
        "",
        "Selected packages:",
        *[f"- {key}: {value:,}" for key, value in selected_package_counts.most_common(20)],
        "",
        "Samples:",
    ]
    for row in selected[:30]:
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number')} | "
            f"{row['source_key']} | {row['queue_bucket']} | {short(row.get('text_sample') or row.get('confirmed_text') or row.get('portuguese_text'))}"
        )
    lines.extend(
        [
            "",
            "Decision options:",
            *[f"- {option}" for option in DECISION_OPTIONS],
            "",
            "Safety note:",
            "- Queue only: no source/output file reads, no confirmation updates, no model promotion and no output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    opportunity_run_id: int | None = None,
    per_profile: int = DEFAULT_PER_PROFILE,
    profiles: str | None = None,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    selected_profiles = normalize_profiles(profiles)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_opportunity_run_id = opportunity_run_id or latest_opportunity_run_id(conn)
        opportunity_run = fetch_opportunity_run(conn, opportunity_run_id=selected_opportunity_run_id)
        candidates = fetch_candidates(
            conn,
            opportunity_run_id=selected_opportunity_run_id,
            profiles=selected_profiles,
            include_existing=include_existing,
        )
        selected = select_stratified(candidates, per_profile=per_profile)
        paths = report_paths(settings)
        queue_run_id = insert_queue_run(
            conn,
            opportunity_run=opportunity_run,
            selected=selected,
            paths=paths,
            per_profile=per_profile,
            profiles=selected_profiles,
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
        profiles=selected_profiles,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_semantic_short_label_pair_review_queue] Queue generated")
    print(f"[issue_semantic_short_label_pair_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_semantic_short_label_pair_review_queue] Opportunity run id: {opportunity_run['id']}")
    print(f"[issue_semantic_short_label_pair_review_queue] Candidates after filters: {len(candidates):,}")
    print(f"[issue_semantic_short_label_pair_review_queue] Selected: {len(selected):,}")
    print(f"[issue_semantic_short_label_pair_review_queue] Report: {txt_path}")
    print(f"[issue_semantic_short_label_pair_review_queue] CSV: {csv_path}")
    print(f"[issue_semantic_short_label_pair_review_queue] JSONL: {jsonl_path}")
    print(f"[issue_semantic_short_label_pair_review_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "opportunity_run_id": int(opportunity_run["id"]),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a review queue for semantic + short-label pair opportunities.")
    parser.add_argument("--opportunity-run-id", type=int, default=None)
    parser.add_argument("--per-profile", type=int, default=DEFAULT_PER_PROFILE)
    parser.add_argument("--profiles", default=None)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        opportunity_run_id=args.opportunity_run_id,
        per_profile=args.per_profile,
        profiles=args.profiles,
        include_existing=args.include_existing,
    )
