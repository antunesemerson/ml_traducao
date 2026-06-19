from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_relation_semantic_composer_dry_run_v2"
DIAGNOSTIC_RULE_VERSION = "issue_relation_possessive_context_diagnostic_v1"
COMPOSER_NAME = "relation_leading_possessive_semantic_composer_v2"
SOURCE_ROUTES = {
    "leading_possessive_relation_with_name",
    "visible_pair_possessive_before_relation",
}

RELATION_TOKEN_RE = re.compile(
    r"\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\]",
    re.IGNORECASE,
)
LEADING_POSSESSIVE_RE = re.compile(
    r"^(?P<prefix>\s*)(?P<poss>Meu|Minha|Seu|Sua|meu|minha|seu|sua)\s+"
    r"(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])",
    re.IGNORECASE,
)
PAIR_FORM_RE = re.compile(
    r"(?P<prefix>\b)(?P<pair>meu/minha|minha/meu|meu\(a\)|sua/seu|seu/sua)\s+"
    r"(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])",
    re.IGNORECASE,
)
NAME_AFTER_RELATION_RE = re.compile(
    r"\]\s+(?:\[[^\]]*(?:Get(?:Titled)?FirstName|GetName|GetShortUIName|GetTitledFirstName)[^\]]*\]|[A-Z])",
    re.IGNORECASE,
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_relation_semantic_composer_dry_run_diag_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_relation_possessive_context_diagnostic_runs
        WHERE rule_version = ?
          AND candidate_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (DIAGNOSTIC_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No relation possessive diagnostic run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_relation_semantic_composer_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            composer_name TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            composed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            semantic_review_required_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_relation_semantic_composer_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            route_key TEXT NOT NULL,
            composer_status TEXT NOT NULL,
            block_reason TEXT,
            semantic_review_required INTEGER NOT NULL DEFAULT 1,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_relation_semantic_composer_runs(id) ON DELETE CASCADE
        )
        """
    )


def add_uppercase_filter(token: str) -> str:
    if token.endswith("]") and "|U" not in token:
        return token[:-1] + "|U]"
    return token


def token_list(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]", text or "")


def normalize_token(token: str) -> str:
    return token.replace("|U", "").replace("|l", "").replace("|L", "")


def compatible_token_change(current: str, corrected: str) -> bool:
    before = token_list(current)
    after = token_list(corrected)
    if len(before) != len(after):
        return False
    return [normalize_token(token) for token in before] == [normalize_token(token) for token in after]


def normalize_relation_boundary_spacing(text: str) -> str:
    return re.sub(r"(\|U\])\s+,", r"\1,", text)


def compose(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("current_text") or ""
    route_key = row.get("route_key") or ""
    reasons: list[str] = []
    corrected = current

    if route_key == "leading_possessive_relation_with_name":
        match = LEADING_POSSESSIVE_RE.search(current)
        if not match:
            reasons.append("leading_possessive_pattern_not_found")
        else:
            corrected = LEADING_POSSESSIVE_RE.sub(
                lambda m: f"{m.group('prefix')}{add_uppercase_filter(m.group('token'))}",
                current,
                count=1,
            )
            corrected = normalize_relation_boundary_spacing(corrected)
            if not NAME_AFTER_RELATION_RE.search(corrected[:260]):
                reasons.append("no_clear_name_after_relation_token")
    elif route_key == "visible_pair_possessive_before_relation":
        if not PAIR_FORM_RE.search(current):
            reasons.append("pair_form_pattern_not_found")
        else:
            corrected = PAIR_FORM_RE.sub(
                lambda m: f"{m.group('prefix')}{add_uppercase_filter(m.group('token'))}",
                current,
                count=1,
            )
            corrected = normalize_relation_boundary_spacing(corrected)
    else:
        reasons.append("route_not_supported_by_composer")

    if corrected == current:
        reasons.append("no_text_delta")
    if not compatible_token_change(current, corrected):
        reasons.append("unsupported_structural_token_delta")

    status = "composed_review_required" if not reasons else "blocked"
    return {
        **row,
        "composer_status": status,
        "block_reason": ";".join(reasons),
        "semantic_review_required": 1,
        "corrected_text": corrected if status != "blocked" else "",
    }


def fetch_source_rows(conn, diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_relation_possessive_context_diagnostic_items
        WHERE run_id = ?
          AND route_key IN ('leading_possessive_relation_with_name', 'visible_pair_possessive_before_relation')
        ORDER BY route_key, relative_path, source_line_number, segment_id
        """,
        (diagnostic_run_id,),
    ).fetchall()
    return [compose(dict(row)) for row in rows]


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    diagnostic_run_id: int,
    state_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "composer_status",
        "block_reason",
        "semantic_review_required",
        "route_key",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "current_text",
        "corrected_text",
        "english_text",
        "spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(row["composer_status"] for row in rows)
    route_counts = Counter(row["route_key"] for row in rows)
    block_counts = Counter(row["block_reason"] or "none" for row in rows)
    lines = [
        "Issue relation semantic composer dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Composer name: {COMPOSER_NAME}",
        f"Run id: {run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Segment-state run id: {state_run_id}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Composed review-required: {status_counts.get('composed_review_required', 0):,}",
        f"- Blocked: {status_counts.get('blocked', 0):,}",
        f"- Semantic review required: {sum(int(row['semantic_review_required']) for row in rows):,}",
        "",
        "Routes:",
        *[f"- {label}: {count:,}" for label, count in route_counts.most_common()],
        "",
        "Blocks:",
        *[f"- {label}: {count:,}" for label, count in block_counts.most_common()],
        "",
        "Composed samples:",
    ]
    for row in [item for item in rows if item["composer_status"] == "composed_review_required"][:25]:
        lines.extend(
            [
                f"- {row['route_key']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
            ]
        )
    lines.extend(["", "Blocked samples:"])
    blocked = [item for item in rows if item["composer_status"] == "blocked"]
    if blocked:
        for row in blocked[:25]:
            lines.append(
                f"- {row['block_reason']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Dry-run only: no confirmations, no source/output writes, no production run.",
            "- All composed rows require review before checkpoint/apply because this is a semantic rewrite.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        diagnostic = conn.execute(
            "SELECT * FROM ml_issue_relation_possessive_context_diagnostic_runs WHERE id = ?",
            (selected_diagnostic_run_id,),
        ).fetchone()
        if diagnostic is None:
            raise RuntimeError(f"Diagnostic run not found: {selected_diagnostic_run_id}")
        diagnostic = dict(diagnostic)
        rows = fetch_source_rows(conn, selected_diagnostic_run_id)
        state_run_id = int(diagnostic["segment_state_run_id"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_diagnostic_run_id)
        status_counts = Counter(row["composer_status"] for row in rows)
        route_counts = Counter(row["route_key"] for row in rows)
        block_counts = Counter(row["block_reason"] or "none" for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_relation_semantic_composer_runs (
                rule_version,
                diagnostic_run_id,
                segment_state_run_id,
                composer_name,
                candidate_count,
                composed_count,
                blocked_count,
                semantic_review_required_count,
                production_release_allowed,
                route_counts_json,
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_diagnostic_run_id,
                state_run_id,
                COMPOSER_NAME,
                len(rows),
                status_counts.get("composed_review_required", 0),
                status_counts.get("blocked", 0),
                sum(int(row["semantic_review_required"]) for row in rows),
                json.dumps(route_counts, ensure_ascii=False, sort_keys=True),
                json.dumps(block_counts, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_relation_semantic_composer_items (
                    run_id,
                    diagnostic_run_id,
                    diagnostic_item_id,
                    segment_state_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    route_key,
                    composer_status,
                    block_reason,
                    semantic_review_required,
                    current_text,
                    corrected_text,
                    english_text,
                    spanish_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_diagnostic_run_id,
                    int(row["id"]),
                    state_run_id,
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    int(row.get("source_line_number") or 0),
                    row["route_key"],
                    row["composer_status"],
                    row["block_reason"],
                    int(row["semantic_review_required"]),
                    row["current_text"],
                    row["corrected_text"],
                    row.get("english_text") or "",
                    row.get("spanish_text") or "",
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            diagnostic_run_id=selected_diagnostic_run_id,
            state_run_id=state_run_id,
            rows=rows,
        )
        conn.commit()
    print(f"Relation semantic composer dry-run: {run_id}")
    print(f"Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"Candidates: {len(rows):,}")
    print(f"Composed review-required: {status_counts.get('composed_review_required', 0):,}")
    print(f"Blocked: {status_counts.get('blocked', 0):,}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "candidate_count": len(rows),
        "composed_count": status_counts.get("composed_review_required", 0),
        "blocked_count": status_counts.get("blocked", 0),
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run semantic composer for RelationToMe possessive surfaces.")
    parser.add_argument("--diagnostic-run-id", type=int)
    args = parser.parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id)
