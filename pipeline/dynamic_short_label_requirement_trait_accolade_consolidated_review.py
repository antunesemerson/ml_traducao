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


TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "short_label_style_microagent")
DIRECT_SOURCE_DECISION = "needs_dynamic_short_requirement_trait_accolade_policy"
RESIDUAL_SOURCE_DECISION = "needs_dynamic_short_requirement_trait_accolade_policy"

ALLOWED_DECISIONS = {
    "dynamic_short_trait_accolade_ready_false_reopen",
    "dynamic_short_trait_accolade_ready_lifecycle",
    "needs_dynamic_short_trait_requirement_policy",
    "needs_dynamic_short_accolade_requirement_policy",
    "needs_dynamic_short_modifier_descriptor_policy",
    "needs_dynamic_short_trait_accolade_scope_getter_policy",
    "needs_dynamic_short_trait_accolade_script_value_policy",
    "needs_dynamic_short_trait_accolade_effect_list_policy",
    "needs_dynamic_short_trait_accolade_title_law_policy",
    "needs_dynamic_short_trait_accolade_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"NÃ|Ãƒ|Â|�|\b(?:Successo|consiguio|consiguió|ganaste|tendras|tendrás|"
    r"posesion|posesión|reclamacion|reclamación|sera|será|mas|más|facil|fácil|the|your|you|their|cannot)\b",
    re.IGNORECASE,
)
ACCOLADE_RE = re.compile(r"accolade|GetMaA|maa_|acclaimed|knight|accolade_type", re.IGNORECASE)
TRAIT_RE = re.compile(r"trait|GetTrait|target_trait|característica|traço|trait_track|lifestyle", re.IGNORECASE)
MODIFIER_RE = re.compile(r"modifier|GetModifier|descriptor|character_modifier|opinion_modifier", re.IGNORECASE)
SCOPE_GETTER_RE = re.compile(
    r"\[[^\]]*(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|recipient|target|root)[^\]]*\]|"
    r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|Get[A-Za-z0-9_]+\(",
    re.IGNORECASE,
)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.IGNORECASE)
EFFECT_LIST_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|effect|effects_l_", re.IGNORECASE)
TITLE_LAW_RE = re.compile(
    r"title|primary_title|liege|empire|duchy|county|counties|domain|realm|law|government|succession|vassal|doctrine",
    re.IGNORECASE,
)


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
        SELECT segment_id, final_state, state_group, needs_output_apply,
               confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_family_shapes(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> dict[int, tuple[str, ...]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, issue_family
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        """,
        (ledger_run_id, *segment_ids),
    ).fetchall()
    by_segment: dict[int, set[str]] = {}
    for row in rows:
        by_segment.setdefault(int(row["segment_id"]), set()).add(row["issue_family"])
    return {segment_id: tuple(sorted(families)) for segment_id, families in by_segment.items()}


def collect_sources(requirement_jsonl: Path, residual_jsonl: Path) -> tuple[list[dict[str, Any]], int]:
    by_segment: dict[int, dict[str, Any]] = {}
    source_files: dict[int, set[str]] = defaultdict(set)
    source_decisions: dict[int, set[str]] = defaultdict(set)
    total_raw = 0

    for row in read_jsonl(requirement_jsonl):
        if row.get("requirement_tooltip_decision") != DIRECT_SOURCE_DECISION:
            continue
        total_raw += 1
        segment_id = int(row["segment_id"])
        by_segment.setdefault(segment_id, row)
        source_files[segment_id].add(str(requirement_jsonl))
        source_decisions[segment_id].add(row["requirement_tooltip_decision"])

    for row in read_jsonl(residual_jsonl):
        if row.get("residual_decision") != RESIDUAL_SOURCE_DECISION:
            continue
        total_raw += 1
        segment_id = int(row["segment_id"])
        by_segment.setdefault(segment_id, row)
        source_files[segment_id].add(str(residual_jsonl))
        source_decisions[segment_id].add(row["residual_decision"])

    rows: list[dict[str, Any]] = []
    for segment_id in sorted(by_segment):
        row = dict(by_segment[segment_id])
        row["_source_files"] = sorted(source_files[segment_id])
        row["_source_decisions"] = sorted(source_decisions[segment_id])
        rows.append(row)
    return rows, total_raw


def state_ready(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state["state_group"] == "pending"
        and int(state["is_closed"] or 0) == 0
        and int(state["needs_output_apply"] or 0) == 0
        and int(state["confirmed_matches_output"] or 0) == 1
    )


def exact_family_shape(families: tuple[str, ...] | None) -> bool:
    return families == tuple(sorted(TARGET_FAMILIES))


def token_boundaries_ok(text: str) -> bool:
    return text.count("[") == text.count("]") and text.count("$") % 2 == 0


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Accolade", ACCOLADE_RE),
        ("Trait", TRAIT_RE),
        ("ModifierDescriptor", MODIFIER_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("EffectList", EFFECT_LIST_RE),
        ("TitleLaw", TITLE_LAW_RE),
    ]:
        if pattern.search(text) and label not in labels:
            labels.append(label)
    if CK3_TOKEN_RE.search(text) and "CK3DynamicToken" not in labels:
        labels.append("CK3DynamicToken")
    return labels


def classify(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> tuple[str, str, str]:
    text = str(row.get("current_text") or "")
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "key", "current_text"))
    if not state_ready(state):
        return "blocked_uncertain", "not_pending_confirmed", "state guard failed in selected segment-state run"
    if not exact_family_shape(families):
        return "blocked_uncertain", "family_shape_guard", "open issue families are no longer exactly dynamic + short_label"
    if not text.strip() or not token_boundaries_ok(text):
        return "needs_dynamic_short_trait_accolade_residual_repair", "broken_or_missing_text", "missing text or malformed token boundary"
    if RESIDUAL_RE.search(text):
        return "needs_dynamic_short_trait_accolade_residual_repair", "visible_residual_or_mojibake", "visible residual or mojibake remains"
    if EFFECT_LIST_RE.search(haystack):
        return "needs_dynamic_short_trait_accolade_effect_list_policy", "trait_accolade_effect_list", "trait/accolade is embedded in effect list or multiline"
    if SCRIPT_VALUE_RE.search(haystack):
        return "needs_dynamic_short_trait_accolade_script_value_policy", "trait_accolade_script_value", "trait/accolade is coupled to numeric/script value"
    if ACCOLADE_RE.search(haystack):
        return "needs_dynamic_short_accolade_requirement_policy", "accolade_requirement", "accolade/acclaimed-knight requirement needs policy"
    if TRAIT_RE.search(haystack):
        return "needs_dynamic_short_trait_requirement_policy", "trait_requirement", "trait requirement or trait label needs policy"
    if MODIFIER_RE.search(haystack):
        if TITLE_LAW_RE.search(haystack):
            return "needs_dynamic_short_trait_accolade_title_law_policy", "title_law_modifier_descriptor", "modifier/descriptor is coupled to title/law/domain context"
        return "needs_dynamic_short_modifier_descriptor_policy", "modifier_descriptor", "modifier or descriptor needs policy"
    if TITLE_LAW_RE.search(haystack):
        return "needs_dynamic_short_trait_accolade_title_law_policy", "title_law_trait_accolade", "trait/accolade branch is coupled to title/law/domain context"
    if SCOPE_GETTER_RE.search(text):
        return "needs_dynamic_short_trait_accolade_scope_getter_policy", "trait_accolade_scope_getter", "scope/getter mixed with trait/accolade"
    if CK3_TOKEN_RE.search(text):
        return "needs_new_microagent", "unclassified_trait_accolade_token", "trait/accolade token did not fit a known safe subpolicy"
    if int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return "dynamic_short_trait_accolade_ready_false_reopen", "ready_false_reopen_trait_accolade", "plain trait/accolade label appears aligned for future false-reopen lifecycle"
    return "dynamic_short_trait_accolade_ready_lifecycle", "ready_lifecycle_trait_accolade", "plain trait/accolade label appears aligned for future lifecycle"


def decide(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state, families)
    text = str(row.get("current_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": text,
        "source_files": row["_source_files"],
        "source_decisions": row["_source_decisions"],
        "trait_accolade_decision": decision,
        "trait_accolade_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(text),
        "requires_lifecycle_later": decision in {
            "dynamic_short_trait_accolade_ready_false_reopen",
            "dynamic_short_trait_accolade_ready_lifecycle",
        },
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_short_label_requirement_trait_accolade_consolidated_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def recommendation(decision_counts: Counter[str]) -> str:
    ready = (
        decision_counts["dynamic_short_trait_accolade_ready_false_reopen"]
        + decision_counts["dynamic_short_trait_accolade_ready_lifecycle"]
    )
    if ready >= 5:
        return "prepare_readonly_lifecycle_for_dynamic_short_trait_accolade_ready"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_dynamic_short_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 8:
            return f"register_specific_policy_microagent_for_{top_decision}"
    if decision_counts["needs_dynamic_short_trait_accolade_residual_repair"] >= 5:
        return "prepare_residual_split_before_any_apply"
    return "fragmented_migrate_to_script_value_or_global_diagnostic"


def validate_rows(rows: list[dict[str, Any]]) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_files",
        "source_decisions",
        "trait_accolade_decision",
        "trait_accolade_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    seen: set[int] = set()
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if not set(row["source_decisions"]) <= {DIRECT_SOURCE_DECISION, RESIDUAL_SOURCE_DECISION}:
            raise SystemExit(f"unexpected source decisions for {segment_id}: {row['source_decisions']}")
        if row["trait_accolade_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for {segment_id}: {row['trait_accolade_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {segment_id}")
        if row["corrected_text"]:
            if CK3_TOKEN_RE.findall(row["current_text"]) != CK3_TOKEN_RE.findall(row["corrected_text"]):
                raise SystemExit(f"CK3 token mismatch in corrected_text: {segment_id}")


def write_reports(rows: list[dict[str, Any]], total_raw: int) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["trait_accolade_decision"] for row in rows)
    subpolicy_counts = Counter(row["trait_accolade_subpolicy"] for row in rows)
    ready = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    rec = recommendation(decision_counts)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic + short_label requirement trait/accolade consolidated review",
        "",
        f"total_raw: {total_raw}",
        f"total_deduplicated: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_for_future_lifecycle: {ready}",
            f"future_apply_candidates: {apply_count}",
            f"recommendation: {rec}",
            "",
            "Safety: read-only review; no lifecycle, apply, segment-state, confirmations, production, reindex, training, source edits, or output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirement-jsonl", required=True)
    parser.add_argument("--residual-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source, total_raw = collect_sources(db.project_path(args.requirement_jsonl), db.project_path(args.residual_jsonl))
    segment_ids = [int(row["segment_id"]) for row in source]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed = [decide(row, states.get(int(row["segment_id"])), family_shapes.get(int(row["segment_id"]))) for row in source]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed, total_raw)
    ready = sum(1 for row in reviewed if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in reviewed if row["requires_apply_later"])

    print(f"total_raw={total_raw}")
    print(f"total_deduplicated={len(reviewed)}")
    print(f"ready_for_future_lifecycle={ready}")
    print(f"future_apply_candidates={apply_count}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))
    print(f"recommendation={recommendation(decision_counts)}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")


if __name__ == "__main__":
    main()
