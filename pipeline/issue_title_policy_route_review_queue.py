from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_title_policy_route_diagnostic import RULE_VERSION as DIAGNOSTIC_RULE_VERSION
from issue_title_policy_route_diagnostic import route_lane


RULE_VERSION = "issue_title_policy_route_review_queue_v1"
AGENT_KEY = "micro_title_policy_router"


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
    if not row:
        raise RuntimeError("No finished ml_issue_ledger_runs found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_policy_route_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_title_policy_route_review_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            diagnostic_rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            lane TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            limit_count INTEGER NOT NULL DEFAULT 0,
            per_prefix INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_title_policy_route_review_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            lane TEXT NOT NULL,
            key_prefix TEXT,
            suffix_hint TEXT,
            route_bucket TEXT,
            text_sample TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, ledger_item_id)
        )
        """
    )


def parse_evidence(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("evidence_json") or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def key_prefix(key: str) -> str:
    return key.split("_", 1)[0] + "_" if "_" in key else key[:16]


def suffix_hint(text: str) -> str:
    value = text.strip().lower()
    for suffix in (
        "ense",
        "ano",
        "iano",
        "ês",
        "esa",
        "í",
        "ita",
        "eiro",
        "eiro",
        "aco",
        "aco",
        "ino",
        "eno",
        "eu",
        "ota",
    ):
        if value.endswith(suffix):
            return f"suffix_{suffix}"
    if " " in value:
        return "multiword"
    if not value:
        return "blank"
    return "other_suffix"


def route_bucket(row: dict[str, Any]) -> str:
    key = str(row.get("source_key") or "")
    text = str(row.get("evidence_text") or "")
    prefix = key_prefix(key)
    suffix = suffix_hint(text)
    if prefix in {"b_", "c_", "d_", "k_", "e_"}:
        return f"{prefix.rstrip('_')}_adj_{suffix}"
    if key.upper() == key and key.endswith("_adj"):
        return f"upper_adj_{suffix}"
    return f"misc_adj_{suffix}"


def fetch_candidates(conn, *, ledger_run_id: int, lane: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND issue_family = 'title_policy_microagent'
        ORDER BY relative_path, source_line_number, segment_id
        """,
        (ledger_run_id,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if route_lane(item) == lane:
            output.append(item)
    return output


def select_rows(rows: list[dict[str, Any]], *, limit: int, per_prefix: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    prefix_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    for row in rows:
        prefix = key_prefix(str(row.get("source_key") or ""))
        bucket = route_bucket(row)
        if per_prefix and prefix_counts[prefix] >= per_prefix:
            continue
        if per_prefix and bucket_counts[bucket] >= max(5, per_prefix // 3):
            continue
        selected.append(row)
        prefix_counts[prefix] += 1
        bucket_counts[bucket] += 1
        if limit and len(selected) >= limit:
            break
    return selected


def write_files(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    template_path: Path,
    ledger_run_id: int,
    queue_run_id: int,
    lane: str,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    prefix_counts = Counter(key_prefix(str(row.get("source_key") or "")) for row in selected)
    bucket_counts = Counter(route_bucket(row) for row in selected)
    path_counts = Counter(str(row.get("relative_path") or "") for row in selected)
    lines = [
        "Title policy route review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Agent: {AGENT_KEY}",
        f"Lane: {lane}",
        "",
        "Coverage:",
        f"- Candidates available: {len(candidates):,}",
        f"- Selected: {len(selected):,}",
        "",
        "Review guidance:",
        "- This queue does not authorize output writes.",
        "- For landed title adjectives, decide whether the demonym/adjective looks valid PT-BR, needs a specific correction, or needs gazetteer/domain context.",
        "- Do not generalize place-name preservation rules from this queue; this lane is adjective/demonym only.",
        "",
        "Selected prefixes:",
    ]
    for label, count in prefix_counts.most_common():
        lines.append(f"- {label}: {count:,}")
    lines.extend(["", "Selected buckets:"])
    for label, count in bucket_counts.most_common():
        lines.append(f"- {label}: {count:,}")
    lines.extend(["", "Selected paths:"])
    for label, count in path_counts.most_common():
        lines.append(f"- {label}: {count:,}")
    lines.extend(["", "Samples:"])
    for row in selected[:20]:
        lines.append(
            f"- ledger={row['id']} segment={row['segment_id']} | "
            f"{row['relative_path']}:{row['source_key']} | {row['evidence_text']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "ledger_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "lane",
        "key_prefix",
        "suffix_hint",
        "route_bucket",
        "text_sample",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected:
            writer.writerow(queue_row(queue_run_id, ledger_run_id, lane, row))

    with jsonl_path.open("w", encoding="utf-8") as handle, template_path.open("w", encoding="utf-8") as template:
        for row in selected:
            payload = queue_row(queue_run_id, ledger_run_id, lane, row)
            payload["evidence_json"] = parse_evidence(row)
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            template.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "ledger_item_id": row["id"],
                        "segment_id": row["segment_id"],
                        "decision": "",
                        "corrected_text": "",
                        "reason": "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def queue_row(queue_run_id: int, ledger_run_id: int, lane: str, row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("evidence_text") or "")
    key = str(row.get("source_key") or "")
    return {
        "queue_run_id": queue_run_id,
        "ledger_item_id": row["id"],
        "ledger_run_id": ledger_run_id,
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": key,
        "source_line_number": row["source_line_number"],
        "lane": lane,
        "key_prefix": key_prefix(key),
        "suffix_hint": suffix_hint(text),
        "route_bucket": route_bucket(row),
        "text_sample": text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build review queue for title_policy route lanes.")
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--lane", default="landed_title_adjectives")
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--per-prefix", type=int, default=60)
    args = parser.parse_args()

    settings = db.load_settings()
    txt_path, csv_path, jsonl_path, template_path = report_paths(settings)
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        ledger_run_id = args.ledger_run_id or latest_ledger_run_id(conn)
        candidates = fetch_candidates(conn, ledger_run_id=ledger_run_id, lane=args.lane)
        selected = select_rows(candidates, limit=args.limit, per_prefix=args.per_prefix)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_title_policy_route_review_queue_runs (
                rule_version,
                diagnostic_rule_version,
                ledger_run_id,
                agent_key,
                lane,
                candidate_count,
                selected_count,
                limit_count,
                per_prefix,
                report_path,
                csv_path,
                jsonl_path,
                decisions_template_path,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DIAGNOSTIC_RULE_VERSION,
                ledger_run_id,
                AGENT_KEY,
                args.lane,
                len(candidates),
                len(selected),
                args.limit,
                args.per_prefix,
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(template_path),
                started_at,
                started_at,
            ),
        )
        queue_run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        for row in selected:
            qrow = queue_row(queue_run_id, ledger_run_id, args.lane, row)
            conn.execute(
                """
                INSERT OR IGNORE INTO ml_issue_title_policy_route_review_queue_items (
                    run_id,
                    ledger_item_id,
                    ledger_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    lane,
                    key_prefix,
                    suffix_hint,
                    route_bucket,
                    text_sample,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_run_id,
                    row["id"],
                    ledger_run_id,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    args.lane,
                    qrow["key_prefix"],
                    qrow["suffix_hint"],
                    qrow["route_bucket"],
                    qrow["text_sample"],
                    row.get("evidence_json"),
                    created_at,
                ),
            )
        conn.execute(
            """
            UPDATE ml_issue_title_policy_route_review_queue_runs
            SET finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (created_at, created_at, queue_run_id),
        )
        conn.commit()

    write_files(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        template_path=template_path,
        ledger_run_id=ledger_run_id,
        queue_run_id=queue_run_id,
        lane=args.lane,
        candidates=candidates,
        selected=selected,
    )

    print("[issue_title_policy_route_review_queue] Queue generated")
    print(f"[issue_title_policy_route_review_queue] Queue run id: {queue_run_id}")
    print(f"[issue_title_policy_route_review_queue] Ledger run id: {ledger_run_id}")
    print(f"[issue_title_policy_route_review_queue] Lane: {args.lane}")
    print(f"[issue_title_policy_route_review_queue] Candidates: {len(candidates):,}")
    print(f"[issue_title_policy_route_review_queue] Selected: {len(selected):,}")
    print(f"[issue_title_policy_route_review_queue] Report: {txt_path}")
    print(f"[issue_title_policy_route_review_queue] JSONL: {jsonl_path}")
    print(f"[issue_title_policy_route_review_queue] Decisions template: {template_path}")


if __name__ == "__main__":
    main()
