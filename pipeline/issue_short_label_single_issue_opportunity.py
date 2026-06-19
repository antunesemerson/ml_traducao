from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_single_issue_opportunity_v1"
AGENT_KEY = "micro_short_label_style"
ISSUE_FAMILY = "short_label_style_microagent"
ISSUE_KIND = "short_or_compact_label_reopened"

ALLOWED_TOKEN_IMPACTS = {"", "none_or_unknown", "usually_same_tokens", "same_tokens"}
ALLOWED_TOKEN_STATUSES = {"", "ok", "none", "unknown"}
REVIEWABLE_PROFILES = {
    "auto_safe_single_no_token_short",
    "single_no_token_short_label",
    "single_low_token_short_text",
    "single_rules_tooltip_low_token",
    "single_activity_low_token",
    "single_medium_token_short_ui",
}


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


def package_name(relative_path: str | None) -> str:
    path = relative_path or "unknown"
    if "/" in path:
        return path.split("/", 1)[0]
    return path


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_single_issue_opportunity"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_partial_coverage_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished ml_issue_partial_coverage_runs found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_single_issue_opportunity_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            reviewable_count INTEGER NOT NULL DEFAULT 0,
            held_count INTEGER NOT NULL DEFAULT 0,
            estimated_single_issue_segments INTEGER NOT NULL DEFAULT 0,
            profile_counts_json TEXT,
            domain_counts_json TEXT,
            package_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_single_issue_opportunity_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            profile TEXT NOT NULL,
            review_bucket TEXT NOT NULL,
            hold_reason TEXT,
            domain TEXT NOT NULL,
            package TEXT NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            issue_codes_json TEXT,
            token_status TEXT,
            token_impact TEXT,
            route_status TEXT,
            ledger_status TEXT,
            final_state TEXT,
            review_state TEXT,
            english_preview TEXT,
            spanish_preview TEXT,
            portuguese_preview TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_single_issue_opportunity_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_single_issue_opportunity_items_run
        ON ml_issue_short_label_single_issue_opportunity_items(run_id, review_bucket, profile);

        CREATE INDEX IF NOT EXISTS idx_short_label_single_issue_opportunity_items_segment
        ON ml_issue_short_label_single_issue_opportunity_items(segment_id);
        """
    )


def fetch_partial_run(conn, *, partial_coverage_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (partial_coverage_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_coverage_run_id}")
    return dict(row)


def fetch_candidates(conn, *, partial_coverage_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            pci.id AS partial_coverage_item_id,
            pci.ledger_run_id,
            pci.segment_state_run_id,
            pci.segment_id,
            pci.relative_path,
            pci.source_key,
            pci.source_line_number,
            pci.final_state,
            pci.review_state,
            pci.coverage_state,
            pci.total_issue_count,
            pci.open_issue_count,
            l.id AS ledger_item_id,
            l.issue_family,
            l.issue_kind,
            l.agent_key,
            l.status AS ledger_status,
            l.route_status,
            l.token_impact,
            l.token_status,
            l.evidence_json AS ledger_evidence_json,
            ss.english_text,
            ss.spanish_text,
            os.portuguese_text
        FROM ml_issue_partial_coverage_items pci
        JOIN ml_issue_ledger_items l
          ON l.run_id = pci.ledger_run_id
         AND l.segment_id = pci.segment_id
         AND l.issue_family = ?
         AND l.issue_kind = ?
        LEFT JOIN source_segments ss ON ss.id = pci.segment_id
        LEFT JOIN output_segments os ON os.segment_id = pci.segment_id
        WHERE pci.run_id = ?
          AND pci.coverage_state = 'none'
          AND pci.total_issue_count = 1
          AND pci.open_issue_count = 1
        ORDER BY pci.relative_path, pci.source_line_number, pci.source_key, pci.segment_id
        """,
        (ISSUE_FAMILY, ISSUE_KIND, partial_coverage_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    evidence = parse_json(row.get("ledger_evidence_json"), {})
    domain = str(evidence.get("domain") or "domain_unknown")
    text_length = int(evidence.get("text_length") or 0)
    token_count = int(evidence.get("token_count") or 0)
    word_count = int(evidence.get("word_count") or 0)
    issue_codes = [str(item) for item in evidence.get("issue_codes") or [] if str(item).strip()]
    token_status = str(row.get("token_status") or "").strip().lower()
    token_impact = str(row.get("token_impact") or "").strip().lower()
    final_state = str(row.get("final_state") or "")
    review_state = str(row.get("review_state") or "")

    facts = {
        "domain": domain,
        "text_length": text_length,
        "token_count": token_count,
        "word_count": word_count,
        "issue_codes": issue_codes,
    }

    if issue_codes:
        return "hold_issue_codes_or_surface_risk", "hold", "issue_codes_present", facts
    if token_status not in ALLOWED_TOKEN_STATUSES or token_impact not in ALLOWED_TOKEN_IMPACTS:
        return "hold_token_risk", "hold", "token_status_or_impact_not_allowed", facts
    if row.get("ledger_status") != "open" or row.get("route_status") not in {"candidate", "active", ""}:
        return "hold_state_not_candidate", "hold", "ledger_state_not_candidate", facts
    if domain == "domain_titles_names":
        return "hold_titles_names_need_title_policy", "hold", "title_name_policy_required", facts
    if text_length > 140 or token_count > 7:
        return "hold_dynamic_or_long_text", "hold", "too_long_or_many_tokens", facts

    if (
        final_state == "reopen_auto_confirmed_autofix"
        and review_state == "auto_confirmed"
        and token_count == 0
        and text_length <= 30
    ):
        return "auto_safe_single_no_token_short", "reviewable", "", facts
    if token_count == 0 and text_length <= 30:
        return "single_no_token_short_label", "reviewable", "", facts
    if domain == "domain_rules_tooltips" and token_count <= 3 and text_length <= 100:
        return "single_rules_tooltip_low_token", "reviewable", "", facts
    if domain == "domain_interactions_activities" and token_count <= 3 and text_length <= 100:
        return "single_activity_low_token", "reviewable", "", facts
    if token_count <= 2 and text_length <= 70:
        return "single_low_token_short_text", "reviewable", "", facts
    if token_count <= 4 and text_length <= 120:
        return "single_medium_token_short_ui", "reviewable", "", facts
    return "hold_needs_stratified_review", "hold", "outside_low_risk_profiles", facts


def enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        profile, review_bucket, hold_reason, facts = classify(row)
        evidence = parse_json(row.get("ledger_evidence_json"), {})
        payload = {
            **row,
            "profile": profile,
            "review_bucket": review_bucket,
            "hold_reason": hold_reason,
            "domain": facts["domain"],
            "package": package_name(row.get("relative_path")),
            "text_length": facts["text_length"],
            "token_count": facts["token_count"],
            "word_count": facts["word_count"],
            "issue_codes_json": json.dumps(facts["issue_codes"], ensure_ascii=False, sort_keys=True),
            "english_preview": short(row.get("english_text")),
            "spanish_preview": short(row.get("spanish_text")),
            "portuguese_preview": short(row.get("portuguese_text")),
            "evidence_json": json.dumps(
                {
                    **evidence,
                    "profile": profile,
                    "review_bucket": review_bucket,
                    "hold_reason": hold_reason,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        enriched.append(payload)
    return enriched


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    partial_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "profile",
        "review_bucket",
        "hold_reason",
        "domain",
        "package",
        "text_length",
        "token_count",
        "word_count",
        "final_state",
        "review_state",
        "english_preview",
        "spanish_preview",
        "portuguese_preview",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow({**{field: row.get(field) for field in fieldnames}, "id": index})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, row in enumerate(rows, start=1):
            payload = {field: row.get(field) for field in fieldnames}
            payload["id"] = index
            payload["partial_coverage_item_id"] = row.get("partial_coverage_item_id")
            payload["ledger_item_id"] = row.get("ledger_item_id")
            payload["evidence"] = parse_json(row.get("evidence_json"), {})
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    profile_counts = Counter(row["profile"] for row in rows)
    reviewable_counts = Counter(row["profile"] for row in rows if row["review_bucket"] == "reviewable")
    hold_counts = Counter(row["hold_reason"] for row in rows if row["review_bucket"] == "hold")
    domain_counts = Counter(row["domain"] for row in rows)
    package_counts = Counter(row["package"] for row in rows)
    lines = [
        "Short-label single-issue opportunity diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Partial coverage run id: {partial_run['id']}",
        f"Ledger run id: {partial_run['ledger_run_id']}",
        f"Candidates: {len(rows):,}",
        f"Reviewable low-risk profiles: {sum(reviewable_counts.values()):,}",
        f"Held for other specialists/manual review: {sum(hold_counts.values()):,}",
        "",
        "Reviewable profiles:",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in reviewable_counts.most_common())
    lines.extend(
        [
            "",
            "Hold reasons:",
            *[f"- {key}: {value:,}" for key, value in hold_counts.most_common()],
            "",
            "Top profiles:",
            *[f"- {key}: {value:,}" for key, value in profile_counts.most_common(12)],
            "",
            "Top domains:",
            *[f"- {key}: {value:,}" for key, value in domain_counts.most_common(12)],
            "",
            "Top packages:",
            *[f"- {key}: {value:,}" for key, value in package_counts.most_common(12)],
            "",
            "Interpretation:",
            "- This report does not close, repair or apply output.",
            "- Reviewable means suitable for stratified human sampling or a future guarded checkpoint.",
            "- Held means the issue should be delegated to another specialist or needs richer context first.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def insert_run(
    conn,
    *,
    partial_run: dict[str, Any],
    rows: list[dict[str, Any]],
    paths: tuple[Path, Path, Path],
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    profile_counts = Counter(row["profile"] for row in rows)
    domain_counts = Counter(row["domain"] for row in rows)
    package_counts = Counter(row["package"] for row in rows)
    reviewable_count = sum(1 for row in rows if row["review_bucket"] == "reviewable")
    held_count = len(rows) - reviewable_count
    cur = conn.execute(
        """
        INSERT INTO ml_issue_short_label_single_issue_opportunity_runs (
            rule_version,
            partial_coverage_run_id,
            ledger_run_id,
            segment_state_run_id,
            candidate_count,
            reviewable_count,
            held_count,
            estimated_single_issue_segments,
            profile_counts_json,
            domain_counts_json,
            package_counts_json,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            int(partial_run["id"]),
            int(partial_run["ledger_run_id"]),
            int(partial_run["segment_state_run_id"]),
            len(rows),
            reviewable_count,
            held_count,
            reviewable_count,
            json.dumps(dict(profile_counts), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(domain_counts), ensure_ascii=False, sort_keys=True),
            json.dumps(dict(package_counts), ensure_ascii=False, sort_keys=True),
            str(paths[0]),
            str(paths[1]),
            str(paths[2]),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cur.lastrowid)
    created_at = now
    conn.executemany(
        """
        INSERT INTO ml_issue_short_label_single_issue_opportunity_items (
            run_id,
            partial_coverage_item_id,
            ledger_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            profile,
            review_bucket,
            hold_reason,
            domain,
            package,
            text_length,
            token_count,
            word_count,
            issue_codes_json,
            token_status,
            token_impact,
            route_status,
            ledger_status,
            final_state,
            review_state,
            english_preview,
            spanish_preview,
            portuguese_preview,
            evidence_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                int(row["partial_coverage_item_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["profile"],
                row["review_bucket"],
                row["hold_reason"],
                row["domain"],
                row["package"],
                int(row["text_length"]),
                int(row["token_count"]),
                int(row["word_count"]),
                row["issue_codes_json"],
                row.get("token_status"),
                row.get("token_impact"),
                row.get("route_status"),
                row.get("ledger_status"),
                row.get("final_state"),
                row.get("review_state"),
                row["english_preview"],
                row["spanish_preview"],
                row["portuguese_preview"],
                row["evidence_json"],
                created_at,
            )
            for row in rows
        ],
    )
    return run_id


def main(*, partial_coverage_run_id: int | None = None) -> dict[str, Any]:
    started_at = datetime.now()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        ensure_tables(conn)
        selected_run_id = partial_coverage_run_id or latest_partial_coverage_run_id(conn)
        partial_run = fetch_partial_run(conn, partial_coverage_run_id=selected_run_id)
        rows = enrich_rows(fetch_candidates(conn, partial_coverage_run_id=selected_run_id))
        paths = report_paths(settings)
        run_id = insert_run(conn, partial_run=partial_run, rows=rows, paths=paths, started_at=started_at)
        write_outputs(txt_path=paths[0], csv_path=paths[1], jsonl_path=paths[2], run_id=run_id, partial_run=partial_run, rows=rows)
        conn.commit()

    reviewable_count = sum(1 for row in rows if row["review_bucket"] == "reviewable")
    held_count = len(rows) - reviewable_count
    print("[issue_short_label_single_issue_opportunity] Diagnostic generated")
    print(f"[issue_short_label_single_issue_opportunity] Run id: {run_id}")
    print(f"[issue_short_label_single_issue_opportunity] Partial coverage run id: {selected_run_id}")
    print(f"[issue_short_label_single_issue_opportunity] Candidates: {len(rows):,}")
    print(f"[issue_short_label_single_issue_opportunity] Reviewable: {reviewable_count:,}")
    print(f"[issue_short_label_single_issue_opportunity] Held: {held_count:,}")
    print(f"[issue_short_label_single_issue_opportunity] Report: {paths[0]}")
    print(f"[issue_short_label_single_issue_opportunity] CSV: {paths[1]}")
    print(f"[issue_short_label_single_issue_opportunity] JSONL: {paths[2]}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run_id,
        "candidate_count": len(rows),
        "reviewable_count": reviewable_count,
        "held_count": held_count,
        "report_path": str(paths[0]),
        "csv_path": str(paths[1]),
        "jsonl_path": str(paths[2]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Profile short-label single-issue opportunities for learning review.")
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
