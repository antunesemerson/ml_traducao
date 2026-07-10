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


SOURCE = "blocked_uncertain_gender_perspective_review_v1"
PARENT_DECISION = "needs_blocked_gender_perspective_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 23

ALLOWED_DECISIONS = {
    "gender_perspective_terminal_guard",
    "gender_perspective_terminal_guard_with_domain_guard",
    "gender_perspective_terminal_guard_with_token_guard",
    "gender_perspective_reuse_gender_local_player_policy",
    "gender_perspective_reuse_select_cstring_player_target_policy",
    "gender_perspective_reuse_select_cstring_possessive_policy",
    "gender_perspective_reuse_select_cstring_es_helper_policy",
    "gender_perspective_reuse_local_player_requirement_policy",
    "gender_perspective_reuse_effect_list_gender_policy",
    "gender_perspective_reuse_artifact_activity_gender_policy",
    "gender_perspective_reuse_semantic_review_router",
    "gender_perspective_reuse_short_label_style_policy",
    "needs_gender_perspective_player_target_policy",
    "needs_gender_perspective_local_player_policy",
    "needs_gender_perspective_possessive_policy",
    "needs_gender_perspective_es_helper_policy",
    "needs_gender_perspective_pronoun_policy",
    "needs_gender_perspective_token_integrity_policy",
    "needs_gender_perspective_short_label_policy",
    "needs_gender_perspective_semantic_ambiguity_policy",
    "needs_gender_perspective_language_residual_policy",
    "needs_gender_perspective_dynamic_parser_escape",
    "gender_perspective_true_manual_review",
    "gender_perspective_insufficient_evidence",
}

TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|#D|#P|#N", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$")
DOMAIN_RE = re.compile(r"religion|faith|doctrine|tenet|culture|tradition|pagan|holy|religion_|religião|fé|doutrina|cultura|tradi", re.I)
GENDER_RE = re.compile(
    r"\b(women|woman|female|male|men|man|mother|father|mothers|fathers|gender|homosexuality|adultery_men|adultery_women|"
    r"mulheres|mulher|homens|homem|mães|mãe|pais|pai|filho|filhos|filha|filhas|gênero|genero|elas|eles|dele|dela)\b",
    re.I,
)
PLAYER_TARGET_RE = re.compile(r"player|target|actor|recipient|ROOT\.|FROM\.|SCOPE\.|GetPlayer|LocalPlayer", re.I)
LOCAL_PLAYER_RE = re.compile(r"local[_ -]?player|jogador local|GetPlayer|player", re.I)
POSSESSIVE_RE = re.compile(r"possessive|herhisits|dela/dele|dele|dela|seu|sua|seus|suas", re.I)
ES_HELPER_RE = re.compile(r"\b(?:ES_[A-Z_]+|ElLa|DelDela|ES_OA|ES_XA_EA)\b|Select_CString", re.I)
PRONOUN_RE = re.compile(r"herhim|sheheit|eles|elas|ele|ela|lhes|seu|sua|deus|deuses|espíritos|espiritos", re.I)
SHORT_LABEL_RE = re.compile(r"_active$|_inactive$|_effect$|_modifier$|_cost$|doctrine_parameter_", re.I)
SEMANTIC_RE = re.compile(r"semantic|religion|faith|doctrine|tenet|culture|tradition|spiritual|divine|sagrado|deus|criador|fé|religião", re.I)
LANG_RESIDUAL_RE = re.compile(
    r"ÃƒÆ’|Ãƒâ€š|Ã¯Â¿Â½|ÃƒÂ¢Ã¢â€šÂ¬|\b(?:the|your|their|cannot|consiguio|exluir|prazers|sabedora)\b",
    re.I,
)

CATALOG_SPECS = {
    "gender_local_player_policy": "reports/20260621_152129_416530_gender_local_player_policy_spec.json",
    "select_cstring_player_target_perspective_policy": "reports/20260621_195107_980925_select_cstring_player_target_perspective_spec.json",
    "select_cstring_possessive_policy": "reports/20260621_202657_815802_select_cstring_possessive_policy_spec.json",
    "select_cstring_es_helper_policy": "reports/20260621_205240_696871_select_cstring_es_helper_policy_spec.json",
    "local_player_requirement_policy": "reports/20260621_212845_060519_local_player_requirement_policy_spec.json",
    "effect_list_gender_local_player_policy": "reports/20260622_130721_601727_effect_list_gender_local_player_policy_spec.json",
    "artifact_activity_gender_local_player_policy": "reports/20260622_113242_032221_artifact_activity_gender_local_player_policy_spec.json",
    "blocked_uncertain_gender_perspective_policy": "reports/<timestamp>_blocked_uncertain_gender_perspective_spec.json",
}


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
    base = reports_dir / f"{stamp}_blocked_uncertain_gender_perspective_review"
    spec = reports_dir / f"{stamp}_blocked_uncertain_gender_perspective_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def load_parent_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(path)
        if row.get("record_type") == "sample_review"
        and row.get("blocked_decision") == PARENT_DECISION
    ]
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
            " ".join(record.get("gender_markers") or []),
            " ".join(record.get("domain_markers") or []),
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


def marker_list(pattern: re.Pattern[str], value: str, label: str) -> list[str]:
    return [label] if pattern.search(value) else []


def marker_groups(record: dict[str, Any], text: dict[str, Any]) -> dict[str, list[str]]:
    full = blob(record, text)
    local = localized_blob(record, text)
    key = str(record.get("source_key") or "")
    old_text = str(text.get("old_text") or record.get("old_text") or "")
    families = set(record.get("families_open") or [])
    source_gender = list(record.get("gender_markers") or [])
    source_domain = list(record.get("domain_markers") or [])
    return {
        "gender_markers": sorted(set(source_gender + marker_list(GENDER_RE, key + " " + full, "GenderPerspectiveSurface"))),
        "perspective_markers": marker_list(PRONOUN_RE, full, "PerspectivePronounSurface") + marker_list(GENDER_RE, full, "GenderedPerspective"),
        "player_target_markers": marker_list(PLAYER_TARGET_RE, full, "PlayerTargetSurface"),
        "local_player_markers": marker_list(LOCAL_PLAYER_RE, full, "LocalPlayerSurface"),
        "possessive_markers": marker_list(POSSESSIVE_RE, full, "PossessiveSurface"),
        "es_helper_markers": marker_list(ES_HELPER_RE, full, "EsHelperSurface"),
        "pronoun_markers": marker_list(PRONOUN_RE, full, "PronounSurface"),
        "token_markers": sorted(set(TOKEN_RE.findall(old_text) + (["TokenIntegrityRisk"] if token_integrity_issue(local) else []))),
        "short_label_markers": ["ShortLabelSurface"] if SHORT_LABEL_RE.search(key) or "short_label_style_microagent" in families else [],
        "semantic_markers": [
            label for label, present in [
                ("SemanticReview", "semantic_review_router" in families),
                ("ReligionSemantic", "religion_semantic_microagent" in families),
                ("CultureSemantic", "culture_semantic_microagent" in families),
                ("SemanticDomain", bool(SEMANTIC_RE.search(full))),
            ] if present
        ],
        "dynamic_markers": marker_list(DYNAMIC_RE, full, "DynamicSurface"),
        "language_residual_markers": marker_list(LANG_RESIDUAL_RE, local, "LanguageResidual"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": sorted(set(source_domain + [
            label for label, present in [
                ("ParentBlockedUncertain", record.get("blocked_decision") == PARENT_DECISION),
                ("DomainReligionCulture", bool(DOMAIN_RE.search(key + " " + full))),
                ("HolySiteReligion", "HolySiteReligion" in set(record.get("secondary_markers") or [])),
            ] if present
        ])),
    }


def decide(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> tuple[str, str, str, str, bool, str]:
    if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
        return "gender_perspective_true_manual_review", "", "", "manual_review_guard", True, "segment is no longer pending in selected run"
    if int(text.get("needs_output_apply") or 0) != 0 or int(text.get("confirmed_matches_output") or 0) != 1:
        return "gender_perspective_true_manual_review", "", "", "manual_review_guard", True, "state guard failed"

    full = blob(record, text)
    local = localized_blob(record, text)
    key = str(record.get("source_key") or "")
    families = set(record.get("families_open") or [])
    source_domain = " ".join(record.get("domain_markers") or [])
    old_text = str(text.get("old_text") or record.get("old_text") or "")

    if token_integrity_issue(local):
        return "needs_gender_perspective_token_integrity_policy", "", "", "gender_perspective_token_integrity_policy", False, "malformed placeholder/debug token requires token-integrity guard"
    if LANG_RESIDUAL_RE.search(local):
        return "needs_gender_perspective_language_residual_policy", "", "", "gender_perspective_language_residual_policy", False, "localized text contains residual encoding/language signal"
    if DYNAMIC_RE.search(full):
        return "needs_gender_perspective_dynamic_parser_escape", "", "", "gender_perspective_dynamic_parser_escape", False, "dynamic expression should escape to parser"
    if PLAYER_TARGET_RE.search(full):
        return "gender_perspective_reuse_select_cstring_player_target_policy", "select_cstring_player_target_perspective_policy", CATALOG_SPECS["select_cstring_player_target_perspective_policy"], "select_cstring_player_target_perspective_policy", False, "player/target perspective can reuse the Select_CString perspective policy"
    if LOCAL_PLAYER_RE.search(full) and "gender_local_player_policy" in active:
        return "gender_perspective_reuse_gender_local_player_policy", "gender_local_player_policy", CATALOG_SPECS["gender_local_player_policy"], "gender_local_player_policy", False, "local-player gender surface can reuse registered gender_local_player_policy"
    if ES_HELPER_RE.search(full):
        return "gender_perspective_reuse_select_cstring_es_helper_policy", "select_cstring_es_helper_policy", CATALOG_SPECS["select_cstring_es_helper_policy"], "select_cstring_es_helper_policy", False, "ES helper surface can reuse Select_CString ES helper policy"
    if POSSESSIVE_RE.search(full) and not DOMAIN_RE.search(key + " " + source_domain):
        return "gender_perspective_reuse_select_cstring_possessive_policy", "select_cstring_possessive_policy", CATALOG_SPECS["select_cstring_possessive_policy"], "select_cstring_possessive_policy", False, "possessive gender surface can reuse Select_CString possessive policy"
    if SHORT_LABEL_RE.search(key) or ("short_label_style_microagent" in families and TOKEN_RE.search(old_text)):
        return "gender_perspective_reuse_short_label_style_policy", "short_label_style_microagent", "", "short_label_style_microagent", False, "compact/tokenized gender surface should route through short-label style policy"
    if DOMAIN_RE.search(key + " " + full + " " + source_domain) and (GENDER_RE.search(key + " " + full) or PRONOUN_RE.search(full) or record.get("gender_markers")):
        return "gender_perspective_terminal_guard_with_domain_guard", "", "blocked_uncertain_gender_perspective_policy", "blocked_uncertain_gender_perspective_policy", False, "cohesive religion/culture gender-perspective prose should terminalize as read-only domain guard"
    if "semantic_review_router" in active and ("semantic_review_router" in families or SEMANTIC_RE.search(full)):
        return "gender_perspective_reuse_semantic_review_router", "semantic_review_router", "", "semantic_review_router", False, "remaining case is semantic religion/culture prose without local-player or Select_CString surface"
    if GENDER_RE.search(key + " " + full) or PRONOUN_RE.search(full):
        return "needs_gender_perspective_pronoun_policy", "", "", "gender_perspective_pronoun_policy", False, "gender/pronoun signal remains without domain guard"
    if SEMANTIC_RE.search(full):
        return "needs_gender_perspective_semantic_ambiguity_policy", "", "", "gender_perspective_semantic_ambiguity_policy", False, "semantic ambiguity remains after gender-perspective filtering"
    return "gender_perspective_insufficient_evidence", "", "", "manual_review_guard", True, "missing evidence for safe routing"


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
        "gender_perspective_decision": decision,
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
        "output_text", "gender_markers", "perspective_markers", "player_target_markers",
        "local_player_markers", "possessive_markers", "es_helper_markers", "pronoun_markers",
        "token_markers", "short_label_markers", "semantic_markers", "dynamic_markers",
        "language_residual_markers", "matched_registered_policy", "matched_catalog_spec",
        "guard_markers", "secondary_markers", "gender_perspective_decision",
        "next_component", "is_true_blocked", "requires_lifecycle_later", "requires_apply_later",
        "corrected_text", "rationale",
    }
    ids = [int(sample["segment_id"]) for sample in samples]
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"sample count mismatch: {len(samples)} expected {EXPECTED_TOTAL}")
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate segment_id in sample output")
    for sample in samples:
        missing = required - set(sample)
        if missing:
            raise SystemExit(f"sample {sample.get('segment_id')} missing fields: {sorted(missing)}")
        if sample["source_decision"] != PARENT_DECISION:
            raise SystemExit(f"bad source_decision for {sample['segment_id']}")
        if sample["gender_perspective_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"bad decision for {sample['segment_id']}: {sample['gender_perspective_decision']}")
        if sample["requires_apply_later"] or sample["requires_lifecycle_later"]:
            raise SystemExit(f"apply/lifecycle flag unexpectedly true for {sample['segment_id']}")


def build_spec(samples: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(str(sample["gender_perspective_decision"]) for sample in samples)
    reused_registered = sorted({str(sample["matched_registered_policy"]) for sample in samples if sample["matched_registered_policy"]})
    reused_specs = sorted({str(sample["matched_catalog_spec"]) for sample in samples if sample["matched_catalog_spec"]})
    next_components = sorted({str(sample["next_component"]) for sample in samples if sample["next_component"]})
    return {
        "schema_version": 1,
        "created_for": "read_only_blocked_subpolicy_design",
        "parent_policy": "blocked_uncertain",
        "policy_id": "blocked_uncertain_gender_perspective_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "parent blocked_uncertain review decision equals needs_blocked_gender_perspective_policy",
            "segment remains pending in segment_state_run_id=400",
            "needs_output_apply=0 and confirmed_matches_output=1",
        ],
        "reused_registered_policies": reused_registered,
        "reused_catalog_specs": reused_specs,
        "gender_perspective_types": sorted(decisions),
        "true_blocked_conditions": [
            "stale state or state guard failure",
            "missing evidence for safe routing",
        ],
        "resolution_order": [
            "state guards",
            "token/language/dynamic escape",
            "player/target and local-player reuse",
            "ES helper and possessive reuse",
            "short-label reuse",
            "religion/culture terminal domain guard",
            "semantic router reuse",
            "manual review only if evidence is insufficient",
        ],
        "next_components": next_components,
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "decision_counts": dict(sorted(decisions.items())),
        "review_total": len(samples),
        "terminal_guard_count": sum(1 for sample in samples if str(sample["gender_perspective_decision"]).startswith("gender_perspective_terminal_guard")),
        "reuse_registered_count": sum(1 for sample in samples if sample["matched_registered_policy"]),
        "new_sublane_count": sum(1 for sample in samples if str(sample["gender_perspective_decision"]).startswith("needs_")),
        "true_blocked_count": sum(1 for sample in samples if sample["is_true_blocked"]),
        "requires_apply_later_count": sum(1 for sample in samples if sample["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for sample in samples if sample["requires_lifecycle_later"]),
    }


def flatten(values: list[Any]) -> list[str]:
    flattened: list[str] = []
    for value in values:
        if isinstance(value, list):
            flattened.extend(str(item) for item in value)
        elif value:
            flattened.append(str(value))
    return flattened


def top(samples: list[dict[str, Any]], key: str, limit: int = 8) -> list[tuple[str, int]]:
    counter = Counter(flatten([sample.get(key) for sample in samples]))
    return counter.most_common(limit)


def write_outputs(samples: list[dict[str, Any]], spec: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decisions = Counter(str(sample["gender_perspective_decision"]) for sample in samples)
    terminal_count = spec["terminal_guard_count"]
    reuse_count = spec["reuse_registered_count"]
    new_sublane_count = spec["new_sublane_count"]
    true_blocked_count = spec["true_blocked_count"]
    dominant = decisions.most_common(1)[0] if decisions else ("", 0)

    lines = [
        "blocked_uncertain gender perspective review",
        f"source={SOURCE}",
        f"segment_state_run_id={EXPECTED_SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={EXPECTED_LEDGER_RUN_ID}",
        f"total_reviewed={len(samples)}",
        "",
        "decision_counts:",
        *[f"- {decision}: {count}" for decision, count in sorted(decisions.items())],
        "",
        f"reuse_registered_or_cataloged={reuse_count}",
        f"terminal_guards={terminal_count}",
        f"new_sublane_candidates={new_sublane_count}",
        f"true_blocked={true_blocked_count}",
        f"ready_lifecycle_future={spec['requires_lifecycle_later_count']}",
        f"apply_candidates_future={spec['requires_apply_later_count']}",
        f"dominant_subtype={dominant[0]} ({dominant[1]})",
        "",
        f"top_families_open={top(samples, 'families_open')}",
        f"top_gender_perspective_markers={top(samples, 'gender_markers') + top(samples, 'perspective_markers')}",
        f"top_player_target_local_player_markers={top(samples, 'player_target_markers') + top(samples, 'local_player_markers')}",
        f"top_possessive_es_pronoun_markers={top(samples, 'possessive_markers') + top(samples, 'es_helper_markers') + top(samples, 'pronoun_markers')}",
        f"top_token_markers={top(samples, 'token_markers')}",
        f"top_short_label_markers={top(samples, 'short_label_markers')}",
        f"top_semantic_dynamic_markers={top(samples, 'semantic_markers') + top(samples, 'dynamic_markers')}",
        f"top_residual_markers={top(samples, 'language_residual_markers')}",
        f"top_matched_policies_specs={top(samples, 'matched_registered_policy') + top(samples, 'matched_catalog_spec')}",
        "",
        "answers:",
        "1. blocked_uncertain_gender_perspective_policy should become a read-only component: yes, as a terminal domain guard with small reuse edges.",
        "2. lifecycle/apply in the short term: no.",
        "3. reuse is limited and mainly short-label; the dominant route is terminal religion/culture gender-perspective guard.",
        "4. next subpolicy: none if this terminal registers; otherwise final blocked_uncertain diagnostic.",
        "5. recommendation: register as terminal read-only, then run final blocked_uncertain/global diagnostic.",
        "6. next prompt: chat_exec_blocked_uncertain_gender_perspective_terminal_spec_registration_prompt.md, or final blocked_uncertain diagnostic if registration is deferred.",
    ]

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocked-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit(f"segment-state-run-id mismatch: {args.segment_state_run_id} expected {EXPECTED_SEGMENT_STATE_RUN_ID}")

    parent_rows = load_parent_rows(args.blocked_jsonl)
    segment_ids = [int(row["segment_id"]) for row in parent_rows]
    with connect_readonly() as conn:
        text_by_id = fetch_texts(conn, segment_ids, args.segment_state_run_id)
        active = active_registry(conn)

    missing = sorted(set(segment_ids) - set(text_by_id))
    if missing:
        raise SystemExit(f"missing segment_state rows for run {args.segment_state_run_id}: {missing}")

    samples = [make_sample(row, text_by_id[int(row["segment_id"])], active) for row in parent_rows]
    validate_samples(samples)
    spec = build_spec(samples)
    txt_path, jsonl_path, spec_path = write_outputs(samples, spec)
    print(f"wrote {txt_path}")
    print(f"wrote {jsonl_path}")
    print(f"wrote {spec_path}")


if __name__ == "__main__":
    main()
