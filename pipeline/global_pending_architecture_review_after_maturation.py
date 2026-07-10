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


MACRO_LANES = (
    "false_reopen_or_lifecycle_candidate",
    "dynamic_expression",
    "custom_loc_or_scope_getter",
    "gender_or_local_player",
    "domain_context",
    "event_context",
    "requirement_tooltip",
    "effect_list_or_multiline",
    "title_law_government",
    "trait_modifier_accolade",
    "name_dynasty_nickname",
    "residual_visible",
    "short_label_style",
    "semantic_plain_context",
    "autofix_unknown",
    "model_training_candidate",
    "blocked_uncertain",
)

CK3_DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|Get[A-Za-z0-9_]+\(|\[[^\]]+\]|\$[^$]+\$", re.I)
CUSTOM_SCOPE_RE = re.compile(r"Custom\(|Get[A-Za-z0-9_]+\(|ROOT\.|SCOPE\.|TARGET\.|CHARACTER\.|LocalPlayer", re.I)
GENDER_RE = re.compile(r"gender|local_player|ES_(?:OA|XA|EA|ElLa|DelDela|A|O)|Get(?:SheHe|HerHis|HerHim|WomanMan|WomenMen)|você|vocês", re.I)
DOMAIN_RE = re.compile(r"culture|religion|faith|artifact|activity|title|law|government|trait|accolade|nickname|dynasty|house|court|building|war|scheme", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|memory|interaction|activity|journey|travel|petition|scheme|flavor", re.I)
TOOLTIP_RE = re.compile(r"tooltip|_tt\b|_tt$|requirement|required|trigger|available|can_|cannot|unlock_tt|template_tt", re.I)
EFFECT_RE = re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#low|#high|effect", re.I)
TITLE_RE = re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|vassal|liege", re.I)
TRAIT_RE = re.compile(r"trait|modifier|accolade|knight|descriptor|ArtifactAdverb|ArtifactBookContentQuality", re.I)
NAME_RE = re.compile(r"name|nickname|dynasty|house|epithet|GetName|Muhammad|Cicero|Kalila|Dimna", re.I)
RESIDUAL_RE = re.compile(r"\b(the|your|you|their|has|will|cannot|consiguio|consiguió|ganaste|tendras|será|mas|más|muy|fácil|facil)\b|NÃ|Ãƒ|Â", re.I)

RECENT_SATURATION = [
    {
        "branch": "dynamic_semantic_exact_combo",
        "reviewed": 240,
        "ready": 0,
        "apply": 0,
        "note": "614 eligible, top split produced custom_loc/residual/requirement/domain/effect rather than closure",
    },
    {
        "branch": "dynamic_semantic_residual",
        "reviewed": 55,
        "ready": 0,
        "apply": 0,
        "note": "residual became concept/custom_loc/domain, not safe repair",
    },
    {
        "branch": "dynamic_semantic_custom_loc",
        "reviewed": 55,
        "ready": 0,
        "apply": 0,
        "note": "custom_loc split to scope_getter/name_title/effect/gender/event",
    },
    {
        "branch": "custom_loc_scope_getter",
        "reviewed": 21,
        "ready": 0,
        "apply": 0,
        "note": "event_context reached 10, then narrowed to 6 trait/descriptor and 4 local_player",
    },
    {
        "branch": "custom_loc_event_trait_modifier",
        "reviewed": 6,
        "ready": 0,
        "apply": 0,
        "note": "descriptor lexical knowledge, not direct lifecycle/apply",
    },
]

POLICY_CANDIDATES = {
    "scope_getter_policy": ("dynamic_ck3_expression_microagent", "semantic_review_router"),
    "custom_loc_scope_policy": ("dynamic_ck3_expression_microagent", "semantic_review_router"),
    "local_player_policy": ("gender_token_microagent", "local_player_context_microagent"),
    "actor_target_policy": ("dynamic_ck3_expression_microagent", "semantic_review_router"),
    "requirement_tooltip_policy": ("short_label_style_microagent", "semantic_review_router"),
    "effect_list_multiline_policy": ("short_label_style_microagent", "dynamic_ck3_expression_microagent"),
    "title_law_policy": ("title_policy_microagent", "dynamic_ck3_expression_microagent"),
    "trait_accolade_policy": ("dynamic_ck3_expression_microagent", "semantic_review_router"),
    "descriptor_lexical_policy": ("dynamic_ck3_expression_microagent", "semantic_review_router"),
    "domain_context_composer": ("semantic_review_router", "culture_semantic_microagent", "religion_semantic_microagent"),
    "event_context_composer": ("semantic_review_router", "long_text_composer"),
    "semantic_context_composer": ("semantic_review_router",),
}


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


def fetch_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
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
            s.needs_reopen,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.priority_score,
            out.portuguese_text AS output_text,
            src.spanish_text,
            src.english_text,
            src.old_text
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


def text_blob(segment_rows: list[dict[str, Any]]) -> str:
    first = segment_rows[0]
    return " ".join(
        str(value or "")
        for value in (
            first.get("relative_path"),
            first.get("source_key"),
            first.get("output_text"),
            first.get("spanish_text"),
            first.get("english_text"),
            " ".join(row.get("issue_family") or "" for row in segment_rows),
            " ".join(row.get("issue_kind") or "" for row in segment_rows),
        )
    )


def family_tuple(segment_rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(row["issue_family"]) for row in segment_rows}))


def classify_lane(segment_rows: list[dict[str, Any]]) -> str:
    families = set(family_tuple(segment_rows))
    first = segment_rows[0]
    blob = text_blob(segment_rows)
    if int(first.get("needs_reopen") or 0) == 1 and int(first.get("confirmed_matches_output") or 0) == 1:
        if not any(pattern.search(blob) for pattern in (CK3_DYNAMIC_RE, DOMAIN_RE, EVENT_RE, RESIDUAL_RE)):
            return "false_reopen_or_lifecycle_candidate"
    if "gender_token_microagent" in families or "local_player_context_microagent" in families or GENDER_RE.search(blob):
        return "gender_or_local_player"
    if "dynamic_ck3_expression_microagent" in families and CUSTOM_SCOPE_RE.search(blob):
        return "custom_loc_or_scope_getter"
    if "dynamic_ck3_expression_microagent" in families or CK3_DYNAMIC_RE.search(blob):
        return "dynamic_expression"
    if TOOLTIP_RE.search(blob):
        return "requirement_tooltip"
    if EFFECT_RE.search(blob):
        return "effect_list_or_multiline"
    if TITLE_RE.search(blob) or "title_policy_microagent" in families:
        return "title_law_government"
    if TRAIT_RE.search(blob):
        return "trait_modifier_accolade"
    if NAME_RE.search(blob) or "nickname_name_policy" in families:
        return "name_dynasty_nickname"
    if RESIDUAL_RE.search(blob) or "spanish_residual_microagent" in families:
        return "residual_visible"
    if "short_label_style_microagent" in families:
        return "short_label_style"
    if EVENT_RE.search(blob) or "long_text_composer" in families:
        return "event_context"
    if DOMAIN_RE.search(blob) or {"culture_semantic_microagent", "religion_semantic_microagent"} & families:
        return "domain_context"
    if "semantic_review_router" in families:
        return "semantic_plain_context"
    if "autofix_unknown_microagent" in families:
        return "autofix_unknown"
    if "model_training_microagent" in families:
        return "model_training_candidate"
    return "blocked_uncertain"


def mechanism_for_lane(lane: str) -> str:
    return {
        "false_reopen_or_lifecycle_candidate": "read-only lifecycle guard, then protected lifecycle only if cohort is clean",
        "dynamic_expression": "symbolic CK3 token parser plus narrow dynamic policy",
        "custom_loc_or_scope_getter": "custom loc/scope parser with reusable actor/title/descriptor subpolicies",
        "gender_or_local_player": "gender/local-player policy with perspective tests",
        "domain_context": "retrieval-backed domain composer by path/key/family",
        "event_context": "event context composer with actor/recipient perspective extraction",
        "requirement_tooltip": "requirement tooltip policy with condition/effect grammar",
        "effect_list_or_multiline": "multiline/effect-list normalizer before semantic review",
        "title_law_government": "title/law/government glossary plus CK3 getter policy",
        "trait_modifier_accolade": "trait/accolade/descriptive lexical policy",
        "name_dynasty_nickname": "name/dynasty/nickname policy with named-entity guard",
        "residual_visible": "repair only after dependency filters prove no dynamic/context blocker",
        "short_label_style": "compact UI short-label policy, batch by surface",
        "semantic_plain_context": "semantic composer with retrieved parallels",
        "autofix_unknown": "single-family triage into named policies before apply",
        "model_training_candidate": "active-learning batch for supervised labels",
        "blocked_uncertain": "human review or new evidence collection",
    }[lane]


def build_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = group_by_segment(rows)
    issue_distribution = Counter()
    family_segments: dict[str, set[int]] = defaultdict(set)
    family_issues = Counter()
    combo_rank = Counter()
    single_family = Counter()
    lane_segments: dict[str, set[int]] = defaultdict(set)
    lane_family_counts: dict[str, Counter[str]] = defaultdict(Counter)
    lane_combo_counts: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    lane_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for segment_id, segment_rows in grouped.items():
        issue_count = len(segment_rows)
        issue_distribution["1_issue" if issue_count == 1 else "2_issues" if issue_count == 2 else "3_plus_issues"] += 1
        combo = family_tuple(segment_rows)
        combo_rank[combo] += 1
        if len(combo) == 1:
            single_family[combo[0]] += 1
        for row in segment_rows:
            family = str(row["issue_family"])
            family_segments[family].add(segment_id)
            family_issues[family] += 1

        lane = classify_lane(segment_rows)
        lane_segments[lane].add(segment_id)
        lane_combo_counts[lane][combo] += 1
        for family in combo:
            lane_family_counts[lane][family] += 1
        if len(lane_examples[lane]) < 5:
            first = segment_rows[0]
            lane_examples[lane].append(
                {
                    "segment_id": segment_id,
                    "source_key": first.get("source_key"),
                    "relative_path": first.get("relative_path"),
                }
            )

    family_rank = Counter({family: len(segment_ids) for family, segment_ids in family_segments.items()})
    return {
        "grouped": grouped,
        "pending_segments_crossed": len(grouped),
        "open_issues_crossed": len(rows),
        "issue_distribution": issue_distribution,
        "family_rank": family_rank,
        "family_issues": family_issues,
        "combo_rank": combo_rank,
        "single_family": single_family,
        "lane_counts": Counter({lane: len(ids) for lane, ids in lane_segments.items()}),
        "lane_family_counts": lane_family_counts,
        "lane_combo_counts": lane_combo_counts,
        "lane_examples": lane_examples,
    }


def estimate_policy_volume(policy: str, diagnostic: dict[str, Any]) -> int:
    family_rank: Counter[str] = diagnostic["family_rank"]
    lane_counts: Counter[str] = diagnostic["lane_counts"]
    if policy == "scope_getter_policy":
        return lane_counts["custom_loc_or_scope_getter"] + lane_counts["dynamic_expression"]
    if policy == "custom_loc_scope_policy":
        return lane_counts["custom_loc_or_scope_getter"]
    if policy == "local_player_policy":
        return lane_counts["gender_or_local_player"]
    if policy == "actor_target_policy":
        return max(0, lane_counts["event_context"] // 3 + lane_counts["custom_loc_or_scope_getter"] // 4)
    if policy == "requirement_tooltip_policy":
        return lane_counts["requirement_tooltip"] + family_rank["short_label_style_microagent"] // 5
    if policy == "effect_list_multiline_policy":
        return lane_counts["effect_list_or_multiline"] + family_rank["short_label_style_microagent"] // 8
    if policy == "title_law_policy":
        return lane_counts["title_law_government"] + family_rank["title_policy_microagent"]
    if policy == "trait_accolade_policy":
        return lane_counts["trait_modifier_accolade"]
    if policy == "descriptor_lexical_policy":
        return max(5, lane_counts["trait_modifier_accolade"] // 3)
    if policy == "domain_context_composer":
        return lane_counts["domain_context"]
    if policy == "event_context_composer":
        return lane_counts["event_context"]
    if policy == "semantic_context_composer":
        return lane_counts["semantic_plain_context"] + family_rank["semantic_review_router"] // 5
    return 0


def recent_branch_hits(policy: str) -> int:
    hits = {
        "scope_getter_policy": 4,
        "custom_loc_scope_policy": 4,
        "local_player_policy": 3,
        "actor_target_policy": 3,
        "requirement_tooltip_policy": 5,
        "effect_list_multiline_policy": 5,
        "title_law_policy": 4,
        "trait_accolade_policy": 3,
        "descriptor_lexical_policy": 1,
        "domain_context_composer": 5,
        "event_context_composer": 4,
        "semantic_context_composer": 6,
    }
    return hits.get(policy, 0)


def policy_risk(policy: str) -> str:
    if policy in {"descriptor_lexical_policy", "effect_list_multiline_policy", "requirement_tooltip_policy"}:
        return "medium"
    if policy in {"scope_getter_policy", "custom_loc_scope_policy", "local_player_policy", "actor_target_policy"}:
        return "high"
    if policy.endswith("_composer"):
        return "medium_high"
    return "medium"


def next_test(policy: str) -> str:
    return {
        "scope_getter_policy": "build parser-backed scope/getter cohort with no apply, then measure ready rate",
        "custom_loc_scope_policy": "review custom_loc+scope by path family and isolate reusable helper names",
        "local_player_policy": "run local-player perspective audit across gender/dynamic combos",
        "actor_target_policy": "create actor/target/recipient event-context policy review",
        "requirement_tooltip_policy": "review requirement tooltip cohort with condition/effect surface split",
        "effect_list_multiline_policy": "review effect-list multiline cohort before any semantic/apply attempt",
        "title_law_policy": "review title/law getters with glossary guard",
        "trait_accolade_policy": "review trait/accolade/domain descriptors as lexical policy",
        "descriptor_lexical_policy": "record as reusable lexical component, do not lifecycle yet",
        "domain_context_composer": "sample culture/religion/domain context with retrieval examples",
        "event_context_composer": "sample event desc/options with actor/recipient perspective extraction",
        "semantic_context_composer": "route semantic plain context through retrieval-backed composer",
    }[policy]


def choose_strategies(diagnostic: dict[str, Any]) -> list[dict[str, Any]]:
    combo_rank: Counter[tuple[str, ...]] = diagnostic["combo_rank"]
    lane_counts: Counter[str] = diagnostic["lane_counts"]
    return [
        {
            "priority": 1,
            "name": "macro_lane_router_architecture_prompt",
            "rationale": "current large families overlap; route by macro-lane before opening more micro-splits",
            "expected_gain_band": "high_architecture_gain",
        },
        {
            "priority": 2,
            "name": "semantic_short_label_or_autofix_unknown_cohort_review",
            "rationale": f"largest exact combos remain {combo_rank.most_common(2)}; pick one broad cohort but classify by lanes first",
            "expected_gain_band": "medium_to_high",
        },
        {
            "priority": 3,
            "name": "parser_backed_dynamic_expression_design_prompt",
            "rationale": f"dynamic/custom/scope lanes cover {lane_counts['custom_loc_or_scope_getter'] + lane_counts['dynamic_expression']} pending segments and recent splits saturated",
            "expected_gain_band": "medium_architecture_gain",
        },
    ]


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_global_pending_architecture_review_after_maturation"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def write_jsonl(path: Path, run_info: dict[str, Any], diagnostic: dict[str, Any], strategies: list[dict[str, Any]]) -> None:
    records: list[dict[str, Any]] = [
        {
            "record_type": "summary",
            "segment_state_run_id": run_info["segment_state_run_id"],
            "ledger_run_id": run_info["ledger_run_id"],
            "pending_segments": diagnostic["pending_segments_crossed"],
            "open_issues": diagnostic["open_issues_crossed"],
            "issue_distribution": dict(diagnostic["issue_distribution"]),
        }
    ]
    for rank, (family, count) in enumerate(diagnostic["family_rank"].most_common(50), 1):
        records.append({"record_type": "family", "family": family, "pending_segments": count, "open_issues": diagnostic["family_issues"][family], "rank": rank})
    for rank, (families, count) in enumerate(diagnostic["combo_rank"].most_common(75), 1):
        records.append({"record_type": "combination", "families": list(families), "pending_segments": count, "rank": rank})
    for rank, (family, count) in enumerate(diagnostic["single_family"].most_common(50), 1):
        records.append({"record_type": "single_family", "family": family, "pending_segments": count, "rank": rank})
    total = diagnostic["pending_segments_crossed"]
    for lane in MACRO_LANES:
        count = diagnostic["lane_counts"][lane]
        records.append(
            {
                "record_type": "lane",
                "lane": lane,
                "pending_segments": count,
                "percent_pending": round((count / total * 100) if total else 0, 2),
                "top_families": diagnostic["lane_family_counts"][lane].most_common(5),
                "top_combinations": [(list(combo), n) for combo, n in diagnostic["lane_combo_counts"][lane].most_common(5)],
                "examples": diagnostic["lane_examples"][lane],
                "recommended_mechanism": mechanism_for_lane(lane),
            }
        )
    for policy in POLICY_CANDIDATES:
        records.append(
            {
                "record_type": "reusable_policy",
                "policy": policy,
                "recent_branch_hits": recent_branch_hits(policy),
                "estimated_volume": estimate_policy_volume(policy, diagnostic),
                "risk": policy_risk(policy),
                "recommendation": next_test(policy),
            }
        )
    for item in RECENT_SATURATION:
        records.append({"record_type": "saturation_signal", **item})
    for strategy in strategies:
        records.append({"record_type": "strategy", **strategy})
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, run_info: dict[str, Any], diagnostic: dict[str, Any], strategies: list[dict[str, Any]]) -> None:
    total = diagnostic["pending_segments_crossed"]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Resumo executivo\n")
        handle.write(
            "A rede ainda tem volume grande, mas os ramos dinamicos/semanticos recentes estao saturando como micro-splits: mapeiam conhecimento, quase nao geram ready/apply.\n"
        )
        handle.write("Recomendacao: parar de abrir galhos pequenos como default e introduzir roteador por macro-lane + componentes reutilizaveis.\n\n")

        handle.write("Estado atual\n")
        handle.write(f"segment_state_run_id: {run_info['segment_state_run_id']}\n")
        handle.write(f"ledger_run_id: {run_info['ledger_run_id']}\n")
        handle.write(f"pending_segments_crossed: {total}\n")
        handle.write(f"open_issues_in_pending: {diagnostic['open_issues_crossed']}\n")
        for key, value in diagnostic["issue_distribution"].items():
            handle.write(f"- {key}: {value}\n")
        handle.write("\n")

        handle.write("Pendencias por familia\n")
        for family, count in diagnostic["family_rank"].most_common(15):
            handle.write(f"- {family}: {count} segmentos, {diagnostic['family_issues'][family]} issues\n")
        handle.write("\n")

        handle.write("Pendencias por combinacao\n")
        handle.write("Top 20 exatas:\n")
        for families, count in diagnostic["combo_rank"].most_common(20):
            handle.write(f"- {' + '.join(families)}: {count}\n")
        handle.write("\nTop 20 com 2+ familias:\n")
        multi = [(families, count) for families, count in diagnostic["combo_rank"].most_common() if len(families) >= 2]
        for families, count in multi[:20]:
            handle.write(f"- {' + '.join(families)}: {count}\n")
        handle.write("\nTop 15 single-family:\n")
        for family, count in diagnostic["single_family"].most_common(15):
            handle.write(f"- {family}: {count}\n")
        handle.write("\n")

        handle.write("Macro-lanes restantes\n")
        for lane, count in diagnostic["lane_counts"].most_common():
            pct = (count / total * 100) if total else 0
            handle.write(f"- {lane}: {count} ({pct:.1f}%)\n")
            handle.write(f"  mecanismo: {mechanism_for_lane(lane)}\n")
            top_combos = diagnostic["lane_combo_counts"][lane].most_common(3)
            if top_combos:
                handle.write(f"  combos: {', '.join(f'{'+'.join(combo)}={n}' for combo, n in top_combos)}\n")
            examples = diagnostic["lane_examples"][lane][:5]
            if examples:
                handle.write(f"  exemplos: {examples}\n")
        handle.write("\n")

        handle.write("Saturacao detectada\n")
        for item in RECENT_SATURATION:
            handle.write(f"- {item['branch']}: reviewed={item['reviewed']}, ready={item['ready']}, apply={item['apply']}; {item['note']}\n")
        handle.write("- residual_repair recente virou contexto/dinamica em vez de reparo mecanico seguro.\n")
        handle.write("- combos grandes continuam grandes, mas dynamic+semantic demonstrou baixo fechamento direto sem parser/compositor.\n\n")

        handle.write("Policies reutilizaveis candidatas\n")
        for policy in POLICY_CANDIDATES:
            handle.write(
                f"- {policy}: hits_recentes={recent_branch_hits(policy)}, volume_estimado={estimate_policy_volume(policy, diagnostic)}, "
                f"risco={policy_risk(policy)}, proximo_teste={next_test(policy)}\n"
            )
        handle.write("\n")

        handle.write("Arquitetura recomendada\n")
        handle.write("- Mixture-of-experts governado por ledger: o ledger escolhe macro-lane e so depois microagente.\n")
        handle.write("- Roteador hierarquico por macro-lane antes de subpolicy: evita galhos de 5-10 itens sem ganho direto.\n")
        handle.write("- Parser simbolico CK3 para Custom, Select_CString, Scope/Getters, Concept e ScriptValue antes de review semantico.\n")
        handle.write("- Retrieval/context composer por relative_path, source_key, familia e padrao de token.\n")
        handle.write("- Active learning: pequenas filas humanas por incerteza + impacto, especialmente descriptor/local_player/actor_target.\n")
        handle.write("- Promotion gates por zero false-safe e ganho de cobertura por cohort, nao so F1 global.\n")
        handle.write("- Memoria de policies reaproveitaveis por subpolicy para transformar conhecimento de galhos pequenos em componente.\n\n")

        handle.write("Proximos prompts recomendados\n")
        for strategy in strategies:
            handle.write(f"{strategy['priority']}. {strategy['name']}: {strategy['rationale']} ({strategy['expected_gain_band']})\n")
        handle.write("\n")

        handle.write("Quando rodar producao full\n")
        handle.write("- Agora: nao vale como proximo passo principal; os snapshots/read-only ja mostram saturacao estrutural, nao falta medicao de output.\n")
        handle.write("- Full mede interacao real entre geracao, validadores, apply gates e diffs que diagnosticos nao medem.\n")
        handle.write("- Ajuda pouco quando os maiores bloqueios sao contexto/dinamica/policies ainda nao implementadas.\n")
        handle.write("- E obrigatoria apos trocar source/output em update do CK3 ou antes de uma entrega externa com cohort novo.\n")
        handle.write("- Antes da atualizacao do jogo: rodar full so depois de um componente reutilizavel mostrar ganho em dry-run com zero false-safe.\n")
        handle.write("- Apos atualizar CK3: rodar full para reindex/snapshot novo e recalibrar ledger, mas isso fica fora deste prompt.\n\n")

        handle.write("Validacoes\n")
        handle.write("- Banco aberto em modo read-only.\n")
        handle.write("- JSONL agregado, sem uma linha por segmento.\n")
        handle.write("- Sem lifecycle, apply, segment-state, issue-ledger, confirmations, reindex, treino, source/output changes.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Global pending architecture review after maturation.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()

    conn = connect_readonly()
    segment_run = fetch_run(conn, "segment_state_runs", args.segment_state_run_id)
    ledger_run = fetch_run(conn, "ml_issue_ledger_runs", args.ledger_run_id)
    rows = fetch_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    diagnostic = build_diagnostic(rows)
    run_info = {
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "segment_run_finished_at": segment_run.get("finished_at"),
        "ledger_run_finished_at": ledger_run.get("finished_at"),
    }
    strategies = choose_strategies(diagnostic)
    txt_path, jsonl_path = output_paths()
    write_txt(txt_path, run_info, diagnostic, strategies)
    write_jsonl(jsonl_path, run_info, diagnostic, strategies)

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"pending_segments_crossed: {diagnostic['pending_segments_crossed']}")
    print(f"open_issues_in_pending: {diagnostic['open_issues_crossed']}")
    print("top_families:")
    for family, count in diagnostic["family_rank"].most_common(5):
        print(f"  {family}: {count}")
    print("top_combinations:")
    for families, count in diagnostic["combo_rank"].most_common(5):
        print(f"  {' + '.join(families)}: {count}")
    print("top_lanes:")
    for lane, count in diagnostic["lane_counts"].most_common(5):
        print(f"  {lane}: {count}")


if __name__ == "__main__":
    main()
