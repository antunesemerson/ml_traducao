from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_pure_no_token_shadow_blocker_diagnostic_v1"


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_shadow_policy_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished pure no-token shadow policy run found.")
    return int(row["id"])


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_issue_short_label_pure_no_token_shadow_blocker_diagnostic"


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def package_from_path(relative_path: str | None) -> str:
    value = str(relative_path or "").replace("\\", "/")
    if not value:
        return "unknown"
    parts = value.split("/")
    return parts[0] if len(parts) > 1 else "root"


def short(text: str | None, limit: int = 150) -> str:
    value = (text or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def route_for(row: dict[str, Any]) -> tuple[str, str, str]:
    reason = str(row.get("reason") or "")
    relative_path = str(row.get("relative_path") or "")
    source_key = str(row.get("source_key") or "")
    evidence = parse_json(row.get("ledger_evidence_json"))
    domain = str(evidence.get("domain") or "")

    if reason in {
        "compact_nominal_label_no_token",
        "medium_nominal_label_no_token",
        "event_title_short_no_token",
        "short_title_like_sentence_surface",
    }:
        return (
            "already_safe_nominal_label",
            "micro_short_label_style",
            "Already classified as safe by the shadow policy; no new neuron needed here.",
        )

    if reason == "english_surface_hint":
        return (
            "english_surface_or_source_audit",
            "micro_english_residual_repair",
            "English-looking surface needs source/output audit or residual repair before any closure.",
        )

    if reason == "custom_localization_fragment_requires_context":
        return (
            "custom_localization_fragment_context",
            "micro_custom_localization_fragment",
            "Short fragment needs host localization context before it can be marked safe.",
        )

    if reason == "event_option_or_dialogue_requires_context":
        return (
            "event_option_dialogue_context",
            "micro_event_dialogue_option",
            "Event option or dialogue line needs event-context validation, not a generic label policy.",
        )

    if reason == "sentence_or_dialogue_surface_requires_context":
        if domain == "domain_rules_tooltips" or source_key.endswith("_desc") or "GLOSS" in source_key:
            return (
                "tooltip_gloss_or_description_surface",
                "micro_requirement_tooltip_surface",
                "Description/gloss surface should be validated by tooltip or semantic sentence neurons.",
            )
        if relative_path.startswith("activities/") or "_events" in relative_path:
            return (
                "event_sentence_surface",
                "micro_event_surface_router",
                "Sentence-like event surface needs event-router validation.",
            )
        return (
            "general_sentence_surface",
            "micro_sentence_surface_semantic",
            "Sentence-like label needs semantic surface validation before lifecycle closure.",
        )

    if reason == "long_or_clause_like_no_token_text":
        if relative_path.startswith("achievements") or source_key.startswith("ACHIEVEMENT"):
            return (
                "achievement_clause_or_description",
                "micro_achievement_text_semantic",
                "Achievement title/description clauses need achievement-specific semantic validation.",
            )
        return (
            "long_clause_no_token_semantic",
            "micro_long_clause_semantic",
            "Long or clause-like token-free text is outside the pure short-label policy.",
        )

    return (
        "unclassified_shadow_blocker",
        "coordinator_needs_route",
        "Unknown blocker reason; keep for coordinator inspection.",
    )


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            safe_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            reason_counts_json TEXT,
            package_counts_json TEXT,
            domain_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            shadow_status TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            confidence_label TEXT NOT NULL,
            route_lane TEXT NOT NULL,
            suggested_agent_key TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            issue_kind TEXT,
            issue_family TEXT,
            domain TEXT NOT NULL,
            package TEXT NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_pure_no_token_shadow_blocker_items_run
        ON ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_items(run_id, route_lane, reason);

        CREATE INDEX IF NOT EXISTS idx_pure_no_token_shadow_blocker_items_segment
        ON ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_items(segment_id);
        """
    )


def fetch_rows(conn, *, shadow_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_shadow_policy_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"Shadow policy run not found: {shadow_run_id}")

    rows = conn.execute(
        """
        SELECT
            item.id AS shadow_item_id,
            item.run_id AS shadow_run_id,
            item.ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.shadow_status,
            item.decision,
            item.reason,
            item.confidence_label,
            item.evidence_text,
            ledger.issue_kind,
            ledger.issue_family,
            ledger.evidence_json AS ledger_evidence_json
        FROM ml_issue_short_label_pure_no_token_shadow_policy_items item
        LEFT JOIN ml_issue_ledger_items ledger
          ON ledger.id = item.ledger_item_id
        WHERE item.run_id = ?
        ORDER BY item.shadow_status, item.reason, item.relative_path, item.source_line_number, item.source_key
        """,
        (shadow_run_id,),
    ).fetchall()
    return dict(run), [dict(row) for row in rows]


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    route_counts: Counter[str],
    reason_counts: Counter[str],
    package_counts: Counter[str],
    domain_counts: Counter[str],
    route_examples: dict[str, list[dict[str, Any]]],
) -> None:
    fields = [
        "run_id",
        "shadow_run_id",
        "shadow_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "shadow_status",
        "decision",
        "reason",
        "confidence_label",
        "route_lane",
        "suggested_agent_key",
        "recommendation",
        "issue_kind",
        "issue_family",
        "domain",
        "package",
        "text_length",
        "word_count",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({"run_id": run_id, **row})

    payload = {
        "rule_version": RULE_VERSION,
        "run_id": run_id,
        "shadow_run_id": shadow_run["id"],
        "ledger_run_id": shadow_run.get("ledger_run_id"),
        "candidate_count": len(rows),
        "safe_count": sum(1 for row in rows if row["shadow_status"] == "shadow_ready"),
        "blocked_count": sum(1 for row in rows if row["shadow_status"] != "shadow_ready"),
        "route_counts": dict(route_counts),
        "reason_counts": dict(reason_counts),
        "package_counts": dict(package_counts),
        "domain_counts": dict(domain_counts),
        "route_examples": route_examples,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Short Label Pure No-Token Shadow Blocker Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Shadow run id: {shadow_run['id']}",
        f"Ledger run id: {shadow_run.get('ledger_run_id')}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Candidates: {len(rows):,}",
        f"Safe by shadow: {payload['safe_count']:,}",
        f"Blocked by shadow: {payload['blocked_count']:,}",
        "",
        "Route lanes:",
    ]
    for key, value in route_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Reasons:"])
    for key, value in reason_counts.most_common():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Top packages:"])
    for key, value in package_counts.most_common(25):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Domains:"])
    for key, value in domain_counts.most_common(20):
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "Route examples:"])
    for route, examples in route_examples.items():
        lines.append(f"- {route}:")
        for row in examples[:10]:
            lines.append(
                f"  - segment={row['segment_id']} | {row['suggested_agent_key']} | "
                f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                f"{short(row['evidence_text'], 110)}"
            )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- The pure no-token neuron is saturated for its current safe nominal-label lane.",
            "- Remaining blockers should be routed to context-specific neurons instead of expanding this policy blindly.",
            "- This diagnostic is read-only: it writes learning tables and reports only.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, shadow_run_id: int | None = None, sample_per_route: int = 12) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run, source_rows = fetch_rows(conn, shadow_run_id=selected_shadow_run_id)

        route_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        package_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        route_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
        output_rows: list[dict[str, Any]] = []

        for row in source_rows:
            evidence = parse_json(row.get("ledger_evidence_json"))
            route_lane, suggested_agent_key, recommendation = route_for(row)
            domain = str(evidence.get("domain") or "domain_unknown")
            package = str(evidence.get("package") or package_from_path(row.get("relative_path")))
            text = str(row.get("evidence_text") or "")
            word_count = int(evidence.get("word_count") or len([part for part in text.split() if part]))
            text_length = int(evidence.get("text_length") or len(text))
            out = {
                "shadow_run_id": selected_shadow_run_id,
                "shadow_item_id": row["shadow_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "shadow_status": row["shadow_status"],
                "decision": row["decision"],
                "reason": row["reason"],
                "confidence_label": row["confidence_label"],
                "route_lane": route_lane,
                "suggested_agent_key": suggested_agent_key,
                "recommendation": recommendation,
                "issue_kind": row.get("issue_kind") or "",
                "issue_family": row.get("issue_family") or "",
                "domain": domain,
                "package": package,
                "text_length": text_length,
                "word_count": word_count,
                "evidence_text": row.get("evidence_text") or "",
            }
            output_rows.append(out)
            route_counts[route_lane] += 1
            reason_counts[out["reason"]] += 1
            package_counts[package] += 1
            domain_counts[domain] += 1
            if len(route_examples[route_lane]) < sample_per_route:
                route_examples[route_lane].append(out)

        base = report_base(settings)
        txt_path = base.with_suffix(".txt")
        csv_path = base.with_suffix(".csv")
        json_path = base.with_suffix(".json")
        now = db.utc_now()
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_runs (
                rule_version,
                shadow_run_id,
                ledger_run_id,
                candidate_count,
                safe_count,
                blocked_count,
                route_counts_json,
                reason_counts_json,
                package_counts_json,
                domain_counts_json,
                report_path,
                csv_path,
                json_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_run_id,
                shadow_run.get("ledger_run_id"),
                len(output_rows),
                sum(1 for row in output_rows if row["shadow_status"] == "shadow_ready"),
                sum(1 for row in output_rows if row["shadow_status"] != "shadow_ready"),
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(reason_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(package_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(domain_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(json_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_shadow_blocker_diagnostic_items (
                run_id,
                shadow_run_id,
                shadow_item_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                shadow_status,
                decision,
                reason,
                confidence_label,
                route_lane,
                suggested_agent_key,
                recommendation,
                issue_kind,
                issue_family,
                domain,
                package,
                text_length,
                word_count,
                evidence_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    row["shadow_run_id"],
                    row["shadow_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["shadow_status"],
                    row["decision"],
                    row["reason"],
                    row["confidence_label"],
                    row["route_lane"],
                    row["suggested_agent_key"],
                    row["recommendation"],
                    row["issue_kind"],
                    row["issue_family"],
                    row["domain"],
                    row["package"],
                    row["text_length"],
                    row["word_count"],
                    row["evidence_text"],
                    now,
                )
                for row in output_rows
            ],
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        json_path=json_path,
        run_id=run_id,
        shadow_run=shadow_run,
        rows=output_rows,
        route_counts=route_counts,
        reason_counts=reason_counts,
        package_counts=package_counts,
        domain_counts=domain_counts,
        route_examples=route_examples,
    )

    print("[issue_short_label_pure_no_token_shadow_blocker_diagnostic] Diagnostic generated")
    print(f"[issue_short_label_pure_no_token_shadow_blocker_diagnostic] Run id: {run_id}")
    print(f"[issue_short_label_pure_no_token_shadow_blocker_diagnostic] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_short_label_pure_no_token_shadow_blocker_diagnostic] Rows: {len(output_rows):,}")
    for route, count in route_counts.most_common():
        print(f"[issue_short_label_pure_no_token_shadow_blocker_diagnostic] {route}: {count:,}")
    print(f"[issue_short_label_pure_no_token_shadow_blocker_diagnostic] Report: {txt_path}")
    return {
        "run_id": run_id,
        "shadow_run_id": selected_shadow_run_id,
        "candidate_count": len(output_rows),
        "route_counts": dict(route_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Route pure no-token shadow blockers into narrower learning lanes.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--sample-per-route", type=int, default=12)
    args = parser.parse_args()
    main(shadow_run_id=args.shadow_run_id, sample_per_route=args.sample_per_route)
