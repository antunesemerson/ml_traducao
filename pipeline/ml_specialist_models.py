from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import db
import ml_holdout_eval
import ml_train_risk
from ml_build_dataset import update_run_counts
from ml_train_risk import (
    DEFAULT_TRAIN_STRATEGY,
    DELTA_FEATURE_SET,
    LANGUAGE_FEATURE_SET,
    risk_label,
)


RULE_VERSION = "ml_specialist_models_v1"
DATASET_VERSION = "specialist_bootstrap_v1"


@dataclass(frozen=True)
class SpecialistConfig:
    name: str
    description: str
    scope_sql: str
    default_feature_set: str
    default_train_strategy: str
    default_safe_threshold: float
    default_safe_multiplier: int
    holdout_min_negative: int


SPECIALISTS: dict[str, SpecialistConfig] = {
    "titles": SpecialistConfig(
        name="specialist_titles",
        description=(
            "Titles, title adjectives, cultural title names, toponyms, and culture title labels."
        ),
        scope_sql=(
            "relative_path IN ("
            "'titles_l_spanish.yml', "
            "'titles_cultural_names_l_spanish.yml', "
            "'culture/culture_titles_l_spanish.yml'"
            ")"
        ),
        default_feature_set=DELTA_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.93,
        default_safe_multiplier=5,
        holdout_min_negative=20,
    ),
    "title_names": SpecialistConfig(
        name="specialist_title_names",
        description=(
            "Landed-title proper names and toponyms in titles_l_spanish.yml, excluding adjective keys."
        ),
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND source_key NOT LIKE '%\\_adj%' ESCAPE '\\' "
            "AND source_key NOT LIKE '%\\_pre' ESCAPE '\\' "
            "AND ("
            "source_key LIKE 'b\\_%' ESCAPE '\\' OR "
            "source_key LIKE 'c\\_%' ESCAPE '\\' OR "
            "source_key LIKE 'd\\_%' ESCAPE '\\' OR "
            "source_key LIKE 'k\\_%' ESCAPE '\\' OR "
            "source_key LIKE 'e\\_%' ESCAPE '\\'"
            ")"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.96,
        default_safe_multiplier=5,
        holdout_min_negative=20,
    ),
    "title_adjectives": SpecialistConfig(
        name="specialist_title_adjectives",
        description="Landed-title adjective keys, including demonyms and contextual adjective forms.",
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND source_key LIKE '%\\_adj%' ESCAPE '\\'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.90,
        default_safe_multiplier=5,
        holdout_min_negative=10,
    ),
    "title_prefixes": SpecialistConfig(
        name="specialist_title_prefixes",
        description=(
            "Landed-title prefix/demonym forms ending in _pre, such as Afro, Franco, "
            "oesteeslavo, and similar pre-title cultural modifiers."
        ),
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND source_key LIKE '%\\_pre' ESCAPE '\\'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.95,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "title_baronies": SpecialistConfig(
        name="specialist_title_baronies",
        description="Landed-title proper names with b_ keys, mostly barony/local holding names.",
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND instr(source_key, '_adj') = 0 "
            "AND substr(source_key, -4) != '_pre' "
            "AND substr(source_key, 1, 2) = 'b_'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.93,
        default_safe_multiplier=5,
        holdout_min_negative=10,
    ),
    "title_counties": SpecialistConfig(
        name="specialist_title_counties",
        description="Landed-title proper names with c_ keys, mostly county/local region names.",
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND instr(source_key, '_adj') = 0 "
            "AND substr(source_key, -4) != '_pre' "
            "AND substr(source_key, 1, 2) = 'c_'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.93,
        default_safe_multiplier=5,
        holdout_min_negative=10,
    ),
    "title_duchies": SpecialistConfig(
        name="specialist_title_duchies",
        description="Landed-title proper names with d_ keys, duchy-level names and regional exceptions.",
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND instr(source_key, '_adj') = 0 "
            "AND substr(source_key, -4) != '_pre' "
            "AND substr(source_key, 1, 2) = 'd_'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.95,
        default_safe_multiplier=5,
        holdout_min_negative=10,
    ),
    "title_kingdoms": SpecialistConfig(
        name="specialist_title_kingdoms",
        description="Landed-title proper names with k_ keys, kingdom-level names with higher localization ambiguity.",
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND instr(source_key, '_adj') = 0 "
            "AND substr(source_key, -4) != '_pre' "
            "AND substr(source_key, 1, 2) = 'k_'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.97,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "title_empires": SpecialistConfig(
        name="specialist_title_empires",
        description="Landed-title proper names with e_ keys, empire-level names and major contextual exceptions.",
        scope_sql=(
            "relative_path = 'titles_l_spanish.yml' "
            "AND instr(source_key, '_adj') = 0 "
            "AND substr(source_key, -4) != '_pre' "
            "AND substr(source_key, 1, 2) = 'e_'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.97,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "title_cultural_names": SpecialistConfig(
        name="specialist_title_cultural_names",
        description="Cultural title name overrides from titles_cultural_names_l_spanish.yml.",
        scope_sql="relative_path = 'titles_cultural_names_l_spanish.yml'",
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.98,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "culture_title_labels": SpecialistConfig(
        name="specialist_culture_title_labels",
        description="Culture title labels and ruler-title context from culture/culture_titles_l_spanish.yml.",
        scope_sql="relative_path = 'culture/culture_titles_l_spanish.yml'",
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.90,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "religion": SpecialistConfig(
        name="specialist_religion",
        description=(
            "Religion names, gods, possessives, old/adherent/name/adj labels, "
            "excluding narrow boundaries owned by subagents."
        ),
        scope_sql=(
            "relative_path LIKE 'religion/%' AND NOT ("
            "relative_path = 'religion/religion_paganism_l_spanish.yml' "
            "AND source_key LIKE 'acham_%_possessive'"
            ")"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.95,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "religion_bosnian_terms": SpecialistConfig(
        name="specialist_religion_bosnian_terms",
        description="Bosnian Church terms that often preserve original religious titles.",
        scope_sql=(
            "relative_path = 'religion/religion_christianity_l_spanish.yml' "
            "AND source_key LIKE 'bosnian_%'"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.97,
        default_safe_multiplier=5,
        holdout_min_negative=3,
    ),
    "religion_sufri": SpecialistConfig(
        name="specialist_religion_sufri",
        description="Sufri/Sufrism labels and descriptions, distinct from Sufi/Sufism.",
        scope_sql="relative_path = 'religion/religion_islam_l_spanish.yml' AND source_key LIKE 'sufri_%'",
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.97,
        default_safe_multiplier=5,
        holdout_min_negative=2,
    ),
    "religion_possessive_gods": SpecialistConfig(
        name="specialist_religion_possessive_gods",
        description=(
            "Religious possessive fields that need safe PT-BR prepositions, "
            "excluding known Acham paganism boundary rows."
        ),
        scope_sql=(
            "relative_path LIKE 'religion/%' AND ("
            "source_key LIKE '%_god_name_possessive' OR "
            "source_key LIKE '%_deity_name_possessive' OR "
            "source_key LIKE '%_devil_name_possessive' OR "
            "source_key LIKE '%_death_deity_%' OR "
            "source_key LIKE '%_name_possessive' OR "
            "source_key LIKE '%_possessive'"
            ") AND NOT ("
            "relative_path = 'religion/religion_paganism_l_spanish.yml' "
            "AND source_key LIKE 'acham_%_possessive'"
            ")"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.96,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "religion_preserved_terms": SpecialistConfig(
        name="specialist_religion_preserved_terms",
        description=(
            "Small religion-specific terms that may be preserved instead of translated, "
            "excluding possessive fields owned by religion_possessive_gods."
        ),
        scope_sql=(
            "relative_path LIKE 'religion/%' AND ("
            "source_key LIKE 'dab_qhuas_%' OR "
            "source_key LIKE 'tolotang_%' OR "
            "source_key LIKE '%_priest' OR "
            "source_key LIKE '%_afterlife'"
            ") AND NOT ("
            "source_key LIKE '%_god_name_possessive' OR "
            "source_key LIKE '%_deity_name_possessive' OR "
            "source_key LIKE '%_devil_name_possessive' OR "
            "source_key LIKE '%_death_deity_%' OR "
            "source_key LIKE '%divine_realm%' OR "
            "source_key LIKE '%_name_possessive' OR "
            "source_key LIKE '%_possessive'"
            ")"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.97,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "religion_divine_realm_contextual_boundary": SpecialistConfig(
        name="specialist_religion_divine_realm_contextual_boundary",
        description=(
            "Divine-realm religion fields where preserved sacred names and contextual PT-BR "
            "translations need a narrower boundary before broad automation."
        ),
        scope_sql="relative_path LIKE 'religion/%' AND source_key LIKE '%divine_realm%'",
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.98,
        default_safe_multiplier=5,
        holdout_min_negative=4,
    ),
    "religion_dab_qhuas_terms": SpecialistConfig(
        name="specialist_religion_dab_qhuas_terms",
        description=(
            "Dab Qhuas preserved religious terms, excluding divine-realm aliases "
            "and possessive fields owned by narrower boundaries."
        ),
        scope_sql=(
            "relative_path = 'religion/religion_paganism_l_spanish.yml' "
            "AND source_key LIKE 'dab_qhuas_%' "
            "AND source_key NOT LIKE '%divine_realm%' "
            "AND NOT ("
            "source_key LIKE '%_god_name_possessive' OR "
            "source_key LIKE '%_deity_name_possessive' OR "
            "source_key LIKE '%_devil_name_possessive' OR "
            "source_key LIKE '%_death_deity_%' OR "
            "source_key LIKE '%_name_possessive' OR "
            "source_key LIKE '%_possessive'"
            ")"
        ),
        default_feature_set=LANGUAGE_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.97,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "select_cstring_ep3_laamp_roles": SpecialistConfig(
        name="specialist_select_cstring_ep3_laamp_roles",
        description=(
            "EP3 LAAMP Select_CString role scenes, including contextual role nouns, "
            "gendered articles, and scene-specific rewrites."
        ),
        scope_sql=(
            "relative_path = 'dlc/ep3/ep3_laamp_decision_events_l_spanish.yml' "
            "AND (english_text LIKE '%Select_CString%' "
            "OR spanish_text LIKE '%Select_CString%' "
            "OR old_text LIKE '%Select_CString%')"
        ),
        default_feature_set=DELTA_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.98,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "select_cstring_ep3_laamp_spanish_role_boundary": SpecialistConfig(
        name="specialist_select_cstring_ep3_laamp_spanish_role_boundary",
        description=(
            "EP3 LAAMP Select_CString role branches that still carry Spanish articles, "
            "prepositions, or plural adjectives inside dynamic alternatives."
        ),
        scope_sql=(
            "relative_path = 'dlc/ep3/ep3_laamp_decision_events_l_spanish.yml' "
            "AND old_text LIKE '%Select_CString%' "
            "AND (old_text LIKE '%''la %' "
            "OR old_text LIKE '%''el %' "
            "OR old_text LIKE '%''las %' "
            "OR old_text LIKE '%''los %' "
            "OR old_text LIKE '%''una %' "
            "OR old_text LIKE '%''un %' "
            "OR old_text LIKE '%''a la%' "
            "OR old_text LIKE '%''a las%' "
            "OR old_text LIKE '%''a los%' "
            "OR old_text LIKE '%''con la%' "
            "OR old_text LIKE '%''con el%' "
            "OR old_text LIKE '%enfadadas%' "
            "OR old_text LIKE '%enfadados%')"
        ),
        default_feature_set=DELTA_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.99,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
    "select_cstring_ep3_laamp_role_correction_candidate": SpecialistConfig(
        name="specialist_select_cstring_ep3_laamp_role_corrections",
        description=(
            "Reviewed PT-BR correction candidates for EP3 LAAMP Select_CString role "
            "branches after the Spanish-role boundary blocks unsafe text."
        ),
        scope_sql=(
            "relative_path = 'dlc/ep3/ep3_laamp_decision_events_l_spanish.yml' "
            "AND old_text LIKE '%Select_CString%' "
            "AND (old_text LIKE '%''la %' "
            "OR old_text LIKE '%''el %' "
            "OR old_text LIKE '%''las %' "
            "OR old_text LIKE '%''los %' "
            "OR old_text LIKE '%''una %' "
            "OR old_text LIKE '%''un %' "
            "OR old_text LIKE '%''a la%' "
            "OR old_text LIKE '%''a las%' "
            "OR old_text LIKE '%''a los%' "
            "OR old_text LIKE '%''con la%' "
            "OR old_text LIKE '%''con el%' "
            "OR old_text LIKE '%enfadadas%' "
            "OR old_text LIKE '%enfadados%')"
        ),
        default_feature_set=DELTA_FEATURE_SET,
        default_train_strategy=DEFAULT_TRAIN_STRATEGY,
        default_safe_threshold=0.99,
        default_safe_multiplier=5,
        holdout_min_negative=5,
    ),
}


SPECIALIST_GROUPS: dict[str, list[str]] = {
    "all": ["titles", "religion"],
    "title_subspecialists": [
        "title_names",
        "title_adjectives",
        "title_prefixes",
        "title_cultural_names",
        "culture_title_labels",
    ],
    "title_rank_subspecialists": [
        "title_baronies",
        "title_counties",
        "title_duchies",
        "title_kingdoms",
        "title_empires",
    ],
    "title_promising_subspecialists": [
        "title_names",
        "title_adjectives",
        "culture_title_labels",
    ],
    "religion_subspecialists": [
        "religion_bosnian_terms",
        "religion_sufri",
        "religion_possessive_gods",
        "religion_preserved_terms",
        "religion_divine_realm_contextual_boundary",
        "religion_dab_qhuas_terms",
    ],
    "religion_promising_subspecialists": [
        "religion_bosnian_terms",
        "religion_possessive_gods",
        "religion_preserved_terms",
        "religion_divine_realm_contextual_boundary",
        "religion_dab_qhuas_terms",
    ],
    "religion_preserved_subspecialists": [
        "religion_preserved_terms",
        "religion_divine_realm_contextual_boundary",
        "religion_dab_qhuas_terms",
    ],
    "select_cstring_ep3_laamp_subspecialists": [
        "select_cstring_ep3_laamp_roles",
        "select_cstring_ep3_laamp_spanish_role_boundary",
        "select_cstring_ep3_laamp_role_correction_candidate",
    ],
    "all_with_title_subspecialists": [
        "titles",
        "religion",
        "title_names",
        "title_adjectives",
        "title_prefixes",
        "title_cultural_names",
        "culture_title_labels",
    ],
    "all_with_religion_subspecialists": [
        "titles",
        "religion",
        "religion_bosnian_terms",
        "religion_sufri",
        "religion_possessive_gods",
        "religion_preserved_terms",
        "religion_divine_realm_contextual_boundary",
        "religion_dab_qhuas_terms",
    ],
    "all_with_token_gate_subspecialists": [
        "titles",
        "religion",
        "select_cstring_ep3_laamp_roles",
        "select_cstring_ep3_laamp_spanish_role_boundary",
        "select_cstring_ep3_laamp_role_correction_candidate",
    ],
    "operational_title_religion_v1": [
        "religion",
        "title_names",
        "title_adjectives",
        "title_prefixes",
        "title_cultural_names",
        "culture_title_labels",
        "title_baronies",
        "title_counties",
        "title_duchies",
        "title_kingdoms",
        "title_empires",
    ],
}


EXAMPLE_COLUMNS = [
    "segment_id",
    "relative_path",
    "source_key",
    "source_line_number",
    "english_text",
    "spanish_text",
    "old_text",
    "output_text",
    "candidate_text",
    "final_text",
    "label",
    "action_label",
    "issue_label",
    "trust_level",
    "evidence_source",
    "evidence_id",
    "confidence_score",
    "locked",
    "token_count",
    "has_english",
    "has_old",
    "text_length",
    "reasons_json",
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> str:
    if total <= 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_base_dataset_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_dataset_runs
        WHERE source_scope = 'feedback+local_learning+locked_human'
          AND finished_at IS NOT NULL
          AND total_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished base ML dataset found. Run ml-dataset first.")
    return int(row["id"])


def insert_dataset_run(
    conn,
    config: SpecialistConfig,
    base_dataset_run_id: int,
    binary_risk: bool,
    started_at: str,
) -> int:
    notes = json.dumps(
        {
            "base_dataset_run_id": base_dataset_run_id,
            "binary_risk": binary_risk,
            "description": config.description,
            "scope_sql": config.scope_sql,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cursor = conn.execute(
        """
        INSERT INTO ml_dataset_runs (
            rule_version,
            dataset_version,
            source_scope,
            notes,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, DATASET_VERSION, config.name, notes, started_at, started_at),
    )
    return int(cursor.lastrowid)


def specialist_rows(conn, base_dataset_run_id: int, config: SpecialistConfig) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {", ".join(EXAMPLE_COLUMNS)}
        FROM ml_training_examples
        WHERE run_id = ?
          AND ({config.scope_sql})
          AND candidate_text IS NOT NULL
          AND candidate_text <> ''
        ORDER BY id
        """,
        (base_dataset_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def specialist_reason(row: dict[str, Any], config: SpecialistConfig, base_dataset_run_id: int) -> str:
    try:
        original = json.loads(row.get("reasons_json") or "{}")
    except json.JSONDecodeError:
        original = {"raw": row.get("reasons_json")}
    payload = {
        "rule_version": RULE_VERSION,
        "dataset_version": DATASET_VERSION,
        "specialist": config.name,
        "base_dataset_run_id": base_dataset_run_id,
        "original": original,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def normalize_for_specialist(
    row: dict[str, Any],
    config: SpecialistConfig,
    base_dataset_run_id: int,
    binary_risk: bool,
) -> dict[str, Any]:
    normalized = dict(row)
    original_risk = risk_label(str(row.get("action_label") or ""), row.get("issue_label"))
    if binary_risk and original_risk != "auto_safe":
        normalized["action_label"] = "needs_human"
    normalized["reasons_json"] = specialist_reason(row, config, base_dataset_run_id)
    return normalized


def insert_examples(
    conn,
    run_id: int,
    rows: list[dict[str, Any]],
    created_at: str,
) -> None:
    columns = ["run_id", *EXAMPLE_COLUMNS, "created_at"]
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"""
        INSERT OR IGNORE INTO ml_training_examples (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        """,
        [
            (
                run_id,
                *[row.get(column) for column in EXAMPLE_COLUMNS],
                created_at,
            )
            for row in rows
        ],
    )


def count_risk_labels(rows: list[dict[str, Any]]) -> Counter[str]:
    return Counter(risk_label(str(row.get("action_label") or ""), row.get("issue_label")) for row in rows)


def top_counts(rows: list[dict[str, Any]], key: str, limit: int = 12) -> list[str]:
    counter = Counter(str(row.get(key) or "none") for row in rows)
    if not counter:
        return ["- none: 0"]
    return [f"- {name}: {total}" for name, total in counter.most_common(limit)]


def build_dataset(
    conn,
    config: SpecialistConfig,
    base_dataset_run_id: int,
    binary_risk: bool,
    started_at: str,
    require_two_labels: bool = True,
) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    source_rows = specialist_rows(conn, base_dataset_run_id, config)
    if not source_rows:
        raise RuntimeError(f"No examples found for {config.name}.")
    rows = [
        normalize_for_specialist(row, config, base_dataset_run_id, binary_risk=binary_risk)
        for row in source_rows
    ]
    risk_counts = count_risk_labels(rows)
    if require_two_labels and len(risk_counts) < 2:
        raise RuntimeError(f"{config.name} needs at least two risk labels; got {dict(risk_counts)}.")

    run_id = insert_dataset_run(conn, config, base_dataset_run_id, binary_risk, started_at)
    insert_examples(conn, run_id, rows, started_at)
    counts = update_run_counts(conn, run_id, now())
    return run_id, counts, rows


def run_specialist(
    config: SpecialistConfig,
    base_dataset_run_id: int | None,
    safe_threshold: float | None,
    safe_multiplier: int | None,
    feature_set: str | None,
    train_strategy: str | None,
    binary_risk: bool,
    dataset_only: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    threshold = safe_threshold if safe_threshold is not None else config.default_safe_threshold
    multiplier = safe_multiplier if safe_multiplier is not None else config.default_safe_multiplier
    features = feature_set or config.default_feature_set
    strategy = train_strategy or config.default_train_strategy

    print(f"[ml_specialist_models] Starting {config.name}")
    print(f"[ml_specialist_models] Rule version: {RULE_VERSION}")
    print(f"[ml_specialist_models] Threshold: {threshold:.2f}")
    print(f"[ml_specialist_models] Feature set: {features}")
    print(f"[ml_specialist_models] Train strategy: {strategy}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        base_dataset_run_id = base_dataset_run_id or latest_base_dataset_run_id(conn)
        dataset_run_id, counts, rows = build_dataset(
            conn,
            config,
            base_dataset_run_id,
            binary_risk=binary_risk,
            started_at=started_at,
            require_two_labels=not dataset_only,
        )
        conn.commit()

    if dataset_only:
        risk_counts = count_risk_labels(rows)
        elapsed = datetime.now() - started_at_dt
        report_lines = [
            "ML specialist dataset report",
            f"Started at: {started_at}",
            f"Elapsed: {elapsed}",
            f"Rule version: {RULE_VERSION}",
            f"Specialist: {config.name}",
            f"Description: {config.description}",
            f"Base dataset run id: {base_dataset_run_id}",
            f"Specialist dataset run id: {dataset_run_id}",
            "",
            "Dataset:",
            f"- Total examples: {counts['total']}",
            f"- Positive examples: {counts['positive']} ({percent(counts['positive'], counts['total'])})",
            f"- Negative examples: {counts['negative']} ({percent(counts['negative'], counts['total'])})",
            f"- Strong positives: {counts['strong_positive']}",
            f"- Strong negatives: {counts['strong_negative']}",
            f"- Binary risk normalization: {binary_risk}",
            "",
            "Risk labels after specialist normalization:",
            *[f"- {label}: {risk_counts[label]}" for label in sorted(risk_counts)],
            "",
            "Top paths:",
            *top_counts(rows, "relative_path"),
            "",
            "Top action labels:",
            *top_counts(rows, "action_label"),
            "",
            "Top issue labels:",
            *top_counts(rows, "issue_label"),
            "",
            "Interpretation:",
            "- This command only builds the specialist dataset; it does not train, score, promote, or write output.",
            "- Run ml-threshold-sweep on this dataset before training the next specialist candidate.",
        ]
        report_path = db.write_report(settings, f"ml_specialist_dataset_{config.name}", report_lines)
        print(f"[ml_specialist_models] Dataset run id: {dataset_run_id}")
        print(f"[ml_specialist_models] Dataset-only report: {report_path}")
        print(f"[ml_specialist_models] Done {config.name}")
        return {
            "specialist": config.name,
            "dataset_run_id": dataset_run_id,
            "model_run_id": None,
            "report_path": str(report_path),
            "holdout_error": "dataset_only",
            "risk_counts": dict(risk_counts),
        }

    model_run_id = ml_train_risk.main(
        dataset_run_id=dataset_run_id,
        safe_threshold=threshold,
        safe_multiplier=multiplier,
        feature_set=features,
        train_strategy=strategy,
        model_kind=config.name,
    )

    holdout_error = None
    try:
        ml_holdout_eval.main(
            dataset_run_id=dataset_run_id,
            safe_threshold=threshold,
            min_negative=config.holdout_min_negative,
            safe_multiplier=multiplier,
            feature_set=features,
            train_strategy=strategy,
            sample_limit=20,
        )
    except Exception as exc:  # Keep the specialist run auditable even when holdout is under-sampled.
        holdout_error = str(exc)
        print(f"[ml_specialist_models] Holdout warning for {config.name}: {holdout_error}")

    risk_counts = count_risk_labels(rows)
    elapsed = datetime.now() - started_at_dt
    report_lines = [
        "ML specialist model report",
        f"Started at: {started_at}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Specialist: {config.name}",
        f"Description: {config.description}",
        f"Base dataset run id: {base_dataset_run_id}",
        f"Specialist dataset run id: {dataset_run_id}",
        f"Model run id: {model_run_id}",
        "",
        "Dataset:",
        f"- Total examples: {counts['total']}",
        f"- Positive examples: {counts['positive']} ({percent(counts['positive'], counts['total'])})",
        f"- Negative examples: {counts['negative']} ({percent(counts['negative'], counts['total'])})",
        f"- Strong positives: {counts['strong_positive']}",
        f"- Strong negatives: {counts['strong_negative']}",
        f"- Binary risk normalization: {binary_risk}",
        "",
        "Risk labels after specialist normalization:",
        *[f"- {label}: {risk_counts[label]}" for label in sorted(risk_counts)],
        "",
        "Top paths:",
        *top_counts(rows, "relative_path"),
        "",
        "Top action labels:",
        *top_counts(rows, "action_label"),
        "",
        "Top issue labels:",
        *top_counts(rows, "issue_label"),
        "",
        "Training configuration:",
        f"- Feature set: {features}",
        f"- Train strategy: {strategy}",
        f"- Safe threshold: {threshold:.2f}",
        f"- Safe multiplier: {multiplier}",
        "",
        "Holdout:",
        f"- Error: {holdout_error or 'none'}",
        "",
        "Interpretation:",
        "- This specialist is a dry-run model; it does not write output or promote decisions.",
        "- Existing deterministic validators and human memory remain stronger than specialist votes.",
        "- Next step is selective specialist scoring plus an auditor dry-run.",
    ]
    report_path = db.write_report(settings, f"ml_specialist_models_{config.name}", report_lines)
    print(f"[ml_specialist_models] Dataset run id: {dataset_run_id}")
    print(f"[ml_specialist_models] Model run id: {model_run_id}")
    print(f"[ml_specialist_models] Report: {report_path}")
    print(f"[ml_specialist_models] Done {config.name}")
    return {
        "specialist": config.name,
        "dataset_run_id": dataset_run_id,
        "model_run_id": model_run_id,
        "report_path": str(report_path),
        "holdout_error": holdout_error,
        "risk_counts": dict(risk_counts),
    }


def main(
    specialist: str = "all",
    base_dataset_run_id: int | None = None,
    safe_threshold: float | None = None,
    safe_multiplier: int | None = None,
    feature_set: str | None = None,
    train_strategy: str | None = None,
    binary_risk: bool = True,
    dataset_only: bool = False,
) -> None:
    specialist_keys = SPECIALIST_GROUPS.get(specialist, [specialist])
    configs = [SPECIALISTS[key] for key in specialist_keys]

    results = []
    for config in configs:
        results.append(
            run_specialist(
                config,
                base_dataset_run_id=base_dataset_run_id,
                safe_threshold=safe_threshold,
                safe_multiplier=safe_multiplier,
                feature_set=feature_set,
                train_strategy=train_strategy,
                binary_risk=binary_risk,
                dataset_only=dataset_only,
            )
        )

    settings = db.load_settings()
    report_lines = [
        "ML specialist models summary",
        f"Started at: {now()}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Results:",
    ]
    for result in results:
        report_lines.extend(
            [
                f"- {result['specialist']}: dataset_run_id={result['dataset_run_id']}, "
                f"model_run_id={result['model_run_id']}, holdout_error={result['holdout_error'] or 'none'}",
            ]
        )
    report_path = db.write_report(settings, "ml_specialist_models_summary", report_lines)
    print(f"[ml_specialist_models] Summary report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build and train local specialist ML models.")
    parser.add_argument(
        "--specialist",
        choices=sorted({*SPECIALIST_GROUPS, *SPECIALISTS}),
        default="all",
    )
    parser.add_argument("--base-dataset-run-id", type=int, default=None)
    parser.add_argument("--safe-threshold", type=float, default=None)
    parser.add_argument("--safe-multiplier", type=int, default=None)
    parser.add_argument("--feature-set", default=None)
    parser.add_argument("--train-strategy", default=None)
    parser.add_argument("--multi-risk", action="store_true", help="Keep original risk actions instead of binary review/safe.")
    parser.add_argument("--dataset-only", action="store_true", help="Build specialist dataset(s) without training models.")
    args = parser.parse_args()
    main(
        specialist=args.specialist,
        base_dataset_run_id=args.base_dataset_run_id,
        safe_threshold=args.safe_threshold,
        safe_multiplier=args.safe_multiplier,
        feature_set=args.feature_set,
        train_strategy=args.train_strategy,
        binary_risk=not args.multi_risk,
        dataset_only=args.dataset_only,
    )
