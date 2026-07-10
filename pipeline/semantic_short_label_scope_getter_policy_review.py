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


TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!")
GETTER_RE = re.compile(r"\[[^\]]*\bGet[A-Za-z0-9_]*(?:\.|[|\]])")
CONCEPT_RE = re.compile(
    r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[(?:county|military_building|dynasty_prestige|title|faith|situation)[^\]]*\]|"
    r"\b(?:PrimaryTitle|CountyData|Faith|Artifact|building|dynasty|title|county|faith)\b",
    re.IGNORECASE,
)
CUSTOM_GENDER_RE = re.compile(
    r"Custom\(|custom_loc|ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)",
    re.IGNORECASE,
)
TOOLTIP_RE = re.compile(r"_tt\d*$|tooltip|effects_l_|#P|#low|#high", re.IGNORECASE)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|success|failure|flavor|toast|scheme|ongoing|outcome|childhood|court_|"
    r"diplomacy_|learning_|natural_disaster|nomad|governor|frankokratia",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"claimant|primarytitle|county|faith|artifact|building|dynasty|military|title|court|"
    r"theology|diplomacy|situation|government",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:consigui[oó]|todo|acepta|torrente|posesi[oó]n|gana|azar|paliza|mensual|"
    r"ser[aá]|m[aá]s|f[aá]cil|revela|falsedad|manipulas|monta|n[uú]mero|vergonzoso)\b",
    re.IGNORECASE,
)
TOKEN_BOUNDARY_RE = re.compile(r"\w\?\w|[\[\]]{2,}|\$\s*\$")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            final_state,
            state_group,
            needs_output_apply,
            confirmed_matches_output,
            needs_reopen,
            is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            COUNT(*) AS open_issue_count,
            SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_count,
            SUM(CASE WHEN issue_family = 'short_label_style_microagent' THEN 1 ELSE 0 END) AS short_label_count,
            SUM(CASE WHEN issue_family NOT IN ('semantic_review_router', 'short_label_style_microagent') THEN 1 ELSE 0 END) AS other_family_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def collect_scope_getter_rows(effect_list_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(effect_list_path)):
        if row.get("effect_list_decision") != "needs_scope_getter_policy":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


def has_ready_state(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def has_exact_open_family_shape(family_shape: dict[str, Any] | None) -> bool:
    return bool(
        family_shape
        and int(family_shape.get("open_issue_count") or 0) == 2
        and int(family_shape.get("semantic_count") or 0) == 1
        and int(family_shape.get("short_label_count") or 0) == 1
        and int(family_shape.get("other_family_count") or 0) == 0
    )


def ready_decision(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> str | None:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    if not has_ready_state(state) or not has_exact_open_family_shape(family_shape):
        return None
    if not GETTER_RE.search(text):
        return None
    if CUSTOM_GENDER_RE.search(haystack) or DOMAIN_RE.search(haystack) or RESIDUAL_RE.search(text):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if int(state.get("needs_reopen") or 0) == 1:
        return "scope_getter_ready_false_reopen"
    if TOOLTIP_RE.search(haystack):
        return "scope_getter_ready_tooltip_lifecycle"
    return None


def policy_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    if CUSTOM_GENDER_RE.search(haystack):
        return "needs_scope_getter_custom_loc_or_gender_policy", "custom_loc_or_gender"
    if CONCEPT_RE.search(haystack):
        return "needs_scope_getter_concept_policy", "scope_getter_with_concept"
    if TOOLTIP_RE.search(haystack):
        return "needs_scope_getter_requirement_policy", "scope_getter_requirement_or_tooltip"
    if EVENT_RE.search(haystack):
        return "needs_event_context_composer", "scope_getter_event_context"
    if DOMAIN_RE.search(haystack):
        return "needs_domain_context", "scope_getter_domain_context"
    if RESIDUAL_RE.search(text):
        return "needs_residual_repair", "visible_spanish_or_english_residual"
    if GETTER_RE.search(text):
        return "needs_scope_getter_policy", "scope_getter_expression"
    return "blocked_uncertain", "blocked_uncertain"


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> dict[str, Any]:
    ready = ready_decision(row, state, family_shape)
    if ready:
        return {
            "scope_getter_decision": ready,
            "scope_getter_subpolicy": ready.removeprefix("scope_getter_ready_").removesuffix("_lifecycle"),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "scope/getter item appears aligned for future narrow lifecycle",
        }
    decision, subpolicy = policy_decision(row)
    if not has_ready_state(state):
        notes = "blocked by segment_state guard; kept out of ready lifecycle"
    elif not has_exact_open_family_shape(family_shape):
        notes = "blocked by open issue family shape guard; kept out of ready lifecycle"
    else:
        notes = f"routed to {decision}; no apply or lifecycle emitted by this review"
    return {
        "scope_getter_decision": decision,
        "scope_getter_subpolicy": subpolicy,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_semantic_short_label_scope_getter_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["scope_getter_decision"] for row in rows)
    subpolicy_counts = Counter(row["scope_getter_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["scope_getter_decision"].startswith("scope_getter_ready_"))
    scope_needs_count = sum(count for decision, count in decision_counts.items() if decision.startswith("needs_scope_getter_"))

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    if ready_count >= 5:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif scope_needs_count >= 6:
        recommendation = "record_future_scope_getter_microagent_and_close_combo"
    else:
        recommendation = "close_combo_and_return_to_global_diagnostic"

    lines = [
        "Semantic short-label scope/getter policy review",
        "",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_for_future_lifecycle: {ready_count}",
            "apply_candidates_future: 0",
            f"scope_getter_needs_total: {scope_needs_count}",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effect-list-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source_rows = collect_scope_getter_rows(args.effect_list_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_effect_list_decision": "needs_scope_getter_policy",
                **decide(row, states.get(segment_id), family_shapes.get(segment_id)),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready_count = sum(1 for row in reviewed if row["scope_getter_decision"].startswith("scope_getter_ready_"))
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={ready_count}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
