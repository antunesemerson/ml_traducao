from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_pure_no_token_review_queue_v1"
TARGET_FAMILY = "short_label_style_microagent"
TARGET_ISSUE_KIND = "short_or_compact_label_reopened"
QUEUE_NAME = "short_label_pure_no_token_policy_review"
QUEUE_STRATEGY = "stratified_package_domain_sample"
AGENT_KEY = "micro_short_label_style"
DEFAULT_DOMAINS = ("domain_general",)


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished ml_issue_ledger_runs found.")
    return int(row["id"])


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def normalize_domains(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_DOMAINS
    if value.strip().lower() in {"all", "*"}:
        return tuple()
    domains = tuple(part.strip() for part in value.split(",") if part.strip())
    return domains or DEFAULT_DOMAINS


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_issue_short_label_pure_no_token_review_queue"


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_review_queue_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            queue_name TEXT NOT NULL,
            queue_strategy TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            package_counts_json TEXT,
            domain_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_review_queue_items (
            id INTEGER PRIMARY KEY,
            queue_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            domain TEXT NOT NULL,
            package TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            word_count INTEGER NOT NULL,
            token_count INTEGER NOT NULL,
            active_action TEXT,
            candidate_action TEXT,
            policy_action TEXT,
            confidence_score REAL,
            evidence_text TEXT,
            review_decision TEXT NOT NULL DEFAULT 'pending',
            review_notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_short_label_pure_no_token_review_queue_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_queue_items_run
        ON ml_issue_short_label_pure_no_token_review_queue_items(queue_run_id, review_decision);

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_queue_items_segment
        ON ml_issue_short_label_pure_no_token_review_queue_items(segment_id);
        """
    )


def fetch_candidates(conn, *, ledger_run_id: int, domains: tuple[str, ...] = DEFAULT_DOMAINS) -> list[dict[str, Any]]:
    raw_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                item.id AS ledger_item_id,
                item.segment_id,
                item.relative_path,
                item.source_key,
                item.source_line_number,
                item.active_action,
                item.candidate_action,
                item.policy_action,
                item.confidence_score,
                item.evidence_text,
                item.evidence_json
            FROM ml_issue_ledger_items item
            WHERE item.run_id = ?
              AND item.issue_family = ?
              AND item.issue_kind = ?
              AND item.status = 'open'
              AND item.validation_status = 'not_validated'
            ORDER BY item.relative_path, item.source_line_number, item.segment_id
            """,
            (ledger_run_id, TARGET_FAMILY, TARGET_ISSUE_KIND),
        )
    ]
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        evidence = parse_json(row.get("evidence_json"))
        if int(evidence.get("token_count") or 0) != 0:
            continue
        domain = evidence.get("domain") or "domain_unknown"
        if domains and domain not in domains:
            continue
        issue_codes = evidence.get("issue_codes") or []
        if issue_codes:
            continue
        rows.append(
            {
                **row,
                "domain": domain,
                "package": evidence.get("package") or "unknown",
                "text_length": int(evidence.get("text_length") or 0),
                "word_count": int(evidence.get("word_count") or 0),
                "token_count": int(evidence.get("token_count") or 0),
            }
        )
    return rows


def stratified_select(rows: list[dict[str, Any]], *, total_limit: int, per_package: int) -> list[dict[str, Any]]:
    by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_package[row["package"]].append(row)

    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for package, package_rows in sorted(by_package.items(), key=lambda item: len(item[1]), reverse=True):
        for row in package_rows[:per_package]:
            if row["segment_id"] in seen:
                continue
            selected.append(row)
            seen.add(row["segment_id"])
            if len(selected) >= total_limit:
                return selected

    for row in rows:
        if row["segment_id"] in seen:
            continue
        selected.append(row)
        seen.add(row["segment_id"])
        if len(selected) >= total_limit:
            break
    return selected


def main(
    *,
    ledger_run_id: int | None = None,
    limit: int = 120,
    per_package: int = 12,
    domains: tuple[str, ...] = DEFAULT_DOMAINS,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger = ledger_run_id or latest_ledger_run_id(conn)
        candidates = fetch_candidates(conn, ledger_run_id=selected_ledger, domains=domains)
        selected = stratified_select(candidates, total_limit=limit, per_package=per_package)

        package_counts = Counter(row["package"] for row in candidates)
        domain_counts = Counter(row["domain"] for row in candidates)

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_review_queue_runs (
                rule_version, ledger_run_id, queue_name, queue_strategy,
                candidate_count, selected_count, package_counts_json, domain_counts_json,
                started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_ledger,
                QUEUE_NAME,
                QUEUE_STRATEGY,
                len(candidates),
                len(selected),
                json.dumps(dict(package_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(domain_counts), ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        queue_run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_review_queue_items (
                queue_run_id, ledger_item_id, segment_id, relative_path, source_key,
                source_line_number, domain, package, text_length, word_count, token_count,
                active_action, candidate_action, policy_action, confidence_score, evidence_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    queue_run_id,
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["domain"],
                    row["package"],
                    row["text_length"],
                    row["word_count"],
                    row["token_count"],
                    row["active_action"],
                    row["candidate_action"],
                    row["policy_action"],
                    row["confidence_score"],
                    row["evidence_text"],
                    created_at,
                )
                for row in selected
            ],
        )

        base = report_base(settings)
        txt_path = base.with_suffix(".txt")
        csv_path = base.with_suffix(".csv")
        jsonl_path = base.with_suffix(".jsonl")
        decisions_path = base.with_name(base.name + "_decisions_template.csv")

        conn.execute(
            """
            UPDATE ml_issue_short_label_pure_no_token_review_queue_runs
            SET report_path = ?, csv_path = ?, jsonl_path = ?, decisions_template_path = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), str(decisions_path), db.utc_now(), db.utc_now(), queue_run_id),
        )
        conn.commit()

    fields = [
        "queue_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "domain",
        "package",
        "text_length",
        "word_count",
        "token_count",
        "active_action",
        "candidate_action",
        "policy_action",
        "confidence_score",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            writer.writerow({"queue_run_id": queue_run_id, **{key: row.get(key) for key in fields if key != "queue_run_id"}})

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(json.dumps({"queue_run_id": queue_run_id, **row}, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "queue_run_id",
                "segment_id",
                "decision",
                "corrected_text",
                "notes",
            ],
        )
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "segment_id": row["segment_id"],
                    "decision": "pending",
                    "corrected_text": "",
                    "notes": "",
                }
            )

    selected_package_counts = Counter(row["package"] for row in selected)
    lines = [
        "Short Label Pure No-Token Review Queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {selected_ledger}",
        f"Target domains: {', '.join(domains) if domains else 'all'}",
        f"Candidates: {len(candidates):,}",
        f"Selected: {len(selected):,}",
        "",
        "Review contract:",
        "- Decision safe_short_label: text is already acceptable PT-BR for a short UI label.",
        "- Decision semantic_error: text meaning/word choice is wrong or unnatural.",
        "- Decision residual_spanish: visible Spanish remains.",
        "- Decision structure_error: CK3 syntax/token/placeholder risk exists.",
        "- Decision needs_context: cannot decide without wider game context.",
        "",
        "Selected by package:",
    ]
    for package, count in selected_package_counts.most_common():
        lines.append(f"- {package}: {count}")
    lines.extend(["", "Files:", f"- csv: {csv_path}", f"- jsonl: {jsonl_path}", f"- decisions_template: {decisions_path}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_pure_no_token_review_queue] Queue generated")
    print(f"[issue_short_label_pure_no_token_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_pure_no_token_review_queue] Ledger run id: {selected_ledger}")
    print(f"[issue_short_label_pure_no_token_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_short_label_pure_no_token_review_queue] Selected: {len(selected):,}")
    print(f"[issue_short_label_pure_no_token_review_queue] Report: {txt_path}")
    print(f"[issue_short_label_pure_no_token_review_queue] Decisions: {decisions_path}")
    return {
        "queue_run_id": queue_run_id,
        "ledger_run_id": selected_ledger,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "report_path": str(txt_path),
        "decisions_template_path": str(decisions_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a stratified pure no-token short-label review queue.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-package", type=int, default=12)
    parser.add_argument(
        "--domains",
        default=",".join(DEFAULT_DOMAINS),
        help="Comma-separated evidence domains to sample; use 'all' for no domain filter.",
    )
    args = parser.parse_args()
    main(
        ledger_run_id=args.ledger_run_id,
        limit=args.limit,
        per_package=args.per_package,
        domains=normalize_domains(args.domains),
    )
