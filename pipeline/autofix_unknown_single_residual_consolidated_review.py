from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE_SPECS = {
    "single_family": ("decision", "needs_residual_repair"),
    "dynamic": ("dynamic_decision", "needs_residual_repair"),
    "concept": ("concept_decision", "needs_residual_repair"),
    "concept_domain": ("concept_domain_decision", "needs_residual_repair"),
    "event_context": ("event_context_decision", "needs_residual_repair"),
}

TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")
CK3_DYNAMIC_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|Select_CString|Custom\(|ScriptValue|Concept|"
    r"\bGet[A-Za-z0-9_]*\b|\b(?:ROOT|CHARACTER|TARGET|SCOPE|THIS)\.",
    re.IGNORECASE,
)
GENDER_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)\b|Get(?:SheHe|HerHis|WomanMan|WomenMen)|custom_loc",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(
    r"historical_characters|culture|religion|faith|holy_order|law|succession|title|rank|"
    r"trait_|nickname|dynasty|house|government|factions|ai_personality|great_project|"
    r"artifact|buildings|regiment|acclaimed_knight|diarch|realm|war|schemes",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc$|desc\.|option|toast|story_cycle|scheme|interaction|memory|memories|"
    r"activity|activities|travel|journey|tourism|roaming|contest|tournament|board_game",
    re.IGNORECASE,
)
SPANISH_RESIDUAL_RE = re.compile(
    r"\b(el|la|los|las|un|una|uno|del|de la|este|esta|estos|estas|como|mientras|"
    r"probabilidad|fuerza|verdadero|verdadera|señor|viajeros|agotados|montañas|oasis|"
    r"personaje|guerra|reino|condado|ducado)\b",
    re.IGNORECASE,
)
ENGLISH_RESIDUAL_RE = re.compile(
    r"\b(the|will|must|cannot|should|kingdom|county|duchy|realm|war|hostile|opinion|"
    r"character|travelers|tired|hidden|mountains)\b",
    re.IGNORECASE,
)
BAD_ENCODING_RE = re.compile(r"[�]|(?:Ã.|Â.|Ð.|Ñ.)")

SAFE_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bprobabilidad\b", re.IGNORECASE), "chance"),
    (re.compile(r"\bfuerza\b", re.IGNORECASE), "força"),
    (re.compile(r"\bverdadero\b", re.IGNORECASE), "verdadeiro"),
    (re.compile(r"\bverdadera\b", re.IGNORECASE), "verdadeira"),
    (re.compile(r"\bseñor\b", re.IGNORECASE), "senhor"),
    (re.compile(r"\bviajeros\b", re.IGNORECASE), "viajantes"),
    (re.compile(r"\bagotados\b", re.IGNORECASE), "exaustos"),
    (re.compile(r"\bmontañas\b", re.IGNORECASE), "montanhas"),
    (re.compile(r"\bpequeños\b", re.IGNORECASE), "pequenos"),
    (re.compile(r"\bocultos\b", re.IGNORECASE), "ocultos"),
    (re.compile(r"\bentre\b", re.IGNORECASE), "entre"),
    (re.compile(r"\bcomo\b", re.IGNORECASE), "como"),
]


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
            needs_reopen,
            needs_output_apply,
            confirmed_matches_output
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
            SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_count,
            SUM(CASE WHEN issue_family != 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS other_family_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        GROUP BY segment_id
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def tokens_preserved(before: str, after: str) -> bool:
    return tokens(before) == tokens(after)


def mechanically_correct(text: str) -> str:
    corrected = text
    for pattern, replacement in SAFE_REPLACEMENTS:
        corrected = pattern.sub(replacement, corrected)
    corrected = re.sub(r"\s+([,.;:!?])", r"\1", corrected)
    corrected = re.sub(r" {2,}", " ", corrected)
    return corrected


def clean_apply_guard(
    row: dict[str, Any],
    state: dict[str, Any] | None,
    family_shape: dict[str, Any] | None,
    corrected_text: str,
) -> bool:
    text = as_text(row.get("current_text"))
    return bool(
        state
        and family_shape
        and state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(family_shape.get("open_issue_count") or 0) == 1
        and int(family_shape.get("autofix_count") or 0) == 1
        and int(family_shape.get("other_family_count") or 0) == 0
        and corrected_text
        and corrected_text != text
        and tokens_preserved(text, corrected_text)
        and not BAD_ENCODING_RE.search(corrected_text)
        and not re.search(r"\w\?\w", corrected_text)
        and text.count("[") == text.count("]")
        and text.count("$") % 2 == 0
        and not DOMAIN_RE.search(" ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text]))
        and not GENDER_RE.search(text)
    )


def decide(row: dict[str, Any], state: dict[str, Any] | None, family_shape: dict[str, Any] | None) -> dict[str, Any]:
    text = as_text(row.get("current_text"))
    haystack = " ".join([as_text(row.get("relative_path")), as_text(row.get("key")), text])
    corrected = mechanically_correct(text)
    safe_guard = clean_apply_guard(row, state, family_shape, corrected)

    if GENDER_RE.search(haystack):
        decision = "needs_gender_or_custom_loc_policy"
        repair_kind = "blocked_gender_or_custom_loc"
        corrected = ""
    elif CK3_DYNAMIC_RE.search(text):
        decision = "needs_dynamic_expression_agent"
        repair_kind = "blocked_dynamic_expression"
        corrected = ""
    elif DOMAIN_RE.search(haystack):
        decision = "needs_domain_context"
        repair_kind = "blocked_domain_context"
        corrected = ""
    elif EVENT_RE.search(haystack):
        decision = "needs_event_context_composer"
        repair_kind = "blocked_event_context"
        corrected = ""
    elif safe_guard and SPANISH_RESIDUAL_RE.search(text):
        decision = "safe_spanish_residual_repair"
        repair_kind = "spanish_residual"
    elif safe_guard and ENGLISH_RESIDUAL_RE.search(text):
        decision = "safe_english_residual_repair"
        repair_kind = "english_residual"
    elif safe_guard and corrected != text:
        decision = "safe_punctuation_or_spacing_microrepair"
        repair_kind = "punctuation_or_spacing"
    elif SPANISH_RESIDUAL_RE.search(text) or ENGLISH_RESIDUAL_RE.search(text):
        decision = "needs_semantic_review"
        repair_kind = "blocked_semantic_review"
        corrected = ""
    else:
        decision = "blocked_uncertain"
        repair_kind = "blocked_uncertain"
        corrected = ""

    requires_apply = decision.startswith("safe_")
    return {
        "residual_decision": decision,
        "repair_kind": repair_kind,
        "corrected_text": corrected if requires_apply else "",
        "requires_apply_later": requires_apply,
        "requires_lifecycle_later": False,
        "notes": (
            "mechanical residual repair candidate for future protected apply"
            if requires_apply
            else f"retained as {decision}; no safe mechanical corrected_text emitted"
        ),
    }


def collect_sources(args: argparse.Namespace) -> tuple[dict[int, dict[str, Any]], Counter[str]]:
    specs = {
        "single_family": args.single_family_jsonl,
        "dynamic": args.dynamic_jsonl,
        "concept": args.concept_jsonl,
        "concept_domain": args.concept_domain_jsonl,
        "event_context": args.event_context_jsonl,
    }
    by_segment: dict[int, dict[str, Any]] = {}
    raw_counts: Counter[str] = Counter()
    for source_name, path_value in specs.items():
        decision_field, residual_value = SOURCE_SPECS[source_name]
        for row in read_jsonl(db.project_path(path_value)):
            if row.get(decision_field) != residual_value:
                continue
            raw_counts[source_name] += 1
            segment_id = int(row["segment_id"])
            if segment_id not in by_segment:
                by_segment[segment_id] = {
                    "segment_id": segment_id,
                    "key": row["key"],
                    "relative_path": row["relative_path"],
                    "current_text": row["current_text"],
                    "source_residual_reports": [],
                }
            by_segment[segment_id]["source_residual_reports"].append(source_name)
    return by_segment, raw_counts


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_autofix_unknown_single_residual_consolidated_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], raw_counts: Counter[str]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["residual_decision"] for row in rows)
    repair_counts = Counter(row["repair_kind"] for row in rows)
    apply_total = sum(count for key, count in decision_counts.items() if key.startswith("safe_"))
    recommendation = (
        "prepare_protected_residual_apply"
        if apply_total >= 10
        else "return_to_global_diagnostic_or_open_semantic_short_label_review"
    )

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    lines = [
        "Autofix unknown single-family residual consolidated review",
        "",
        "Raw counts by source:",
    ]
    for key in ("single_family", "dynamic", "concept", "concept_domain", "event_context"):
        lines.append(f"- {key}: {raw_counts.get(key, 0):,}")
    lines.extend(["", f"deduplicated_reviewed: {len(rows):,}", "", "Decision counts:"])
    if decision_counts:
        for key, count in decision_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Repair/block kinds:"])
    if repair_counts:
        for key, count in repair_counts.most_common():
            lines.append(f"- {key}: {count:,}")
    else:
        lines.append("- none: 0")
    lines.extend(["", f"apply_candidates_future: {apply_total:,}", "Safe examples:"])
    safe_examples = [row for row in rows if row["requires_apply_later"]][:5]
    if safe_examples:
        for row in safe_examples:
            lines.append(f"- segment_id={row['segment_id']} {row['repair_kind']}: {row['corrected_text'][:160]}")
    else:
        lines.append("- none: 0")
    lines.extend(["", f"Recommendation: {recommendation}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, repair_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    parser.add_argument("--single-family-jsonl", required=True)
    parser.add_argument("--dynamic-jsonl", required=True)
    parser.add_argument("--concept-jsonl", required=True)
    parser.add_argument("--concept-domain-jsonl", required=True)
    parser.add_argument("--event-context-jsonl", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    by_segment, raw_counts = collect_sources(args)
    segment_ids = sorted(by_segment)
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)
    reviewed: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        base = by_segment[segment_id]
        residual = decide(base, states.get(segment_id), family_shapes.get(segment_id))
        reviewed.append({**base, **residual})
    jsonl_path, txt_path, decision_counts, repair_counts = write_reports(reviewed, raw_counts)
    apply_total = sum(count for key, count in decision_counts.items() if key.startswith("safe_"))
    print("raw_counts=" + json.dumps(dict(raw_counts), ensure_ascii=False, sort_keys=True))
    print(f"deduplicated_reviewed={len(reviewed)}")
    print(f"apply_candidates={apply_total}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("repair_kinds=" + json.dumps(dict(repair_counts), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
