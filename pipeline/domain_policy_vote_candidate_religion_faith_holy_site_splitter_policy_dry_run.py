from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_splitter_policy_dry_run_v1"
AGENT_KEY = "religion_faith_doctrine_holy_site_effect_requirement_splitter_policy"
REVIEW_JSONL_PATH = Path(
    "reports/20260630_124634_175870_domain_policy_vote_candidate_religion_faith_holy_site_effect_requirement_review.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 509
EXPECTED_EFFECT_REQUIREMENT_COUNT = 7
EXPECTED_NAME_RUNTIME_COUNT = 4
EXPECTED_ROUTED_COUNT = EXPECTED_EFFECT_REQUIREMENT_COUNT + EXPECTED_NAME_RUNTIME_COUNT


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing registry agent: {AGENT_KEY}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    guards = notes.get("guards") or {}
    if payload.get("operational_state") != "shadow":
        raise SystemExit("registry operational_state guard failed")
    if payload.get("decision_role") != "route_and_split":
        raise SystemExit("registry decision_role guard failed")
    if notes.get("candidate_generation_allowed") is not False:
        raise SystemExit("registry candidate_generation guard failed")
    if notes.get("auto_apply_allowed") is not False:
        raise SystemExit("registry auto_apply guard failed")
    if notes.get("lifecycle_allowed") is not False:
        raise SystemExit("registry lifecycle guard failed")
    if notes.get("production_release_allowed") is not False:
        raise SystemExit("registry production guard failed")
    if any(bool(guards.get(key)) for key in ("candidate_generation", "apply", "lifecycle", "segment_state", "reindex", "full_production")):
        raise SystemExit("registry guards unexpectedly enabled")
    return {
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "scope_group": payload.get("scope_group"),
    }


def route_for(row: dict[str, Any]) -> str:
    subfamily = str(row.get("operational_subfamily") or "")
    if subfamily == "holy_site_effect_requirement":
        return "route_holy_site_effect_requirement"
    if subfamily == "holy_site_name_runtime":
        return "route_holy_site_name_runtime"
    raise SystemExit(f"unexpected subfamily for routing dry-run: {subfamily}")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    rows = read_jsonl(REVIEW_JSONL_PATH)
    routed_source = [
        row
        for row in rows
        if row.get("operational_subfamily") in {"holy_site_effect_requirement", "holy_site_name_runtime"}
    ]
    if len(routed_source) != EXPECTED_ROUTED_COUNT:
        raise SystemExit(f"routed count guard failed: {len(routed_source)} expected {EXPECTED_ROUTED_COUNT}")
    with connect_readonly() as conn:
        registry = validate_registry(conn)

    routed: list[dict[str, Any]] = []
    for row in sorted(routed_source, key=lambda item: (route_for(item), int(item["segment_id"]))):
        routed.append(
            {
                "record_type": "holy_site_splitter_policy_route",
                "source": SOURCE,
                "agent_key": AGENT_KEY,
                "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "risk_bucket": row.get("risk_bucket"),
                "operational_subfamily": row.get("operational_subfamily"),
                "route": route_for(row),
                "route_status": "split_only_no_candidate",
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "latest_state_group": row.get("latest_state_group"),
                "latest_final_state": row.get("latest_final_state"),
                "latest_needs_output_apply": int(row.get("latest_needs_output_apply") or 0),
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
            }
        )

    route_counts = Counter(row["route"] for row in routed)
    subfamily_counts = Counter(row["operational_subfamily"] for row in routed)
    risk_counts = Counter(row["risk_bucket"] for row in routed)
    if route_counts.get("route_holy_site_effect_requirement", 0) != EXPECTED_EFFECT_REQUIREMENT_COUNT:
        raise SystemExit("effect route count guard failed")
    if route_counts.get("route_holy_site_name_runtime", 0) != EXPECTED_NAME_RUNTIME_COUNT:
        raise SystemExit("name route count guard failed")

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_splitter_dry_run",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "agent_key": AGENT_KEY,
        "registry": registry,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "input_review_jsonl": str(REVIEW_JSONL_PATH),
        "routed_count": len(routed),
        "route_counts": dict(route_counts),
        "subfamily_counts": dict(subfamily_counts),
        "risk_bucket_counts": dict(risk_counts),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Review the 11 routed rows manually before deciding whether a tiny human packet is useful; "
            "do not generate candidates from this splitter yet."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_religion_faith_doctrine_holy_site_splitter_policy_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, routed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "religion_faith_doctrine holy-site splitter policy dry-run",
        "",
        f"agent_key: {AGENT_KEY}",
        f"routed_count: {len(routed)}",
        "",
        "route_counts:",
        *[f"- {count} | {key}" for key, count in route_counts.most_common()],
        "",
        "risk_bucket_counts:",
        *[f"- {count} | {key}" for key, count in risk_counts.most_common()],
        "",
        "items:",
    ]
    for row in routed:
        output = str(row.get("current_output_text") or "").replace("\n", "\\n")
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['route']} | {row['source_key']}",
                f"- risk: {row['risk_bucket']}",
                f"- output: {output[:420]}",
            ]
        )
    lines.extend(
        [
            "",
            "guards:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
