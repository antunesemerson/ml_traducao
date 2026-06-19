from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "issue_semantic_short_label_pair_opportunity_v1"
PAIR_KEY = "semantic_review_router|short_label_style_microagent"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
          AND ledger_item_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished issue ledger run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_pair_opportunity"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_pair_opportunity_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            reviewable_count INTEGER NOT NULL DEFAULT 0,
            held_count INTEGER NOT NULL DEFAULT 0,
            profile_counts_json TEXT,
            package_counts_json TEXT,
            hold_reason_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_pair_opportunity_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            profile TEXT NOT NULL,
            review_bucket TEXT NOT NULL,
            hold_reason TEXT,
            char_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            line_count INTEGER NOT NULL DEFAULT 0,
            has_dynamic_marker INTEGER NOT NULL DEFAULT 0,
            has_format_marker INTEGER NOT NULL DEFAULT 0,
            has_quote_escape_risk INTEGER NOT NULL DEFAULT 0,
            issue_kinds_json TEXT,
            text_sample TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_semantic_short_label_pair_opportunity_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_short_label_pair_items_run
        ON ml_issue_semantic_short_label_pair_opportunity_items(run_id, review_bucket, profile);

        CREATE INDEX IF NOT EXISTS idx_semantic_short_label_pair_items_segment
        ON ml_issue_semantic_short_label_pair_opportunity_items(segment_id);
        """
    )


def top_package(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


def sample_text(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    compact = text.replace("\n", "\\n").replace("\t", "\\t")
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def classify_text(text: str, issue_kinds: list[str]) -> tuple[str, str, str]:
    chars = len(text)
    tokens = protected_tokens(text)
    token_count = len(tokens)
    line_count = max(text.count("\n") + 1, 1) if text else 0
    has_dynamic = any(
        marker in text
        for marker in (
            "Select_CString",
            "SelectLocalization",
            "Custom(",
            "Concept(",
            "LocalPlayerString",
            "GetScriptedGui",
        )
    )
    has_quote_escape_risk = '\\"' in text or "\\'" in text
    compact_kind = "short_or_compact_label_reopened" in issue_kinds

    if line_count > 1:
        return "pair_multiline", "held", "multiline_text"
    if has_quote_escape_risk:
        return "pair_quote_escape_risk", "held", "quote_escape_risk"
    if has_dynamic:
        if chars <= 180 and token_count <= 4:
            return "pair_dynamic_short", "held", "needs_dynamic_specialist"
        return "pair_dynamic_or_long", "held", "needs_dynamic_or_long_specialist"
    if chars <= 80 and token_count == 0 and compact_kind:
        return "pair_no_token_short_label", "reviewable", ""
    if chars <= 140 and token_count <= 2 and compact_kind:
        return "pair_low_token_short_text", "reviewable", ""
    if chars <= 180 and token_count <= 4 and compact_kind:
        return "pair_medium_token_short_ui", "reviewable", ""
    if chars <= 220 and token_count <= 6:
        return "pair_semantic_short_review", "held", "needs_semantic_policy_sample"
    return "pair_long_or_many_tokens", "held", "too_long_or_many_tokens"


def fetch_candidates(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            WITH segment_families AS (
                SELECT
                    segment_id,
                    GROUP_CONCAT(issue_family, '|') AS family_key,
                    GROUP_CONCAT(issue_kind, '|') AS issue_kinds
                FROM (
                    SELECT segment_id, issue_family, issue_kind
                    FROM ml_issue_ledger_items
                    WHERE run_id = ?
                    ORDER BY segment_id, issue_family, issue_kind
                )
                GROUP BY segment_id
            ),
            pair_segments AS (
                SELECT segment_id, issue_kinds
                FROM segment_families
                WHERE family_key = ?
            )
            SELECT
                p.segment_id,
                p.issue_kinds,
                state.relative_path,
                state.source_key,
                state.source_line_number,
                COALESCE(confirm.confirmed_text, output.portuguese_text, ledger.evidence_text, '') AS text_value
            FROM pair_segments p
            JOIN segment_state_items state
              ON state.segment_id = p.segment_id
             AND state.run_id = (
                SELECT segment_state_run_id
                FROM ml_issue_ledger_runs
                WHERE id = ?
             )
            LEFT JOIN segment_confirmations confirm
              ON confirm.segment_id = p.segment_id
            LEFT JOIN output_segments output
              ON output.segment_id = p.segment_id
            LEFT JOIN ml_issue_ledger_items ledger
              ON ledger.run_id = ?
             AND ledger.segment_id = p.segment_id
             AND ledger.issue_family = 'short_label_style_microagent'
            ORDER BY state.relative_path, state.source_line_number, state.source_key
            """,
            (ledger_run_id, PAIR_KEY, ledger_run_id, ledger_run_id),
        )
    ]


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "run_id",
        "ledger_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "profile",
        "review_bucket",
        "hold_reason",
        "char_count",
        "word_count",
        "token_count",
        "line_count",
        "has_dynamic_marker",
        "has_format_marker",
        "has_quote_escape_risk",
        "issue_kinds_json",
        "text_sample",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    profiles = Counter(row["profile"] for row in rows)
    buckets = Counter(row["review_bucket"] for row in rows)
    holds = Counter(row["hold_reason"] for row in rows if row["hold_reason"])
    packages = Counter(top_package(row["relative_path"]) for row in rows)
    reviewable = [row for row in rows if row["review_bucket"] == "reviewable"]
    held = [row for row in rows if row["review_bucket"] != "reviewable"]

    lines = [
        "Semantic + short-label pair opportunity diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Pair key: {PAIR_KEY}",
        "",
        "Summary:",
        f"- candidates: {len(rows):,}",
        f"- reviewable: {len(reviewable):,}",
        f"- held: {len(held):,}",
        "",
        "Profiles:",
        *[f"- {key}: {value:,}" for key, value in profiles.most_common(20)],
        "",
        "Review buckets:",
        *[f"- {key}: {value:,}" for key, value in buckets.most_common(20)],
        "",
        "Hold reasons:",
        *[f"- {key}: {value:,}" for key, value in holds.most_common(20)],
        "",
        "Top packages:",
        *[f"- {key}: {value:,}" for key, value in packages.most_common(20)],
        "",
        "Reviewable sample:",
    ]
    if reviewable:
        for row in reviewable[:25]:
            lines.append(
                f"- segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | "
                f"{row['source_key']} | {row['profile']} | {row['text_sample']}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "Held sample:"])
    if held:
        for row in held[:25]:
            lines.append(
                f"- {row['hold_reason']} | segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | "
                f"{row['source_key']} | {row['profile']} | {row['text_sample']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This diagnostic targets segments where the only open families are semantic_review_router and short_label_style_microagent.",
            "- Reviewable rows are candidates for a guarded semantic-short-label policy sample; held rows need a richer specialist first.",
            "",
            "Safety note:",
            "- Diagnostic only: no source/output file reads, no confirmation updates, no model promotion and no output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = now_iso()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        source_rows = fetch_candidates(conn, ledger_run_id=selected_ledger_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        run_id = conn.execute(
            """
            INSERT INTO ml_issue_semantic_short_label_pair_opportunity_runs (
                rule_version, ledger_run_id, started_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (RULE_VERSION, selected_ledger_run_id, started_at, started_at),
        ).lastrowid

        rows: list[dict[str, Any]] = []
        now = now_iso()
        for source in source_rows:
            text = source.get("text_value") or ""
            issue_kinds = [kind for kind in str(source.get("issue_kinds") or "").split("|") if kind]
            profile, bucket, hold_reason = classify_text(text, issue_kinds)
            tokens = protected_tokens(text)
            item = {
                "run_id": run_id,
                "ledger_run_id": selected_ledger_run_id,
                "segment_id": int(source["segment_id"]),
                "relative_path": source["relative_path"],
                "source_key": source["source_key"],
                "source_line_number": source["source_line_number"],
                "profile": profile,
                "review_bucket": bucket,
                "hold_reason": hold_reason,
                "char_count": len(text),
                "word_count": len(text.split()),
                "token_count": len(tokens),
                "line_count": max(text.count("\n") + 1, 1) if text else 0,
                "has_dynamic_marker": int(
                    any(marker in text for marker in ("Select_CString", "SelectLocalization", "Custom(", "Concept(", "LocalPlayerString", "GetScriptedGui"))
                ),
                "has_format_marker": int("#" in text or "$" in text),
                "has_quote_escape_risk": int('\\"' in text or "\\'" in text),
                "issue_kinds_json": json.dumps(issue_kinds, ensure_ascii=False),
                "text_sample": sample_text(text),
            }
            rows.append(item)

        conn.executemany(
            """
            INSERT INTO ml_issue_semantic_short_label_pair_opportunity_items (
                run_id, ledger_run_id, segment_id, relative_path, source_key, source_line_number,
                profile, review_bucket, hold_reason, char_count, word_count, token_count,
                line_count, has_dynamic_marker, has_format_marker, has_quote_escape_risk,
                issue_kinds_json, text_sample, created_at
            ) VALUES (
                :run_id, :ledger_run_id, :segment_id, :relative_path, :source_key, :source_line_number,
                :profile, :review_bucket, :hold_reason, :char_count, :word_count, :token_count,
                :line_count, :has_dynamic_marker, :has_format_marker, :has_quote_escape_risk,
                :issue_kinds_json, :text_sample, :created_at
            )
            """,
            [{**row, "created_at": now} for row in rows],
        )

        profiles = Counter(row["profile"] for row in rows)
        holds = Counter(row["hold_reason"] for row in rows if row["hold_reason"])
        packages = Counter(top_package(row["relative_path"]) for row in rows)
        reviewable_count = sum(1 for row in rows if row["review_bucket"] == "reviewable")
        held_count = len(rows) - reviewable_count
        finished_at = now_iso()
        conn.execute(
            """
            UPDATE ml_issue_semantic_short_label_pair_opportunity_runs
            SET total_candidates = ?,
                reviewable_count = ?,
                held_count = ?,
                profile_counts_json = ?,
                package_counts_json = ?,
                hold_reason_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                reviewable_count,
                held_count,
                json.dumps(dict(profiles), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(packages), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(holds), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=int(run_id),
        ledger_run_id=selected_ledger_run_id,
        rows=rows,
    )
    print("[issue_semantic_short_label_pair_opportunity] Diagnostic generated")
    print(f"[issue_semantic_short_label_pair_opportunity] Run id: {run_id}")
    print(f"[issue_semantic_short_label_pair_opportunity] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_semantic_short_label_pair_opportunity] Candidates: {len(rows):,}")
    print(f"[issue_semantic_short_label_pair_opportunity] Reviewable: {reviewable_count:,}")
    print(f"[issue_semantic_short_label_pair_opportunity] Held: {held_count:,}")
    print(f"[issue_semantic_short_label_pair_opportunity] Report: {txt_path}")
    print(f"[issue_semantic_short_label_pair_opportunity] CSV: {csv_path}")
    print(f"[issue_semantic_short_label_pair_opportunity] JSONL: {jsonl_path}")
    return {
        "run_id": int(run_id),
        "ledger_run_id": selected_ledger_run_id,
        "candidates": len(rows),
        "reviewable": reviewable_count,
        "held": held_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose semantic + short-label pair opportunities.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id)
