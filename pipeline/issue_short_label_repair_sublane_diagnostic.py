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
from issue_review_assisted_draft import (
    english_hits,
    has_actual_mojibake,
    spanish_hits,
)


RULE_VERSION = "issue_short_label_repair_sublane_diagnostic_v1"
ISSUE_FAMILY = "short_label_style_microagent"
AGENT_KEY = "micro_short_label_style"


DYNAMIC_MARKERS = (
    "Select_CString(",
    "SelectLocalization(",
    "Custom('ES_",
    'Custom("ES_',
    "GetPlayer.",
    "GetShortUIName",
)
GENDER_MARKERS = (
    "Custom('ES_OA')",
    'Custom("ES_OA")',
    "Custom('ES_AO')",
    'Custom("ES_AO")',
    "Custom('ES_EA')",
    'Custom("ES_EA")',
)


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


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_repair_sublane_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


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


def package_name(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else relative_path


def has_bold_no(text: str) -> bool:
    return bool(re.search(r"#bold\s+no#!", text, flags=re.IGNORECASE))


def has_dynamic(text: str, issue_kind: str) -> bool:
    if "dynamic" in issue_kind:
        return True
    return any(marker in text for marker in DYNAMIC_MARKERS)


def has_gender_marker(text: str) -> bool:
    return any(marker in text for marker in GENDER_MARKERS)


def classify_sublane(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    text = row.get("evidence_text") or ""
    evidence = parse_json(row.get("evidence_json"))
    issue_kind = str(row.get("issue_kind") or "")
    domain = str(evidence.get("domain") or "domain_unknown")
    token_count = int(evidence.get("token_count") or 0)
    text_length = int(evidence.get("text_length") or len(text))
    word_count = int(evidence.get("word_count") or 0)
    facts = {
        "domain": domain,
        "package": evidence.get("package") or package_name(row.get("relative_path") or "unknown"),
        "token_count": token_count,
        "text_length": text_length,
        "word_count": word_count,
    }

    if has_actual_mojibake(text):
        return "short_label_mojibake_repair", "encoding_repair_candidate", facts
    if has_gender_marker(text):
        return "short_label_gender_token_delegate", "delegate_to_gender_token_microagent", facts
    if has_dynamic(text, issue_kind):
        return "short_label_dynamic_expression_delegate", "delegate_to_dynamic_ck3_microagent", facts

    spanish = spanish_hits(text)
    english = english_hits(text)
    if has_bold_no(text):
        return "short_label_bold_no_repair", "mechanical_bold_no_ptbr_candidate", facts
    if spanish:
        return "short_label_spanish_residual_repair", "spanish_residual:" + ",".join(spanish[:4]), facts
    if english:
        return "short_label_english_residual_repair", "english_residual:" + ",".join(english[:4]), facts

    if token_count == 0 and text_length <= 30:
        return "short_label_clean_no_token_candidate", "possible_no_token_label_policy", facts
    if domain == "domain_rules_tooltips" and token_count <= 3 and text_length <= 100:
        return "short_label_rules_tooltip_semantic", "needs_semantic_rule_tooltip_validation", facts
    if domain == "domain_interactions_activities" and token_count <= 3 and text_length <= 100:
        return "short_label_activity_semantic", "needs_activity_context_validation", facts
    if token_count <= 3 and text_length <= 100:
        return "short_label_compact_ui_semantic", "needs_compact_ui_semantic_validation", facts
    return "short_label_contextual_semantic_router", "needs_context_or_composition", facts


def fetch_rows(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS ledger_item_id,
            item.run_id AS ledger_run_id,
            item.state_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.final_state,
            item.state_group,
            item.active_action,
            item.candidate_action,
            item.policy_action,
            item.confirmation_level,
            item.confirmation_label,
            item.issue_kind,
            item.issue_role,
            item.issue_severity,
            item.agent_key,
            item.route_status,
            item.proposed_action,
            item.token_impact,
            item.token_status,
            item.confidence_score,
            item.evidence_text,
            item.evidence_json,
            item.validation_status,
            item.status
        FROM ml_issue_ledger_items item
        WHERE item.run_id = ?
          AND item.issue_family = ?
          AND item.status = 'open'
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.id
        """,
        (ledger_run_id, ISSUE_FAMILY),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_repair_sublane_diagnostic_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            issue_family TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            sublane_counts_json TEXT,
            domain_counts_json TEXT,
            package_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_repair_sublane_diagnostic_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            sublane TEXT NOT NULL,
            sublane_reason TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            domain TEXT NOT NULL,
            package TEXT NOT NULL,
            token_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            confidence_score REAL,
            active_action TEXT,
            candidate_action TEXT,
            policy_action TEXT,
            token_impact TEXT,
            token_status TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_repair_sublane_diagnostic_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_sublane_diag_items_run
        ON ml_issue_short_label_repair_sublane_diagnostic_items(run_id, sublane);

        CREATE INDEX IF NOT EXISTS idx_short_label_sublane_diag_items_ledger
        ON ml_issue_short_label_repair_sublane_diagnostic_items(ledger_run_id, ledger_item_id);
        """
    )


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    sublane_counts: Counter[str],
    domain_counts: Counter[str],
    package_counts: Counter[str],
    sublane_domain_counts: dict[str, Counter[str]],
    sublane_kind_counts: dict[str, Counter[str]],
    examples: dict[str, list[dict[str, Any]]],
) -> None:
    fields = [
        "sublane",
        "sublane_reason",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "issue_kind",
        "domain",
        "package",
        "token_count",
        "text_length",
        "word_count",
        "active_action",
        "candidate_action",
        "policy_action",
        "token_impact",
        "token_status",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    payload = {
        "rule_version": RULE_VERSION,
        "run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "candidate_count": len(rows),
        "sublane_counts": dict(sublane_counts),
        "domain_counts": dict(domain_counts),
        "package_counts": dict(package_counts),
        "sublane_domain_counts": {key: dict(value) for key, value in sublane_domain_counts.items()},
        "sublane_kind_counts": {key: dict(value) for key, value in sublane_kind_counts.items()},
        "examples": examples,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "Short Label Repair Sublane Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Open short_label issues: {len(rows):,}",
        "",
        "Sublanes:",
    ]
    for sublane, count in sublane_counts.most_common():
        lines.append(f"- {sublane}: {count:,}")
        for domain, domain_count in sublane_domain_counts[sublane].most_common(4):
            lines.append(f"  - domain {domain}: {domain_count:,}")
        for kind, kind_count in sublane_kind_counts[sublane].most_common(4):
            lines.append(f"  - kind {kind}: {kind_count:,}")
    lines.extend(["", "Domains:"])
    for domain, count in domain_counts.most_common(20):
        lines.append(f"- {domain}: {count:,}")
    lines.extend(["", "Top packages:"])
    for package, count in package_counts.most_common(25):
        lines.append(f"- {package}: {count:,}")
    lines.extend(["", "Examples:"])
    for sublane, sublane_examples in examples.items():
        lines.append(f"- {sublane}:")
        for row in sublane_examples[:8]:
            lines.append(
                f"  - segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | "
                f"{row['sublane_reason']} | {short(row['evidence_text'], 130)}"
            )
    lines.extend(
        [
            "",
            "Recommendation:",
            "- Treat short_label_bold_no_repair as the first narrow repair candidate if sample validation remains clean.",
            "- Treat English/Spanish residual lanes as repair queues, not as safe label policies.",
            "- Delegate dynamic and gender lanes to their existing CK3/dynamic/gender neurons.",
            "- Keep semantic lanes as review/router input until enough positive evidence exists.",
            "",
            "Safety note:",
            "- This diagnostic only writes learning tables and reports.",
            "- It does not write source/output, does not create confirmations, and does not promote production policy.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None, sample_per_sublane: int = 12) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_ledger_run_id(conn)
        source_rows = fetch_rows(conn, ledger_run_id=selected_ledger_run_id)

        classified_rows: list[dict[str, Any]] = []
        sublane_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        package_counts: Counter[str] = Counter()
        sublane_domain_counts: dict[str, Counter[str]] = defaultdict(Counter)
        sublane_kind_counts: dict[str, Counter[str]] = defaultdict(Counter)
        examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for row in source_rows:
            sublane, reason, facts = classify_sublane(row)
            out = {
                "ledger_run_id": selected_ledger_run_id,
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "sublane": sublane,
                "sublane_reason": reason,
                "issue_kind": row["issue_kind"],
                "domain": facts["domain"],
                "package": facts["package"],
                "token_count": facts["token_count"],
                "text_length": facts["text_length"],
                "word_count": facts["word_count"],
                "confidence_score": row["confidence_score"],
                "active_action": row["active_action"],
                "candidate_action": row["candidate_action"],
                "policy_action": row["policy_action"],
                "token_impact": row["token_impact"],
                "token_status": row["token_status"],
                "evidence_text": row["evidence_text"],
            }
            classified_rows.append(out)
            sublane_counts[sublane] += 1
            domain_counts[out["domain"]] += 1
            package_counts[out["package"]] += 1
            sublane_domain_counts[sublane][out["domain"]] += 1
            sublane_kind_counts[sublane][out["issue_kind"]] += 1
            if len(examples[sublane]) < sample_per_sublane:
                examples[sublane].append(out)

        txt_path, csv_path, json_path = report_paths(settings)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_repair_sublane_diagnostic_runs (
                rule_version,
                ledger_run_id,
                issue_family,
                candidate_count,
                sublane_counts_json,
                domain_counts_json,
                package_counts_json,
                report_path,
                csv_path,
                json_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_ledger_run_id,
                ISSUE_FAMILY,
                len(classified_rows),
                json.dumps(dict(sublane_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(domain_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(package_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(json_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_repair_sublane_diagnostic_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                sublane,
                sublane_reason,
                issue_kind,
                domain,
                package,
                token_count,
                text_length,
                word_count,
                confidence_score,
                active_action,
                candidate_action,
                policy_action,
                token_impact,
                token_status,
                evidence_text,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                (
                    run_id,
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["sublane"],
                    row["sublane_reason"],
                    row["issue_kind"],
                    row["domain"],
                    row["package"],
                    row["token_count"],
                    row["text_length"],
                    row["word_count"],
                    row["confidence_score"],
                    row["active_action"],
                    row["candidate_action"],
                    row["policy_action"],
                    row["token_impact"],
                    row["token_status"],
                    row["evidence_text"],
                    now,
                )
                for row in classified_rows
            ],
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        json_path=json_path,
        run_id=run_id,
        ledger_run_id=selected_ledger_run_id,
        rows=classified_rows,
        sublane_counts=sublane_counts,
        domain_counts=domain_counts,
        package_counts=package_counts,
        sublane_domain_counts=sublane_domain_counts,
        sublane_kind_counts=sublane_kind_counts,
        examples=examples,
    )

    print("[issue_short_label_repair_sublane_diagnostic] Diagnostic generated")
    print(f"[issue_short_label_repair_sublane_diagnostic] Run id: {run_id}")
    print(f"[issue_short_label_repair_sublane_diagnostic] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_short_label_repair_sublane_diagnostic] Rows: {len(classified_rows):,}")
    for sublane, count in sublane_counts.most_common():
        print(f"[issue_short_label_repair_sublane_diagnostic] {sublane}: {count:,}")
    print(f"[issue_short_label_repair_sublane_diagnostic] Report: {txt_path}")
    print(f"[issue_short_label_repair_sublane_diagnostic] CSV: {csv_path}")
    print(f"[issue_short_label_repair_sublane_diagnostic] JSON: {json_path}")
    return {
        "run_id": run_id,
        "ledger_run_id": selected_ledger_run_id,
        "candidate_count": len(classified_rows),
        "sublane_counts": dict(sublane_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classify open short-label issues into smaller repair/semantic sublanes.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--sample-per-sublane", type=int, default=12)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, sample_per_sublane=args.sample_per_sublane)
