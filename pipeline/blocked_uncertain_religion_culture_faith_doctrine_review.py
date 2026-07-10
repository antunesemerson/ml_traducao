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


SOURCE = "blocked_uncertain_religion_culture_faith_doctrine_review_v1"
PARENT_DECISION = "needs_blocked_religion_culture_faith_doctrine_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 39

ALLOWED_DECISIONS = {
    "faith_doctrine_terminal_guard",
    "faith_doctrine_terminal_guard_with_short_label",
    "faith_doctrine_reuse_not_requirement_effect_culture_religion_router",
    "faith_doctrine_reuse_domain_context_religion_holy_site_policy",
    "faith_doctrine_reuse_holy_site_effect_name_policy",
    "faith_doctrine_reuse_semantic_review_router",
    "faith_doctrine_reuse_short_label_style_policy",
    "faith_doctrine_reuse_gender_local_player_policy",
    "needs_faith_doctrine_faith_name_policy",
    "needs_faith_doctrine_doctrine_name_policy",
    "needs_faith_doctrine_doctrine_tenet_policy",
    "needs_faith_doctrine_tenet_policy",
    "needs_faith_doctrine_holy_site_policy",
    "needs_faith_doctrine_culture_religion_bridge_policy",
    "needs_faith_doctrine_short_label_policy",
    "needs_faith_doctrine_token_integrity_policy",
    "needs_faith_doctrine_gender_perspective_policy",
    "needs_faith_doctrine_semantic_ambiguity_policy",
    "needs_faith_doctrine_language_residual_policy",
    "needs_faith_doctrine_dynamic_parser_escape",
    "faith_doctrine_true_manual_review",
    "faith_doctrine_insufficient_evidence",
}

TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|#P|#N", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
FAITH_RE = re.compile(r"faith|religion_desc|_religion_|pagan|christian|buddh|islam|hindu|jewish|acharya|vinaya|iconoclast|cainitism|sabianism|maturidi|muwalladi", re.I)
DOCTRINE_RE = re.compile(r"doctrine|doctrina", re.I)
TENET_RE = re.compile(r"(^|_)tenet_", re.I)
RELIGION_RE = re.compile(r"religion|faith|doctrine|tenet|divine|sacred|spiritual|pagan|islam|buddh|christian|allah|deus", re.I)
CULTURE_RE = re.compile(r"culture|tradition|heritage|ethos|language", re.I)
HOLY_SITE_RE = re.compile(r"holy[_ -]?site|holy site|sant[uo]ari|santuarios locais|lugar santo", re.I)
NAME_TITLE_RE = re.compile(r"title|_name\b|name_|GetName|dynasty|house|realm|theocracy", re.I)
SHORT_LABEL_RE = re.compile(r"doctrine_parameter|faith_conversion_cost|_active$|_inactive$|_cost$|_effect$|_modifier$", re.I)
GENDER_RE = re.compile(r"\b(women|woman|female|male|men|man|mother|father|homosexuality|adultery_men|adultery_women|gender|crianças)\b", re.I)
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
    base = reports_dir / f"{stamp}_blocked_uncertain_religion_culture_faith_doctrine_review"
    spec = reports_dir / f"{stamp}_blocked_uncertain_religion_culture_faith_doctrine_spec.json"
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
            if record.get("religion_culture_decision") == PARENT_DECISION:
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
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ml_agent_registry'").fetchone():
        return set()
    rows = conn.execute("SELECT agent_key FROM ml_agent_registry WHERE status = 'active'").fetchall()
    return {str(row["agent_key"]) for row in rows}


def full_blob(record: dict[str, Any], text: dict[str, Any]) -> str:
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
    blob = full_blob(record, text)
    local = localized_blob(record, text)
    key = str(record.get("source_key") or "")
    families = set(record.get("families_open") or [])
    token_surface = TOKEN_RE.findall(str(text.get("old_text") or record.get("old_text") or ""))
    return {
        "faith_markers": ["FaithNameSurface"] if FAITH_RE.search(key + " " + blob) else [],
        "doctrine_markers": ["DoctrineSurface"] if DOCTRINE_RE.search(key + " " + blob) else [],
        "tenet_markers": ["TenetSurface"] if TENET_RE.search(key) else [],
        "religion_markers": ["ReligionDomain"] if RELIGION_RE.search(blob) else [],
        "culture_markers": ["CultureReligionBridge"] if CULTURE_RE.search(blob) else [],
        "holy_site_markers": ["HolySiteSurface"] if HOLY_SITE_RE.search(blob) else [],
        "name_title_markers": ["NameTitleSurface"] if NAME_TITLE_RE.search(key + " " + blob) else [],
        "short_label_markers": ["ShortLabelSurface"] if SHORT_LABEL_RE.search(key) or "short_label_style_microagent" in families else [],
        "token_markers": sorted(set(token_surface + (["TokenIntegrityRisk"] if token_integrity_issue(local) else []))),
        "gender_perspective_markers": ["GenderPerspective"] if GENDER_RE.search(blob) else [],
        "semantic_markers": [
            label
            for label, present in [
                ("SemanticReview", "semantic_review_router" in families),
                ("ReligionSemantic", "religion_semantic_microagent" in families),
                ("ShortLabel", "short_label_style_microagent" in families),
            ]
            if present
        ],
        "language_residual_markers": ["LanguageResidual"] if LANG_RESIDUAL_RE.search(local) else [],
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for label, present in [
                ("ParentFaithDoctrine", record.get("religion_culture_decision") == PARENT_DECISION),
                ("RouterBlocked", record.get("primary_route") == "blocked_uncertain"),
                ("DynamicSurface", bool(DYNAMIC_RE.search(blob))),
            ]
            if present
        ],
    }


def decide(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> tuple[str, str, str, str, bool, str]:
    if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
        return "faith_doctrine_true_manual_review", "", "", "manual_review_guard", True, "segment is no longer pending in selected run"
    if int(text.get("needs_output_apply") or 0) != 0 or int(text.get("confirmed_matches_output") or 0) != 1:
        return "faith_doctrine_true_manual_review", "", "", "manual_review_guard", True, "state guard failed"

    blob = full_blob(record, text)
    local = localized_blob(record, text)
    key = str(record.get("source_key") or "")
    families = set(record.get("families_open") or [])

    if token_integrity_issue(local):
        return "needs_faith_doctrine_token_integrity_policy", "", "", "faith_doctrine_token_integrity_policy", False, "malformed placeholder/debug token requires token-integrity sublane"
    if LANG_RESIDUAL_RE.search(local):
        return "needs_faith_doctrine_language_residual_policy", "", "", "faith_doctrine_language_residual_policy", False, "localized text contains residual encoding/language signal"
    if GENDER_RE.search(blob):
        return "needs_faith_doctrine_gender_perspective_policy", "", "", "faith_doctrine_gender_perspective_policy", False, "faith/doctrine text carries gender or perspective-specific semantics"
    if HOLY_SITE_RE.search(blob):
        if "domain_context_religion_holy_site_policy" in active:
            return (
                "faith_doctrine_reuse_domain_context_religion_holy_site_policy",
                "domain_context_religion_holy_site_policy",
                "reports/20260623_114642_559414_domain_context_religion_holy_site_policy_spec.json",
                "domain_context_religion_holy_site_policy",
                False,
                "holy-site surface can reuse registered domain-context religion/holy-site splitter",
            )
        return "needs_faith_doctrine_holy_site_policy", "", "", "faith_doctrine_holy_site_policy", False, "holy-site surface lacks confident registered guard"
    if TENET_RE.search(key):
        return "needs_faith_doctrine_tenet_policy", "", "", "faith_doctrine_tenet_policy", False, "tenet-specific key escaped into faith/doctrine branch"
    if key.startswith("doctrine_") or "doctrine_" in key or "doctrine_parameter" in key:
        return "needs_faith_doctrine_doctrine_tenet_policy", "", "", "faith_doctrine_doctrine_tenet_policy", False, "doctrine description/parameter is the dominant faith-doctrine sublane"
    if SHORT_LABEL_RE.search(key) or "short_label_style_microagent" in families:
        return "needs_faith_doctrine_short_label_policy", "", "", "faith_doctrine_short_label_policy", False, "short faith/doctrine UI surface needs a distinct guard"
    if NAME_TITLE_RE.search(key + " " + blob):
        return "needs_faith_doctrine_doctrine_name_policy", "", "", "faith_doctrine_doctrine_name_policy", False, "name/title surface inside doctrine branch needs name guard"
    if FAITH_RE.search(key + " " + blob):
        return "needs_faith_doctrine_faith_name_policy", "", "", "faith_doctrine_faith_name_policy", False, "faith/religion named description is the secondary faith-doctrine sublane"
    if CULTURE_RE.search(blob):
        return "needs_faith_doctrine_culture_religion_bridge_policy", "", "", "faith_doctrine_culture_religion_bridge_policy", False, "culture/religion bridge surface remains inside faith/doctrine branch"
    if DYNAMIC_RE.search(blob):
        return "needs_faith_doctrine_dynamic_parser_escape", "", "", "faith_doctrine_dynamic_parser_escape", False, "dynamic expression should escape to parser"
    if "semantic_review_router" in active and "semantic_review_router" in families:
        return "faith_doctrine_reuse_semantic_review_router", "semantic_review_router", "", "semantic_review_router", False, "generic semantic router can hold remaining semantic ambiguity"
    if record.get("source_key"):
        return "needs_faith_doctrine_semantic_ambiguity_policy", "", "", "faith_doctrine_semantic_ambiguity_policy", False, "faith/doctrine evidence remains but no narrower marker matched"
    return "faith_doctrine_insufficient_evidence", "", "", "manual_review_guard", True, "missing evidence for safe routing"


def make_sample(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> dict[str, Any]:
    decision, registered, catalog, next_component, true_blocked, rationale = decide(record, text, active)
    return {
        "record_type": "sample_review",
        "segment_id": int(record["segment_id"]),
        "relative_path": str(record.get("relative_path") or ""),
        "source_key": str(record.get("source_key") or ""),
        "families_open": record.get("families_open") or [],
        "source_decision": PARENT_DECISION,
        "parent_policy": "blocked_uncertain_religion_culture_policy",
        "primary_route": "blocked_uncertain",
        "old_text": str(text.get("old_text") or record.get("old_text") or ""),
        "confirmed_text": str(text.get("confirmed_text") or text.get("output_text") or record.get("confirmed_text") or ""),
        "output_text": str(text.get("output_text") or record.get("output_text") or ""),
        **marker_groups(record, text),
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "faith_doctrine_decision": decision,
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
        "output_text", "faith_markers", "doctrine_markers", "tenet_markers",
        "religion_markers", "culture_markers", "holy_site_markers", "name_title_markers",
        "short_label_markers", "token_markers", "gender_perspective_markers",
        "semantic_markers", "language_residual_markers", "matched_registered_policy",
        "matched_catalog_spec", "guard_markers", "secondary_markers",
        "faith_doctrine_decision", "next_component", "is_true_blocked",
        "requires_lifecycle_later", "requires_apply_later", "corrected_text", "rationale",
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
        if row["faith_doctrine_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid faith_doctrine_decision for {segment_id}: {row['faith_doctrine_decision']}")
        if row["requires_apply_later"] or row["requires_lifecycle_later"]:
            raise SystemExit(f"future action flag unexpectedly true for {segment_id}")


def choose_next(decision_counts: Counter[str], reuse_count: int, terminal_count: int, true_blocked_count: int) -> tuple[str, str]:
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    if reuse_count >= 15:
        return "register_read_only_splitter", "chat_exec_blocked_uncertain_religion_culture_faith_doctrine_policy_catalog_registration_prompt.md"
    if terminal_count >= 15:
        return "register_terminal_read_only", "chat_exec_blocked_uncertain_religion_culture_faith_doctrine_terminal_spec_registration_prompt.md"
    if dominant_decision.startswith("needs_") and dominant_count >= 12:
        slug = dominant_decision.replace("needs_faith_doctrine_", "").replace("_policy", "")
        return "open_dominant_sublane", f"chat_exec_blocked_uncertain_religion_culture_faith_doctrine_{slug}_review_prompt.md"
    if true_blocked_count >= 20:
        return "manual_review_guard_queue", "chat_exec_blocked_uncertain_manual_review_guard_prompt.md"
    return "fragmented_return_to_token_integrity", "chat_exec_blocked_uncertain_token_integrity_review_prompt.md"


def write_outputs(args: argparse.Namespace, samples: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["faith_doctrine_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["faith_doctrine_decision"].startswith("faith_doctrine_reuse_"))
    terminal_count = sum(1 for row in samples if row["faith_doctrine_decision"].startswith("faith_doctrine_terminal_guard"))
    new_sublane_count = sum(1 for row in samples if row["faith_doctrine_decision"].startswith("needs_"))
    true_blocked_count = sum(1 for row in samples if row["is_true_blocked"])
    assessment, next_prompt = choose_next(decision_counts, reuse_count, terminal_count, true_blocked_count)
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    marker_fields = [
        "faith_markers", "doctrine_markers", "tenet_markers", "religion_markers",
        "culture_markers", "holy_site_markers", "name_title_markers", "short_label_markers",
        "token_markers", "gender_perspective_markers", "semantic_markers",
        "language_residual_markers", "guard_markers", "secondary_markers",
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
        "parent_policy": "blocked_uncertain_religion_culture_policy",
        "policy_id": "blocked_uncertain_religion_culture_faith_doctrine_policy",
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
        "should_become_read_only_component": reuse_count >= 15 or terminal_count >= 15,
        "should_register_now": reuse_count >= 15 or terminal_count >= 15,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_blocked_subpolicy_design",
        "parent_policy": "blocked_uncertain_religion_culture_policy",
        "policy_id": "blocked_uncertain_religion_culture_faith_doctrine_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "entry_conditions": [
            "parent faith_doctrine branch == needs_blocked_religion_culture_faith_doctrine_policy",
            "segment remains pending in run 400",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
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
        "faith_doctrine_types": [{"decision": key, "sampled": value} for key, value in decision_counts.most_common()],
        "true_blocked_conditions": [
            {"decision": key, "sampled": value}
            for key, value in decision_counts.most_common()
            if key in {"faith_doctrine_true_manual_review", "faith_doctrine_insufficient_evidence"}
        ],
        "resolution_order": [
            "state guard",
            "token integrity and language residual",
            "gender perspective",
            "holy site reuse/sublane",
            "tenet escape",
            "doctrine/parameter surfaces",
            "short-label surfaces",
            "faith/religion named descriptions",
            "culture/religion bridge",
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
        handle.write("Blocked uncertain religion/culture faith-doctrine review\n\n")
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
    parser.add_argument("--religion-culture-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, default=EXPECTED_LEDGER_RUN_ID)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment-state guard failed: {args.segment_state_run_id} expected {EXPECTED_SEGMENT_STATE_RUN_ID}")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit(f"ledger guard failed: {args.ledger_run_id} expected {EXPECTED_LEDGER_RUN_ID}")

    parent_rows = load_parent_rows(Path(args.religion_culture_jsonl))
    segment_ids = [int(row["segment_id"]) for row in parent_rows]
    if len(segment_ids) != len(set(segment_ids)):
        raise SystemExit("duplicate segment ids in parent rows")

    conn = connect_readonly()
    try:
        text_by_id = fetch_texts(conn, segment_ids, args.segment_state_run_id)
        active = active_registry(conn)
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
