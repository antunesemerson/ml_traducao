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


LANES: list[dict[str, Any]] = [
    {"id": "00_safety_or_output_blocker", "label": "Safety or Output Blocker", "priority": 0, "component": "state_guard"},
    {"id": "01_false_reopen_lifecycle", "label": "False Reopen Lifecycle", "priority": 1, "component": "false_reopen_lifecycle_bridge"},
    {"id": "02_dynamic_parser", "label": "Dynamic Parser", "priority": 2, "component": "ck3_dynamic_symbolic_parser"},
    {"id": "03_custom_loc_scope_getter", "label": "Custom Loc Scope Getter", "priority": 3, "component": "custom_loc_scope_parser"},
    {"id": "04_gender_local_player", "label": "Gender Local Player", "priority": 4, "component": "gender_local_player_policy"},
    {"id": "05_actor_target_recipient", "label": "Actor Target Recipient", "priority": 5, "component": "actor_target_recipient_policy"},
    {"id": "06_requirement_tooltip", "label": "Requirement Tooltip", "priority": 6, "component": "requirement_tooltip_policy"},
    {"id": "07_effect_list_multiline", "label": "Effect List Multiline", "priority": 7, "component": "effect_list_multiline_policy"},
    {"id": "08_domain_context", "label": "Domain Context", "priority": 8, "component": "domain_context_composer"},
    {"id": "09_event_context", "label": "Event Context", "priority": 9, "component": "event_context_composer"},
    {"id": "10_title_law_government", "label": "Title Law Government", "priority": 10, "component": "title_law_government_policy"},
    {"id": "11_trait_modifier_accolade", "label": "Trait Modifier Accolade", "priority": 11, "component": "trait_modifier_accolade_policy"},
    {"id": "12_name_dynasty_nickname", "label": "Name Dynasty Nickname", "priority": 12, "component": "name_dynasty_nickname_policy"},
    {"id": "13_residual_visible", "label": "Residual Visible", "priority": 13, "component": "residual_dependency_filtered_repair"},
    {"id": "14_short_label_style", "label": "Short Label Style", "priority": 14, "component": "short_label_style_policy"},
    {"id": "15_semantic_context", "label": "Semantic Context", "priority": 15, "component": "semantic_context_composer"},
    {"id": "16_autofix_unknown_single", "label": "Autofix Unknown Single", "priority": 16, "component": "autofix_unknown_single_router"},
    {"id": "17_model_training_candidate", "label": "Model Training Candidate", "priority": 17, "component": "active_learning_selector"},
    {"id": "99_blocked_uncertain", "label": "Blocked Uncertain", "priority": 99, "component": "human_review_or_evidence_collection"},
]

LANE_BY_ID = {lane["id"]: lane for lane in LANES}

DYNAMIC_RE = re.compile(
    r"Custom\(|Select_CString|SelectLocalization|Concept\(|ScriptValue|GetTrait|GetPlayer|GetLocalPlayer|"
    r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|\[[^\]]+\]|\$[^$]+\$",
    re.IGNORECASE,
)
CUSTOM_SCOPE_RE = re.compile(r"Custom\(|CustomLoc|Get[A-Za-z0-9_]+\(|scope|ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.", re.IGNORECASE)
GENDER_RE = re.compile(
    r"gender|local_player|Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|"
    r"Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|você|vocês|seu|sua|seus|suas",
    re.IGNORECASE,
)
ACTOR_RE = re.compile(r"\b(?:actor|target|recipient|root|from|scope|addressee|ROOT|FROM|TARGET|SCOPE|CHARACTER)\b", re.IGNORECASE)
TOOLTIP_RE = re.compile(r"tooltip|_tt\b|_tt$|requirement|required|trigger|unlock|available|can_|cannot|NO_CHANCE|valid_|invalid_", re.IGNORECASE)
EFFECT_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#low|#high|effect|modifier list|gain|loss", re.IGNORECASE)
DOMAIN_RE = re.compile(r"culture|religion|faith|artifact|activity|court|building|war|scheme|travel|tournament|memory|legend", re.IGNORECASE)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|interaction|activity|journey|petition|scheme|flavor|memory", re.IGNORECASE)
TITLE_RE = re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege|rank|holding", re.IGNORECASE)
TRAIT_RE = re.compile(r"trait|modifier|accolade|acclaimed|knight|ArtifactAdverb|ArtifactBookContentQuality|descriptor", re.IGNORECASE)
NAME_RE = re.compile(r"name|nickname|dynasty|house|epithet|GetName|GetDynasty|Muhammad|Cicero|Kalila|Dimna", re.IGNORECASE)
RESIDUAL_RE = re.compile(r"\b(the|your|you|their|has|will|cannot|consiguio|consiguió|ganaste|tendras|será|mas|más|muy|facil|fácil)\b|NÃ|Ãƒ|Â", re.IGNORECASE)
MODEL_RE = re.compile(r"training|model|uncertain|low_confidence|human", re.IGNORECASE)


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_run(conn: sqlite3.Connection, table: str, run_id: int) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"run not found: {table}.id={run_id}")
    result = dict(row)
    if "finished_at" in result and not result["finished_at"]:
        raise SystemExit(f"run is not finalized: {table}.id={run_id}")
    return result


def fetch_pending_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            li.segment_id,
            li.issue_family,
            li.issue_kind,
            li.issue_severity,
            li.agent_key,
            li.relative_path,
            li.source_key,
            s.final_state,
            s.state_group,
            s.locked,
            s.needs_human,
            s.needs_output_apply,
            s.needs_reopen,
            s.confirmed_matches_output,
            s.priority_score,
            out.portuguese_text AS output_text,
            src.old_text,
            src.spanish_text,
            src.english_text,
            '' AS confirmed_text
        FROM ml_issue_ledger_items li
        JOIN segment_state_items s
          ON s.segment_id = li.segment_id
         AND s.run_id = ?
        LEFT JOIN output_segments out
          ON out.segment_id = li.segment_id
        LEFT JOIN source_segments src
          ON src.id = li.segment_id
        WHERE li.run_id = ?
          AND li.status = 'open'
          AND s.state_group = 'pending'
          AND COALESCE(s.is_closed, 0) = 0
        ORDER BY s.priority_score DESC, li.segment_id, li.issue_family
        """,
        (segment_state_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def group_by_segment(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(row)
    return grouped


def families_for(segment_rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(row["issue_family"]) for row in segment_rows}))


def blob_for(segment_rows: list[dict[str, Any]]) -> str:
    first = segment_rows[0]
    pieces = [
        first.get("relative_path"),
        first.get("source_key"),
        first.get("old_text"),
        first.get("spanish_text"),
        first.get("english_text"),
        first.get("confirmed_text"),
        first.get("output_text"),
        " ".join(str(row.get("issue_family") or "") for row in segment_rows),
        " ".join(str(row.get("issue_kind") or "") for row in segment_rows),
        " ".join(str(row.get("agent_key") or "") for row in segment_rows),
    ]
    return " ".join(str(piece or "") for piece in pieces)


def has_clean_false_reopen(segment_rows: list[dict[str, Any]], blob: str) -> bool:
    first = segment_rows[0]
    confirmed = first.get("confirmed_text") or ""
    output = first.get("output_text") or ""
    if not (int(first.get("needs_reopen") or 0) == 1 and int(first.get("confirmed_matches_output") or 0) == 1):
        return False
    if confirmed and output and confirmed != output:
        return False
    return not any(pattern.search(blob) for pattern in (DYNAMIC_RE, GENDER_RE, DOMAIN_RE, EVENT_RE, RESIDUAL_RE))


def candidate_lanes(segment_rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    first = segment_rows[0]
    families = set(families_for(segment_rows))
    blob = blob_for(segment_rows)
    lanes: list[tuple[str, str]] = []

    if (
        int(first.get("needs_output_apply") or 0) != 0
        or int(first.get("confirmed_matches_output") or 0) != 1
        or int(first.get("locked") or 0) != 0
    ):
        lanes.append(("00_safety_or_output_blocker", "state/output guard is not clean"))
    if has_clean_false_reopen(segment_rows, blob):
        lanes.append(("01_false_reopen_lifecycle", "clean false-reopen signal with confirmed output"))
    if "dynamic_ck3_expression_microagent" in families or DYNAMIC_RE.search(blob):
        lanes.append(("02_dynamic_parser", "dynamic family or CK3 dynamic token pattern"))
    if CUSTOM_SCOPE_RE.search(blob):
        lanes.append(("03_custom_loc_scope_getter", "custom localization, scope, or getter pattern"))
    if "gender_token_microagent" in families or "local_player_context_microagent" in families or GENDER_RE.search(blob):
        lanes.append(("04_gender_local_player", "gender token, local player, pronoun, or possessive pattern"))
    if ACTOR_RE.search(blob):
        lanes.append(("05_actor_target_recipient", "actor/target/recipient/root/scope perspective pattern"))
    if TOOLTIP_RE.search(blob):
        lanes.append(("06_requirement_tooltip", "requirement, unlock, or tooltip surface"))
    if EFFECT_RE.search(blob):
        lanes.append(("07_effect_list_multiline", "effect list, multiline, bullet, gain/loss, or modifier-list surface"))
    if DOMAIN_RE.search(blob) or {"culture_semantic_microagent", "religion_semantic_microagent"} & families:
        lanes.append(("08_domain_context", "domain-sensitive CK3 vocabulary or semantic family"))
    if EVENT_RE.search(blob) or "long_text_composer" in families:
        lanes.append(("09_event_context", "event, interaction, narrative, or context-heavy surface"))
    if TITLE_RE.search(blob) or "title_policy_microagent" in families:
        lanes.append(("10_title_law_government", "title/law/government/rank pattern"))
    if TRAIT_RE.search(blob):
        lanes.append(("11_trait_modifier_accolade", "trait, modifier, accolade, or descriptor pattern"))
    if NAME_RE.search(blob) or "nickname_name_policy" in families:
        lanes.append(("12_name_dynasty_nickname", "name, dynasty, house, nickname, or named-entity pattern"))
    if RESIDUAL_RE.search(blob) or "spanish_residual_microagent" in families:
        lanes.append(("13_residual_visible", "visible Spanish/English/mojibake residual marker"))
    if "short_label_style_microagent" in families:
        lanes.append(("14_short_label_style", "short-label style family"))
    if "semantic_review_router" in families:
        lanes.append(("15_semantic_context", "semantic review family"))
    if families == {"autofix_unknown_microagent"}:
        lanes.append(("16_autofix_unknown_single", "single-family autofix_unknown segment"))
    if MODEL_RE.search(blob):
        lanes.append(("17_model_training_candidate", "uncertainty/model-training marker"))
    if not lanes:
        lanes.append(("99_blocked_uncertain", "no confident routing rule matched"))
    return sorted(lanes, key=lambda item: LANE_BY_ID[item[0]]["priority"])


def confidence_for(primary: str, lane_reasons: list[tuple[str, str]]) -> str:
    if primary in {"00_safety_or_output_blocker", "99_blocked_uncertain"}:
        return "high" if primary == "00_safety_or_output_blocker" else "low"
    if len(lane_reasons) >= 4:
        return "medium"
    if primary in {"02_dynamic_parser", "03_custom_loc_scope_getter", "04_gender_local_player", "06_requirement_tooltip"}:
        return "high"
    return "medium"


def route_segments(grouped: dict[int, list[dict[str, Any]]]) -> dict[int, dict[str, Any]]:
    routed: dict[int, dict[str, Any]] = {}
    for segment_id, segment_rows in grouped.items():
        lanes = candidate_lanes(segment_rows)
        primary = lanes[0][0]
        secondaries = [lane for lane, _ in lanes[1:]]
        first = segment_rows[0]
        routed[segment_id] = {
            "segment_id": segment_id,
            "relative_path": first.get("relative_path") or "",
            "source_key": first.get("source_key") or "",
            "families": list(families_for(segment_rows)),
            "primary_lane": primary,
            "secondary_lanes": secondaries,
            "reason": lanes[0][1],
            "confidence": confidence_for(primary, lanes),
            "recommended_component": LANE_BY_ID[primary]["component"],
            "priority_score": float(first.get("priority_score") or 0),
        }
    return routed


def expected_gain_band(count: int) -> str:
    if count > 300:
        return "high"
    if count >= 100:
        return "medium"
    if count >= 30:
        return "low"
    return "research"


def risk_for_lane(lane: str) -> str:
    if lane in {"01_false_reopen_lifecycle", "14_short_label_style", "16_autofix_unknown_single"}:
        return "medium"
    if lane in {"06_requirement_tooltip", "07_effect_list_multiline", "13_residual_visible"}:
        return "medium_high"
    if lane in {"02_dynamic_parser", "03_custom_loc_scope_getter", "04_gender_local_player", "05_actor_target_recipient"}:
        return "high"
    return "medium"


def prompt_for_lane(lane: str) -> str:
    return {
        "01_false_reopen_lifecycle": "chat_exec_false_reopen_lifecycle_cohort_review_prompt.md",
        "02_dynamic_parser": "chat_exec_parser_backed_dynamic_expression_design_prompt.md",
        "03_custom_loc_scope_getter": "chat_exec_custom_loc_scope_parser_cohort_review_prompt.md",
        "04_gender_local_player": "chat_exec_gender_local_player_policy_consolidated_review_prompt.md",
        "05_actor_target_recipient": "chat_exec_actor_target_recipient_policy_review_prompt.md",
        "06_requirement_tooltip": "chat_exec_requirement_tooltip_router_cohort_review_prompt.md",
        "07_effect_list_multiline": "chat_exec_effect_list_multiline_policy_review_prompt.md",
        "08_domain_context": "chat_exec_domain_context_composer_cohort_review_prompt.md",
        "09_event_context": "chat_exec_event_context_composer_cohort_review_prompt.md",
        "13_residual_visible": "chat_exec_residual_dependency_filtered_review_prompt.md",
        "14_short_label_style": "chat_exec_short_label_style_router_cohort_review_prompt.md",
        "15_semantic_context": "chat_exec_semantic_context_composer_cohort_review_prompt.md",
        "16_autofix_unknown_single": "chat_exec_autofix_unknown_single_router_cohort_review_prompt.md",
    }.get(lane, "chat_exec_macro_lane_router_cohort_validation_prompt.md")


def build_cohorts(routed: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, tuple[str, ...], tuple[str, ...]]] = Counter()
    for item in routed.values():
        secondary_key = tuple(item["secondary_lanes"][:3])
        counts[(item["primary_lane"], tuple(item["families"]), secondary_key)] += 1
    cohorts: list[dict[str, Any]] = []
    for (primary, families, secondaries), count in counts.most_common():
        risk = risk_for_lane(primary)
        if count >= 100 or (count >= 50 and risk in {"medium", "medium_high"}) or (count >= 30 and primary == "01_false_reopen_lifecycle"):
            cohorts.append(
                {
                    "cohort_key": f"{primary}::{'+'.join(families)}::{'+'.join(secondaries) if secondaries else 'no_secondary'}",
                    "primary_lane": primary,
                    "secondary_lanes": list(secondaries),
                    "families": list(families),
                    "segments": count,
                    "actionable_reason": "large routed cohort with stable family/lane shape",
                    "recommended_prompt": prompt_for_lane(primary),
                    "risk": risk,
                    "expected_gain_band": expected_gain_band(count),
                }
            )
    return cohorts


def build_spec(segment_state_run_id: int, ledger_run_id: int) -> dict[str, Any]:
    lanes = []
    for lane in LANES:
        lanes.append(
            {
                "id": lane["id"],
                "label": lane["label"],
                "priority": lane["priority"],
                "purpose": f"Route pending segments to {lane['component']} before micro-splitting.",
                "entry_conditions": [rule_condition(lane["id"])],
                "secondary_lane_preservation": True,
                "recommended_component": lane["component"],
                "promotion_gate": "read-only cohort validation, zero false-safe sample, then protected lifecycle/apply only when explicitly authorized",
            }
        )
    return {
        "schema_version": 1,
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "created_for": "read_only_architecture_review",
        "lanes": lanes,
        "router_rules": [
            {
                "priority": lane["priority"],
                "lane": lane["id"],
                "condition": rule_condition(lane["id"]),
                "confidence": "heuristic_v1",
            }
            for lane in LANES
        ],
        "recommended_rollout": [
            "implement read-only router/parser component and compare against this report",
            "add reusable composers for semantic/domain/event context cohorts",
            "add lifecycle bridges only when cohort validation shows clean ready sets",
            "allow protected apply only after context filters and token preservation gates",
            "run full production only after dry-run gain and zero false-safe evidence",
        ],
    }


def rule_condition(lane: str) -> str:
    return {
        "00_safety_or_output_blocker": "state/output guard is not clean",
        "01_false_reopen_lifecycle": "clean confirmed/output false-reopen without dynamic/context/residual blockers",
        "02_dynamic_parser": "dynamic_ck3_expression family or CK3 dynamic tokens",
        "03_custom_loc_scope_getter": "Custom/custom loc/scope/getter markers",
        "04_gender_local_player": "gender token, Select_CString, ES_* or local-player markers",
        "05_actor_target_recipient": "actor/target/recipient/root/from/scope perspective markers",
        "06_requirement_tooltip": "tooltip, requirement, unlock, can/cannot or trigger surface",
        "07_effect_list_multiline": "effect list, bullet, multiline, gain/loss or modifier list",
        "08_domain_context": "culture/religion/artifact/activity/domain semantic markers",
        "09_event_context": "event, desc, option, interaction, narrative or contextual surface",
        "10_title_law_government": "title, law, government, realm, rank or vassalage markers",
        "11_trait_modifier_accolade": "trait, modifier, accolade, knight or descriptor markers",
        "12_name_dynasty_nickname": "name, dynasty, house, nickname or named entity markers",
        "13_residual_visible": "visible Spanish/English/mojibake residual after stronger blockers",
        "14_short_label_style": "short_label_style family",
        "15_semantic_context": "semantic_review_router family",
        "16_autofix_unknown_single": "single-family autofix_unknown segment",
        "17_model_training_candidate": "uncertainty or active-learning marker",
        "99_blocked_uncertain": "no safe route matched",
    }[lane]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_macro_lane_router_architecture_review"
    spec = reports_dir / f"{stamp}_macro_lane_router_architecture_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def write_jsonl(path: Path, routed: dict[int, dict[str, Any]], cohorts: list[dict[str, Any]], segment_state_run_id: int, ledger_run_id: int) -> None:
    total = len(routed)
    lane_counts = Counter(item["primary_lane"] for item in routed.values())
    overlap_counts: Counter[tuple[str, str]] = Counter()
    lane_family_counts: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in routed.values():
        lane_family_counts[item["primary_lane"]][tuple(item["families"])] += 1
        for secondary in item["secondary_lanes"]:
            overlap_counts[(item["primary_lane"], secondary)] += 1
        if len(samples[item["primary_lane"]]) < 5:
            samples[item["primary_lane"]].append(item)

    records: list[dict[str, Any]] = [
        {"record_type": "summary", "segment_state_run_id": segment_state_run_id, "ledger_run_id": ledger_run_id, "pending_segments": total}
    ]
    for rank, (lane, count) in enumerate(lane_counts.most_common(), 1):
        records.append(
            {
                "record_type": "lane",
                "lane": lane,
                "segments": count,
                "percent": round(count / total * 100 if total else 0, 2),
                "primary_rank": rank,
                "recommended_component": LANE_BY_ID[lane]["component"],
                "top_family_combinations": [(list(families), n) for families, n in lane_family_counts[lane].most_common(5)],
            }
        )
    for (primary, secondary), count in overlap_counts.most_common(75):
        records.append({"record_type": "lane_overlap", "primary_lane": primary, "secondary_lane": secondary, "segments": count})
    for cohort in cohorts:
        records.append({"record_type": "cohort", **cohort})
    for lane, items in samples.items():
        for item in items:
            records.append(
                {
                    "record_type": "sample",
                    "lane": lane,
                    "segment_id": item["segment_id"],
                    "relative_path": item["relative_path"],
                    "source_key": item["source_key"],
                    "families": item["families"],
                    "secondary_lanes": item["secondary_lanes"],
                    "reason": item["reason"],
                    "confidence": item["confidence"],
                }
            )
    for lane in LANES:
        records.append(
            {
                "record_type": "router_rule",
                "priority": lane["priority"],
                "lane": lane["id"],
                "condition": rule_condition(lane["id"]),
                "confidence": "heuristic_v1",
            }
        )
    for strategy in next_strategies(lane_counts, cohorts):
        records.append({"record_type": "strategy", **strategy})

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def next_strategies(lane_counts: Counter[str], cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "priority": 1,
            "name": "chat_exec_macro_lane_router_cohort_validation_prompt.md",
            "expected_gain_band": "high",
            "risk": "medium",
            "next_prompt": "validate top routed cohorts against samples before pipeline integration",
        },
        {
            "priority": 2,
            "name": "chat_exec_parser_backed_dynamic_expression_design_prompt.md",
            "expected_gain_band": "high",
            "risk": "high",
            "next_prompt": f"design parser for {lane_counts['02_dynamic_parser']} primary dynamic segments plus preserved secondary lanes",
        },
        {
            "priority": 3,
            "name": "chat_exec_gender_local_player_policy_consolidated_review_prompt.md",
            "expected_gain_band": "medium",
            "risk": "high",
            "next_prompt": f"consolidate gender/local-player after priority routing found {lane_counts['04_gender_local_player']} primary cases",
        },
    ]


def write_txt(path: Path, routed: dict[int, dict[str, Any]], cohorts: list[dict[str, Any]]) -> None:
    total = len(routed)
    lane_counts = Counter(item["primary_lane"] for item in routed.values())
    overlap_counts: Counter[tuple[str, str]] = Counter()
    family_counts: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    for item in routed.values():
        family_counts[item["primary_lane"]][tuple(item["families"])] += 1
        for secondary in item["secondary_lanes"]:
            overlap_counts[(item["primary_lane"], secondary)] += 1

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Macro-lane router architecture review\n\n")
        handle.write("Resumo executivo\n")
        handle.write("O roteador reduz a fragmentacao ao promover uma lane primaria por prioridade e preservar lanes secundarias para cohorts reutilizaveis.\n")
        handle.write("A lane primaria dominante passa a ser 02_dynamic_parser; gender/local-player continua grande, mas aparece muito como secundaria sob dinamica.\n")
        handle.write("O roteador deve virar componente read-only real antes de qualquer lifecycle/apply novo.\n\n")

        handle.write("Distribuicao por lane primaria\n")
        for lane, count in lane_counts.most_common():
            handle.write(f"- {lane}: {count} ({count / total * 100:.1f}%) -> {LANE_BY_ID[lane]['component']}\n")
            top_families = family_counts[lane].most_common(3)
            if top_families:
                rendered = "; ".join(f"{' + '.join(families)}={n}" for families, n in top_families)
                handle.write(f"  familias/combinacoes: {rendered}\n")
        handle.write("\n")

        handle.write("Top overlaps de lanes\n")
        for (primary, secondary), count in overlap_counts.most_common(20):
            handle.write(f"- {primary} + {secondary}: {count}\n")
        handle.write("\n")

        handle.write("Cohorts grandes acionaveis\n")
        for cohort in cohorts[:20]:
            handle.write(
                f"- {cohort['cohort_key']}: {cohort['segments']} segmentos, risco={cohort['risk']}, "
                f"ganho={cohort['expected_gain_band']}, prompt={cohort['recommended_prompt']}\n"
            )
        handle.write("\n")

        handle.write("Respostas objetivas\n")
        handle.write("- O roteador reduz fragmentacao: sim, porque transforma subpolicies pequenas em secundarias preservadas dentro de cohorts maiores.\n")
        top_lane = lane_counts.most_common(1)[0]
        handle.write(f"- Lane primaria dominante: {top_lane[0]} com {top_lane[1]} segmentos.\n")
        handle.write(
            f"- gender_or_local_player continua maior que dynamic_parser depois da prioridade? nao; 04_gender_local_player={lane_counts['04_gender_local_player']} e 02_dynamic_parser={lane_counts['02_dynamic_parser']}.\n"
        )
        handle.write("- residual_visible deve ser tratado como residual real somente apos filtros de dinamica/contexto; muitos casos migraram para lanes mais fortes.\n")
        handle.write("- Primeiro componente reutilizavel: roteador read-only + parser dinamico, porque preserva gender/custom/scope/effect como secundarias.\n")
        handle.write("- Ganho esperado antes de full production: validar cohorts >300 com zero false-safe e estimar fechamento por dry-run.\n")
        handle.write("- Lanes para registrar, nao perseguir agora: descriptor/name/title pequenos, 99_blocked_uncertain e model_training sem cohort revisado.\n\n")

        handle.write("Plano de rollout\n")
        handle.write("1. Criar componente read-only de parser/roteador e comparar com este relatorio.\n")
        handle.write("2. Adicionar compositores reutilizaveis para semantic/domain/event context.\n")
        handle.write("3. Criar lifecycle bridges somente quando cohort ready passar limiar.\n")
        handle.write("4. Apply protegido somente depois de filtros de contexto e preservacao de tokens.\n")
        handle.write("5. Producao full so apos dry-run com ganho e zero false-safe.\n\n")

        handle.write("Proximos prompts recomendados\n")
        for strategy in next_strategies(lane_counts, cohorts):
            handle.write(f"{strategy['priority']}. {strategy['name']}: {strategy['next_prompt']}\n")
        handle.write("\n")

        handle.write("Validacoes\n")
        handle.write("- Banco aberto em modo read-only.\n")
        handle.write("- JSONL agregado e spec JSON parseavel.\n")
        handle.write("- Sem lifecycle, apply, segment-state, issue-ledger, confirmations, reindex, treino, source/output changes.\n")


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_macro_lane_router_architecture_review"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{stamp}_macro_lane_router_architecture_spec.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only macro-lane router architecture review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()

    conn = connect_readonly()
    fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
    fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
    rows = fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    grouped = group_by_segment(rows)
    routed = route_segments(grouped)
    cohorts = build_cohorts(routed)
    txt_path, jsonl_path, spec_path = output_paths()
    write_txt(txt_path, routed, cohorts)
    write_jsonl(jsonl_path, routed, cohorts, args.segment_state_run_id, args.ledger_run_id)
    spec = build_spec(args.segment_state_run_id, args.ledger_run_id)
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    lane_counts = Counter(item["primary_lane"] for item in routed.values())
    overlap_counts: Counter[tuple[str, str]] = Counter()
    for item in routed.values():
        for secondary in item["secondary_lanes"]:
            overlap_counts[(item["primary_lane"], secondary)] += 1

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"pending_segments: {len(routed)}")
    print("top_lanes:")
    for lane, count in lane_counts.most_common(8):
        print(f"  {lane}: {count}")
    print("top_overlaps:")
    for (primary, secondary), count in overlap_counts.most_common(5):
        print(f"  {primary} + {secondary}: {count}")
    print("top_cohorts:")
    for cohort in cohorts[:5]:
        print(f"  {cohort['cohort_key']}: {cohort['segments']}")


if __name__ == "__main__":
    main()
