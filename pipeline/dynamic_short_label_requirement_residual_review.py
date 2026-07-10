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


SOURCE_DECISION = "needs_dynamic_short_requirement_residual_repair"
TARGET_FAMILIES = ("dynamic_ck3_expression_microagent", "short_label_style_microagent")

ALLOWED_DECISIONS = {
    "dynamic_short_requirement_safe_spanish_residual_repair",
    "dynamic_short_requirement_safe_english_residual_repair",
    "dynamic_short_requirement_safe_ptbr_fluency_repair",
    "needs_dynamic_short_requirement_title_law_policy",
    "needs_dynamic_short_requirement_trait_accolade_policy",
    "needs_dynamic_short_requirement_artifact_activity_policy",
    "needs_dynamic_short_requirement_script_value_policy",
    "needs_dynamic_short_requirement_effect_list_policy",
    "needs_dynamic_short_requirement_scope_getter_policy",
    "needs_dynamic_short_requirement_semantic_review",
    "blocked_uncertain",
}

CK3_TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|@[A-Za-z0-9_]+!",
    re.IGNORECASE,
)
BAD_ENCODING_RE = re.compile(r"NÃ|Ãƒ|Â|�")
WORD_QUESTION_RE = re.compile(r"\w\?\w")
SPANISH_RE = re.compile(
    r"\b(?:consiguio|consiguió|ganaste|tendras|tendrás|posesion|posesión|"
    r"reclamacion|reclamación|sera|será|mas|más|facil|fácil|debe|tener)\b",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(r"\b(?:the|your|you|their|has|have|will|cannot|must|should)\b", re.IGNORECASE)
PTBR_FLUENCY_RE = re.compile(r"\bnão-\[|em o\b|algo de\b|não deve ter a\b", re.IGNORECASE)
TITLE_LAW_RE = re.compile(
    r"title|primary_title|liege|empire|duchy|county|domain|GetTitleByKey|law|government|realm|succession|doctrine",
    re.IGNORECASE,
)
TRAIT_ACCOLADE_RE = re.compile(r"accolade|GetMaA|maa_|knight|trait|modifier|GetModifier", re.IGNORECASE)
ARTIFACT_ACTIVITY_RE = re.compile(r"artifact|activity|travel|tournament|legend|item|journey|feast|wedding|hunt", re.IGNORECASE)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.IGNORECASE)
EFFECT_LIST_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|effect|effects_l_", re.IGNORECASE)
SCOPE_GETTER_RE = re.compile(
    r"\[[^\]]*(?:ROOT|CHARACTER|TARGET|SCOPE|THIS|actor|recipient|target|root)[^\]]*\]|"
    r"\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]|Get[A-Za-z0-9_]+\(",
    re.IGNORECASE,
)


SAFE_REPAIRS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bnão-\[", re.IGNORECASE), "não ["),
]


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


def source_rows(requirement_jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(requirement_jsonl):
        if row.get("requirement_tooltip_decision") != SOURCE_DECISION:
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
        ("SpanishResidual", SPANISH_RE),
        ("EnglishResidual", ENGLISH_RE),
        ("PTBRFluency", PTBR_FLUENCY_RE),
        ("TitleLaw", TITLE_LAW_RE),
        ("TraitAccolade", TRAIT_ACCOLADE_RE),
        ("ArtifactActivity", ARTIFACT_ACTIVITY_RE),
        ("ScriptValue", SCRIPT_VALUE_RE),
        ("EffectList", EFFECT_LIST_RE),
        ("ScopeGetter", SCOPE_GETTER_RE),
    ]:
        if pattern.search(text) and label not in labels:
            labels.append(label)
    if CK3_TOKEN_RE.search(text) and "CK3DynamicToken" not in labels:
        labels.append("CK3DynamicToken")
    return labels


def safe_corrected_text(text: str) -> str:
    corrected = text
    for pattern, replacement in SAFE_REPAIRS:
        corrected = pattern.sub(replacement, corrected)
    return corrected


def can_safe_repair(text: str) -> bool:
    if any(
        pattern.search(text)
        for pattern in (
            TITLE_LAW_RE,
            TRAIT_ACCOLADE_RE,
            ARTIFACT_ACTIVITY_RE,
            SCRIPT_VALUE_RE,
            EFFECT_LIST_RE,
            SCOPE_GETTER_RE,
        )
    ):
        return False
    corrected = safe_corrected_text(text)
    return corrected != text and CK3_TOKEN_RE.findall(corrected) == CK3_TOKEN_RE.findall(text)


def classify(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> tuple[str, str, str, str]:
    text = str(row.get("current_text") or "")
    haystack = " ".join(str(row.get(key) or "") for key in ("relative_path", "key", "current_text"))
    if not state_ready(state):
        return "blocked_uncertain", "not_pending_confirmed", "", "state guard failed in selected segment-state run"
    if not exact_family_shape(families):
        return "blocked_uncertain", "family_shape_guard", "", "open issue families are no longer exactly dynamic + short_label"
    if not text.strip() or not token_boundaries_ok(text):
        return "blocked_uncertain", "broken_or_missing_text", "", "missing text or malformed token boundary"
    if can_safe_repair(text):
        corrected = safe_corrected_text(text)
        if SPANISH_RE.search(text):
            return "dynamic_short_requirement_safe_spanish_residual_repair", "spanish_residual_short_safe", corrected, "short mechanical Spanish residual repair candidate"
        if ENGLISH_RE.search(text):
            return "dynamic_short_requirement_safe_english_residual_repair", "english_residual_short_safe", corrected, "short mechanical English residual repair candidate"
        return "dynamic_short_requirement_safe_ptbr_fluency_repair", "ptbr_fluency_short_safe", corrected, "short mechanical PT-BR fluency repair candidate"
    if TRAIT_ACCOLADE_RE.search(haystack):
        return "needs_dynamic_short_requirement_trait_accolade_policy", "trait_accolade_residual", "", "residual is coupled to trait/accolade/modifier tokens"
    if SCRIPT_VALUE_RE.search(haystack):
        return "needs_dynamic_short_requirement_script_value_policy", "script_value_residual", "", "residual is coupled to numeric/script value"
    if EFFECT_LIST_RE.search(haystack):
        return "needs_dynamic_short_requirement_effect_list_policy", "effect_list_residual", "", "residual is coupled to effect list or multiline"
    if ARTIFACT_ACTIVITY_RE.search(haystack):
        return "needs_dynamic_short_requirement_artifact_activity_policy", "artifact_activity_residual", "", "residual is coupled to artifact/activity context"
    if TITLE_LAW_RE.search(haystack):
        return "needs_dynamic_short_requirement_title_law_policy", "title_law_residual", "", "residual is coupled to title/law/religion-domain context"
    if SCOPE_GETTER_RE.search(text):
        return "needs_dynamic_short_requirement_scope_getter_policy", "scope_getter_residual", "", "residual is coupled to scope/getter tokens"
    if BAD_ENCODING_RE.search(text) or WORD_QUESTION_RE.search(text) or SPANISH_RE.search(text) or ENGLISH_RE.search(text):
        return "needs_dynamic_short_requirement_semantic_review", "semantic_residual", "", "residual requires semantic review before any repair"
    return "blocked_uncertain", "uncertain_residual", "", "no safe mechanical repair pattern matched"


def decide(row: dict[str, Any], state: dict[str, Any] | None, families: tuple[str, ...] | None) -> dict[str, Any]:
    decision, subpolicy, corrected_text, notes = classify(row, state, families)
    text = str(row.get("current_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": text,
        "source_requirement_tooltip_decision": row.get("requirement_tooltip_decision"),
        "residual_decision": decision,
        "residual_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(text),
        "requires_lifecycle_later": False,
        "requires_apply_later": decision.startswith("dynamic_short_requirement_safe_"),
        "corrected_text": corrected_text,
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_short_label_requirement_residual_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def recommendation(decision_counts: Counter[str]) -> str:
    safe_count = sum(count for decision, count in decision_counts.items() if decision.startswith("dynamic_short_requirement_safe_"))
    if safe_count >= 5:
        return "prepare_separate_protected_apply_for_safe_requirement_residual_repairs"
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_")})
    if needs_counts:
        top_decision, top_count = needs_counts.most_common(1)[0]
        if top_count >= 5:
            return f"register_specific_policy_microagent_for_{top_decision}_do_not_apply_now"
    return "fragmented_migrate_to_script_value_trait_accolade_or_global_diagnostic"


def validate_rows(rows: list[dict[str, Any]], source_count: int) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_requirement_tooltip_decision",
        "residual_decision",
        "residual_subpolicy",
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
        if row["source_requirement_tooltip_decision"] != SOURCE_DECISION:
            raise SystemExit(f"wrong source decision for {segment_id}: {row['source_requirement_tooltip_decision']}")
        if row["residual_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision for {segment_id}: {row['residual_decision']}")
        corrected = row["corrected_text"]
        if row["requires_apply_later"] and not corrected:
            raise SystemExit(f"apply candidate without corrected_text: {segment_id}")
        if corrected:
            if BAD_ENCODING_RE.search(corrected):
                raise SystemExit(f"bad encoding marker in corrected_text: {segment_id}")
            if WORD_QUESTION_RE.search(corrected):
                raise SystemExit(f"question mark inside word in corrected_text: {segment_id}")
            if CK3_TOKEN_RE.findall(row["current_text"]) != CK3_TOKEN_RE.findall(corrected):
                raise SystemExit(f"CK3 token mismatch in corrected_text: {segment_id}")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["residual_decision"] for row in rows)
    subpolicy_counts = Counter(row["residual_subpolicy"] for row in rows)
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    rec = recommendation(decision_counts)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic + short_label requirement residual review",
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
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(db.project_path(args.requirement_jsonl))
    segment_ids = [int(row["segment_id"]) for row in source]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        family_shapes = fetch_family_shapes(conn, args.ledger_run_id, segment_ids)

    reviewed = [decide(row, states.get(int(row["segment_id"])), family_shapes.get(int(row["segment_id"]))) for row in source]
    validate_rows(reviewed, len(source))
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    apply_count = sum(1 for row in reviewed if row["requires_apply_later"])

    print(f"source_rows={len(source)}")
    print(f"total_reviewed={len(reviewed)}")
    print(f"future_apply_candidates={apply_count}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))
    print(f"recommendation={recommendation(decision_counts)}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")


if __name__ == "__main__":
    main()
