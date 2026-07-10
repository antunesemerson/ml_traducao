from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PAIR_FAMILIES = {"autofix_unknown_microagent", "semantic_review_router"}
SAMPLE_LIMIT = 240

DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|\b(?:Get[A-Za-z0-9_]*|Custom|Select_CString|ScriptValue|Concept|ROOT\.|CHARACTER\.|TARGET\.)\b",
    re.IGNORECASE,
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)|custom_localization",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|royals|"
    r"el|la|los|las|una|uno|verdadero|verdadera|fuerza|probabilidad|opinion|hostile)\b",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"historical_characters|custom_localization|culture|religion|faith|holy_order|law|succession|"
    r"title|rank|trait_|nickname|dynasty|house|government|factions|ai_personality|great_project|"
    r"coat_of_arms|artifact|buildings|regiment|acclaimed_knight|diarch",
    re.IGNORECASE,
)
CONTEXT_RE = re.compile(
    r"event|interaction|faction|contract_scheme|major_decisions|bookmark|memories|memory|"
    r"activities/|journey|travel|tourism|roaming|activity|tooltip|tutorial|game_rules|_tt$|_desc$|desc\.",
    re.IGNORECASE,
)
COMPANION_RE = re.compile(
    r"building|buildings|artifact|court_artifacts|diarchies|dlc|regiment|accolade|acclaimed|activity",
    re.IGNORECASE,
)
NEW_MICROAGENT_RE = re.compile(
    r"combat|weapon|vassal|stance|claim|casus|belli|prefix|nickname|epithet|trait|title|landed",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def word_count(value: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!", " ", value)
    return len(WORD_RE.findall(cleaned))


def text_for(row: dict[str, Any]) -> str:
    for key in ("spanish_text", "old_text", "english_text"):
        value = as_text(row.get(key))
        if value:
            return value
    return ""


def read_reviewed_segment_ids(paths: list[str]) -> set[int]:
    reviewed: set[int] = set()
    for value in paths:
        path = db.project_path(value)
        if not path.exists():
            raise SystemExit(f"Reviewed JSONL not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
                if "segment_id" in payload:
                    reviewed.add(int(payload["segment_id"]))
    return reviewed


def open_readonly_connection() -> sqlite3.Connection:
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_pair_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH open_issues AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
                SUM(CASE WHEN issue_family NOT IN ('autofix_unknown_microagent', 'semantic_review_router') THEN 1 ELSE 0 END) AS other_issue_count,
                SUM(
                    CASE
                        WHEN issue_family NOT IN ('autofix_unknown_microagent', 'semantic_review_router')
                         AND lower(issue_severity) IN ('high', 'error', 'critical')
                        THEN 1 ELSE 0
                    END
                ) AS high_out_of_scope_count,
                GROUP_CONCAT(DISTINCT issue_family) AS issue_families,
                GROUP_CONCAT(DISTINCT issue_kind) AS issue_kinds,
                MAX(relative_path) AS ledger_relative_path,
                MAX(source_key) AS ledger_source_key
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            s.segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.final_state,
            s.state_group,
            s.needs_reopen,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.priority_score,
            src.spanish_text,
            src.english_text,
            src.old_text,
            oi.open_issue_count,
            oi.autofix_count,
            oi.semantic_count,
            oi.other_issue_count,
            oi.high_out_of_scope_count,
            oi.issue_families,
            oi.issue_kinds
        FROM open_issues oi
        JOIN segment_state_items s
          ON s.segment_id = oi.segment_id
         AND s.run_id = ?
        LEFT JOIN source_segments src
          ON src.id = oi.segment_id
        WHERE s.state_group = 'pending'
          AND oi.autofix_count > 0
          AND oi.semantic_count > 0
        ORDER BY s.priority_score DESC, s.segment_id
        """,
        (ledger_run_id, segment_state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_global_bottlenecks(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            li.issue_family,
            COUNT(DISTINCT li.segment_id) AS pending_segments,
            COUNT(*) AS open_issues,
            SUM(CASE WHEN lower(li.issue_severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issues
        FROM ml_issue_ledger_items li
        JOIN segment_state_items s
          ON s.segment_id = li.segment_id
         AND s.run_id = ?
        WHERE li.run_id = ?
          AND li.status = 'open'
          AND s.state_group = 'pending'
        GROUP BY li.issue_family
        ORDER BY pending_segments DESC, open_issues DESC, li.issue_family
        LIMIT 20
        """,
        (segment_state_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = text_for(row)
    haystack = " ".join(
        [
            as_text(row.get("relative_path")),
            as_text(row.get("source_key")),
            as_text(row.get("issue_kinds")),
            as_text(row.get("issue_families")),
            text,
        ]
    )
    wc = word_count(text)
    exact_pair = int(row.get("open_issue_count") or 0) == 2 and int(row.get("other_issue_count") or 0) == 0
    flags = {
        "exact_pair": exact_pair,
        "pair_plus_high_out_of_scope": int(row.get("high_out_of_scope_count") or 0) > 0,
        "with_domain": bool(DOMAIN_RE.search(haystack)),
        "with_dynamic": bool(DYNAMIC_RE.search(text)),
        "with_context": bool(CONTEXT_RE.search(haystack)) or wc >= 9,
        "with_residual": bool(RESIDUAL_RE.search(text)),
        "with_gender_custom_loc": bool(GENDER_RE.search(haystack)),
        "probable_new_microagent": bool(NEW_MICROAGENT_RE.search(haystack)),
        "companion_surface": bool(COMPANION_RE.search(haystack)),
    }
    if flags["pair_plus_high_out_of_scope"]:
        bucket = "pair_plus_high_out_of_scope"
    elif flags["with_gender_custom_loc"]:
        bucket = "gender_custom_loc"
    elif flags["with_dynamic"]:
        bucket = "dynamic"
    elif flags["with_domain"]:
        bucket = "domain"
    elif flags["with_residual"]:
        bucket = "residual"
    elif flags["with_context"]:
        bucket = "context"
    elif flags["probable_new_microagent"]:
        bucket = "probable_new_microagent"
    elif flags["exact_pair"]:
        bucket = "exact_pair_clean"
    else:
        bucket = "pair_mixed_other"

    if bucket == "exact_pair_clean" and wc <= 8:
        readiness = "composition_ready_likely"
    elif bucket in {"exact_pair_clean", "context"} and flags["companion_surface"]:
        readiness = "companion_ready_likely"
    elif bucket == "context":
        readiness = "context_ready_likely"
    else:
        readiness = "not_ready_likely"

    return {"bucket": bucket, "readiness": readiness, "word_count": wc, **flags}


def recommendation(unreviewed_count: int, sample_counts: Counter[str], bucket_counts: Counter[str], global_rows: list[dict[str, Any]]) -> str:
    ready = (
        sample_counts["composition_ready_likely"]
        + sample_counts["companion_ready_likely"]
        + sample_counts["context_ready_likely"]
    )
    sample_total = sum(sample_counts.values())
    ready_density = ready / sample_total if sample_total else 0.0
    if unreviewed_count >= SAMPLE_LIMIT and ready_density >= 0.25:
        return "prepare_batch5_review"
    dominant_blocker, blocker_count = ("none", 0)
    for name, count in bucket_counts.most_common():
        if name not in {"exact_pair_clean"}:
            dominant_blocker, blocker_count = name, count
            break
    top_global = global_rows[0]["issue_family"] if global_rows else "none"
    if blocker_count >= max(ready, 1):
        return f"prepare_dominant_sublane:{dominant_blocker}"
    if top_global not in PAIR_FAMILIES:
        return f"compare_migration_to_global_bottleneck:{top_global}"
    return "autofix_semantic_near_saturation_review_smaller_batch_or_migrate"


def write_reports(
    *,
    segment_state_run_id: int,
    ledger_run_id: int,
    reviewed_paths: list[str],
    rows: list[dict[str, Any]],
    unreviewed_rows: list[dict[str, Any]],
    classified: list[dict[str, Any]],
    global_rows: list[dict[str, Any]],
    reviewed_ids: set[int],
) -> tuple[Path, Path, dict[str, Any]]:
    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    txt_path = reports_dir / f"{stamp}_autofix_semantic_saturation_diagnostic.txt"
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_saturation_diagnostic.jsonl"

    bucket_counts = Counter(item["bucket"] for item in classified)
    readiness_counts = Counter(item["readiness"] for item in classified[:SAMPLE_LIMIT])
    flag_counts = Counter()
    for item in classified:
        for flag in (
            "exact_pair",
            "pair_plus_high_out_of_scope",
            "with_domain",
            "with_dynamic",
            "with_context",
            "with_residual",
            "with_gender_custom_loc",
            "probable_new_microagent",
        ):
            if item.get(flag):
                flag_counts[flag] += 1

    rec = recommendation(len(unreviewed_rows), readiness_counts, bucket_counts, global_rows)
    summary = {
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "reviewed_jsonl_count": len(reviewed_paths),
        "reviewed_segment_ids_loaded": len(reviewed_ids),
        "pending_autofix_semantic_pair": len(rows),
        "pending_autofix_semantic_pair_unreviewed": len(unreviewed_rows),
        "bucket_counts": dict(bucket_counts),
        "flag_counts": dict(flag_counts),
        "batch5_sample_size": min(SAMPLE_LIMIT, len(classified)),
        "batch5_readiness_estimate": dict(readiness_counts),
        "recommendation": rec,
        "top_global_bottlenecks": global_rows[:10],
    }

    lines = [
        "Autofix + semantic saturation diagnostic",
        f"segment_state_run_id: {segment_state_run_id}",
        f"ledger_run_id: {ledger_run_id}",
        "",
        "Inputs:",
        *[f"- exclude_reviewed_jsonl: {path}" for path in reviewed_paths],
        "",
        "Summary:",
        f"- pending_autofix_semantic_pair: {len(rows):,}",
        f"- reviewed_segment_ids_loaded: {len(reviewed_ids):,}",
        f"- pending_autofix_semantic_pair_unreviewed: {len(unreviewed_rows):,}",
        "",
        "Bucket distribution among unreviewed:",
    ]
    if bucket_counts:
        for key, count in bucket_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Profile flags among unreviewed:"])
    for key in (
        "exact_pair",
        "pair_plus_high_out_of_scope",
        "with_domain",
        "with_dynamic",
        "with_context",
        "with_residual",
        "with_gender_custom_loc",
        "probable_new_microagent",
    ):
        lines.append(f"- {key}: {flag_counts.get(key, 0):,}")
    lines.extend(["", f"Batch5 sample estimate, first {summary['batch5_sample_size']:,} unreviewed by priority:"])
    if readiness_counts:
        for key, count in readiness_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Top global bottlenecks:"])
    for row in global_rows[:10]:
        lines.append(
            f"- {row['issue_family']}: pending_segments={int(row['pending_segments'] or 0):,}, "
            f"open_issues={int(row['open_issues'] or 0):,}, high_issues={int(row['high_issues'] or 0):,}"
        )
    lines.extend(["", f"Recommendation: {rec}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_rows: list[dict[str, Any]] = [{"type": "summary", **summary}]
    for row, item in zip(unreviewed_rows[:SAMPLE_LIMIT], classified[:SAMPLE_LIMIT], strict=False):
        json_rows.append(
            {
                "type": "sample",
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "bucket": item["bucket"],
                "readiness": item["readiness"],
                "word_count": item["word_count"],
                "open_issue_count": row["open_issue_count"],
                "other_issue_count": row["other_issue_count"],
                "high_out_of_scope_count": row["high_out_of_scope_count"],
                "issue_families": row["issue_families"],
            }
        )
    jsonl_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in json_rows) + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--exclude-reviewed-jsonl", action="append", default=[])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reviewed_ids = read_reviewed_segment_ids(args.exclude_reviewed_jsonl)
    with open_readonly_connection() as conn:
        pair_rows = fetch_pair_rows(conn, args.segment_state_run_id, args.ledger_run_id)
        unreviewed_rows = [row for row in pair_rows if int(row["segment_id"]) not in reviewed_ids]
        classified = [classify(row) for row in unreviewed_rows]
        global_rows = fetch_global_bottlenecks(conn, args.segment_state_run_id, args.ledger_run_id)
    txt_path, jsonl_path, summary = write_reports(
        segment_state_run_id=args.segment_state_run_id,
        ledger_run_id=args.ledger_run_id,
        reviewed_paths=args.exclude_reviewed_jsonl,
        rows=pair_rows,
        unreviewed_rows=unreviewed_rows,
        classified=classified,
        global_rows=global_rows,
        reviewed_ids=reviewed_ids,
    )
    print(f"pending_pair={summary['pending_autofix_semantic_pair']}")
    print(f"unreviewed_pair={summary['pending_autofix_semantic_pair_unreviewed']}")
    print(f"recommendation={summary['recommendation']}")
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")


if __name__ == "__main__":
    main()
