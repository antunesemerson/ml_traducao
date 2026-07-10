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
import requirement_effect_router_readonly as router


PRIMARY_ROUTE = "effect_list_multiline_policy"

ALLOWED_DECISIONS = {
    "effect_list_multiline_terminal_policy",
    "effect_list_multiline_terminal_policy_with_scope_guard",
    "effect_list_multiline_terminal_policy_with_event_guard",
    "effect_list_multiline_terminal_policy_with_domain_guard",
    "needs_effect_list_bullets_policy",
    "needs_effect_list_multiline_block_policy",
    "needs_effect_list_scope_getter_policy",
    "needs_effect_list_artifact_activity_policy",
    "needs_effect_list_title_law_policy",
    "needs_effect_list_trait_accolade_policy",
    "needs_effect_list_script_value_policy",
    "needs_effect_list_concept_policy",
    "needs_effect_list_gender_local_player_policy",
    "needs_effect_list_event_context",
    "needs_effect_list_domain_context",
    "needs_effect_list_residual_repair",
    "needs_effect_list_dynamic_parser_escape",
    "effect_list_multiline_blocked_uncertain",
}

EFFECT_LIST_MARKERS = [
    ("EffectListBullet", re.compile(r"\$EFFECT_LIST_BULLET\$", re.I)),
    ("InlineEffectColor", re.compile(r"#P|#N|#V|#EMP|#bold|#weak|#high|#low", re.I)),
    ("CostGainLoss", re.compile(r"\bgain|gains|lose|loses|cost|custo|ganha|perde|recebe|perda\b", re.I)),
    ("RequirementList", re.compile(r"cannot|must|requires|required|valid|invalid|precisa|exige|necess", re.I)),
]

MULTILINE_MARKERS = [
    ("EscapedNewline", re.compile(r"\\n")),
    ("ActualNewline", re.compile(r"\n")),
    ("IndentMarker", re.compile(r"#indent|#!", re.I)),
    ("MultipleSentences", re.compile(r"\..+\.", re.S)),
]

GUARD_MARKERS = [
    ("ScopeGetterGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("EventGuard", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("DomainGuard", re.compile(r"culture|dynasty|house|title|law|government|realm|vassal|liege|religion|faith|county|building", re.I)),
]

SECONDARY_MARKERS = [
    ("ArtifactActivity", re.compile(r"artifact|activity|travel|tournament|legend|item|journey|hunt|feast|wedding", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|landed|county|duchy|kingdom|empire", re.I)),
    ("TraitAccolade", re.compile(r"trait|accolade|acclaimed_knight|knight|modifier|prowess|skills", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\|[^\]]+\]|Concept\(", re.I)),
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|local_player|GetPlayer|GetLocalPlayer|\bvoc(?:ê|e)\b|\bseu\b|\bsua\b", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("Domain", re.compile(r"culture|dynasty|house|title|law|government|realm|vassal|liege|religion|faith|county|building", re.I)),
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def connect_readonly() -> sqlite3.Connection:
    conn = router.connect_readonly()
    conn.execute("PRAGMA query_only = ON")
    return conn


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def fetch_texts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, str]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    candidates = [
        """
        SELECT segment_id, old_text, confirmed_text, output_text
        FROM segment_texts
        WHERE segment_id IN ({placeholders})
        """,
        """
        SELECT id AS segment_id, old_text, confirmed_text, output_text
        FROM segments
        WHERE id IN ({placeholders})
        """,
    ]
    for sql in candidates:
        try:
            rows = conn.execute(sql.format(placeholders=placeholders), segment_ids).fetchall()
        except sqlite3.Error:
            continue
        return {
            int(row["segment_id"]): {
                "old_text": str(row["old_text"] or ""),
                "confirmed_text": str(row["confirmed_text"] or ""),
                "output_text": str(row["output_text"] or ""),
            }
            for row in rows
        }
    return {}


def classify(
    effect_markers: list[str],
    multiline_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str]:
    effect = set(effect_markers)
    multiline = set(multiline_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if "ResidualVisible" in secondary:
        return "needs_effect_list_residual_repair", "residual_dependency_filtered_repair", "visible residual remains"
    if "EffectListBullet" in effect:
        return "needs_effect_list_bullets_policy", "effect_list_bullets_policy", "explicit EFFECT_LIST_BULLET marker"
    if "ArtifactActivity" in secondary:
        return "needs_effect_list_artifact_activity_policy", "effect_list_artifact_activity_policy", "artifact/activity surface dominates"
    if "ScriptValue" in secondary:
        return "needs_effect_list_script_value_policy", "effect_list_script_value_policy", "ScriptValue/numeric surface dominates"
    if "TraitAccolade" in secondary:
        return "needs_effect_list_trait_accolade_policy", "effect_list_trait_accolade_policy", "trait/accolade/modifier surface dominates"
    if "TitleLaw" in secondary:
        return "needs_effect_list_title_law_policy", "effect_list_title_law_policy", "title/law surface dominates"
    if "GenderLocalPlayer" in secondary:
        return "needs_effect_list_gender_local_player_policy", "effect_list_gender_local_player_policy", "gender/local-player dynamic dominates"
    if "Concept" in secondary:
        return "needs_effect_list_concept_policy", "effect_list_concept_policy", "concept expression dominates"
    if "ScopeGetterGuard" in guards:
        return "needs_effect_list_scope_getter_policy", "effect_list_scope_getter_policy", "scope/getter dominates"
    if "EventGuard" in guards or "Event" in secondary:
        return "effect_list_multiline_terminal_policy_with_event_guard", "terminal_router_policy", "effect-list/multiline with event guard"
    if "DomainGuard" in guards or "Domain" in secondary:
        return "effect_list_multiline_terminal_policy_with_domain_guard", "terminal_router_policy", "effect-list/multiline with domain guard"
    if multiline:
        return "needs_effect_list_multiline_block_policy", "effect_list_multiline_block_policy", "generic multiline block dominates"
    if "DynamicToken" in secondary:
        return "needs_effect_list_dynamic_parser_escape", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if effect:
        return "effect_list_multiline_terminal_policy", "terminal_router_policy", "plain effect-list/multiline terminal surface"
    return "effect_list_multiline_blocked_uncertain", "human_review_or_evidence_collection", "insufficient effect-list/multiline evidence"


def diverse_sample(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        family_key = "|".join(record["families_open"]) or "no_family"
        marker_key = "|".join(marker for marker in record["router_markers"] if marker in {"ScopeGetter", "Event", "DynamicToken", "GenderLocalPlayer", "ArtifactActivity", "Domain", "Concept", "ScriptValue", "AccoladeTrait", "ResidualVisible"}) or "no_secondary"
        buckets[f"{family_key}::{marker_key}"].append(record)
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for _, bucket in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(selected) >= limit:
            break
        record = bucket[0]
        selected.append(record)
        seen.add(record["segment_id"])
    if len(selected) < limit:
        for record in records:
            if len(selected) >= limit:
                break
            if record["segment_id"] not in seen:
                selected.append(record)
                seen.add(record["segment_id"])
    return selected[:limit]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_multiline_policy_review"
    spec = reports_dir / f"{stamp}_effect_list_multiline_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only effect-list multiline policy review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    conn = connect_readonly()
    router.fetch_runs(conn, args.segment_state_run_id, args.ledger_run_id)
    grouped = router.fetch_pending_rows(conn, args.segment_state_run_id, args.ledger_run_id)

    route_records: list[dict[str, Any]] = []
    for segment_id, rows in grouped.items():
        blob = router.blob_for(rows)
        markers = router.detect_markers(blob)
        route, reason = router.route_for(blob, markers)
        if route != PRIMARY_ROUTE:
            continue
        first = rows[0]
        route_records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(router.families_for(rows)),
                "router_markers": markers,
                "router_reason": reason,
                "needs_output_apply": int(first.get("needs_output_apply") or 0),
                "confirmed_matches_output": int(first.get("confirmed_matches_output") or 0),
            }
        )

    route_records.sort(key=lambda row: (row["relative_path"], row["source_key"], row["segment_id"]))
    sample = diverse_sample(route_records, args.limit)
    texts = fetch_texts(conn, [row["segment_id"] for row in sample])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    multiline_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()

    for row in sample:
        text = texts.get(row["segment_id"], {"old_text": "", "confirmed_text": "", "output_text": ""})
        blob = " ".join([row["relative_path"], row["source_key"], text["old_text"], text["confirmed_text"], text["output_text"], " ".join(row["router_markers"])])
        effect_markers = detect(EFFECT_LIST_MARKERS, blob)
        multiline_markers = detect(MULTILINE_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row["router_markers"]) | set(detect(SECONDARY_MARKERS, blob)))
        decision, next_component, rationale = classify(effect_markers, multiline_markers, guard_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {row['segment_id']}")
        if row["needs_output_apply"] != 0 or row["confirmed_matches_output"] != 1:
            decision = "effect_list_multiline_blocked_uncertain"
            next_component = "state_guard"
            rationale = "state guard failed: needs_output_apply or confirmed_matches_output"
        decision_counts[decision] += 1
        family_counts.update(row["families_open"] or ["NoOpenFamily"])
        effect_counts.update(effect_markers or ["NoEffectListMarker"])
        multiline_counts.update(multiline_markers or ["NoMultilineMarker"])
        guard_counts.update(guard_markers or ["NoGuardMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "families_open": row["families_open"],
                "primary_route": PRIMARY_ROUTE,
                "old_text": text["old_text"],
                "confirmed_text": text["confirmed_text"],
                "output_text": text["output_text"],
                "effect_list_markers": effect_markers,
                "multiline_markers": multiline_markers,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "effect_list_multiline_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("effect_list_multiline_terminal_policy"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if dominant.startswith("needs_") and dominant_count >= 50:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 50"
    elif terminal_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_multiline_policy_terminal_spec_prompt.md"
        stop_rule = "terminal_majority_prepare_readonly_spec_registration"
    else:
        next_prompt = "chat_exec_artifact_activity_effect_policy_review_prompt.md"
        stop_rule = "fragmented_return_to_next_large_route"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": PRIMARY_ROUTE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "entry_conditions": [
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "effect_list_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "explicit bullets",
            "artifact/activity",
            "ScriptValue, trait/accolade, title/law",
            "gender/local-player and concept",
            "scope/getter",
            "event/domain terminal guards",
            "generic multiline block",
        ],
        "next_components": [
            "effect_list_bullets_policy",
            "effect_list_multiline_block_policy",
            "effect_list_scope_getter_policy",
            "effect_list_artifact_activity_policy",
            "effect_list_script_value_policy",
            "effect_list_concept_policy",
            "artifact_activity_effect_policy",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "ambiguous multiline/effect surface"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "route_universe": len(route_records),
        "sampled": len(results),
        "terminal_policy_majority": terminal_count > len(results) / 2,
        "stop_rule": stop_rule,
    }

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "segment_state_run_id": args.segment_state_run_id,
            "ledger_run_id": args.ledger_run_id,
            "primary_route": PRIMARY_ROUTE,
            "route_universe": len(route_records),
            "total_reviewed": len(results),
            "decision_counts": dict(decision_counts),
            "family_counts": dict(family_counts),
            "effect_list_marker_counts": dict(effect_counts),
            "multiline_marker_counts": dict(multiline_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "terminal_policy_majority": terminal_count > len(results) / 2,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "effect_list_multiline_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common():
            handle.write(json.dumps({"record_type": "family_count", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in effect_counts.most_common():
            handle.write(json.dumps({"record_type": "effect_list_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in guard_counts.most_common():
            handle.write(json.dumps({"record_type": "guard_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by effect-list multiline stop rule"),
            ("chat_exec_artifact_activity_effect_policy_review_prompt.md", "next large route without spec if effect-list fragments"),
            ("chat_exec_global_post_architecture_diagnostic_prompt.md", "global check after registered router expansion"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Effect-list multiline policy review\n\n")
        handle.write(f"- universo total da rota: {len(route_records)}\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("effect_list_multiline_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop families_open:\n")
        for family, count in family_counts.most_common(15):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop effect-list markers:\n")
        for marker, count in effect_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- effect_list_multiline_policy deve virar componente read-only real.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Registrar depois como policy do requirement_effect_router_readonly depende da proxima subpolicy dominante.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"route_universe: {len(route_records)}")
    print(f"total_reviewed: {len(results)}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
