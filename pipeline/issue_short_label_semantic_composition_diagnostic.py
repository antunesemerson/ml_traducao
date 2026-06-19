from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_semantic_composition_diagnostic_v1"
DIAGNOSTIC_NAME = "short_label_semantic_large_bottleneck_v1"
PRODUCTION_RELEASE_ALLOWED = 0

SHORT_FAMILY_PREFIXES = ("short_label",)
SEMANTIC_FAMILY_PREFIXES = ("semantic", "semantic_review")
CK3_TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|\$[^$]+\$|Select_CString|Custom\(|Get[A-Z][A-Za-z_]*|ROOT\.|CHARACTER\.|TARGET\.|#\w+)",
    re.UNICODE,
)
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9_']+", re.UNICODE)


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_partial_coverage_run(conn, partial_run_id: int | None) -> dict[str, Any]:
    if partial_run_id is None:
        row = conn.execute(
            """
            SELECT *
            FROM ml_issue_partial_coverage_runs
            WHERE finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM ml_issue_partial_coverage_runs
            WHERE id = ?
            """,
            (partial_run_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("No finished ml_issue_partial_coverage_runs row found.")
    return dict(row)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_semantic_composition_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_semantic_composition_diagnostic_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            diagnostic_name TEXT NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            total_relevant_segments INTEGER NOT NULL DEFAULT 0,
            dual_family_segments INTEGER NOT NULL DEFAULT 0,
            short_only_segments INTEGER NOT NULL DEFAULT 0,
            semantic_only_segments INTEGER NOT NULL DEFAULT 0,
            dual_covered_ready_recheck_count INTEGER NOT NULL DEFAULT 0,
            semantic_covered_short_open_count INTEGER NOT NULL DEFAULT 0,
            short_covered_semantic_open_count INTEGER NOT NULL DEFAULT 0,
            dual_uncovered_short_semantic_count INTEGER NOT NULL DEFAULT 0,
            short_only_open_count INTEGER NOT NULL DEFAULT 0,
            semantic_only_open_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            tier_counts_json TEXT,
            path_group_counts_json TEXT,
            surface_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(partial_coverage_run_id) REFERENCES ml_issue_partial_coverage_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_semantic_composition_diagnostic_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            path_group TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            final_state TEXT,
            state_group TEXT,
            coverage_state TEXT NOT NULL,
            review_state TEXT NOT NULL,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            has_short_label INTEGER NOT NULL DEFAULT 0,
            has_semantic_review INTEGER NOT NULL DEFAULT 0,
            short_label_covered INTEGER NOT NULL DEFAULT 0,
            semantic_review_covered INTEGER NOT NULL DEFAULT 0,
            short_label_open INTEGER NOT NULL DEFAULT 0,
            semantic_review_open INTEGER NOT NULL DEFAULT 0,
            composition_bucket TEXT NOT NULL,
            opportunity_tier TEXT NOT NULL,
            recommended_next_step TEXT NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            surface_bucket TEXT NOT NULL,
            has_ck3_token INTEGER NOT NULL DEFAULT 0,
            has_markup INTEGER NOT NULL DEFAULT 0,
            has_newline INTEGER NOT NULL DEFAULT 0,
            issue_families_json TEXT,
            covered_families_json TEXT,
            open_families_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            old_text TEXT,
            output_text TEXT,
            confirmed_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_semantic_composition_diagnostic_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, segment_id)
        );

        CREATE INDEX IF NOT EXISTS idx_short_semantic_composition_items_run_bucket
        ON ml_issue_short_label_semantic_composition_diagnostic_items(run_id, composition_bucket, opportunity_tier);

        CREATE INDEX IF NOT EXISTS idx_short_semantic_composition_items_path
        ON ml_issue_short_label_semantic_composition_diagnostic_items(run_id, path_group, surface_bucket);

        CREATE INDEX IF NOT EXISTS idx_short_semantic_composition_items_segment
        ON ml_issue_short_label_semantic_composition_diagnostic_items(segment_id);
        """
    )


def family_count(payload: dict[str, Any], prefixes: tuple[str, ...]) -> int:
    total = 0
    for family, count in payload.items():
        if any(str(family).startswith(prefix) for prefix in prefixes):
            try:
                total += int(count or 0)
            except (TypeError, ValueError):
                total += 1
    return total


def path_group(relative_path: str) -> str:
    value = relative_path.replace("\\", "/")
    if "/" in value:
        return value.split("/", 1)[0] or "root"
    name = Path(value).name
    for suffix in ("_l_spanish.yml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)] or "root"
    return name or "root"


def word_count(value: str | None) -> int:
    return len(WORD_RE.findall(value or ""))


def surface_bucket(words: int) -> str:
    if words <= 0:
        return "empty"
    if words == 1:
        return "single_word"
    if words <= 3:
        return "short_phrase_2_3"
    if words <= 8:
        return "compact_phrase_4_8"
    return "long_9_plus"


def text_for_surface(row: dict[str, Any]) -> str:
    for column in ("confirmed_text", "output_text", "old_text", "spanish_text", "english_text"):
        value = row.get(column)
        if value is not None and str(value) != "":
            return str(value)
    return ""


def classify_bucket(
    *,
    has_short: bool,
    has_semantic: bool,
    short_covered: bool,
    semantic_covered: bool,
    short_open: bool,
    semantic_open: bool,
    coverage_state: str,
) -> str:
    if has_short and has_semantic:
        if short_covered and semantic_covered and not short_open and not semantic_open:
            return "dual_covered_ready_recheck"
        if semantic_covered and short_open and not semantic_open:
            return "semantic_covered_short_open"
        if short_covered and semantic_open and not short_open:
            return "short_covered_semantic_open"
        if short_open and semantic_open and not short_covered and not semantic_covered:
            return "dual_uncovered_short_semantic"
        if short_open or semantic_open:
            return "dual_mixed_open"
        if coverage_state == "full":
            return "dual_covered_ready_recheck"
        return "dual_mixed_other"
    if has_short:
        if short_covered and not short_open:
            return "short_only_covered"
        if short_open:
            return "short_only_open"
        return "short_only_other"
    if has_semantic:
        if semantic_covered and not semantic_open:
            return "semantic_only_covered"
        if semantic_open:
            return "semantic_only_open"
        return "semantic_only_other"
    return "not_target"


def opportunity_tier(bucket: str) -> str:
    mapping = {
        "dual_covered_ready_recheck": "tier_1_whole_segment_recheck",
        "semantic_covered_short_open": "tier_2_expand_short_label_lane",
        "short_covered_semantic_open": "tier_3_expand_semantic_lane",
        "dual_uncovered_short_semantic": "tier_4_cluster_dual_uncovered",
        "short_only_open": "tier_5_short_only_scale",
        "semantic_only_open": "tier_6_semantic_only_scale",
    }
    return mapping.get(bucket, "tier_7_monitor_or_cluster")


def recommended_next_step(bucket: str) -> str:
    mapping = {
        "dual_covered_ready_recheck": "create_whole_segment_recheck_queue",
        "semantic_covered_short_open": "expand_short_label_context_or_surface_microagent",
        "short_covered_semantic_open": "expand_semantic_pair_or_context_microagent",
        "dual_uncovered_short_semantic": "cluster_dual_uncovered_by_path_and_surface",
        "short_only_open": "route_short_only_to_short_label_lanes",
        "semantic_only_open": "route_semantic_only_to_semantic_router",
        "short_only_covered": "monitor_short_label_covered_without_lifecycle_need",
        "semantic_only_covered": "monitor_semantic_covered_without_lifecycle_need",
    }
    return mapping.get(bucket, "inspect_mixed_state_before_new_policy")


def fetch_candidate_rows(conn, partial_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_conf AS (
            SELECT c.*
            FROM segment_confirmations c
            JOIN (
                SELECT segment_id, MAX(id) AS max_id
                FROM segment_confirmations
                GROUP BY segment_id
            ) latest ON latest.segment_id = c.segment_id
                    AND latest.max_id = c.id
        )
        SELECT
            pci.*,
            source.english_text,
            source.spanish_text,
            source.old_text,
            output.portuguese_text AS output_text,
            conf.confirmed_text
        FROM ml_issue_partial_coverage_items pci
        JOIN source_segments source ON source.id = pci.segment_id
        LEFT JOIN output_segments output ON output.segment_id = pci.segment_id
        LEFT JOIN latest_conf conf ON conf.segment_id = pci.segment_id
        WHERE pci.run_id = ?
          AND pci.is_closed = 0
          AND (
              pci.issue_families_json LIKE '%short_label%'
              OR pci.issue_families_json LIKE '%semantic%'
          )
        ORDER BY pci.relative_path, pci.source_line_number, pci.segment_id
        """,
        (partial_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_item(row: dict[str, Any], partial_run: dict[str, Any], run_id: int) -> dict[str, Any] | None:
    issue_families = parse_json_dict(row.get("issue_families_json"))
    covered_families = parse_json_dict(row.get("covered_families_json"))
    open_families = parse_json_dict(row.get("open_families_json"))

    has_short = family_count(issue_families, SHORT_FAMILY_PREFIXES) > 0
    has_semantic = family_count(issue_families, SEMANTIC_FAMILY_PREFIXES) > 0
    if not has_short and not has_semantic:
        return None

    short_covered = family_count(covered_families, SHORT_FAMILY_PREFIXES) > 0
    semantic_covered = family_count(covered_families, SEMANTIC_FAMILY_PREFIXES) > 0
    short_open = family_count(open_families, SHORT_FAMILY_PREFIXES) > 0
    semantic_open = family_count(open_families, SEMANTIC_FAMILY_PREFIXES) > 0
    coverage_state = str(row.get("coverage_state") or "none")
    bucket = classify_bucket(
        has_short=has_short,
        has_semantic=has_semantic,
        short_covered=short_covered,
        semantic_covered=semantic_covered,
        short_open=short_open,
        semantic_open=semantic_open,
        coverage_state=coverage_state,
    )
    text = text_for_surface(row)
    words = word_count(text)
    return {
        "run_id": run_id,
        "partial_coverage_run_id": int(partial_run["id"]),
        "ledger_run_id": int(partial_run["ledger_run_id"]),
        "segment_state_run_id": int(partial_run["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "path_group": path_group(row.get("relative_path") or ""),
        "source_key": row.get("source_key") or "",
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state") or "",
        "state_group": row.get("state_group") or "",
        "coverage_state": coverage_state,
        "review_state": row.get("review_state") or "",
        "total_issue_count": int(row.get("total_issue_count") or 0),
        "covered_issue_count": int(row.get("covered_issue_count") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "has_short_label": 1 if has_short else 0,
        "has_semantic_review": 1 if has_semantic else 0,
        "short_label_covered": 1 if short_covered else 0,
        "semantic_review_covered": 1 if semantic_covered else 0,
        "short_label_open": 1 if short_open else 0,
        "semantic_review_open": 1 if semantic_open else 0,
        "composition_bucket": bucket,
        "opportunity_tier": opportunity_tier(bucket),
        "recommended_next_step": recommended_next_step(bucket),
        "text_length": len(text),
        "word_count": words,
        "surface_bucket": surface_bucket(words),
        "has_ck3_token": 1 if CK3_TOKEN_RE.search(text) else 0,
        "has_markup": 1 if "#" in text else 0,
        "has_newline": 1 if "\n" in text else 0,
        "issue_families_json": json.dumps(issue_families, ensure_ascii=False, sort_keys=True),
        "covered_families_json": json.dumps(covered_families, ensure_ascii=False, sort_keys=True),
        "open_families_json": json.dumps(open_families, ensure_ascii=False, sort_keys=True),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "created_at": db.utc_now(),
    }


def top_nested_counts(items: list[dict[str, Any]], key_a: str, key_b: str, limit: int = 12) -> dict[str, list[tuple[str, int]]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        counters[str(item[key_a])][str(item[key_b])] += 1
    return {
        bucket: counter.most_common(limit)
        for bucket, counter in sorted(counters.items(), key=lambda pair: (-sum(pair[1].values()), pair[0]))
    }


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = [
        "run_id",
        "partial_coverage_run_id",
        "ledger_run_id",
        "segment_state_run_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "source_line_number",
        "final_state",
        "state_group",
        "coverage_state",
        "review_state",
        "total_issue_count",
        "covered_issue_count",
        "open_issue_count",
        "has_short_label",
        "has_semantic_review",
        "short_label_covered",
        "semantic_review_covered",
        "short_label_open",
        "semantic_review_open",
        "composition_bucket",
        "opportunity_tier",
        "recommended_next_step",
        "text_length",
        "word_count",
        "surface_bucket",
        "has_ck3_token",
        "has_markup",
        "has_newline",
        "issue_families_json",
        "covered_families_json",
        "open_families_json",
        "english_text",
        "spanish_text",
        "old_text",
        "output_text",
        "confirmed_text",
        "created_at",
    ]
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"""
        INSERT INTO ml_issue_short_label_semantic_composition_diagnostic_items
        ({", ".join(columns)})
        VALUES ({placeholders})
        """,
        [tuple(item.get(column) for column in columns) for item in items],
    )


def write_outputs(
    *,
    items: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    partial_run: dict[str, Any],
    sample_limit: int,
) -> None:
    bucket_counts = Counter(item["composition_bucket"] for item in items)
    tier_counts = Counter(item["opportunity_tier"] for item in items)
    path_counts = Counter(item["path_group"] for item in items)
    surface_counts = Counter(item["surface_bucket"] for item in items)
    path_by_bucket = top_nested_counts(items, "composition_bucket", "path_group")
    surface_by_bucket = top_nested_counts(items, "composition_bucket", "surface_bucket")

    lines: list[str] = [
        "Short-label + semantic-review composition diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Diagnostic run id: {run_id}",
        f"Partial coverage run id: {partial_run['id']}",
        f"Ledger run id: {partial_run['ledger_run_id']}",
        f"Segment-state run id: {partial_run['segment_state_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Relevant pending segments: {len(items):,}",
        f"- Dual-family segments: {sum(1 for item in items if item['has_short_label'] and item['has_semantic_review']):,}",
        f"- Short-only segments: {sum(1 for item in items if item['has_short_label'] and not item['has_semantic_review']):,}",
        f"- Semantic-only segments: {sum(1 for item in items if item['has_semantic_review'] and not item['has_short_label']):,}",
        "",
        "Opportunity tiers:",
    ]
    for tier, count in tier_counts.most_common():
        lines.append(f"- {tier}: {count:,}")
    lines.extend(["", "Composition buckets:"])
    for bucket, count in bucket_counts.most_common():
        lines.append(f"- {bucket}: {count:,}")
    lines.extend(["", "Top path groups:"])
    for path, count in path_counts.most_common(20):
        lines.append(f"- {path}: {count:,}")
    lines.extend(["", "Surface buckets:"])
    for surface, count in surface_counts.most_common():
        lines.append(f"- {surface}: {count:,}")

    lines.extend(["", "Top path groups by composition bucket:"])
    for bucket, values in path_by_bucket.items():
        lines.append(f"- {bucket}:")
        for path, count in values[:10]:
            lines.append(f"  - {path}: {count:,}")

    lines.extend(["", "Surface mix by composition bucket:"])
    for bucket, values in surface_by_bucket.items():
        lines.append(f"- {bucket}:")
        for surface, count in values[:8]:
            lines.append(f"  - {surface}: {count:,}")

    lines.extend(
        [
            "",
            "Practical interpretation:",
            "- Tier 1 is the fastest scale opportunity: existing issue-level coverage should move to whole-segment recheck.",
            "- Tier 2 should expand short-label lanes because semantic coverage is already present.",
            "- Tier 3 should expand semantic/context lanes because short-label coverage is already present.",
            "- Tier 4 is research/discovery: cluster by path and surface before creating more microagents.",
            "",
            "Samples by bucket:",
        ]
    )
    for bucket in sorted(bucket_counts):
        lines.append(f"- {bucket}:")
        shown = 0
        for item in items:
            if item["composition_bucket"] != bucket:
                continue
            lines.append(
                "  - "
                f"segment={item['segment_id']} | path={item['relative_path']} | key={item['source_key']} | "
                f"issues={item['covered_issue_count']}/{item['total_issue_count']} | "
                f"open={item['open_issue_count']} | surface={item['surface_bucket']} | "
                f"next={item['recommended_next_step']}"
            )
            shown += 1
            if shown >= sample_limit:
                break
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    csv_columns = [
        "run_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "coverage_state",
        "review_state",
        "total_issue_count",
        "covered_issue_count",
        "open_issue_count",
        "composition_bucket",
        "opportunity_tier",
        "recommended_next_step",
        "surface_bucket",
        "word_count",
        "text_length",
        "has_ck3_token",
        "has_markup",
        "has_newline",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns)
        writer.writeheader()
        for item in items:
            writer.writerow({column: item.get(column) for column in csv_columns})

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def run(partial_run_id: int | None, sample_limit: int) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        partial_run = latest_partial_coverage_run(conn, partial_run_id)
        report_path, csv_path, jsonl_path = report_paths(settings)
        now = db.utc_now()
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_semantic_composition_diagnostic_runs (
                rule_version,
                diagnostic_name,
                partial_coverage_run_id,
                ledger_run_id,
                segment_state_run_id,
                production_release_allowed,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DIAGNOSTIC_NAME,
                int(partial_run["id"]),
                int(partial_run["ledger_run_id"]),
                int(partial_run["segment_state_run_id"]),
                PRODUCTION_RELEASE_ALLOWED,
                str(report_path),
                str(csv_path),
                str(jsonl_path),
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        rows = fetch_candidate_rows(conn, int(partial_run["id"]))
        items = [
            item
            for item in (build_item(row, partial_run, run_id) for row in rows)
            if item is not None and item["composition_bucket"] != "not_target"
        ]
        insert_items(conn, items)

        bucket_counts = Counter(item["composition_bucket"] for item in items)
        tier_counts = Counter(item["opportunity_tier"] for item in items)
        path_group_counts = Counter(item["path_group"] for item in items)
        surface_counts = Counter(item["surface_bucket"] for item in items)
        finished_at = db.utc_now()
        conn.execute(
            """
            UPDATE ml_issue_short_label_semantic_composition_diagnostic_runs
            SET
                total_relevant_segments = ?,
                dual_family_segments = ?,
                short_only_segments = ?,
                semantic_only_segments = ?,
                dual_covered_ready_recheck_count = ?,
                semantic_covered_short_open_count = ?,
                short_covered_semantic_open_count = ?,
                dual_uncovered_short_semantic_count = ?,
                short_only_open_count = ?,
                semantic_only_open_count = ?,
                bucket_counts_json = ?,
                tier_counts_json = ?,
                path_group_counts_json = ?,
                surface_counts_json = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                sum(1 for item in items if item["has_short_label"] and item["has_semantic_review"]),
                sum(1 for item in items if item["has_short_label"] and not item["has_semantic_review"]),
                sum(1 for item in items if item["has_semantic_review"] and not item["has_short_label"]),
                bucket_counts.get("dual_covered_ready_recheck", 0),
                bucket_counts.get("semantic_covered_short_open", 0),
                bucket_counts.get("short_covered_semantic_open", 0),
                bucket_counts.get("dual_uncovered_short_semantic", 0),
                bucket_counts.get("short_only_open", 0),
                bucket_counts.get("semantic_only_open", 0),
                json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(tier_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(path_group_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(surface_counts), ensure_ascii=False, sort_keys=True),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        write_outputs(
            items=items,
            report_path=report_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            partial_run=partial_run,
            sample_limit=sample_limit,
        )
        conn.commit()
        return {
            "run_id": run_id,
            "partial_coverage_run_id": int(partial_run["id"]),
            "ledger_run_id": int(partial_run["ledger_run_id"]),
            "segment_state_run_id": int(partial_run["segment_state_run_id"]),
            "total_relevant_segments": len(items),
            "bucket_counts": dict(bucket_counts),
            "tier_counts": dict(tier_counts),
            "report_path": str(report_path),
            "csv_path": str(csv_path),
            "jsonl_path": str(jsonl_path),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostic for large short_label + semantic_review composition bottlenecks."
    )
    parser.add_argument("--partial-run-id", type=int, default=None)
    parser.add_argument("--sample-limit", type=int, default=8)
    args = parser.parse_args()
    result = run(args.partial_run_id, args.sample_limit)
    print("[issue_short_label_semantic_composition_diagnostic] Complete")
    print(f"[issue_short_label_semantic_composition_diagnostic] Run id: {result['run_id']}")
    print(
        "[issue_short_label_semantic_composition_diagnostic] "
        f"Relevant pending segments: {result['total_relevant_segments']:,}"
    )
    print(f"[issue_short_label_semantic_composition_diagnostic] Buckets: {result['bucket_counts']}")
    print(f"[issue_short_label_semantic_composition_diagnostic] Report: {result['report_path']}")


if __name__ == "__main__":
    main()
