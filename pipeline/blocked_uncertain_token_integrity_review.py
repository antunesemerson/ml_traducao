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


SOURCE = "blocked_uncertain_token_integrity_review_v1"
PARENT_DECISION = "needs_blocked_token_integrity_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 54

ALLOWED_DECISIONS = {
    "token_integrity_terminal_guard",
    "token_integrity_terminal_guard_with_domain_guard",
    "token_integrity_terminal_guard_with_semantic_guard",
    "token_integrity_reuse_dynamic_parser_policy",
    "token_integrity_reuse_semantic_review_router",
    "token_integrity_reuse_short_label_style_policy",
    "token_integrity_reuse_gender_local_player_policy",
    "token_integrity_reuse_requirement_effect_router",
    "token_integrity_reuse_not_requirement_effect_router",
    "needs_token_integrity_debug_marker_policy",
    "needs_token_integrity_formatting_tag_policy",
    "needs_token_integrity_bracket_expression_policy",
    "needs_token_integrity_variable_policy",
    "needs_token_integrity_custom_loc_policy",
    "needs_token_integrity_select_cstring_policy",
    "needs_token_integrity_gender_perspective_policy",
    "needs_token_integrity_short_label_policy",
    "needs_token_integrity_semantic_ambiguity_policy",
    "needs_token_integrity_language_residual_policy",
    "needs_token_integrity_dynamic_parser_escape",
    "token_integrity_true_manual_review",
    "token_integrity_insufficient_evidence",
}

TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|#D|#P|#N", re.I)
DEBUG_RE = re.compile(r"#D\b|PLACEHOLDER|DEBUG|TODO|FIXME|WIP", re.I)
FORMATTING_RE = re.compile(r"#[A-Z][A-Za-z0-9_:.{};,|]*|#!|#P|#N", re.I)
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
CUSTOM_LOC_RE = re.compile(r"Custom\(", re.I)
SELECT_CSTRING_RE = re.compile(r"Select_CString", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
GENDER_RE = re.compile(r"\b(herhim|herhisits|sheheit|women|woman|female|male|men|man|gender|dela/dele|ela/ele)\b", re.I)
SHORT_LABEL_RE = re.compile(r"religionname_|_name$|_namepossessive$|_herhim$|_herhisits$|_sheheit$|PLACEHOLDER", re.I)
SEMANTIC_RE = re.compile(r"religion|faith|god|deus|creator|criador|fate|fertility|knowledge|war|water|wealth", re.I)
LANG_RESIDUAL_RE = re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã¯Â¿Â½|ÃƒÂ¢Ã¢â€šÂ¬|\b(?:consiguio|exluir|prazers|sabedora)\b", re.I)


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_blocked_uncertain_token_integrity_review"
    spec = reports_dir / f"{stamp}_blocked_uncertain_token_integrity_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def load_parent_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("record_type") != "sample_review":
                continue
            if record.get("blocked_decision") == PARENT_DECISION:
                rows.append(record)
    if len(rows) != EXPECTED_TOTAL:
        raise SystemExit(f"parent filter mismatch: {len(rows)} expected {EXPECTED_TOTAL}")
    return rows


def fetch_texts(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.confirmed_matches_output,
            src.old_text,
            src.spanish_text,
            src.english_text,
            out.portuguese_text AS output_text,
            (
              SELECT sc.confirmed_text
              FROM segment_confirmations sc
              WHERE sc.segment_id = s.segment_id
              ORDER BY sc.updated_at DESC, sc.id DESC
              LIMIT 1
            ) AS confirmed_text
        FROM segment_state_items s
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments out ON out.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def active_registry(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT agent_key FROM ml_agent_registry WHERE status = 'active'").fetchall()
    return {str(row["agent_key"]) for row in rows}


def blob(record: dict[str, Any], text: dict[str, Any]) -> str:
    return " ".join(
        [
            str(record.get("relative_path") or ""),
            str(record.get("source_key") or ""),
            str(text.get("old_text") or record.get("old_text") or ""),
            str(text.get("spanish_text") or ""),
            str(text.get("english_text") or ""),
            str(text.get("output_text") or record.get("output_text") or ""),
            " ".join(record.get("families_open") or []),
        ]
    )


def localized_blob(record: dict[str, Any], text: dict[str, Any]) -> str:
    return " ".join(
        [
            str(record.get("relative_path") or ""),
            str(record.get("source_key") or ""),
            str(text.get("old_text") or record.get("old_text") or ""),
            str(text.get("spanish_text") or ""),
            str(text.get("output_text") or record.get("output_text") or ""),
            " ".join(record.get("families_open") or []),
        ]
    )


def token_integrity_issue(value: str) -> bool:
    return value.count("[") != value.count("]") or value.count("$") % 2 != 0 or "#D" in value


def marker_groups(record: dict[str, Any], text: dict[str, Any]) -> dict[str, list[str]]:
    full = blob(record, text)
    local = localized_blob(record, text)
    key = str(record.get("source_key") or "")
    old_text = str(text.get("old_text") or record.get("old_text") or "")
    families = set(record.get("families_open") or [])
    return {
        "token_markers": sorted(set(TOKEN_RE.findall(old_text) + (["TokenIntegrityRisk"] if token_integrity_issue(local) else []))),
        "debug_marker_markers": ["DebugMarkerD"] if DEBUG_RE.search(old_text) else [],
        "formatting_tag_markers": sorted(set(FORMATTING_RE.findall(old_text))),
        "bracket_expression_markers": sorted(set(BRACKET_RE.findall(old_text))),
        "variable_markers": sorted(set(VARIABLE_RE.findall(old_text))),
        "custom_loc_markers": ["CustomLoc"] if CUSTOM_LOC_RE.search(full) else [],
        "select_cstring_markers": ["SelectCString"] if SELECT_CSTRING_RE.search(full) else [],
        "gender_perspective_markers": ["GenderPerspective"] if GENDER_RE.search(key + " " + full) else [],
        "short_label_markers": ["ShortLabelSurface"] if SHORT_LABEL_RE.search(key) or "short_label_style_microagent" in families else [],
        "semantic_markers": [
            label
            for label, present in [
                ("SemanticReview", "semantic_review_router" in families),
                ("ReligionSemantic", "religion_semantic_microagent" in families),
                ("ReligionGodName", bool(SEMANTIC_RE.search(key + " " + full))),
            ]
            if present
        ],
        "dynamic_markers": ["DynamicSurface"] if DYNAMIC_RE.search(full) else [],
        "language_residual_markers": ["LanguageResidual"] if LANG_RESIDUAL_RE.search(local) else [],
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for label, present in [
                ("ParentTokenIntegrity", record.get("blocked_decision") == PARENT_DECISION),
                ("RouterBlocked", record.get("primary_route") == "blocked_uncertain"),
                ("FaithDoctrineTerminalsRegistered", "blocked_uncertain_religion_culture_faith_doctrine_faith_name_policy" in active_registry_marker),
            ]
            if present
        ],
    }


active_registry_marker: set[str] = set()


def decide(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> tuple[str, str, str, str, bool, str]:
    if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
        return "token_integrity_true_manual_review", "", "", "manual_review_guard", True, "segment is no longer pending in selected run"
    if int(text.get("needs_output_apply") or 0) != 0 or int(text.get("confirmed_matches_output") or 0) != 1:
        return "token_integrity_true_manual_review", "", "", "manual_review_guard", True, "state guard failed"

    full = blob(record, text)
    local = localized_blob(record, text)
    key = str(record.get("source_key") or "")
    old_text = str(text.get("old_text") or record.get("old_text") or "")
    families = set(record.get("families_open") or [])

    if LANG_RESIDUAL_RE.search(local):
        return "needs_token_integrity_language_residual_policy", "", "", "token_integrity_language_residual_policy", False, "localized text contains residual encoding/language signal"
    if CUSTOM_LOC_RE.search(full):
        return "needs_token_integrity_custom_loc_policy", "", "", "token_integrity_custom_loc_policy", False, "CustomLoc token needs dedicated parser guard"
    if SELECT_CSTRING_RE.search(full):
        return "needs_token_integrity_select_cstring_policy", "", "", "token_integrity_select_cstring_policy", False, "Select_CString token needs dedicated parser guard"
    if BRACKET_RE.search(old_text):
        return "needs_token_integrity_bracket_expression_policy", "", "", "token_integrity_bracket_expression_policy", False, "bracket expression needs token grammar guard"
    if VARIABLE_RE.search(old_text):
        return "needs_token_integrity_variable_policy", "", "", "token_integrity_variable_policy", False, "variable token needs token grammar guard"
    if DEBUG_RE.search(old_text):
        return "needs_token_integrity_debug_marker_policy", "", "", "token_integrity_debug_marker_policy", False, "#D debug marker is the dominant blocked token-integrity sublane"
    if GENDER_RE.search(key + " " + full):
        return "needs_token_integrity_gender_perspective_policy", "", "", "token_integrity_gender_perspective_policy", False, "gender/perspective surface remains inside token block"
    if FORMATTING_RE.search(old_text):
        return "needs_token_integrity_formatting_tag_policy", "", "", "token_integrity_formatting_tag_policy", False, "formatting token needs token grammar guard"
    if SHORT_LABEL_RE.search(key) or "short_label_style_microagent" in families:
        return "needs_token_integrity_short_label_policy", "", "", "token_integrity_short_label_policy", False, "short-label surface remains inside token block"
    if DYNAMIC_RE.search(full):
        return "needs_token_integrity_dynamic_parser_escape", "", "", "token_integrity_dynamic_parser_escape", False, "dynamic expression should escape to parser"
    if "semantic_review_router" in active and "semantic_review_router" in families:
        return "token_integrity_reuse_semantic_review_router", "semantic_review_router", "", "semantic_review_router", False, "generic semantic router can hold remaining semantic ambiguity"
    if record.get("source_key"):
        return "needs_token_integrity_semantic_ambiguity_policy", "", "", "token_integrity_semantic_ambiguity_policy", False, "token evidence remains but no narrower marker matched"
    return "token_integrity_insufficient_evidence", "", "", "manual_review_guard", True, "missing evidence for safe routing"


def make_sample(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> dict[str, Any]:
    decision, registered, catalog, next_component, true_blocked, rationale = decide(record, text, active)
    return {
        "record_type": "sample_review",
        "segment_id": int(record["segment_id"]),
        "relative_path": str(record.get("relative_path") or ""),
        "source_key": str(record.get("source_key") or ""),
        "families_open": record.get("families_open") or [],
        "source_decision": PARENT_DECISION,
        "parent_policy": "blocked_uncertain",
        "primary_route": "blocked_uncertain",
        "old_text": str(text.get("old_text") or record.get("old_text") or ""),
        "confirmed_text": str(text.get("confirmed_text") or text.get("output_text") or record.get("confirmed_text") or ""),
        "output_text": str(text.get("output_text") or record.get("output_text") or ""),
        **marker_groups(record, text),
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "token_integrity_decision": decision,
        "next_component": next_component,
        "is_true_blocked": true_blocked,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def validate_samples(samples: list[dict[str, Any]]) -> None:
    required = {
        "record_type", "segment_id", "relative_path", "source_key", "families_open",
        "source_decision", "parent_policy", "primary_route", "old_text", "confirmed_text",
        "output_text", "token_markers", "debug_marker_markers", "formatting_tag_markers",
        "bracket_expression_markers", "variable_markers", "custom_loc_markers",
        "select_cstring_markers", "gender_perspective_markers", "short_label_markers",
        "semantic_markers", "dynamic_markers", "language_residual_markers",
        "matched_registered_policy", "matched_catalog_spec", "guard_markers",
        "secondary_markers", "token_integrity_decision", "next_component",
        "is_true_blocked", "requires_lifecycle_later", "requires_apply_later",
        "corrected_text", "rationale",
    }
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review count mismatch: {len(samples)} expected {EXPECTED_TOTAL}")
    seen: set[int] = set()
    for row in samples:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if row["source_decision"] != PARENT_DECISION:
            raise SystemExit(f"wrong source decision for {segment_id}: {row['source_decision']}")
        if row["token_integrity_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid token_integrity_decision for {segment_id}: {row['token_integrity_decision']}")
        if row["requires_apply_later"] or row["requires_lifecycle_later"]:
            raise SystemExit(f"future action flag unexpectedly true for {segment_id}")


def choose_next(decision_counts: Counter[str], reuse_count: int, terminal_count: int, true_blocked_count: int) -> tuple[str, str]:
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    if reuse_count >= 18:
        return "register_read_only_splitter", "chat_exec_blocked_uncertain_token_integrity_policy_catalog_registration_prompt.md"
    if terminal_count >= 18:
        return "register_terminal_read_only", "chat_exec_blocked_uncertain_token_integrity_terminal_spec_registration_prompt.md"
    if dominant_decision.startswith("needs_") and dominant_count >= 16:
        slug = dominant_decision.replace("needs_token_integrity_", "").replace("_policy", "")
        return "open_dominant_sublane", f"chat_exec_blocked_uncertain_token_integrity_{slug}_review_prompt.md"
    if true_blocked_count >= 25:
        return "manual_review_guard_queue", "chat_exec_blocked_uncertain_manual_review_guard_prompt.md"
    return "fragmented_final_blocked_diagnostic", "chat_exec_global_remaining_blocked_uncertain_post_terminal_diagnostic_prompt.md"


def write_outputs(args: argparse.Namespace, samples: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["token_integrity_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["token_integrity_decision"].startswith("token_integrity_reuse_"))
    terminal_count = sum(1 for row in samples if row["token_integrity_decision"].startswith("token_integrity_terminal_guard"))
    new_sublane_count = sum(1 for row in samples if row["token_integrity_decision"].startswith("needs_"))
    true_blocked_count = sum(1 for row in samples if row["is_true_blocked"])
    assessment, next_prompt = choose_next(decision_counts, reuse_count, terminal_count, true_blocked_count)
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    marker_fields = [
        "token_markers", "debug_marker_markers", "formatting_tag_markers",
        "bracket_expression_markers", "variable_markers", "custom_loc_markers",
        "select_cstring_markers", "gender_perspective_markers", "short_label_markers",
        "semantic_markers", "dynamic_markers", "language_residual_markers",
        "guard_markers", "secondary_markers",
    ]
    marker_counts = {
        field: Counter(marker for row in samples for marker in row[field]).most_common(20)
        for field in marker_fields
    }
    family_counts = Counter(family for row in samples for family in row["families_open"])
    matched_counts = Counter(
        row["matched_registered_policy"] or row["matched_catalog_spec"]
        for row in samples
        if row["matched_registered_policy"] or row["matched_catalog_spec"]
    )
    summary = {
        "record_type": "summary",
        "source": SOURCE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "parent_policy": "blocked_uncertain",
        "policy_id": "blocked_uncertain_token_integrity_policy",
        "total_reviewed": len(samples),
        "decision_counts": dict(decision_counts),
        "reuse_registered_or_cataloged_count": reuse_count,
        "terminal_guard_count": terminal_count,
        "new_sublane_candidate_count": new_sublane_count,
        "true_blocked_count": true_blocked_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "package_assessment": assessment,
        "next_prompt": next_prompt,
        "should_become_read_only_component": reuse_count >= 18 or terminal_count >= 18,
        "should_register_now": reuse_count >= 18 or terminal_count >= 18,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_blocked_subpolicy_design",
        "parent_policy": "blocked_uncertain",
        "policy_id": "blocked_uncertain_token_integrity_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "entry_conditions": [
            "parent blocked_decision == needs_blocked_token_integrity_policy",
            "segment remains pending in run 400",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
            "token/debug surface in localized text",
        ],
        "reused_registered_policies": [
            {"policy": key, "sampled": value}
            for key, value in matched_counts.items()
            if not key.endswith(".json")
        ],
        "reused_catalog_specs": [
            {"spec": key, "sampled": value}
            for key, value in matched_counts.items()
            if key.endswith(".json")
        ],
        "token_integrity_types": [{"decision": key, "sampled": value} for key, value in decision_counts.most_common()],
        "true_blocked_conditions": [
            {"decision": key, "sampled": value}
            for key, value in decision_counts.most_common()
            if key in {"token_integrity_true_manual_review", "token_integrity_insufficient_evidence"}
        ],
        "resolution_order": [
            "state guard",
            "language residual",
            "CustomLoc and Select_CString",
            "bracket and variable token grammar",
            "#D debug marker",
            "gender perspective",
            "formatting tag",
            "short-label surface",
            "dynamic parser escape",
            "semantic ambiguity/manual guard",
        ],
        "next_components": [next_prompt],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for field, values in marker_counts.items():
            for value, count in values:
                handle.write(json.dumps({"record_type": f"top_{field}", "value": value, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_family", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for policy, count in matched_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_matched_policy_or_spec", "policy": policy, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Blocked uncertain token-integrity review\n\n")
        for key in [
            "total_reviewed", "reuse_registered_or_cataloged_count", "terminal_guard_count",
            "new_sublane_candidate_count", "true_blocked_count", "ready_lifecycle_future",
            "apply_candidates_future", "dominant_subtype", "dominant_count",
            "package_assessment", "next_prompt", "should_become_read_only_component",
            "should_register_now",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nDecision counts\n")
        for key, value in decision_counts.most_common():
            handle.write(f"- {key}: {value}\n")
        handle.write("\nTop families\n")
        for family, count in family_counts.most_common(20):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop markers\n")
        for field, values in marker_counts.items():
            handle.write(f"- {field}: {dict(values)}\n")
        handle.write("\nAnswers\n")
        handle.write(f"- component_read_only_real: {summary['should_become_read_only_component']}\n")
        handle.write("- short_term_lifecycle_apply: false\n")
        handle.write(f"- reuse_registered_or_cataloged_count: {reuse_count}\n")
        handle.write(f"- next_subpolicy_or_prompt: {next_prompt}\n")
        handle.write(f"- register_now: {summary['should_register_now']}\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocked-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=EXPECTED_LEDGER_RUN_ID)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment-state guard failed: {args.segment_state_run_id} expected {EXPECTED_SEGMENT_STATE_RUN_ID}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger guard failed: {args.ledger_run_id} expected {EXPECTED_LEDGER_RUN_ID}")

    parent_rows = load_parent_rows(Path(args.blocked_jsonl))
    segment_ids = [int(row["segment_id"]) for row in parent_rows]
    if len(segment_ids) != len(set(segment_ids)):
        raise SystemExit("duplicate segment ids in parent rows")

    conn = connect_readonly()
    try:
        text_by_id = fetch_texts(conn, segment_ids, args.segment_state_run_id)
        active = active_registry(conn)
        global active_registry_marker
        active_registry_marker = active
        if len(text_by_id) != len(segment_ids):
            missing = sorted(set(segment_ids) - set(text_by_id))
            raise SystemExit(f"missing segment_state rows: {missing[:10]}")
        samples = [make_sample(row, text_by_id[int(row["segment_id"])], active) for row in parent_rows]
    finally:
        conn.close()

    validate_samples(samples)
    txt_path, jsonl_path, spec_path = write_outputs(args, samples)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"spec={spec_path}")


if __name__ == "__main__":
    main()
