from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_effect_requirement_review_v1"
BASE_JSONL_PATH = Path("reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic.jsonl")
BASE_SUMMARY_PATH = Path("reports/20260630_093427_589843_domain_policy_vote_candidate_religion_faith_doctrine_post507_hold_diagnostic_summary.json")
EXPECTED_FAMILY = "holy_site_effect_or_requirement"
EXPECTED_SURFACE_BUCKET = "religion_faith_doctrine"
EXPECTED_BASE_SEGMENT_STATE_RUN_ID = 507
EXPECTED_COUNT = 92

HOLY_SITE_NAME_RE = re.compile(r"holy_site_[a-z0-9_]+|GetHolySite|holy site name|_holy_site|holy_site_name", re.I)
HOLY_SITE_RUNTIME_RE = re.compile(r"GetNumberOf(?:Controlled)?HolySites|Num(?:Controlled)?HolySites|HolySite", re.I)
REQUIREMENT_RE = re.compile(r"require|must|need|valid|trigger|control|controlled|available|cannot|can_have|is_valid", re.I)
EFFECT_RE = re.compile(r"effect|modifier|fervor|conversion|hostility|advantage|piety|opinion|development|tax|levy|speed|resistance", re.I)
FAITH_RELATION_RE = re.compile(r"\bfaith\b|\[faiths?\||Faith\.|GetFaith|state_faith|head_of_faith", re.I)
BUILDING_PLACE_RE = re.compile(r"building|temple|church|cathedral|mosque|shrine|sanctuary|holy_site|barony|holding|pilgrimage", re.I)
TITLE_REALM_RE = re.compile(r"title|realm|county|counties|kingdom|duchy|empire|ruler|liege|GetPrimaryTitle|GetTitleByKey", re.I)
MULTILINE_EFFECT_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|\$BULLET_WITH_TAB\$|#indent|#weak|#low", re.I)
DENSE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Get[A-Za-z0-9_]+\(|TOKEN_PARAMETER|ScriptValue")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def latest_complete_state_run(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return EXPECTED_BASE_SEGMENT_STATE_RUN_ID
    return int(row["id"])


def fetch_latest_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, final_state, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if summary.get("mode") != "read_only_post507_hold_diagnostic":
        raise SystemExit("base summary mode guard failed")
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_BASE_SEGMENT_STATE_RUN_ID:
        raise SystemExit("base segment_state_run_id guard failed")
    if summary.get("surface_bucket") != EXPECTED_SURFACE_BUCKET:
        raise SystemExit("surface_bucket guard failed")
    if int(summary.get("candidate_generation_count") or 0) != 0:
        raise SystemExit("candidate_generation_count guard failed")
    if int(summary.get("apply_count") or 0) != 0:
        raise SystemExit("apply_count guard failed")
    if int(summary.get("lifecycle_count") or 0) != 0:
        raise SystemExit("lifecycle_count guard failed")
    if summary.get("source_changed") is not False or summary.get("output_changed") is not False:
        raise SystemExit("source/output changed guard failed")

    family_rows = [
        row
        for row in rows
        if row.get("hold_family") == EXPECTED_FAMILY
        and row.get("surface_bucket") == EXPECTED_SURFACE_BUCKET
    ]
    if len(family_rows) != EXPECTED_COUNT:
        raise SystemExit(f"{EXPECTED_FAMILY} count guard failed: {len(family_rows)} expected {EXPECTED_COUNT}")
    duplicates = [segment_id for segment_id, count in Counter(int(row["segment_id"]) for row in family_rows).items() if count > 1]
    if duplicates:
        raise SystemExit(f"duplicate segment ids: {duplicates[:10]}")
    return family_rows


def tags_for(row: dict[str, Any]) -> list[str]:
    blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "current_output_text", "spanish_text", "english_text"))
    tags = []
    checks = [
        ("holy_site_name_runtime", HOLY_SITE_NAME_RE),
        ("holy_site_runtime_count", HOLY_SITE_RUNTIME_RE),
        ("requirement_surface", REQUIREMENT_RE),
        ("effect_surface", EFFECT_RE),
        ("faith_relation", FAITH_RELATION_RE),
        ("building_place", BUILDING_PLACE_RE),
        ("title_realm", TITLE_REALM_RE),
        ("multiline_effect_list", MULTILINE_EFFECT_RE),
        ("dense_token_cluster", DENSE_TOKEN_RE),
    ]
    for label, pattern in checks:
        if pattern.search(blob):
            tags.append(label)
    return tags


def classify(row: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    tags = tags_for(row)
    risk = str(row.get("risk_bucket") or "")
    token_count = int(row.get("token_count") or 0)
    key = str(row.get("source_key") or "")

    if "multiline_effect_list" in tags and (risk == "high_multiline_effect_list" or "EFFECT_LIST" in key):
        return (
            "effect_list_or_requirement_multiline",
            "terminal_hold_or_effect_list_policy",
            "Multiline/effect-list shape must stay out of candidate generation until structural parser handles it.",
            tags,
        )
    if risk == "high_structural_token_density" or token_count >= 7:
        return (
            "dense_token_cluster_hold",
            "terminal_hold_or_parser_later",
            "High token density makes automatic or broad human review unsafe without parser roles.",
            tags,
        )
    if "holy_site_name_runtime" in tags or "holy_site_runtime_count" in tags:
        if "effect_surface" in tags or "requirement_surface" in tags:
            return (
                "holy_site_effect_requirement",
                "splitter_reuse_holy_site_policy",
                "Holy-site runtime/name appears inside effect or requirement wording.",
                tags,
            )
        return (
            "holy_site_name_runtime",
            "splitter_read_only",
            "Holy-site runtime name surface can be split read-only before any parser/apply decision.",
            tags,
        )
    if "building_place" in tags:
        return (
            "building_or_place_holy_site",
            "terminal_hold_or_domain_context_review",
            "Building/place holy-site context is prose-heavy and should not become an automatic candidate.",
            tags,
        )
    if "title_realm" in tags:
        return (
            "title_or_realm_holy_site_context",
            "parser_later",
            "Title/realm context around holy-site requirements needs role-aware parsing.",
            tags,
        )
    if "faith_relation" in tags:
        return (
            "faith_holy_site_relation",
            "parser_later",
            "Faith/holy-site relation needs article/preposition and runtime role handling.",
            tags,
        )
    return (
        "human_context_needed",
        "small_human_or_hold_after_architecture",
        "No narrow safe splitter signal; keep as human/context after parser decisions.",
        tags,
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def representative_examples(rows: list[dict[str, Any]], limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        subtype = str(row["operational_subfamily"])
        if len(grouped[subtype]) < limit:
            grouped[subtype].append(
                {
                    "segment_id": row["segment_id"],
                    "source_key": row["source_key"],
                    "relative_path": row["relative_path"],
                    "risk_bucket": row["risk_bucket"],
                    "recommended_action": row["recommended_action"],
                    "current_output_text": row["current_output_text"],
                    "english_text": row["english_text"],
                }
            )
    return dict(grouped)


def main() -> None:
    base_summary = read_json(BASE_SUMMARY_PATH)
    family_rows = validate_inputs(base_summary, read_jsonl(BASE_JSONL_PATH))
    segment_ids = sorted(int(row["segment_id"]) for row in family_rows)
    with connect_readonly() as conn:
        latest_run_id = latest_complete_state_run(conn)
        latest_states = fetch_latest_states(conn, latest_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in sorted(family_rows, key=lambda item: (str(item.get("risk_bucket") or ""), int(item["segment_id"]))):
        subfamily, action, reason, tags = classify(row)
        state = latest_states.get(int(row["segment_id"]), {})
        reviewed.append(
            {
                "record_type": "holy_site_effect_or_requirement_review",
                "source": SOURCE,
                "base_segment_state_run_id": EXPECTED_BASE_SEGMENT_STATE_RUN_ID,
                "latest_segment_state_run_id": latest_run_id,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "source_line_number": row.get("source_line_number"),
                "lane": "domain_policy_vote_candidate",
                "surface_bucket": row.get("surface_bucket"),
                "hold_family": row.get("hold_family"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": int(row.get("token_count") or 0),
                "bracket_token_count": int(row.get("bracket_token_count") or 0),
                "current_output_text": row.get("current_output_text"),
                "spanish_text": row.get("spanish_text"),
                "english_text": row.get("english_text"),
                "latest_state_group": state.get("state_group"),
                "latest_final_state": state.get("final_state"),
                "latest_needs_output_apply": int(state.get("needs_output_apply") or 0),
                "latest_confirmed_matches_output": int(state.get("confirmed_matches_output") or 0),
                "operational_subfamily": subfamily,
                "recommended_action": action,
                "review_reason": reason,
                "review_tags": tags,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )

    subfamily_counts = Counter(row["operational_subfamily"] for row in reviewed)
    risk_counts = Counter(row["risk_bucket"] for row in reviewed)
    action_counts = Counter(row["recommended_action"] for row in reviewed)
    latest_state_counts = Counter(str(row.get("latest_state_group") or "missing") for row in reviewed)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_review",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_segment_state_run_id": EXPECTED_BASE_SEGMENT_STATE_RUN_ID,
        "latest_segment_state_run_id": latest_run_id,
        "lane": "domain_policy_vote_candidate",
        "surface_bucket": EXPECTED_SURFACE_BUCKET,
        "hold_family": EXPECTED_FAMILY,
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "operational_subfamily_counts": dict(subfamily_counts),
        "risk_bucket_counts": dict(risk_counts),
        "recommended_action_counts": dict(action_counts),
        "latest_state_group_counts": dict(latest_state_counts),
        "representative_examples": representative_examples(reviewed),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Register a read-only splitter for holy_site_effect_requirement/name_runtime only; "
            "keep dense, multiline, building/place, title/realm, and faith-relation contexts in parser-later or hold."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_holy_site_effect_requirement_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, reviewed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate religion_faith_doctrine holy_site_effect_or_requirement review",
        "",
        f"base_segment_state_run_id: {EXPECTED_BASE_SEGMENT_STATE_RUN_ID}",
        f"latest_segment_state_run_id: {latest_run_id}",
        f"review_count: {len(reviewed)}",
        "",
        "operational_subfamily_counts:",
        *[f"- {count} | {key}" for key, count in subfamily_counts.most_common()],
        "",
        "risk_bucket_counts:",
        *[f"- {count} | {key}" for key, count in risk_counts.most_common()],
        "",
        "recommended_action_counts:",
        *[f"- {count} | {key}" for key, count in action_counts.most_common()],
        "",
        "representative_examples:",
    ]
    for subfamily, examples in summary["representative_examples"].items():
        lines.extend(["", f"## {subfamily}"])
        for example in examples:
            output = str(example.get("current_output_text") or "").replace("\n", "\\n")
            lines.extend(
                [
                    f"- segment_id {example['segment_id']} | {example['risk_bucket']} | {example['source_key']}",
                    f"  action: {example['recommended_action']}",
                    f"  output: {output[:420]}",
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
            "",
            f"recommendation: {summary['single_operational_recommendation']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
