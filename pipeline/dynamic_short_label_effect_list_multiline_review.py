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


SOURCE_DECISION = "needs_dynamic_short_effect_list_multiline_policy"
TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "short_label_style_microagent")

ALLOWED_DECISIONS = {
    "effect_list_multiline_ready_false_reopen",
    "effect_list_multiline_ready_lifecycle",
    "needs_effect_list_multiline_simple_policy",
    "needs_effect_list_multiline_bullets_policy",
    "needs_effect_list_multiline_title_law_policy",
    "needs_effect_list_multiline_trait_accolade_policy",
    "needs_effect_list_multiline_artifact_activity_policy",
    "needs_effect_list_multiline_scope_getter_policy",
    "needs_effect_list_multiline_script_value_policy",
    "needs_effect_list_multiline_event_context_composer",
    "needs_effect_list_multiline_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"NÃ|Ãƒ|Â|�|\b(?:He pasado|creedme|consiguio|consiguió|ganaste|tendras|tendrás|"
    r"posesion|posesión|reclamacion|reclamación|sera|será|mas|más|facil|fácil|the|your|you|their|cannot)\b",
    re.IGNORECASE,
)
BULLET_RE = re.compile(r"\$EFFECT_LIST_BULLET\$|list_boilerplate|\\n\s*\$EFFECT_LIST_BULLET\$|\n\s*[-*]", re.IGNORECASE)
MULTILINE_RE = re.compile(r"\\n|\n|#indent|#weak|#WEAK|#bold", re.IGNORECASE)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|#N\s*\[?SCOPE|\b[0-9]+\s*%", re.IGNORECASE)
TITLE_LAW_RE = re.compile(r"title|law|government|realm|succession|vassal|liege|contract|domain|county|counties|duchy|kingdom|empire|heritage", re.IGNORECASE)
TRAIT_ACCOLADE_RE = re.compile(r"trait|GetTrait|accolade|knight|prowess|modifier", re.IGNORECASE)
ARTIFACT_ACTIVITY_RE = re.compile(r"artifact|activity|travel|tournament|legend|item|journey|feast|wedding|hunt", re.IGNORECASE)
SCOPE_GETTER_RE = re.compile(
    r"\[[^\]]*(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|recipient|target|root)[^\]]*\]|"
    r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|Get[A-Za-z0-9_]+\(",
    re.IGNORECASE,
)
EVENT_CONTEXT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|agent_|contract|interaction|perspective", re.IGNORECASE)


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


def source_rows(effect_list_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(effect_list_jsonl):
        if row.get("effect_list_decision") != SOURCE_DECISION:
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate source segment_id: {segment_id}")
        seen.add(segment_id)
        rows.append(row)
    return rows


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
        ("BulletEffectList", BULLET_RE),
        ("Multiline", MULTILINE_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("TitleLaw", TITLE_LAW_RE),
        ("TraitAccolade", TRAIT_ACCOLADE_RE),
        ("ArtifactActivity", ARTIFACT_ACTIVITY_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
        ("EventContext", EVENT_CONTEXT_RE),
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
        return "needs_effect_list_multiline_residual_repair", "broken_or_missing_text", "missing text or malformed token boundary"
    if RESIDUAL_RE.search(text):
        return "needs_effect_list_multiline_residual_repair", "visible_residual_or_mojibake", "visible residual or mojibake remains"
    if SCRIPT_VALUE_RE.search(haystack):
        return "needs_effect_list_multiline_script_value_policy", "script_value_multiline", "script value or numeric multiline requirement"
    if TITLE_LAW_RE.search(haystack) and not BULLET_RE.search(text):
        return "needs_effect_list_multiline_title_law_policy", "title_law_multiline", "title/law/government multiline block"
    if TRAIT_ACCOLADE_RE.search(haystack) and not BULLET_RE.search(text):
        return "needs_effect_list_multiline_trait_accolade_policy", "trait_accolade_multiline", "trait/accolade multiline block"
    if ARTIFACT_ACTIVITY_RE.search(haystack) and not BULLET_RE.search(text):
        return "needs_effect_list_multiline_artifact_activity_policy", "artifact_activity_multiline", "artifact/activity multiline block"
    if BULLET_RE.search(text):
        return "needs_effect_list_multiline_bullets_policy", "bullet_effect_list", "bullet/list-boilerplate multiline effect list"
    if EVENT_CONTEXT_RE.search(haystack):
        return "needs_effect_list_multiline_event_context_composer", "event_context_multiline", "event/context multiline block"
    if SCOPE_GETTER_RE.search(text):
        return "needs_effect_list_multiline_scope_getter_policy", "scope_getter_multiline", "scope/getter mixed with multiline block"
    if MULTILINE_RE.search(text):
        return "needs_effect_list_multiline_simple_policy", "simple_multiline", "simple multiline effect list still needs policy"
    if CK3_TOKEN_RE.search(text):
        return "needs_new_microagent", "unclassified_multiline_token", "multiline token did not fit a known safe subpolicy"
    if int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return "effect_list_multiline_ready_false_reopen", "ready_false_reopen_multiline", "plain multiline appears aligned for future false-reopen lifecycle"
    return "effect_list_multiline_ready_lifecycle", "ready_lifecycle_multiline", "plain multiline appears aligned for future lifecycle"


def decide(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state, families)
    text = str(row.get("current_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": text,
        "source_effect_list_decision": row.get("effect_list_decision"),
        "multiline_decision": decision,
        "multiline_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(text),
        "requires_lifecycle_later": decision in {
            "effect_list_multiline_ready_false_reopen",
            "effect_list_multiline_ready_lifecycle",
        },
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_short_label_effect_list_multiline_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def recommendation(decision_counts: Counter[str]) -> str:
    ready = decision_counts["effect_list_multiline_ready_false_reopen"] + decision_counts["effect_list_multiline_ready_lifecycle"]
    if ready >= 5:
        return "prepare_readonly_lifecycle_for_effect_list_multiline_ready"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_effect_list_multiline_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 8:
            return f"register_specific_policy_microagent_for_{top_decision}_and_close_this_track"
    if decision_counts["needs_effect_list_multiline_residual_repair"] >= 5:
        return "prepare_residual_split_before_any_apply"
    return "fragmented_migrate_to_requirement_residual_or_global_diagnostic"


def validate_rows(rows: list[dict[str, Any]], source_count: int) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_effect_list_decision",
        "multiline_decision",
        "multiline_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    if len(rows) != source_count:
        raise SystemExit(f"review count mismatch: reviewed={len(rows)} source={source_count}")
    seen: set[int] = set()
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if row["source_effect_list_decision"] != SOURCE_DECISION:
            raise SystemExit(f"wrong source decision for {segment_id}: {row['source_effect_list_decision']}")
        if row["multiline_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for {segment_id}: {row['multiline_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {segment_id}")
        if row["corrected_text"] and CK3_TOKEN_RE.findall(row["current_text"]) != CK3_TOKEN_RE.findall(row["corrected_text"]):
            raise SystemExit(f"CK3 token mismatch in corrected_text: {segment_id}")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["multiline_decision"] for row in rows)
    subpolicy_counts = Counter(row["multiline_subpolicy"] for row in rows)
    ready = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    rec = recommendation(decision_counts)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic + short_label effect list multiline review",
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
    parser.add_argument("--effect-list-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(db.project_path(args.effect_list_jsonl))
    segment_ids = [int(row["segment_id"]) for row in source]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed = [decide(row, states.get(int(row["segment_id"])), family_shapes.get(int(row["segment_id"]))) for row in source]
    validate_rows(reviewed, len(source))
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    ready = sum(1 for row in reviewed if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in reviewed if row["requires_apply_later"])

    print(f"source_rows={len(source)}")
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={ready}")
    print(f"future_apply_candidates={apply_count}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))
    print(f"recommendation={recommendation(decision_counts)}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")


if __name__ == "__main__":
    main()
