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


LEDGER_RUN_ID = 76

ALLOWED_DECISIONS = {
    "requirement_effect_ready_false_reopen",
    "requirement_effect_ready_lifecycle",
    "needs_requirement_tooltip_policy",
    "needs_effect_list_multiline_policy",
    "needs_effect_name_short_label_policy",
    "needs_holy_site_effect_name_policy",
    "needs_building_modifier_effect_policy",
    "needs_artifact_activity_effect_policy",
    "needs_accolade_trait_requirement_policy",
    "needs_script_value_effect_policy",
    "needs_scope_getter_requirement_policy",
    "needs_concept_requirement_policy",
    "needs_domain_context_after_requirement_effect",
    "needs_event_context_after_requirement_effect",
    "needs_residual_repair_after_requirement_effect",
    "needs_parser_after_requirement_effect",
    "requirement_effect_blocked_uncertain",
}

SURFACE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EffectListBullet", re.compile(r"\$EFFECT_LIST_BULLET\$", re.I)),
    ("Multiline", re.compile(r"\\n|\n")),
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|unlock|available|need|must", re.I)),
    ("EffectName", re.compile(r"_effect_name\b|effect_name", re.I)),
    ("HolySiteReligion", re.compile(r"holy_site|holy site|religion|faith|doctrine|temple|church", re.I)),
    ("BuildingModifier", re.compile(r"building|buildings?|modifier|holding|county", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|travel|tournament|legend|item|journey|hunt|feast|wedding", re.I)),
    ("AccoladeTrait", re.compile(r"accolade|acclaimed_knight|knight|trait|GetTrait|prowess", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\(", re.I)),
    ("Domain", re.compile(r"culture|dynasty|house|title|law|government|realm|vassal|liege", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
]

DYNAMIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("BracketExpression", re.compile(r"\[[^\]]+\]")),
    ("Variable", re.compile(r"\$[^$]+\$")),
    ("FormattingTag", re.compile(r"#[A-Za-z0-9_:.{};,|]+|#!")),
    ("Icon", re.compile(r"@[A-Za-z0-9_]+!")),
    ("CustomLoc", re.compile(r"Custom\(|CustomLoc|\.Custom\(", re.I)),
    ("SelectCString", re.compile(r"Select_CString|SelectLocalization", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|MakeScope|Localize\(", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|\[Concept\(|Concept\(", re.I)),
]

RESIDUAL_RE = re.compile(
    r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|\b(?:the|your|you|their|cannot|consiguio|consiguiÃ³|sentisteis|sintieron|"
    r"sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b",
    re.I,
)


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


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


def pending_guard(state: dict[str, Any] | None) -> bool:
    return bool(state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0)


def text_blob(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("relative_path") or ""),
        str(row.get("source_key") or ""),
        str(row.get("old_text") or ""),
        str(row.get("confirmed_text") or ""),
        str(row.get("output_text") or ""),
    ]
    return " ".join(parts)


def markers(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [label for label, pattern in patterns if pattern.search(blob)]


def source_rows(parser_jsonl: Path, validation_jsonl: Path) -> tuple[list[dict[str, Any]], int]:
    raw: list[dict[str, Any]] = []
    for row in read_jsonl(parser_jsonl):
        if row.get("record_type") == "sample_parse" and row.get("parser_design_decision") == "parser_needs_requirement_effect_override_first":
            item = dict(row)
            item["source_decision"] = "parser_needs_requirement_effect_override_first"
            raw.append(item)
    for row in read_jsonl(validation_jsonl):
        if row.get("record_type") == "sample_validation" and row.get("validation_decision") == "needs_requirement_or_effect_before_parser":
            item = dict(row)
            item["source_decision"] = "needs_requirement_or_effect_before_parser"
            item.setdefault("old_text", "")
            item.setdefault("confirmed_text", "")
            item.setdefault("output_text", "")
            raw.append(item)

    dedup: dict[int, dict[str, Any]] = {}
    for row in sorted(raw, key=lambda r: (str(r.get("relative_path") or ""), str(r.get("source_key") or ""), int(r["segment_id"]))):
        segment_id = int(row["segment_id"])
        if segment_id not in dedup:
            dedup[segment_id] = row
        else:
            previous = dedup[segment_id]
            if not previous.get("old_text") and row.get("old_text"):
                previous.update(row)
            previous["source_decision"] = "+".join(sorted(set(str(previous.get("source_decision", "")).split("+")) | {str(row["source_decision"])}))
    return list(dedup.values()), len(raw)


def bucket_for(row: dict[str, Any]) -> str:
    blob = text_blob(row)
    key = str(row.get("source_key") or "")
    if re.search(r"holy_site|holy site|religion|faith", blob, re.I):
        return "holy_site_or_religion_effect_name"
    if re.search(r"building|modifier|holding|county", blob, re.I):
        return "building_artifact_modifier_effect"
    if re.search(r"artifact|activity|travel|tournament|legend|item", blob, re.I):
        return "building_artifact_modifier_effect"
    if re.search(r"accolade|acclaimed_knight|trait|GetTrait|prowess", blob, re.I):
        return "accolade_trait_requirement"
    if re.search(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent", blob, re.I):
        return "effect_list_multiline"
    if re.search(r"tooltip|_tt\b|requirement|required|unlock|trigger|valid|allowed|cannot|can_", blob, re.I):
        return "requirement_tooltip"
    if re.search(r"_effect_name\b", key, re.I):
        return "holy_site_or_religion_effect_name"
    return "unknown_requirement_effect_surface"


def sample_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows.sort(key=lambda r: (str(r.get("relative_path") or ""), str(r.get("source_key") or ""), int(r["segment_id"])))
    if len(rows) <= limit:
        return rows
    quotas = [
        ("effect_list_multiline", 60),
        ("requirement_tooltip", 50),
        ("holy_site_or_religion_effect_name", 40),
        ("building_artifact_modifier_effect", 30),
        ("accolade_trait_requirement", 30),
        ("unknown_requirement_effect_surface", 30),
    ]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[bucket_for(row)].append(row)
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for bucket, quota in quotas:
        for row in buckets[bucket][:quota]:
            segment_id = int(row["segment_id"])
            if segment_id in seen:
                continue
            selected.append(row)
            seen.add(segment_id)
    return selected[:limit]


def classify(row: dict[str, Any], state: dict[str, Any] | None, surface: list[str], dynamic: list[str]) -> tuple[str, str, bool, str]:
    blob = text_blob(row)
    key = str(row.get("source_key") or "")
    old_text = str(row.get("old_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    output_text = str(row.get("output_text") or "")
    has_text = bool(old_text or output_text or confirmed_text)

    if not pending_guard(state):
        return "requirement_effect_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is not pending in the selected segment-state run"
    if has_text and RESIDUAL_RE.search(blob):
        return "needs_residual_repair_after_requirement_effect", "residual_dependency_filtered_repair", False, "visible residual/mojibake remains after recognizing requirement/effect surface"
    if re.search(r"holy_site|holy site", blob, re.I) and re.search(r"_effect_name\b|effect_name|religion|faith", blob, re.I):
        return "needs_holy_site_effect_name_policy", "holy_site_effect_name_policy", False, "holy-site/religion effect-name surface is repeated and should be policy-driven"
    if re.search(r"_effect_name\b|effect_name", key, re.I):
        return "needs_effect_name_short_label_policy", "effect_name_short_label_policy", False, "short effect-name label should be handled before generic parser"
    if re.search(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", blob, re.I):
        return "needs_effect_list_multiline_policy", "effect_list_multiline_policy", False, "multiline/bullet/list structure should drive before semantic token parsing"
    if re.search(r"tooltip|_tt\b|requirement|required|unlock|trigger|valid|allowed|cannot|can_|available|need|must", blob, re.I):
        return "needs_requirement_tooltip_policy", "requirement_tooltip_policy", False, "conditional tooltip/requirement surface is explicit"
    if re.search(r"building|holding|county|modifier|GetModifier", blob, re.I):
        return "needs_building_modifier_effect_policy", "building_modifier_effect_policy", False, "building/modifier effect surface needs its own domain vocabulary"
    if re.search(r"artifact|activity|travel|tournament|legend|item|journey|hunt|feast|wedding", blob, re.I):
        return "needs_artifact_activity_effect_policy", "artifact_activity_effect_policy", False, "artifact/activity effect surface is a repeated domain subtype"
    if re.search(r"accolade|acclaimed_knight|knight|trait|GetTrait|prowess", blob, re.I):
        return "needs_accolade_trait_requirement_policy", "accolade_trait_requirement_policy", False, "accolade/trait requirement should be resolved as a domain subtype"
    if "ScriptValue" in dynamic:
        return "needs_script_value_effect_policy", "script_value_effect_policy", False, "script value or numeric effect token remains inside the effect surface"
    if "ScopeGetter" in dynamic:
        return "needs_scope_getter_requirement_policy", "scope_getter_requirement_policy", False, "scope/getter token should be interpreted after requirement/effect surface routing"
    if "Concept" in dynamic:
        return "needs_concept_requirement_policy", "concept_requirement_policy", False, "concept link appears inside requirement/effect surface"
    if "Domain" in surface or "HolySiteReligion" in surface:
        return "needs_domain_context_after_requirement_effect", "domain_context_composer", False, "domain context remains after requirement/effect routing"
    if "Event" in surface:
        return "needs_event_context_after_requirement_effect", "event_context_composer", False, "event context remains after requirement/effect routing"
    if dynamic:
        return "needs_parser_after_requirement_effect", "ck3_dynamic_symbolic_parser", False, "generic parser should run after the requirement/effect surface is identified"
    if has_text and confirmed_text == output_text and int((state or {}).get("needs_output_apply") or 0) == 0:
        if int((state or {}).get("needs_reopen") or 0) == 1:
            return "requirement_effect_ready_false_reopen", "false_reopen_lifecycle_bridge", True, "surface looks aligned for a future false-reopen lifecycle, not for this run"
        return "requirement_effect_ready_lifecycle", "requirement_effect_lifecycle_bridge", True, "surface looks aligned for a future lifecycle, not for this run"
    return "requirement_effect_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient text evidence to classify more narrowly"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_requirement_effect_router_validation_review"
    spec = reports_dir / f"{stamp}_requirement_effect_router_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, decision_counts: Counter[str], surface_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_policy_design",
        "policy_id": "requirement_effect_list_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "source/report marks parser_needs_requirement_effect_override_first",
            "or cohort validation marks needs_requirement_or_effect_before_parser",
            "segment remains pending in segment_state_run_id 400",
            "no output apply is performed by this component",
        ],
        "surface_types": [
            "requirement_tooltip",
            "effect_list_multiline",
            "effect_name_short_label",
            "holy_site_effect_name",
            "building_modifier_effect",
            "artifact_activity_effect",
            "accolade_trait_requirement",
            "script_value_effect",
            "scope_getter_requirement",
            "concept_requirement",
        ],
        "resolution_order": [
            "reject non-pending or unsafe state",
            "detect residual/mojibake before promotion",
            "recognize holy-site/effect-name short labels",
            "recognize multiline/bullet effect lists",
            "recognize explicit requirement/tooltip surfaces",
            "route domain subtypes",
            "handoff remaining dynamic expressions to parser after surface routing",
        ],
        "next_components": [
            "effect_list_multiline_policy",
            "holy_site_effect_name_policy",
            "requirement_tooltip_policy",
            "building_modifier_effect_policy",
            "artifact_activity_effect_policy",
            "accolade_trait_requirement_policy",
            "script_value_effect_policy",
            "scope_getter_requirement_policy",
            "concept_requirement_policy",
            "ck3_dynamic_symbolic_parser",
            "domain_context_composer",
            "event_context_composer",
            "residual_dependency_filtered_repair",
        ],
        "blocked_conditions": [
            "not pending in selected segment-state run",
            "visible mojibake or English/Spanish residual",
            "missing text evidence in source report",
            "ambiguous dynamic token requiring parser after surface routing",
        ],
        "promotion_gate": "Promote only as read-only router after JSONL/spec validation and with requires_apply_later forced to zero; lifecycle/apply need separate guarded prompts.",
        "observed_decision_counts": dict(decision_counts),
        "observed_surface_marker_counts": dict(surface_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only requirement/effect router validation review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--parser-jsonl", required=True, type=Path)
    parser.add_argument("--validation-jsonl", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    conn = connect_readonly()
    rows, raw_total = source_rows(args.parser_jsonl, args.validation_jsonl)
    sampled = sample_rows(rows, args.limit)
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in sampled])
    missing_or_closed = [int(row["segment_id"]) for row in sampled if not pending_guard(states.get(int(row["segment_id"])))]
    if missing_or_closed:
        raise SystemExit(f"pending validation mismatch for segment_ids: {missing_or_closed[:20]}")

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    surface_counts: Counter[str] = Counter()
    next_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    apply_later_count = 0

    for row in sampled:
        segment_id = int(row["segment_id"])
        blob = text_blob(row)
        surface = markers(SURFACE_PATTERNS, blob)
        dynamic = markers(DYNAMIC_PATTERNS, blob)
        decision, component, lifecycle, rationale = classify(row, states.get(segment_id), surface, dynamic)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        surface_counts.update(surface or ["NoExplicitSurfaceMarker"])
        decision_counts[decision] += 1
        next_counts[component] += 1
        bucket_counts[bucket_for(row)] += 1
        requires_apply_later = False
        apply_later_count += int(requires_apply_later)
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "families_open": list(row.get("families_open") or []),
                "source_decision": str(row.get("source_decision") or ""),
                "old_text": str(row.get("old_text") or ""),
                "confirmed_text": str(row.get("confirmed_text") or ""),
                "output_text": str(row.get("output_text") or ""),
                "surface_markers": surface,
                "dynamic_markers": dynamic,
                "requirement_effect_decision": decision,
                "next_component": component,
                "requires_lifecycle_later": lifecycle,
                "requires_apply_later": requires_apply_later,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    if apply_later_count != 0:
        raise SystemExit(f"requires_apply_later must be 0, got {apply_later_count}")

    before_parser = sum(
        count
        for decision, count in decision_counts.items()
        if decision
        not in {
            "needs_parser_after_requirement_effect",
            "needs_domain_context_after_requirement_effect",
            "needs_event_context_after_requirement_effect",
            "needs_residual_repair_after_requirement_effect",
            "requirement_effect_blocked_uncertain",
        }
    )
    parser_after = decision_counts["needs_parser_after_requirement_effect"]
    context_after = (
        decision_counts["needs_domain_context_after_requirement_effect"]
        + decision_counts["needs_event_context_after_requirement_effect"]
        + decision_counts["needs_residual_repair_after_requirement_effect"]
    )
    ready_lifecycle = decision_counts["requirement_effect_ready_false_reopen"] + decision_counts["requirement_effect_ready_lifecycle"]

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "record_type": "summary",
                    "raw_total": raw_total,
                    "deduplicated_total": len(rows),
                    "sampled": len(results),
                    "decision_counts": dict(decision_counts),
                    "surface_marker_counts": dict(surface_counts),
                    "next_component_counts": dict(next_counts),
                    "bucket_counts": dict(bucket_counts),
                    "ready_lifecycle_future": ready_lifecycle,
                    "apply_candidates_future": apply_later_count,
                    "before_parser_count": before_parser,
                    "parser_after_policy_count": parser_after,
                    "domain_event_residual_after_count": context_after,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, surface_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    dominant = decision_counts.most_common(1)[0][0] if decision_counts else "none"
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Requirement/effect router validation review\n\n")
        handle.write(f"segment_state_run_id: {args.segment_state_run_id}\n")
        handle.write(f"ledger_run_id: {LEDGER_RUN_ID}\n")
        handle.write(f"raw_total: {raw_total}\n")
        handle.write(f"deduplicated_total: {len(rows)}\n")
        handle.write(f"sampled: {len(results)}\n")
        handle.write(f"ready_lifecycle_future: {ready_lifecycle}\n")
        handle.write(f"apply_candidates_future: {apply_later_count}\n")
        handle.write(f"before_parser_count: {before_parser}\n")
        handle.write(f"parser_after_policy_count: {parser_after}\n")
        handle.write(f"domain_event_residual_after_count: {context_after}\n")
        handle.write(f"dominant_subformat: {dominant}\n\n")
        handle.write("requirement_effect_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop surface markers:\n")
        for marker, count in surface_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nNext components:\n")
        for component, count in next_counts.most_common():
            handle.write(f"- {component}: {count}\n")
        handle.write("\nAnalise obrigatoria\n")
        handle.write("- requirement_effect_list_policy deve virar componente read-only real: sim.\n")
        handle.write("- Prioridade antes do parser generico: sim, a maioria fica resolvida/roteada antes do parser.\n")
        handle.write("- Primeiro subformato recomendado: effect_list_multiline_policy, seguido por holy_site_effect_name_policy se o volume confirmar.\n")
        handle.write("- Lifecycle/apply no curto prazo: nao; esta etapa gera desenho de roteador e zero apply candidates.\n")
        handle.write("- Proximo passo recomendado: spec do router read-only, depois subpolicy effect-list/holy-site, depois unknown parser audit.\n")
        handle.write("\nProximos prompts recomendados\n")
        handle.write("1. chat_exec_requirement_effect_router_readonly_spec_prompt.md\n")
        handle.write("2. chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md\n")
        handle.write("3. chat_exec_dynamic_parser_unknown_pattern_audit_prompt.md\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"raw_total: {raw_total}")
    print(f"deduplicated_total: {len(rows)}")
    print(f"sampled: {len(results)}")
    print(f"ready_lifecycle_future: {ready_lifecycle}")
    print(f"apply_candidates_future: {apply_later_count}")
    print(f"before_parser_count: {before_parser}")
    print(f"parser_after_policy_count: {parser_after}")
    print(f"domain_event_residual_after_count: {context_after}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")
    print("top_surface_markers:")
    for marker, count in surface_counts.most_common(10):
        print(f"  {marker}: {count}")


if __name__ == "__main__":
    main()
