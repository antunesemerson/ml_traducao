from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_compact_ui_semantic_review_queue_v1"
AGENT_KEY = "micro_short_label_style"
ISSUE_FAMILY = "short_label_style_microagent"
SUBLANE = "short_label_compact_ui_semantic"
QUEUE_STRATEGY = "short_label_compact_ui_semantic_stratified_learning_queue"
DEFAULT_LIMIT = 160
DEFAULT_PER_BUCKET = 20


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_repair_sublane_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label sublane diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_compact_ui_semantic_review_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def size_bucket(row: dict[str, Any]) -> str:
    token_count = int(row.get("token_count") or 0)
    text_length = int(row.get("text_length") or 0)
    word_count = int(row.get("word_count") or 0)
    if token_count == 0 and text_length <= 30:
        return "no_token_tiny"
    if token_count == 0:
        return "no_token_compact"
    if token_count == 1 and text_length <= 45:
        return "one_token_tiny"
    if token_count <= 3 and word_count <= 8:
        return "token_compact"
    return "token_contextual"


def queue_bucket(row: dict[str, Any]) -> str:
    domain = str(row.get("domain") or "domain_unknown")
    return f"{domain}|{size_bucket(row)}"


def priority_score(row: dict[str, Any]) -> float:
    text_length = int(row.get("text_length") or 0)
    token_count = int(row.get("token_count") or 0)
    word_count = int(row.get("word_count") or 0)
    confidence = row.get("confidence_score")
    score = 100.0
    score -= min(text_length, 160) * 0.10
    score -= token_count * 3.0
    score -= max(word_count - 8, 0) * 1.5
    if row.get("active_action") == "auto_safe":
        score += 8.0
    if row.get("candidate_action") == "needs_autofix":
        score += 4.0
    if row.get("policy_action") == "needs_autofix":
        score += 4.0
    if confidence is not None:
        try:
            score += float(confidence) * 10.0
        except (TypeError, ValueError):
            pass
    return round(score, 4)


def suggested_decision(row: dict[str, Any]) -> str:
    if row.get("active_action") == "auto_safe" and row.get("candidate_action") == "needs_autofix":
        return "false_positive_reopen"
    return "needs_domain_context"


def fetch_candidates(
    conn,
    *,
    diagnostic_run_id: int,
    include_existing: bool,
) -> list[dict[str, Any]]:
    existing_sql = (
        ""
        if include_existing
        else """
          AND NOT EXISTS (
              SELECT 1
              FROM ml_issue_review_queue_items queued
              WHERE queued.ledger_item_id = diag.ledger_item_id
                AND queued.agent_key = ?
          )
        """
    )
    params: list[Any] = [diagnostic_run_id, SUBLANE]
    if not include_existing:
        params.append(AGENT_KEY)
    rows = conn.execute(
        f"""
        SELECT
            diag.ledger_item_id AS id,
            diag.ledger_run_id,
            diag.segment_id,
            diag.relative_path,
            diag.source_key,
            diag.source_line_number,
            ? AS issue_family,
            diag.issue_kind,
            ? AS agent_key,
            diag.sublane,
            diag.sublane_reason,
            diag.domain,
            diag.package,
            diag.token_count,
            diag.text_length,
            diag.word_count,
            diag.confidence_score,
            diag.active_action,
            diag.candidate_action,
            diag.policy_action,
            diag.token_impact,
            diag.token_status,
            COALESCE(ledger.evidence_text, diag.evidence_text) AS evidence_text,
            ledger.evidence_json,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM ml_issue_short_label_repair_sublane_diagnostic_items diag
        JOIN ml_issue_ledger_items ledger
          ON ledger.id = diag.ledger_item_id
         AND ledger.run_id = diag.ledger_run_id
        JOIN source_segments source ON source.id = diag.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = diag.segment_id
        WHERE diag.run_id = ?
          AND diag.sublane = ?
          {existing_sql}
        ORDER BY diag.domain, diag.package, diag.text_length, diag.relative_path, diag.source_key
        """,
        (ISSUE_FAMILY, AGENT_KEY, *params),
    ).fetchall()
    candidates = [dict(row) for row in rows]
    for row in candidates:
        evidence = parse_json(row.get("evidence_json"))
        evidence.update(
            {
                "sublane": row["sublane"],
                "sublane_reason": row["sublane_reason"],
                "domain": row["domain"],
                "package": row["package"],
                "token_count": row["token_count"],
                "text_length": row["text_length"],
                "word_count": row["word_count"],
            }
        )
        row["evidence_json"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        row["queue_bucket"] = queue_bucket(row)
        row["priority_score"] = priority_score(row)
        row["suggested_decision"] = suggested_decision(row)
    return candidates


def select_rows(candidates: list[dict[str, Any]], *, limit: int, per_bucket: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["queue_bucket"]].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: (-float(item["priority_score"]), item["relative_path"], item["source_key"]))

    bucket_order = sorted(grouped, key=lambda bucket: (-len(grouped[bucket]), bucket))
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for index in range(per_bucket):
        for bucket in bucket_order:
            rows = grouped[bucket]
            if index >= len(rows):
                continue
            if len(selected) >= limit:
                return selected
            row = rows[index]
            selected.append(row)
            selected_ids.add(int(row["id"]))

    if len(selected) < limit:
        remaining = [row for row in candidates if int(row["id"]) not in selected_ids]
        remaining.sort(key=lambda item: (-float(item["priority_score"]), item["queue_bucket"], item["relative_path"]))
        for row in remaining:
            if len(selected) >= limit:
                break
            selected.append(row)
    return selected


def insert_queue_run(
    conn,
    *,
    ledger_run_id: int,
    limit: int,
    per_bucket: int,
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    run_id = conn.execute(
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
            ledger_run_id,
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            limit,
            per_bucket,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    ).lastrowid
    return int(run_id)


def insert_queue_items(conn, *, run_id: int, ledger_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    conn.executemany(
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                ledger_run_id,
                row["id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["issue_family"],
                row["issue_kind"],
                row["agent_key"],
                row["queue_bucket"],
                row["priority_score"],
                "pending",
                row["suggested_decision"],
                row.get("evidence_text"),
                row.get("evidence_json"),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            )
            for row in rows
        ],
    )


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    diagnostic_run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    candidates_count: int,
    limit: int,
    per_bucket: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    domain_counts = Counter(row["domain"] for row in rows)
    package_counts = Counter(row["package"] for row in rows)

    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "suggested_decision",
        "domain",
        "package",
        "token_count",
        "text_length",
        "word_count",
        "priority_score",
        "active_action",
        "candidate_action",
        "policy_action",
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
                "ledger_run_id": ledger_run_id,
                "ledger_item_id": row["id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "agent_key": AGENT_KEY,
                "queue_bucket": row["queue_bucket"],
                "priority_score": row["priority_score"],
                "suggested_decision": row["suggested_decision"],
                "issue_family": ISSUE_FAMILY,
                "issue_kind": row["issue_kind"],
                "token_impact": row["token_impact"],
                "token_status": row["token_status"],
                "active_action": row["active_action"],
                "candidate_action": row["candidate_action"],
                "policy_action": row["policy_action"],
                "evidence": parse_json(row.get("evidence_json")),
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
                "decision_options": [
                    "safe_short_label",
                    "needs_repair",
                    "false_positive_reopen",
                    "needs_domain_context",
                    "needs_new_microagent",
                    "manual_exception",
                ],
                "corrected_text": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short-label compact UI semantic review queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Coverage:",
        f"- Candidates available: {candidates_count:,}",
        f"- Selected: {len(rows):,}",
        f"- Limit: {limit:,}",
        f"- Per bucket: {per_bucket:,}",
        "",
        "Buckets:",
        *[f"- {bucket}: {count:,}" for bucket, count in bucket_counts.most_common()],
        "",
        "Domains:",
        *[f"- {domain}: {count:,}" for domain, count in domain_counts.most_common()],
        "",
        "Packages:",
        *[f"- {package}: {count:,}" for package, count in package_counts.most_common(20)],
        "",
        "Review guidance:",
        "- Treat each row as evidence for a reusable compact UI label policy, not as production approval.",
        "- Mark false_positive_reopen only when the confirmed/output text is clearly acceptable in PT-BR.",
        "- Mark needs_repair when the label is short but visibly unnatural, literal, residual Spanish/English, or wrong for CK3 UI.",
        "- Mark needs_new_microagent when a narrower pattern appears, such as relation labels, event outcome labels, activity labels, or law/policy labels.",
        "- Do not write output from this queue. Use it to decide the next bridge/checkpoint.",
        "",
        "Files:",
        f"- CSV: {csv_path}",
        f"- JSONL: {jsonl_path}",
        f"- Decisions template: {decisions_template_path}",
        "",
        "Samples:",
    ]
    for row in rows[:30]:
        lines.append(
            f"- ledger {row['id']} | segment {row['segment_id']} | {row['queue_bucket']} | "
            f"{row['relative_path']}::{row['source_key']} | {row['suggested_decision']}"
        )
        lines.append(f"  evidence: {short(row.get('evidence_text'), 150)}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    diagnostic_run_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
    per_bucket: int = DEFAULT_PER_BUCKET,
    include_existing: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        candidates = fetch_candidates(
            conn,
            diagnostic_run_id=selected_diagnostic_run_id,
            include_existing=include_existing,
        )
        if not candidates:
            raise RuntimeError("No candidates found for short_label_compact_ui_semantic.")
        ledger_run_ids = {int(row["ledger_run_id"]) for row in candidates}
        if len(ledger_run_ids) != 1:
            raise RuntimeError(f"Expected one ledger run, got {sorted(ledger_run_ids)}")
        ledger_run_id = next(iter(ledger_run_ids))
        selected = select_rows(candidates, limit=limit, per_bucket=per_bucket)
        queue_run_id = insert_queue_run(
            conn,
            ledger_run_id=ledger_run_id,
            limit=limit,
            per_bucket=per_bucket,
            selected=selected,
            paths=paths,
        )
        insert_queue_items(conn, run_id=queue_run_id, ledger_run_id=ledger_run_id, rows=selected)
        conn.commit()

    write_outputs(
        paths=paths,
        queue_run_id=queue_run_id,
        diagnostic_run_id=selected_diagnostic_run_id,
        ledger_run_id=ledger_run_id,
        rows=selected,
        candidates_count=len(candidates),
        limit=limit,
        per_bucket=per_bucket,
    )

    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    print("[short_label_compact_ui_semantic_review_queue] Queue generated")
    print(f"[short_label_compact_ui_semantic_review_queue] Rule version: {RULE_VERSION}")
    print(f"[short_label_compact_ui_semantic_review_queue] Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"[short_label_compact_ui_semantic_review_queue] Ledger run id: {ledger_run_id}")
    print(f"[short_label_compact_ui_semantic_review_queue] Queue run id: {queue_run_id}")
    print(f"[short_label_compact_ui_semantic_review_queue] Candidates: {len(candidates):,}")
    print(f"[short_label_compact_ui_semantic_review_queue] Selected: {len(selected):,}")
    print(f"[short_label_compact_ui_semantic_review_queue] Report: {txt_path}")
    print(f"[short_label_compact_ui_semantic_review_queue] JSONL: {jsonl_path}")
    print(f"[short_label_compact_ui_semantic_review_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "ledger_run_id": ledger_run_id,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a stratified review queue for compact UI semantic short-label issues.")
    parser.add_argument("--diagnostic-run-id", type=int)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        limit=args.limit,
        per_bucket=args.per_bucket,
        include_existing=args.include_existing,
    )
