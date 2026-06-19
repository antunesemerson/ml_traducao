from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_specialist_blocker_review_queue_v2"
QUEUE_STRATEGY = "semantic_short_label_blocker_specialist_sample"
DEFAULT_LIMIT = 90
PRIMARY_FAMILY = "semantic_review_router"

SPECIALISTS = {
    "event_short_phrase": {
        "agent_key": "micro_event_short_phrase",
        "issue_family": "event_short_phrase_microagent",
        "decision_hint": (
            "Review short event/options/tooltips blocked by the short-label coordinator. "
            "Approve only when PT-BR is natural in context, CK3 tokens are intact, and the line is not a narrative sentence needing wider context."
        ),
        "patterns": (
            "event_option_or_tooltip_surface_requires_event_microagent",
            "ui_only_v8_blocks_event_short_phrase_surface",
            "ui_only_v9_blocks_event_option_surface",
        ),
    },
    "artifact_bane_name_semantics": {
        "agent_key": "micro_artifact_name_semantics",
        "issue_family": "artifact_name_semantics_microagent",
        "decision_hint": (
            "Review artifact/name semantics where English '*bane' may mean slayer, scourge, doom, or anti-target naming. "
            "Approve only when the PT-BR name preserves the intended fantasy naming, not merely possession."
        ),
        "patterns": (
            "artifact_name_bane_semantics_requires_review",
            "ui_only_v10_blocks_artifact_bane_semantic_ambiguity",
        ),
    },
    "english_ui_loanwords": {
        "agent_key": "micro_ui_loanword_hygiene",
        "issue_family": "ui_loanword_hygiene_microagent",
        "decision_hint": (
            "Review visible English loanwords or residual UI terms in PT-BR. "
            "Approve only when the term is intentionally preserved; otherwise classify as repair/context."
        ),
        "literal_terms": (
            "insights",
            "tooltips",
            "levy",
            "paddocks",
            "trinket",
        ),
        "patterns": (
            "english_or_placeholder_literal",
        ),
    },
    "split_gender_suffix": {
        "agent_key": "micro_gender_suffix_surface",
        "issue_family": "gender_suffix_surface_microagent",
        "decision_hint": (
            "Review malformed gender suffix surfaces such as truncated adjectives ending in 'ad aos'. "
            "Approve only when the visible PT-BR text is grammatical for CK3's gender/token context."
        ),
        "patterns": (
            r"awkward_ptbr_literal:.*\\w\+ad",
            "blocks_split_gender_suffix",
        ),
    },
}

DECISION_OPTIONS = [
    "safe_short_label",
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
]


def report_paths(settings: dict[str, Any], specialist: str) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_specialist_blocker_review_queue_{specialist}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def latest_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No guarded expansion dry-run found.")
    return int(row["id"])


def fetch_dry_run(conn, *, dry_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded expansion dry-run not found: {dry_run_id}")
    return dict(row)


def top_package(relative_path: str | None) -> str:
    value = relative_path or "unknown"
    return value.split("/", 1)[0] if "/" in value else value


def matches_specialist(block_reason: str, specialist: dict[str, Any]) -> bool:
    reason = block_reason.lower()
    literal_terms = tuple(str(term).lower() for term in specialist.get("literal_terms", ()))
    if literal_terms and reason.startswith("english_or_placeholder_literal:"):
        return any(term in reason for term in literal_terms)

    for pattern in specialist["patterns"]:
        if pattern.startswith("english_or_placeholder_literal") or pattern.startswith("awkward_ptbr_literal"):
            if re.search(pattern, block_reason, flags=re.IGNORECASE):
                return True
        elif pattern in block_reason:
            return True
    return False


def priority_score(row: dict[str, Any]) -> float:
    reason = str(row.get("block_reason") or "")
    score = 1000.0
    score += min(int(row.get("token_count") or 0), 8) * 20
    score += min(int(row.get("char_count") or 0), 160) / 8
    if any(marker in str(row.get("text_sample") or "") for marker in ("[", "]", "$", "#", "@")):
        score += 80
    if "event_option" in reason or "event_short_phrase" in reason:
        score += 40
    return round(score + (int(row.get("segment_id") or 0) % 97) / 1000, 4)


def fetch_candidates(
    conn,
    *,
    dry_run: dict[str, Any],
    specialist_name: str,
    include_existing: bool,
) -> list[dict[str, Any]]:
    specialist = SPECIALISTS[specialist_name]
    rows = conn.execute(
        """
        SELECT
            item.*,
            ledger.id AS ledger_item_id,
            ledger.issue_kind,
            source.english_text,
            source.spanish_text
        FROM ml_issue_semantic_short_label_pair_guarded_expansion_items item
        JOIN ml_issue_ledger_items ledger
          ON ledger.run_id = ?
         AND ledger.segment_id = item.segment_id
         AND ledger.issue_family = ?
        JOIN source_segments source
          ON source.id = item.segment_id
        WHERE item.run_id = ?
          AND item.dry_run_allowed = 0
          AND COALESCE(item.block_reason, '') <> ''
        ORDER BY item.block_reason, item.relative_path, item.source_line_number, item.source_key
        """,
        (int(dry_run["ledger_run_id"]), PRIMARY_FAMILY, int(dry_run["id"])),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if not matches_specialist(str(row.get("block_reason") or ""), specialist):
            continue
        if not include_existing:
            exists = conn.execute(
                """
                SELECT 1
                FROM ml_issue_review_queue_items item
                WHERE item.segment_id = ?
                  AND item.agent_key = ?
                LIMIT 1
                """,
                (int(row["segment_id"]), specialist["agent_key"]),
            ).fetchone()
            decision_exists = conn.execute(
                """
                SELECT 1
                FROM ml_issue_review_decisions decision
                WHERE decision.segment_id = ?
                  AND decision.agent_key = ?
                  AND decision.valid = 1
                  AND decision.validation_status = 'accepted'
                LIMIT 1
                """,
                (int(row["segment_id"]), specialist["agent_key"]),
            ).fetchone()
            if exists or decision_exists:
                continue
        row["package"] = top_package(row.get("relative_path"))
        row["queue_bucket"] = str(row.get("block_reason") or "unknown_blocker").split(":", 1)[0]
        row["priority_score"] = priority_score(row)
        row["suggested_decision"] = f"audit_{specialist_name}"
        candidates.append(row)
    return candidates


def select_stratified(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(lambda: defaultdict(deque))
    for row in sorted(
        rows,
        key=lambda item: (-float(item["priority_score"]), item["relative_path"], item.get("source_line_number") or 0, item["source_key"]),
    ):
        grouped[row["queue_bucket"]][row["package"]].append(row)

    selected: list[dict[str, Any]] = []
    seen_segments: set[int] = set()
    buckets = deque(sorted(grouped, key=lambda key: (-sum(len(v) for v in grouped[key].values()), key)))
    while buckets and len(selected) < limit:
        bucket = buckets.popleft()
        packages = deque(sorted(grouped[bucket], key=lambda key: (-len(grouped[bucket][key]), key)))
        picked_from_bucket = False
        while packages and len(selected) < limit:
            package = packages.popleft()
            rows_for_package = grouped[bucket][package]
            while rows_for_package:
                row = rows_for_package.popleft()
                segment_id = int(row["segment_id"])
                if segment_id in seen_segments:
                    continue
                selected.append(row)
                seen_segments.add(segment_id)
                picked_from_bucket = True
                break
            if rows_for_package:
                packages.append(package)
        if picked_from_bucket and any(grouped[bucket][package] for package in grouped[bucket]):
            buckets.append(bucket)
    return selected


def insert_queue_run(
    conn,
    *,
    dry_run: dict[str, Any],
    specialist_name: str,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
) -> int:
    specialist = SPECIALISTS[specialist_name]
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
            int(dry_run["ledger_run_id"]),
            specialist["agent_key"],
            specialist["issue_family"],
            QUEUE_STRATEGY,
            limit,
            0,
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
    return int(cur.lastrowid)


def insert_queue_items(
    conn,
    *,
    queue_run_id: int,
    dry_run: dict[str, Any],
    specialist_name: str,
    rows: list[dict[str, Any]],
) -> None:
    specialist = SPECIALISTS[specialist_name]
    now = db.utc_now()
    for row in rows:
        evidence = {
            "dry_run_id": int(dry_run["id"]),
            "dry_run_item_id": int(row["id"]),
            "guard_profile": dry_run.get("guard_profile"),
            "source_block_reason": row.get("block_reason"),
            "source_classifier_decision": row.get("classifier_decision"),
            "source_classifier_reason": row.get("classifier_reason"),
            "specialist": specialist_name,
            "queue_strategy": QUEUE_STRATEGY,
            "char_count": int(row.get("char_count") or 0),
            "token_count": int(row.get("token_count") or 0),
            "package": row["package"],
        }
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
                int(dry_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                specialist["issue_family"],
                row.get("issue_kind") or str(row.get("block_reason") or "specialist_blocker"),
                specialist["agent_key"],
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                row.get("text_sample") or "",
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("text_sample"),
                now,
            ),
        )
        row["queue_item_id"] = int(cur.lastrowid)


def trim(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    dry_run: dict[str, Any],
    specialist_name: str,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
) -> None:
    specialist = SPECIALISTS[specialist_name]
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fields = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "block_reason",
        "package",
        "char_count",
        "token_count",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("text_sample") or ""
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("text_sample") or ""
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
                "review_hint": specialist["decision_hint"],
                "block_reason": row.get("block_reason"),
                "evidence_text": row.get("text_sample") or "",
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    candidate_buckets = Counter(row["queue_bucket"] for row in candidates)
    selected_buckets = Counter(row["queue_bucket"] for row in selected)
    selected_packages = Counter(row["package"] for row in selected)
    lines = [
        "Specialist blocker review queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Specialist: {specialist_name}",
        f"Agent key: {specialist['agent_key']}",
        f"Issue family: {specialist['issue_family']}",
        f"Dry-run id: {dry_run['id']}",
        f"Guard profile: {dry_run.get('guard_profile')}",
        f"Ledger run id: {dry_run['ledger_run_id']}",
        f"Limit: {limit}",
        "",
        "Summary:",
        f"- candidates: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        "",
        "Candidate blocker buckets:",
        *[f"- {key}: {value:,}" for key, value in candidate_buckets.most_common()],
        "",
        "Selected blocker buckets:",
        *[f"- {key}: {value:,}" for key, value in selected_buckets.most_common()],
        "",
        "Selected packages:",
        *[f"- {key}: {value:,}" for key, value in selected_packages.most_common(20)],
        "",
        "Samples:",
    ]
    for row in selected[:50]:
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number')} | "
            f"{row['source_key']} | {row['queue_bucket']} | {trim(row.get('text_sample'), 150)}"
        )
    lines.extend(
        [
            "",
            "Decision options:",
            *[f"- {option}" for option in DECISION_OPTIONS],
            "",
            "Safety note:",
            "- Queue only: no source/output file reads, no confirmation updates, no checkpoint promotion and no output writes.",
            "- These are blocker-specific candidates rejected by the broad short-label coordinator.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    specialist: str,
    dry_run_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
    include_existing: bool = False,
) -> dict[str, Any]:
    if specialist not in SPECIALISTS:
        raise ValueError(f"Unknown specialist: {specialist}. Options: {', '.join(sorted(SPECIALISTS))}")
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_dry_run_id = dry_run_id or latest_dry_run_id(conn)
        dry_run = fetch_dry_run(conn, dry_run_id=selected_dry_run_id)
        candidates = fetch_candidates(
            conn,
            dry_run=dry_run,
            specialist_name=specialist,
            include_existing=include_existing,
        )
        selected = select_stratified(candidates, limit=limit)
        paths = report_paths(settings, specialist)
        queue_run_id = insert_queue_run(
            conn,
            dry_run=dry_run,
            specialist_name=specialist,
            selected=selected,
            paths=paths,
            limit=limit,
        )
        insert_queue_items(
            conn,
            queue_run_id=queue_run_id,
            dry_run=dry_run,
            specialist_name=specialist,
            rows=selected,
        )
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        dry_run=dry_run,
        specialist_name=specialist,
        candidates=candidates,
        selected=selected,
        limit=limit,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[issue_specialist_blocker_review_queue] Queue generated")
    print(f"[issue_specialist_blocker_review_queue] Specialist: {specialist}")
    print(f"[issue_specialist_blocker_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_specialist_blocker_review_queue] Dry-run id: {selected_dry_run_id}")
    print(f"[issue_specialist_blocker_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_specialist_blocker_review_queue] Selected: {len(selected):,}")
    print(f"[issue_specialist_blocker_review_queue] Report: {txt_path}")
    print(f"[issue_specialist_blocker_review_queue] JSONL: {jsonl_path}")
    return {
        "queue_run_id": queue_run_id,
        "specialist": specialist,
        "dry_run_id": selected_dry_run_id,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build blocker-specific specialist review queues from guarded short-label dry-runs.")
    parser.add_argument("--specialist", choices=sorted(SPECIALISTS), required=True)
    parser.add_argument("--dry-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        specialist=args.specialist,
        dry_run_id=args.dry_run_id,
        limit=args.limit,
        include_existing=args.include_existing,
    )
