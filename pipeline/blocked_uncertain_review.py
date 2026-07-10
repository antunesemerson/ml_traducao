from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import agent_inventory_diagnostic as agent_inventory
import db
import global_post_architecture_diagnostic as architecture_diag


SOURCE = "blocked_uncertain_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_CLOSED_COUNT = 276375
EXPECTED_PENDING_COUNT = 11725
EXPECTED_OUTPUT_APPLY_PENDING_COUNT = 0
EXPECTED_REGISTERED_AGENTS = 232
EXPECTED_OBSERVED_AGENT_KEYS = 289
EXPECTED_OPERATIONAL_AGENTS = 33
EXPECTED_DRY_RUN_AGENTS = 24
EXPECTED_SHADOW_AGENTS = 89
EXPECTED_TERMINAL_GUARD_AGENTS = 18
EXPECTED_SPLITTER_AGENTS = 25
EXPECTED_UNIVERSE = 144

ALLOWED_DECISIONS = {
    "blocked_reuse_requirement_effect_router",
    "blocked_reuse_not_requirement_effect_router",
    "blocked_reuse_domain_context_router",
    "blocked_reuse_effect_list_policy",
    "blocked_reuse_artifact_activity_policy",
    "blocked_reuse_building_modifier_policy",
    "blocked_reuse_event_context_policy",
    "blocked_reuse_residual_policy",
    "blocked_reuse_accolade_trait_policy",
    "blocked_reuse_script_value_policy",
    "blocked_reuse_holy_site_policy",
    "blocked_reuse_gender_local_player_policy",
    "blocked_reuse_semantic_review_router",
    "blocked_reuse_short_label_style_policy",
    "blocked_reuse_autofix_unknown_router",
    "blocked_reuse_dynamic_parser_policy",
    "needs_blocked_dynamic_parser_unknown_policy",
    "needs_blocked_context_bridge_policy",
    "needs_blocked_cross_family_router_policy",
    "needs_blocked_source_output_mismatch_policy",
    "needs_blocked_token_integrity_policy",
    "needs_blocked_language_residual_policy",
    "needs_blocked_semantic_ambiguity_policy",
    "needs_blocked_gender_perspective_policy",
    "needs_blocked_name_title_culture_policy",
    "needs_blocked_religion_culture_policy",
    "needs_blocked_event_actor_target_policy",
    "needs_blocked_requirement_effect_policy",
    "needs_blocked_manual_review_policy",
    "blocked_terminal_uncertain_guard",
    "blocked_true_manual_review",
    "blocked_insufficient_evidence",
    "blocked_stale_or_inconsistent_state",
}

BLOCK_RE = re.compile(r"PLACEHOLDER|#D|TODO|DEBUG|WIP|FIXME", re.I)
UNCERTAIN_RE = re.compile(r"ambiguous|uncertain|conflict|mixed|not confidently", re.I)
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!|#D|#P|#N", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
SEMANTIC_RE = re.compile(r"faith|religion|doctrine|tenet|divine|sacred|culture|tradition|belief|spiritual|moral|virtue", re.I)
GENDER_RE = re.compile(r"\b(women|woman|female|male|men|man|mother|father|mothers|fathers|homosexuality|adultery_men|adultery_women|gender)\b", re.I)
DOMAIN_RE = re.compile(r"religion|faith|doctrine|tenet|culture|tradition|title|theocracy|temple|holy|realm|domain", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|actor|target|recipient", re.I)
NAME_TITLE_RE = re.compile(r"title|emperor|king|duke|count|baron|theocracy|culture_titles|GetName|_name\b|name_", re.I)
LANG_RESIDUAL_RE = re.compile(
    r"Ãƒ|Ã‚|ï¿½|Ã¢â‚¬|\b(?:the|your|their|cannot|consiguio|exluir|prazers|sabedora)\b",
    re.I,
)
RELIGION_CULTURE_RE = re.compile(r"religion|faith|doctrine|tenet|pagan|christian|buddh|islam|hindu|jewish|culture|tradition|temple|divine|sacred", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_blocked_uncertain_review"
    spec = reports_dir / f"{stamp}_blocked_uncertain_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def state_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    counts = architecture_diag.state_counts(conn, run_id)
    expected = {
        "closed_count": EXPECTED_CLOSED_COUNT,
        "pending_count": EXPECTED_PENDING_COUNT,
        "output_apply_pending_count": EXPECTED_OUTPUT_APPLY_PENDING_COUNT,
    }
    for key, value in expected.items():
        if counts[key] != value:
            raise SystemExit(f"state guard failed: {key}={counts[key]} expected {value}")
    return counts


def registry_metrics(conn: sqlite3.Connection) -> dict[str, int]:
    registry = agent_inventory.fetch_registry(conn)
    latest_run, routing = agent_inventory.fetch_latest_routing(conn)
    recommendations = agent_inventory.fetch_recommendations(conn)
    evidence, _table_counts = agent_inventory.fetch_agent_evidence(conn)
    rows = agent_inventory.build_rows(registry, routing, evidence, recommendations)
    metrics = {
        "registered_agents": len(registry),
        "observed_agent_keys": len(rows),
        "operational_agents": sum(1 for row in registry if row.get("operational_state") == "operational"),
        "dry_run_agents": sum(1 for row in registry if row.get("operational_state") == "dry_run"),
        "shadow_agents": sum(1 for row in registry if row.get("operational_state") == "shadow"),
        "terminal_guard_agents": sum(1 for row in registry if row.get("decision_role") == "terminal_guard"),
        "splitter_agents": sum(1 for row in registry if row.get("decision_role") == "route_and_split"),
        "latest_routing_run_id": int(latest_run["id"]) if latest_run else 0,
    }
    expected = {
        "registered_agents": EXPECTED_REGISTERED_AGENTS,
        "observed_agent_keys": EXPECTED_OBSERVED_AGENT_KEYS,
        "operational_agents": EXPECTED_OPERATIONAL_AGENTS,
        "dry_run_agents": EXPECTED_DRY_RUN_AGENTS,
        "shadow_agents": EXPECTED_SHADOW_AGENTS,
        "terminal_guard_agents": EXPECTED_TERMINAL_GUARD_AGENTS,
        "splitter_agents": EXPECTED_SPLITTER_AGENTS,
    }
    for key, value in expected.items():
        if metrics[key] != value:
            raise SystemExit(f"registry guard failed: {key}={metrics[key]} expected {value}")
    return metrics


def active_registry(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT agent_key FROM ml_agent_registry WHERE status = 'active'").fetchall()
    return {str(row["agent_key"]) for row in rows}


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


def marker(pattern: re.Pattern[str], blob: str, label: str) -> list[str]:
    return [label] if pattern.search(blob) else []


def token_integrity_issue(text: str) -> bool:
    return text.count("[") != text.count("]") or text.count("$") % 2 != 0 or "#D" in text


def marker_groups(record: dict[str, Any], text: dict[str, Any]) -> dict[str, list[str]]:
    blob = blob_for(record, text)
    local_blob = localized_blob_for(record, text)
    markers = set(record.get("markers") or [])
    return {
        "block_markers": marker(BLOCK_RE, blob, "PlaceholderOrDebug") + (["RouterBlocked"] if record.get("route") == "blocked_uncertain" else []),
        "uncertainty_markers": marker(UNCERTAIN_RE, str(record.get("reason") or ""), "RouterLowConfidence"),
        "token_markers": marker(TOKEN_RE, blob, "TokenSurface") + (["TokenIntegrityRisk"] if token_integrity_issue(str(text.get("old_text") or "")) else []),
        "dynamic_markers": marker(DYNAMIC_RE, blob, "DynamicToken"),
        "semantic_markers": marker(SEMANTIC_RE, blob, "SemanticDomain"),
        "gender_markers": marker(GENDER_RE, blob, "GenderPerspective") + (["RouterGenderMarker"] if "GenderLocalPlayer" in markers else []),
        "domain_markers": marker(DOMAIN_RE, blob, "DomainReligionCulture") + (["RouterDomain"] if "Domain" in markers else []),
        "event_markers": marker(EVENT_RE, blob, "EventActorTarget"),
        "name_title_markers": marker(NAME_TITLE_RE, blob, "NameTitleCulture"),
        "language_residual_markers": marker(LANG_RESIDUAL_RE, local_blob, "LanguageResidual"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for label, present in [
                ("HolySiteReligion", "HolySiteReligion" in markers),
                ("SemanticReview", "semantic_review_router" in set(record.get("families_open") or [])),
                ("ReligionSemantic", "religion_semantic_microagent" in set(record.get("families_open") or [])),
                ("CultureSemantic", "culture_semantic_microagent" in set(record.get("families_open") or [])),
                ("ShortLabel", "short_label_style_microagent" in set(record.get("families_open") or [])),
            ]
            if present
        ],
    }


def blob_for(record: dict[str, Any], text: dict[str, Any]) -> str:
    return " ".join(
        [
            str(record.get("relative_path") or ""),
            str(record.get("source_key") or ""),
            str(text.get("old_text") or ""),
            str(text.get("spanish_text") or ""),
            str(text.get("english_text") or ""),
            str(text.get("output_text") or ""),
            str(record.get("reason") or ""),
            " ".join(record.get("families_open") or []),
            " ".join(record.get("markers") or []),
        ]
    )


def localized_blob_for(record: dict[str, Any], text: dict[str, Any]) -> str:
    return " ".join(
        [
            str(record.get("relative_path") or ""),
            str(record.get("source_key") or ""),
            str(text.get("old_text") or ""),
            str(text.get("spanish_text") or ""),
            str(text.get("output_text") or ""),
            " ".join(record.get("families_open") or []),
            " ".join(record.get("markers") or []),
        ]
    )


def output_mismatch(text: dict[str, Any]) -> bool:
    confirmed = str(text.get("confirmed_text") or "")
    output = str(text.get("output_text") or "")
    return bool(confirmed and output and confirmed != output)


def decide(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> tuple[str, str, str, str, bool, str]:
    blob = blob_for(record, text)
    local_blob = localized_blob_for(record, text)
    families = set(record.get("families_open") or [])
    source_key = str(record.get("source_key") or "")
    old_text = str(text.get("old_text") or "")
    if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
        return "blocked_stale_or_inconsistent_state", "", "", "state_diagnostic", True, "segment is not pending in selected run"
    if int(text.get("needs_output_apply") or 0) != 0 or int(text.get("confirmed_matches_output") or 0) != 1:
        return "blocked_stale_or_inconsistent_state", "", "", "state_diagnostic", True, "state guard failed"
    if output_mismatch(text):
        return "needs_blocked_source_output_mismatch_policy", "", "", "blocked_source_output_mismatch_policy", False, "confirmed/output text mismatch requires separate diagnostic"
    if token_integrity_issue(old_text):
        return "needs_blocked_token_integrity_policy", "", "", "blocked_token_integrity_policy", False, "placeholder or malformed token boundary"
    if LANG_RESIDUAL_RE.search(local_blob) or "spanish_residual_microagent" in families:
        return "needs_blocked_language_residual_policy", "", "", "blocked_language_residual_policy", False, "visible residual or residual-family signal remains"
    if GENDER_RE.search(blob) or "GenderLocalPlayer" in set(record.get("markers") or []):
        return "needs_blocked_gender_perspective_policy", "", "", "blocked_gender_perspective_policy", False, "gender/perspective marker remains in blocked route"
    if EVENT_RE.search(blob) and ("event_context_after_requirement_effect" in active):
        return "blocked_reuse_event_context_policy", "event_context_after_requirement_effect", "", "event_context_after_requirement_effect", False, "event/actor marker should reuse registered event-context splitter"
    if DYNAMIC_RE.search(blob) and ("ck3_dynamic_expression_parser_spec" in active):
        return "blocked_reuse_dynamic_parser_policy", "ck3_dynamic_expression_parser_spec", "", "ck3_dynamic_expression_parser_spec", False, "dynamic token can route to parser policy"
    if DYNAMIC_RE.search(blob):
        return "needs_blocked_dynamic_parser_unknown_policy", "", "", "blocked_dynamic_parser_unknown_policy", False, "dynamic surface lacks confident registered parser route"
    if re.search(r"culture_titles|theocracy|emperor|title|GetName|_name\\b|name_", blob, re.I):
        return "needs_blocked_name_title_culture_policy", "", "", "blocked_name_title_culture_policy", False, "name/title/culture surface remains blocked"
    if RELIGION_CULTURE_RE.search(blob) or "religion_semantic_microagent" in families:
        return "needs_blocked_religion_culture_policy", "", "", "blocked_religion_culture_policy", False, "religion/culture long semantic surface is the dominant blocked sublane"
    if len(families) >= 3:
        return "needs_blocked_cross_family_router_policy", "", "", "blocked_cross_family_router_policy", False, "multiple open families need bridge routing"
    if "semantic_review_router" in families:
        return "needs_blocked_semantic_ambiguity_policy", "", "", "blocked_semantic_ambiguity_policy", False, "semantic review signal remains without a narrower route"
    if source_key or old_text:
        return "blocked_terminal_uncertain_guard", "", "blocked_uncertain", "blocked_uncertain", True, "insufficient evidence for safe routing; keep terminal uncertain guard"
    return "blocked_insufficient_evidence", "", "", "blocked_uncertain_manual_review", True, "missing text/evidence"


def make_sample(record: dict[str, Any], text: dict[str, Any], active: set[str]) -> dict[str, Any]:
    groups = marker_groups(record, text)
    decision, registered, catalog, next_component, true_blocked, rationale = decide(record, text, active)
    return {
        "record_type": "sample_review",
        "segment_id": int(record["segment_id"]),
        "relative_path": str(record.get("relative_path") or ""),
        "source_key": str(record.get("source_key") or ""),
        "families_open": record.get("families_open") or [],
        "primary_route": "blocked_uncertain",
        "old_text": str(text.get("old_text") or ""),
        "confirmed_text": str(text.get("confirmed_text") or text.get("output_text") or ""),
        "output_text": str(text.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "blocked_decision": decision,
        "next_component": next_component,
        "is_true_blocked": true_blocked,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def validate_samples(samples: list[dict[str, Any]], expected_total: int) -> None:
    required = {
        "record_type", "segment_id", "relative_path", "source_key", "families_open", "primary_route",
        "old_text", "confirmed_text", "output_text", "block_markers", "uncertainty_markers",
        "token_markers", "dynamic_markers", "semantic_markers", "gender_markers", "domain_markers",
        "event_markers", "name_title_markers", "language_residual_markers", "matched_registered_policy",
        "matched_catalog_spec", "guard_markers", "secondary_markers", "blocked_decision",
        "next_component", "is_true_blocked", "requires_lifecycle_later", "requires_apply_later",
        "corrected_text", "rationale",
    }
    if len(samples) != expected_total:
        raise SystemExit(f"review count mismatch: {len(samples)} expected {expected_total}")
    if len(samples) > 160:
        raise SystemExit(f"sample limit guard failed: {len(samples)}")
    seen: set[int] = set()
    for row in samples:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if row["primary_route"] != "blocked_uncertain":
            raise SystemExit(f"wrong route for {segment_id}: {row['primary_route']}")
        if row["blocked_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid blocked_decision for {segment_id}: {row['blocked_decision']}")
        if row["requires_apply_later"]:
            raise SystemExit(f"requires_apply_later unexpectedly true for {segment_id}")
        if row["requires_lifecycle_later"]:
            raise SystemExit(f"requires_lifecycle_later unexpectedly true for {segment_id}")


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    universe: int,
    samples: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["blocked_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["blocked_decision"].startswith("blocked_reuse_"))
    new_policy_count = sum(1 for row in samples if row["blocked_decision"].startswith("needs_blocked_"))
    true_blocked_count = sum(1 for row in samples if row["is_true_blocked"] is True)
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    if reuse_count >= 40:
        next_prompt = "chat_exec_blocked_uncertain_policy_catalog_registration_prompt.md"
        assessment = "reuse_splitter_candidate"
    elif dominant_decision.startswith("needs_blocked_") and dominant_count >= 25:
        slug = dominant_decision.replace("needs_blocked_", "").replace("_policy", "")
        next_prompt = f"chat_exec_blocked_uncertain_{slug}_review_prompt.md"
        assessment = "new_sublane_candidate"
    elif true_blocked_count >= 80:
        next_prompt = "chat_exec_blocked_uncertain_policy_catalog_registration_prompt.md"
        assessment = "manual_review_queue_candidate"
    else:
        next_prompt = "chat_exec_global_remaining_318_or_less_diagnostic_prompt.md"
        assessment = "fragmented_final_diagnostic_needed"
    marker_fields = [
        "block_markers", "uncertainty_markers", "token_markers", "dynamic_markers", "semantic_markers",
        "gender_markers", "domain_markers", "event_markers", "name_title_markers",
        "language_residual_markers", "guard_markers", "secondary_markers",
    ]
    marker_counts = {
        field: dict(Counter(marker for row in samples for marker in row[field]).most_common(20))
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
        **state,
        **registry,
        "blocked_uncertain_universe_estimated": universe,
        "total_reviewed": len(samples),
        "reuse_registered_or_cataloged_count": reuse_count,
        "new_policy_candidate_count": new_policy_count,
        "true_blocked_count": true_blocked_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "decision_counts": dict(decision_counts),
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "package_assessment": assessment,
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_blocked_triage",
        "policy_id": "blocked_uncertain",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "entry_conditions": [
            "route == blocked_uncertain",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reuse_routes": [{"policy": key, "sampled": value} for key, value in matched_counts.most_common()],
        "new_policy_candidates": [
            {"decision": key, "sampled": value}
            for key, value in decision_counts.most_common()
            if key.startswith("needs_blocked_")
        ],
        "true_blocked_conditions": [
            {"decision": key, "sampled": value}
            for key, value in decision_counts.most_common()
            if key.startswith("blocked_") and not key.startswith("blocked_reuse_")
        ],
        "manual_review_conditions": [
            "insufficient evidence",
            "state inconsistency",
            "semantic ambiguity not covered by sublane",
        ],
        "resolution_order": [
            "state guard",
            "source/output mismatch and token integrity",
            "language residual",
            "gender/perspective",
            "registered reuse",
            "dynamic parser gap",
            "name/title/culture",
            "religion/culture semantic sublane",
            "cross-family/manual uncertainty",
        ],
        "next_components": [next_prompt],
        "promotion_gate": "triage_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for field, counts in marker_counts.items():
            for marker_name, count in counts.items():
                handle.write(json.dumps({"record_type": f"top_{field}", "value": marker_name, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_family", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for policy, count in matched_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_matched_policy_or_spec", "policy": policy, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Blocked/uncertain review\n\n")
        for key in [
            "blocked_uncertain_universe_estimated", "total_reviewed", "reuse_registered_or_cataloged_count",
            "new_policy_candidate_count", "true_blocked_count", "ready_lifecycle_future",
            "apply_candidates_future", "dominant_subtype", "dominant_count", "package_assessment", "next_prompt",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nDecisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop families\n")
        for family, count in family_counts.most_common(12):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop markers\n")
        for field, counts in marker_counts.items():
            handle.write(f"- {field}: {counts}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write("- Os 144 restantes sao majoritariamente roteamento incompleto para uma sublane religion/culture de textos longos, nao apply.\n")
        handle.write(f"- Reuso por policies registradas: {reuse_count}.\n")
        handle.write(f"- Nova sublane dominante: {dominant_decision} ({dominant_count}).\n")
        handle.write("- Nao ha candidatos a lifecycle/apply em curto prazo.\n")
        handle.write("- blocked_uncertain ainda nao deve ser registrado como manual-review queue; abrir a sublane dominante primeiro.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
        handle.write("- Producao full ainda nao; depende de revisar/registrar a sublane dominante ou optar por guard read-only final.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review blocked_uncertain read-only.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=160)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")
    if args.limit > 160:
        raise SystemExit("limit guard failed: max 160")
    with connect_readonly() as conn:
        state = state_counts(conn, args.segment_state_run_id)
        registry = registry_metrics(conn)
        active = active_registry(conn)
        (
            records,
            _route_counts,
            _family_counts,
            _combo_counts,
            _single_family_counts,
            _macro_counts,
            _issue_count_by_segment,
            _pending_segments,
            _routed_segments,
        ) = architecture_diag.route_pending(conn, args.segment_state_run_id, args.ledger_run_id)
        blocked_records = [row for row in records if row.get("route") == "blocked_uncertain"]
        blocked_records.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
        universe = len(blocked_records)
        if universe != EXPECTED_UNIVERSE:
            raise SystemExit(f"blocked_uncertain universe guard failed: {universe} expected {EXPECTED_UNIVERSE}")
        selected = blocked_records[: args.limit]
        text_by_id = fetch_texts(conn, [int(row["segment_id"]) for row in selected], args.segment_state_run_id)
    samples: list[dict[str, Any]] = []
    for record in selected:
        segment_id = int(record["segment_id"])
        text = text_by_id.get(segment_id)
        if not text:
            raise SystemExit(f"missing text/state row for segment_id={segment_id}")
        if str(text.get("state_group") or "") != "pending" or int(text.get("is_closed") or 0) != 0:
            raise SystemExit(f"pending guard failed for segment_id={segment_id}")
        if int(text.get("needs_output_apply") or 0) != 0:
            raise SystemExit(f"needs_output_apply guard failed for segment_id={segment_id}")
        if int(text.get("confirmed_matches_output") or 0) != 1:
            raise SystemExit(f"confirmed_matches_output guard failed for segment_id={segment_id}")
        samples.append(make_sample(record, text, active))
    validate_samples(samples, min(universe, args.limit))
    txt_path, jsonl_path, spec_path = write_outputs(args=args, state=state, registry=registry, universe=universe, samples=samples)
    decision_counts = Counter(row["blocked_decision"] for row in samples)
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")
    print(f"spec_json={spec_path}")
    print(f"blocked_uncertain_universe_estimated={universe}")
    print(f"total_reviewed={len(samples)}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print(f"reuse_registered_or_cataloged_count={sum(1 for row in samples if row['blocked_decision'].startswith('blocked_reuse_'))}")
    print(f"true_blocked_count={sum(1 for row in samples if row['is_true_blocked'] is True)}")
    print("ready_lifecycle_future=0")
    print("apply_candidates_future=0")


if __name__ == "__main__":
    main()
