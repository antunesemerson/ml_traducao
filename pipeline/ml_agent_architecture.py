from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import db
from ml_specialist_models import SPECIALISTS
from ml_specialist_policy import OPERATIONAL_SPECIALIST_KEYS


RULE_VERSION = "ml_agent_architecture_v1"

POSITIVE_LABELS = {"correct", "contextual_exception"}
NEGATIVE_LABELS = {
    "major_fix",
    "minor_fix",
    "rejected",
    "rejected_suggestion",
    "residual_spanish",
    "semantic_error",
    "structure_error",
    "token_mismatch",
}


@dataclass(frozen=True)
class AgentSpec:
    agent_key: str
    agent_type: str
    parent_agent_key: str | None
    model_kind: str | None
    status: str
    operational_state: str
    decision_role: str
    scope_group: str | None
    scope_sql: str | None
    scope_description: str | None
    default_threshold: float | None
    priority: int
    dashboard_group: str
    notes: dict[str, Any]


def latest_id(conn, table_name: str, where_sql: str = "1 = 1") -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def qualify_scope_sql(scope_sql: str, alias: str) -> str:
    qualified = re.sub(r"(?<![\w.])relative_path(?!\w)", f"{alias}.relative_path", scope_sql)
    qualified = re.sub(r"(?<![\w.])source_key(?!\w)", f"{alias}.source_key", qualified)
    return qualified


def planned_subagents() -> list[AgentSpec]:
    return [
        AgentSpec(
            agent_key="religion_bosnian_terms",
            agent_type="subspecialist",
            parent_agent_key="religion",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_vote",
            scope_group="religion_terms",
            scope_sql=(
                "relative_path = 'religion/religion_christianity_l_spanish.yml' "
                "AND source_key LIKE 'bosnian_%'"
            ),
            scope_description="Bosnian Church terms that often must preserve original religious titles.",
            default_threshold=0.97,
            priority=20,
            dashboard_group="Religion",
            notes={"reason": "Recurring semantic errors from literal translation of Bosnian religious terms."},
        ),
        AgentSpec(
            agent_key="religion_sufri",
            agent_type="subspecialist",
            parent_agent_key="religion",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_vote",
            scope_group="religion_terms",
            scope_sql="relative_path = 'religion/religion_islam_l_spanish.yml' AND source_key LIKE 'sufri_%'",
            scope_description="Sufri/Sufrism labels and descriptions, distinct from Sufi/Sufism.",
            default_threshold=0.97,
            priority=25,
            dashboard_group="Religion",
            notes={"reason": "Recent reviews found Sufri confused with Sufi, a high-risk semantic drift."},
        ),
        AgentSpec(
            agent_key="religion_possessive_gods",
            agent_type="subspecialist",
            parent_agent_key="religion",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_vote",
            scope_group="religion_possessives",
            scope_sql=(
                "relative_path LIKE 'religion/%' AND ("
                "source_key LIKE '%_god_name_possessive' OR "
                "source_key LIKE '%_deity_name_possessive' OR "
                "source_key LIKE '%_devil_name_possessive' OR "
                "source_key LIKE '%_death_deity_%' OR "
                "source_key LIKE '%_name_possessive'"
                ")"
            ),
            scope_description="Religious possessive name fields that often need de/da/do prepositions.",
            default_threshold=0.96,
            priority=30,
            dashboard_group="Religion",
            notes={"reason": "Frequent minor fixes around possessive prepositions in god/deity fields."},
        ),
        AgentSpec(
            agent_key="religion_preserved_terms",
            agent_type="subspecialist",
            parent_agent_key="religion",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_vote",
            scope_group="religion_terms",
            scope_sql=(
                "relative_path LIKE 'religion/%' AND ("
                "source_key LIKE 'dab_qhuas_%' OR "
                "source_key LIKE 'tolotang_%' OR "
                "source_key LIKE '%_priest' OR "
                "source_key LIKE '%_afterlife'"
                ")"
            ),
            scope_description="Small religion-specific terms that may be preserved instead of translated.",
            default_threshold=0.97,
            priority=35,
            dashboard_group="Religion",
            notes={"reason": "Recent reviews found specialized terms generalized into common PT-BR words."},
        ),
    ]


def guarded_subpolicy_agents(*, active: bool) -> list[AgentSpec]:
    state = "operational" if active else "dry_run"
    common_notes = {
        "kind": "symbolic_guarded_subpolicy",
        "auto_apply_allowed": 0,
        "role": "Can lower token-gate risk only after audited human evidence and checkpoint validation.",
    }
    return [
        AgentSpec(
            agent_key="gender_pronoun_english_aligned_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_pronouns",
            scope_sql=None,
            scope_description="Guarded pronoun/gender-token releases aligned with English reference and PT-BR fluency.",
            default_threshold=None,
            priority=32,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_pronoun_policy"},
        ),
        AgentSpec(
            agent_key="mixed_name_gender_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_mixed",
            scope_sql=None,
            scope_description="Guarded mixed name/gender-token rewrites with reviewed structural evidence.",
            default_threshold=None,
            priority=33,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_mixed_name_gender_policy"},
        ),
        AgentSpec(
            agent_key="select_cstring_ui_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_select_cstring",
            scope_sql=None,
            scope_description="Guarded Select_CString UI/context neutralization when the reviewed pattern is stable.",
            default_threshold=None,
            priority=34,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_select_cstring_policy"},
        ),
        AgentSpec(
            agent_key="token_added_english_reference_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_added",
            scope_sql=None,
            scope_description="Guarded token additions that restore tokens present in the English semantic reference.",
            default_threshold=None,
            priority=35,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_token_added_policy"},
        ),
        AgentSpec(
            agent_key="gender_article_removal_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_gender_article",
            scope_sql=None,
            scope_description="Guarded removal of redundant gender article tokens before names.",
            default_threshold=None,
            priority=36,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_gender_article_removal_policy"},
        ),
        AgentSpec(
            agent_key="gender_form_swap_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_gender_form_swap",
            scope_sql=None,
            scope_description="Guarded Custom gender form swaps such as ES_OA to ES_XA within the same scope.",
            default_threshold=None,
            priority=37,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_gender_form_swap_policy"},
        ),
        AgentSpec(
            agent_key="pronoun_form_swap_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_pronoun_form_swap",
            scope_sql=None,
            scope_description="Guarded subject/object pronoun swaps within the same scope.",
            default_threshold=None,
            priority=38,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_pronoun_form_swap_policy"},
        ),
        AgentSpec(
            agent_key="name_form_style_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_name_style",
            scope_sql=None,
            scope_description="Guarded removal of CK3 name style suffixes when the underlying name token is unchanged.",
            default_threshold=None,
            priority=39,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_name_form_style_policy"},
        ),
        AgentSpec(
            agent_key="tutorial_concept_exception_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_tutorial_concepts",
            scope_sql="relative_path LIKE 'tutorial/%'",
            scope_description="Guarded tutorial concept-link exceptions that replace critical variable/icon blocks with reviewed tutorial concept variables.",
            default_threshold=None,
            priority=41,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_tutorial_concept_policy"},
        ),
    ]


def build_agent_specs(
    *,
    composite_gate_active: bool = False,
    composite_gate_info: dict[str, Any] | None = None,
) -> list[AgentSpec]:
    coordinator_state = "operational" if composite_gate_active else "dry_run"
    coordinator_notes: dict[str, Any] = {"current_engine": "ml_specialist_ensemble_policy_v1"}
    if composite_gate_active:
        coordinator_notes.update(
            {
                "active_composite_gate": "segment_token_composite_review_gate",
                "active_checkpoint_id": composite_gate_info.get("active_checkpoint_id") if composite_gate_info else None,
                "active_overlay_run_id": composite_gate_info.get("active_overlay_run_id") if composite_gate_info else None,
                "auto_apply_allowed": composite_gate_info.get("auto_apply_allowed") if composite_gate_info else 0,
            }
        )
    specs = [
        AgentSpec(
            agent_key="deterministic_guards",
            agent_type="guard",
            parent_agent_key=None,
            model_kind=None,
            status="active",
            operational_state="authoritative",
            decision_role="hard_gate",
            scope_group="all",
            scope_sql=None,
            scope_description="Token, structure, locked-human and CK3 placeholder safety gates.",
            default_threshold=None,
            priority=0,
            dashboard_group="Safety",
            notes={"policy": "Always stronger than model or specialist votes."},
        ),
        AgentSpec(
            agent_key="general_macro",
            agent_type="macro_model",
            parent_agent_key=None,
            model_kind="risk_action_classifier",
            status="active",
            operational_state="operational",
            decision_role="baseline_score",
            scope_group="all",
            scope_sql=None,
            scope_description="General risk/quality classifier for the whole localization package.",
            default_threshold=None,
            priority=10,
            dashboard_group="Macro",
            notes={"role": "Provides the default decision when no specialist safely improves it."},
        ),
        AgentSpec(
            agent_key="coordinator_ensemble_v1",
            agent_type="coordinator",
            parent_agent_key="general_macro",
            model_kind=None,
            status="active",
            operational_state=coordinator_state,
            decision_role="route_and_arbitrate",
            scope_group="all",
            scope_sql=None,
            scope_description="Coordinates general score, specialist votes, human learning and safety policy.",
            default_threshold=None,
            priority=15,
            dashboard_group="Coordinator",
            notes=coordinator_notes,
        ),
    ]
    for key, config in SPECIALISTS.items():
        parent = "coordinator_ensemble_v1"
        agent_type = "specialist"
        if key.startswith("title_") or key == "culture_title_labels":
            parent = "titles"
            agent_type = "subspecialist"
        elif key.startswith("religion_"):
            parent = "religion"
            agent_type = "subspecialist"
        elif key == "titles":
            parent = "coordinator_ensemble_v1"
            agent_type = "specialist_legacy"
        operational = key in OPERATIONAL_SPECIALIST_KEYS
        specs.append(
            AgentSpec(
                agent_key=key,
                agent_type=agent_type,
                parent_agent_key=parent,
                model_kind=config.name,
                status="active",
                operational_state="operational" if operational else "experimental",
                decision_role="vote",
                scope_group=key,
                scope_sql=config.scope_sql,
                scope_description=config.description,
                default_threshold=config.default_safe_threshold,
                priority=40 if operational else 70,
                dashboard_group="Titles" if key.startswith("title") or key == "culture_title_labels" else "Religion",
                notes={
                    "feature_set": config.default_feature_set,
                    "train_strategy": config.default_train_strategy,
                    "holdout_min_negative": config.holdout_min_negative,
                    "safe_multiplier": config.default_safe_multiplier,
                },
            )
        )
    active_keys = set(SPECIALISTS)
    specs.extend(guarded_subpolicy_agents(active=composite_gate_active))
    active_keys.update(spec.agent_key for spec in specs)
    specs.extend(spec for spec in planned_subagents() if spec.agent_key not in active_keys)
    return specs


def upsert_agent_registry(conn, specs: list[AgentSpec], now: str) -> None:
    for spec in specs:
        conn.execute(
            """
            INSERT INTO ml_agent_registry (
                agent_key,
                agent_type,
                parent_agent_key,
                model_kind,
                status,
                operational_state,
                decision_role,
                scope_group,
                scope_sql,
                scope_description,
                default_threshold,
                priority,
                dashboard_group,
                created_at,
                updated_at,
                notes_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_key) DO UPDATE SET
                agent_type = excluded.agent_type,
                parent_agent_key = excluded.parent_agent_key,
                model_kind = excluded.model_kind,
                status = excluded.status,
                operational_state = excluded.operational_state,
                decision_role = excluded.decision_role,
                scope_group = excluded.scope_group,
                scope_sql = excluded.scope_sql,
                scope_description = excluded.scope_description,
                default_threshold = excluded.default_threshold,
                priority = excluded.priority,
                dashboard_group = excluded.dashboard_group,
                updated_at = excluded.updated_at,
                notes_json = excluded.notes_json
            """,
            (
                spec.agent_key,
                spec.agent_type,
                spec.parent_agent_key,
                spec.model_kind,
                spec.status,
                spec.operational_state,
                spec.decision_role,
                spec.scope_group,
                spec.scope_sql,
                spec.scope_description,
                spec.default_threshold,
                spec.priority,
                spec.dashboard_group,
                now,
                now,
                json.dumps(spec.notes, ensure_ascii=False, sort_keys=True),
            ),
        )


def scope_active_count(conn, scope_sql: str | None) -> int:
    if not scope_sql:
        row = conn.execute("SELECT COUNT(*) AS total FROM source_segments WHERE is_active = 1").fetchone()
        return int(row["total"] or 0) if row else 0
    qualified = qualify_scope_sql(scope_sql, "s")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM source_segments s
        WHERE s.is_active = 1
          AND ({qualified})
        """
    ).fetchone()
    return int(row["total"] or 0) if row else 0


def latest_score_for_model_kind(conn, model_kind: str | None) -> tuple[int | None, int | None]:
    if not model_kind:
        return None, None
    row = conn.execute(
        """
        SELECT
            m.id AS model_run_id,
            (
                SELECT MAX(sr.id)
                FROM ml_score_runs sr
                WHERE sr.model_run_id = m.id
                  AND sr.finished_at IS NOT NULL
            ) AS score_run_id
        FROM ml_model_runs m
        WHERE m.model_kind = ?
          AND m.finished_at IS NOT NULL
        ORDER BY m.id DESC
        LIMIT 1
        """,
        (model_kind,),
    ).fetchone()
    if not row:
        return None, None
    return (
        int(row["model_run_id"]) if row["model_run_id"] is not None else None,
        int(row["score_run_id"]) if row["score_run_id"] is not None else None,
    )


def routing_status(spec: AgentSpec) -> str:
    if spec.status == "planned":
        return "candidate"
    if spec.operational_state == "operational":
        return "operational"
    if spec.operational_state == "experimental":
        return "experimental"
    return spec.operational_state


def materialize_routing_items(
    conn,
    run_id: int,
    specs: list[AgentSpec],
    recommendation_specs: list[AgentSpec],
    now: str,
    per_agent_limit: int = 250,
) -> int:
    general_score_id = latest_id(
        conn,
        "ml_score_runs",
        """
        finished_at IS NOT NULL
        AND model_run_id IN (
            SELECT id FROM ml_model_runs WHERE model_kind = 'risk_action_classifier'
        )
        """,
    )
    policy_run_id = latest_id(conn, "ml_policy_runs", "finished_at IS NOT NULL AND scored_count > 0")
    recommendation_keys = {spec.agent_key for spec in recommendation_specs}
    materialized_specs = [
        spec
        for spec in specs
        if spec.scope_sql
        and (
            spec.operational_state in {"operational", "experimental"}
            or spec.agent_key in recommendation_keys
        )
    ]
    inserted_total = 0
    for spec in materialized_specs:
        model_run_id, specialist_score_id = latest_score_for_model_kind(conn, spec.model_kind)
        qualified = qualify_scope_sql(spec.scope_sql or "1 = 0", "s")
        reason = json.dumps(
            {
                "agent_key": spec.agent_key,
                "model_kind": spec.model_kind,
                "model_run_id": model_run_id,
                "score_run_id": specialist_score_id,
                "scope_group": spec.scope_group,
                "default_threshold": spec.default_threshold,
                "operational_state": spec.operational_state,
                "per_agent_limit": per_agent_limit,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        before = conn.total_changes
        conn.execute(
            f"""
            INSERT OR IGNORE INTO ml_agent_routing_items (
                run_id,
                segment_id,
                relative_path,
                source_key,
                route_agent_key,
                route_agent_type,
                route_status,
                route_confidence,
                route_reason,
                issue_family,
                general_action,
                policy_action,
                specialist_action,
                recommendation_key,
                created_at
            )
            SELECT
                ? AS run_id,
                s.id AS segment_id,
                s.relative_path,
                s.source_key,
                ? AS route_agent_key,
                ? AS route_agent_type,
                ? AS route_status,
                specialist_item.model_safe_probability AS route_confidence,
                ? AS route_reason,
                ? AS issue_family,
                general_item.final_action AS general_action,
                policy_item.policy_action AS policy_action,
                specialist_item.final_action AS specialist_action,
                ? AS recommendation_key,
                ? AS created_at
            FROM source_segments s
            LEFT JOIN ml_score_items general_item
              ON general_item.segment_id = s.id
             AND general_item.run_id = ?
            LEFT JOIN ml_policy_items policy_item
              ON policy_item.segment_id = s.id
             AND policy_item.run_id = ?
            LEFT JOIN ml_score_items specialist_item
              ON specialist_item.segment_id = s.id
             AND specialist_item.run_id = ?
            WHERE s.is_active = 1
              AND ({qualified})
            ORDER BY s.relative_path, s.source_line_number, s.id
            LIMIT ?
            """,
            (
                run_id,
                spec.agent_key,
                spec.agent_type,
                routing_status(spec),
                reason,
                spec.dashboard_group,
                spec.agent_key if spec.agent_key in recommendation_keys else None,
                now,
                general_score_id,
                policy_run_id,
                specialist_score_id,
                per_agent_limit,
            ),
        )
        inserted_total += conn.total_changes - before

    conn.execute(
        """
        UPDATE ml_agent_routing_runs
        SET notes_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(
                {
                    "mode": "architecture_snapshot",
                    "routed_count_is_scope_sum": True,
                    "routing_items_materialized": True,
                    "routing_items_per_agent_limit": per_agent_limit,
                    "routing_items_inserted": inserted_total,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            now,
            run_id,
        ),
    )
    return inserted_total


def review_evidence(conn, scope_sql: str) -> dict[str, Any]:
    qualified = qualify_scope_sql(scope_sql, "c")
    label_placeholders = ",".join("?" for _ in POSITIVE_LABELS)
    negative_placeholders = ",".join("?" for _ in NEGATIVE_LABELS)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS evidence_count,
            SUM(CASE WHEN c.human_label IN ({label_placeholders}) THEN 1 ELSE 0 END) AS positive_count,
            SUM(CASE WHEN c.human_label IN ({negative_placeholders}) THEN 1 ELSE 0 END) AS negative_count,
            SUM(CASE WHEN c.corrected_text IS NOT NULL AND TRIM(c.corrected_text) <> '' THEN 1 ELSE 0 END) AS corrected_count
        FROM local_learning_candidates c
        WHERE c.local_status = 'reviewed_human'
          AND c.human_label <> 'pending'
          AND ({qualified})
        """,
        (*sorted(POSITIVE_LABELS), *sorted(NEGATIVE_LABELS)),
    ).fetchone()
    samples = conn.execute(
        f"""
        SELECT
            c.segment_id,
            c.relative_path,
            c.source_key,
            c.human_label,
            c.corrected_text,
            c.reason,
            c.reviewed_at
        FROM local_learning_candidates c
        WHERE c.local_status = 'reviewed_human'
          AND c.human_label <> 'pending'
          AND ({qualified})
        ORDER BY c.reviewed_at DESC, c.id DESC
        LIMIT 8
        """
    ).fetchall()
    return {
        "evidence_count": int(row["evidence_count"] or 0) if row else 0,
        "positive_count": int(row["positive_count"] or 0) if row else 0,
        "negative_count": int(row["negative_count"] or 0) if row else 0,
        "corrected_count": int(row["corrected_count"] or 0) if row else 0,
        "samples": [dict(sample) for sample in samples],
    }


def create_routing_run(
    conn,
    specs: list[AgentSpec],
    started_at: str,
    candidate_specs: list[AgentSpec] | None = None,
) -> int:
    general_score_id = latest_id(
        conn,
        "ml_score_runs",
        """
        finished_at IS NOT NULL
        AND model_run_id IN (
            SELECT id FROM ml_model_runs WHERE model_kind = 'risk_action_classifier'
        )
        """,
    )
    policy_run_id = latest_id(conn, "ml_policy_runs", "finished_at IS NOT NULL AND scored_count > 0")
    active_specs = [spec for spec in specs if spec.status == "active" and spec.scope_sql]
    planned_source = candidate_specs if candidate_specs is not None else specs
    planned_specs = [spec for spec in planned_source if spec.scope_sql]
    active_scope_total = sum(scope_active_count(conn, spec.scope_sql) for spec in active_specs)
    planned_scope_total = sum(scope_active_count(conn, spec.scope_sql) for spec in planned_specs)
    segments_total = scope_active_count(conn, None)
    cursor = conn.execute(
        """
        INSERT INTO ml_agent_routing_runs (
            rule_version,
            general_score_run_id,
            policy_run_id,
            coordinator_key,
            agents_considered_count,
            segments_scanned_count,
            routed_count,
            active_agent_covered_count,
            planned_agent_covered_count,
            missing_agent_count,
            conflict_count,
            recommendation_count,
            notes_json,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            general_score_id,
            policy_run_id,
            "coordinator_ensemble_v1",
            len(specs),
            segments_total,
            active_scope_total,
            active_scope_total,
            planned_scope_total,
            0,
            0,
            0,
            json.dumps(
                {
                    "mode": "architecture_snapshot",
                    "routed_count_is_scope_sum": True,
                    "routing_items_materialized": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            started_at,
            started_at,
            started_at,
        ),
    )
    return int(cursor.lastrowid)


def insert_recommendations(
    conn,
    run_id: int,
    planned: list[AgentSpec],
    now: str,
) -> list[dict[str, Any]]:
    inserted: list[dict[str, Any]] = []
    for spec in planned:
        if not spec.scope_sql:
            continue
        evidence = review_evidence(conn, spec.scope_sql)
        if evidence["evidence_count"] == 0:
            status = "watching"
            reason = "Planned scope exists but has no reviewed evidence yet."
        elif evidence["negative_count"] > 0:
            status = "proposed"
            reason = "Reviewed examples include negative or corrected labels in this narrow scope."
        else:
            status = "watching"
            reason = "Reviewed examples exist, but no negative evidence yet."
        is_trainable = spec.agent_key in SPECIALISTS
        conn.execute(
            """
            INSERT INTO ml_agent_recommendations (
                run_id,
                proposed_agent_key,
                parent_agent_key,
                recommendation_type,
                status,
                reason,
                evidence_count,
                positive_count,
                negative_count,
                corrected_count,
                sample_segments_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                spec.agent_key,
                spec.parent_agent_key,
                "train_subagent" if is_trainable else "create_subagent",
                status,
                reason,
                evidence["evidence_count"],
                evidence["positive_count"],
                evidence["negative_count"],
                evidence["corrected_count"],
                json.dumps(evidence["samples"], ensure_ascii=False),
                now,
                now,
            ),
        )
        inserted.append({"spec": spec, **evidence, "status": status, "reason": reason})
    conn.execute(
        """
        UPDATE ml_agent_routing_runs
        SET recommendation_count = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (len(inserted), now, run_id),
    )
    return inserted


def write_architecture_report(
    settings: dict[str, Any],
    run_id: int,
    specs: list[AgentSpec],
    recommendations: list[dict[str, Any]],
    materialized_items: int,
    started_at: datetime,
) -> None:
    active = [spec for spec in specs if spec.status == "active"]
    planned = [spec for spec in specs if spec.status == "planned"]
    experimental = [spec for spec in specs if spec.status == "active" and spec.operational_state == "experimental"]
    operational = [spec for spec in specs if spec.operational_state == "operational"]
    lines = [
        "ML agent architecture report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Routing run id: {run_id}",
        "",
        "Architecture:",
        "- deterministic_guards -> hard safety gate",
        "- general_macro -> whole-package baseline model",
        "- coordinator_ensemble_v1 -> routes/arbitrates macro and specialist votes",
        "- active specialists/subspecialists -> narrow votes by domain",
        "- candidate/experimental subagents -> narrow neurons suggested by reviewed evidence",
        "",
        "Summary:",
        f"- registered agents: {len(specs)}",
        f"- active agents: {len(active)}",
        f"- operational specialist agents: {len(operational)}",
        f"- experimental active agents: {len(experimental)}",
        f"- planned registry agents: {len(planned)}",
        f"- recommendations written: {len(recommendations)}",
        f"- routing items materialized: {materialized_items}",
        "",
        "Active operational agents:",
    ]
    for spec in sorted(operational, key=lambda item: (item.dashboard_group, item.priority, item.agent_key)):
        lines.append(
            f"- {spec.agent_key} | type={spec.agent_type} | parent={spec.parent_agent_key or 'none'} | "
            f"model={spec.model_kind or 'none'} | threshold={spec.default_threshold}"
        )
    lines.extend(["", "Candidate/training recommendations:"])
    for item in recommendations:
        spec = item["spec"]
        lines.append(
            f"- {spec.agent_key} | parent={spec.parent_agent_key} | status={item['status']} | "
            f"evidence={item['evidence_count']} positive={item['positive_count']} "
            f"negative={item['negative_count']} corrected={item['corrected_count']} | {item['reason']}"
        )
    lines.extend(
        [
            "",
            "Dashboard expectations:",
            "- Show macro, coordinator, active specialists and experimental/candidate subagents as a network.",
            "- Show each agent status, model kind, latest model/score, coverage and false-safe safety.",
            "- Show recommendations as training/candidate neurons with evidence counts and sample rows.",
            "- Keep this analytical only; no output writes and no model promotion.",
        ]
    )
    report_path = db.write_report(settings, "ml_agent_architecture", lines)
    print(f"[ml_agent_architecture] Agents registered: {len(specs)}")
    print(f"[ml_agent_architecture] Training recommendations: {len(recommendations)}")
    print(f"[ml_agent_architecture] Routing run id: {run_id}")
    print(f"[ml_agent_architecture] Report: {report_path}")


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    now = started_at.isoformat(timespec="seconds")
    print("[ml_agent_architecture] Starting agent architecture sync")
    print(f"[ml_agent_architecture] Rule version: {RULE_VERSION}")
    recommendation_specs = planned_subagents()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        composite_gate_row = conn.execute(
            """
            SELECT *
            FROM ml_composite_gate_registry
            WHERE gate_key = 'segment_token_composite_review_gate'
              AND operational_state = 'operational'
              AND auto_apply_allowed = 0
            LIMIT 1
            """
        ).fetchone()
        composite_gate_info = dict(composite_gate_row) if composite_gate_row else None
        specs = build_agent_specs(
            composite_gate_active=composite_gate_info is not None,
            composite_gate_info=composite_gate_info,
        )
        upsert_agent_registry(conn, specs, now)
        run_id = create_routing_run(conn, specs, now, recommendation_specs)
        materialized_items = materialize_routing_items(conn, run_id, specs, recommendation_specs, now)
        recommendations = insert_recommendations(
            conn,
            run_id,
            recommendation_specs,
            now,
        )
        conn.commit()
    write_architecture_report(settings, run_id, specs, recommendations, materialized_items, started_at)
    print("[ml_agent_architecture] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync ML agent registry and architecture recommendations.")
    parser.parse_args()
    main()
