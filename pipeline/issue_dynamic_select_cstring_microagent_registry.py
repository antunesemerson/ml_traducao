from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_dynamic_select_cstring_microagent_registry_v1"
DEFAULT_AUDIT_GLOB = "*_issue_dynamic_select_cstring_literal_subtype_audit.csv"
PARENT_AGENT_KEY = "micro_dynamic_ck3_expression"
SCOPE_GROUP = "dynamic_select_cstring_literal_payload"
DASHBOARD_GROUP = "Issue Network"

AGENT_SPECS = {
    "select_cstring_local_player_preterite_verb_rewrite": {
        "agent_type": "microagent",
        "decision_role": "evidence_only",
        "priority": 37,
        "description": "Learns local-player Select_CString branches where Spanish second-person preterite literals map to third-person event/history phrasing.",
    },
    "select_cstring_local_player_reflexive_phrase_rewrite": {
        "agent_type": "microagent",
        "decision_role": "evidence_only",
        "priority": 38,
        "description": "Learns local-player Select_CString reflexive phrase shifts such as te relajaste -> se relajo before PT-BR rewrite.",
    },
    "select_cstring_local_player_possessive_pronoun_rewrite": {
        "agent_type": "microagent",
        "decision_role": "evidence_only",
        "priority": 39,
        "description": "Learns local-player Select_CString possessive shifts such as tus -> sus before PT-BR rewrite.",
    },
    "select_cstring_local_player_future_tense_review": {
        "agent_type": "microagent",
        "decision_role": "evidence_only",
        "priority": 40,
        "description": "Learns local-player Select_CString future-tense branches and separates safe same-form PT-BR futures from cases requiring sentence-level passive or explicit-actor composition.",
    },
    "select_cstring_local_player_pronoun_argument_review": {
        "agent_type": "symbolic_guard",
        "decision_role": "negative_boundary",
        "priority": 41,
        "description": "Guards ambiguous te -> le/se Select_CString payloads that require argument/context review.",
    },
    "select_cstring_local_player_phrase_context_review": {
        "agent_type": "subcoordinator",
        "decision_role": "route_and_arbitrate",
        "priority": 42,
        "description": "Routes mixed local-player Select_CString phrases with first/second-person markers to contextual review.",
    },
    "select_cstring_literal_payload_context_review": {
        "agent_type": "subcoordinator",
        "decision_role": "route_and_arbitrate",
        "priority": 43,
        "description": "Routes Select_CString literal payloads that are not yet covered by verb, reflexive or possessive microagents.",
    },
}


def latest_audit_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(DEFAULT_AUDIT_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No dynamic Select_CString literal subtype audit CSV found.")
    return candidates[0]


def report_path(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_issue_dynamic_select_cstring_microagent_registry.txt"


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def register_agents(conn, *, rows: list[dict[str, Any]], source_audit_csv: Path) -> list[dict[str, Any]]:
    now = db.utc_now()
    counts = Counter(row.get("suggested_microagent") or "" for row in rows)
    subtype_counts: dict[str, Counter[str]] = {}
    maturity_counts: dict[str, Counter[str]] = {}
    for row in rows:
        agent_key = row.get("suggested_microagent") or ""
        subtype_counts.setdefault(agent_key, Counter())[row.get("literal_subtype") or "unknown"] += 1
        maturity_counts.setdefault(agent_key, Counter())[row.get("maturity") or "unknown"] += 1

    registered: list[dict[str, Any]] = []
    for agent_key, count in sorted(counts.items()):
        if not agent_key or count <= 0:
            continue
        spec = AGENT_SPECS.get(agent_key)
        if spec is None:
            continue
        maturity = maturity_counts[agent_key]
        operational_state = "shadow"
        status = "experimental"
        if maturity.get("needs_context", 0) == count:
            operational_state = "experimental"
        notes = {
            "source": RULE_VERSION,
            "source_audit_csv": str(source_audit_csv),
            "evidence_count": count,
            "subtype_counts": dict(subtype_counts[agent_key]),
            "maturity_counts": dict(maturity),
            "auto_apply_allowed": 0,
            "production_release_allowed": 0,
            "status_note": "Registered from dynamic Select_CString literal subtype audit; learning/evidence only.",
        }
        conn.execute(
            """
            INSERT INTO ml_agent_registry (
                agent_key,
                agent_type,
                parent_agent_key,
                model_kind,
                status,
                operational_state,
                decision_role,
                scope_group,
                scope_sql,
                scope_description,
                default_threshold,
                priority,
                dashboard_group,
                created_at,
                updated_at,
                notes_json
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_key) DO UPDATE SET
                agent_type = excluded.agent_type,
                parent_agent_key = excluded.parent_agent_key,
                status = excluded.status,
                operational_state = excluded.operational_state,
                decision_role = excluded.decision_role,
                scope_group = excluded.scope_group,
                scope_description = excluded.scope_description,
                priority = excluded.priority,
                dashboard_group = excluded.dashboard_group,
                updated_at = excluded.updated_at,
                notes_json = excluded.notes_json
            """,
            (
                agent_key,
                spec["agent_type"],
                PARENT_AGENT_KEY,
                status,
                operational_state,
                spec["decision_role"],
                SCOPE_GROUP,
                spec["description"],
                spec["priority"],
                DASHBOARD_GROUP,
                now,
                now,
                json.dumps(notes, ensure_ascii=False, sort_keys=True),
            ),
        )
        registered.append(
            {
                "agent_key": agent_key,
                "count": count,
                "agent_type": spec["agent_type"],
                "status": status,
                "operational_state": operational_state,
                "decision_role": spec["decision_role"],
                "subtype_counts": dict(subtype_counts[agent_key]),
                "maturity_counts": dict(maturity),
            }
        )
    return registered


def write_report(path: Path, *, source_audit_csv: Path, registered: list[dict[str, Any]]) -> None:
    lines = [
        "Issue dynamic Select_CString microagent registry",
        f"Rule version: {RULE_VERSION}",
        f"Source audit CSV: {source_audit_csv}",
        "",
        "Summary:",
        f"- Registered/updated agents: {len(registered):,}",
        "- Auto apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Agents:",
    ]
    for row in registered:
        lines.append(
            f"- {row['agent_key']} [{row['agent_type']}/{row['operational_state']}/{row['decision_role']}]: "
            f"{row['count']:,} evidence rows; maturity={json.dumps(row['maturity_counts'], ensure_ascii=False, sort_keys=True)}; "
            f"subtypes={json.dumps(row['subtype_counts'], ensure_ascii=False, sort_keys=True)}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, audit_csv: str | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    selected_audit_csv = Path(audit_csv) if audit_csv else latest_audit_csv(settings)
    rows = load_rows(selected_audit_csv)
    txt_path = report_path(settings)
    with db.connect(settings) as conn:
        registered = register_agents(conn, rows=rows, source_audit_csv=selected_audit_csv)
        conn.commit()
    write_report(txt_path, source_audit_csv=selected_audit_csv, registered=registered)
    print("[issue_dynamic_select_cstring_microagent_registry] Registry updated")
    print(f"[issue_dynamic_select_cstring_microagent_registry] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_select_cstring_microagent_registry] Agents: {len(registered):,}")
    print(f"[issue_dynamic_select_cstring_microagent_registry] Report: {txt_path}")
    return {"registered": len(registered), "report_path": str(txt_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register dynamic Select_CString literal microagents from subtype audit.")
    parser.add_argument("--audit-csv", default=None)
    args = parser.parse_args()
    main(audit_csv=args.audit_csv)
