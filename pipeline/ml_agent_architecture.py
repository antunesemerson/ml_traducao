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
    for column in ("english_text", "spanish_text", "old_text"):
        qualified = re.sub(rf"(?<![\w.]){column}(?!\w)", f"{alias}.{column}", qualified)
    return qualified


def planned_subagents() -> list[AgentSpec]:
    return [
        AgentSpec(
            agent_key="gender_token_mismatch_guard",
            agent_type="symbolic_guard",
            parent_agent_key="gender_form_swap_subpolicy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="issue_review_gender",
            scope_sql=(
                "old_text LIKE '%herdeir[%Custom(''ES_OA'')%' OR "
                "old_text LIKE '%querid[%Custom(''ES_OA'')%' OR "
                "old_text LIKE '%o/a %Custom(''ES_AnAna'')%'"
            ),
            scope_description="Blocks gender-token mismatch patterns that alter CK3 gender structure or leave mixed o/a forms.",
            default_threshold=None,
            priority=34,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_negative": 3,
                "known_patterns": ["herdeir[ES_OA]", "querid[ES_OA]", "o/a ... ES_AnAna"],
                "next_step": "Keep shadow until token-policy checkpoint can prove zero structural false blocks.",
            },
        ),
        AgentSpec(
            agent_key="gender_token_extra_prefix_guard",
            agent_type="symbolic_guard",
            parent_agent_key="gender_form_swap_subpolicy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="issue_review_gender",
            scope_sql=(
                "old_text LIKE '%Custom(''ES_OA'')%' AND ("
                "old_text LIKE '%cativo[%' OR "
                "old_text LIKE '%amador[%' OR "
                "old_text LIKE '%malvado[%' OR "
                "old_text LIKE '%republicano[%' OR "
                "old_text LIKE '%querid[%')"
            ),
            scope_description="Blocks visible gender stems or prefixes glued to CK3 ES_OA/ES_XA helpers.",
            default_threshold=None,
            priority=35,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_negative": 7,
                "known_patterns": ["cativo[ES_OA]", "amador[ES_OA]", "malvado[ES_OA]", "republicano[ES_OA]"],
                "next_step": "Split into repair patterns only after more corrected_text examples exist.",
            },
        ),
        AgentSpec(
            agent_key="culture_republican_vassal_role_gender_boundary",
            agent_type="symbolic_subpolicy",
            parent_agent_key="micro_culture_semantic",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_context_checkpoint",
            scope_group="issue_partial_coverage_culture_role_gender",
            scope_sql=(
                "relative_path = 'culture/traditions/cultural_traditions_l_spanish.yml' "
                "AND english_text LIKE '%Republican vassal%' "
                "AND old_text LIKE '%um[%Custom(''ES_XA'')%' "
                "AND old_text LIKE '%vassal[%Custom(''ES_OA'')%' "
                "AND old_text LIKE '%republicano[%Custom(''ES_OA'')%'"
            ),
            scope_description=(
                "Explains culture tradition effects that appoint a character as a republican vassal using "
                "coordinated ES_XA/ES_OA gender helpers for article, role noun and adjective."
            ),
            default_threshold=None,
            priority=36,
            dashboard_group="Issue Network",
            notes={
                "shadow_table": "ml_issue_culture_semantic_pattern_shadow_runs",
                "latest_shadow_run_id": 3,
                "checkpoint_table": "ml_issue_culture_semantic_pattern_checkpoint_runs",
                "latest_checkpoint_run_id": 2,
                "high_issue_checkpoint_run_id": 15,
                "partial_coverage_run_id": 55,
                "seed_segment": "appoint_podesta_desc",
                "allowed_shadow_items": 1,
                "blocked_shadow_items": 0,
                "known_pattern": "um[ES_XA] vassal[ES_OA] republicano[ES_OA] for English Republican vassal.",
                "required_supporting_families": [
                    "dynamic_ck3_expression_microagent",
                    "gender_token_microagent",
                    "high_issue_auditor",
                ],
                "maturity_status": "new_shadow_checkpoint",
                "safety_note": "Shadow-only: no source/output read, no production write, no correction application.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_residual_spanish_guard",
            agent_type="symbolic_guard",
            parent_agent_key="select_cstring_ui_subpolicy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="issue_review_gender",
            scope_sql=(
                "old_text LIKE '%Select_CString%' AND ("
                "old_text LIKE '%deber%' OR "
                "old_text LIKE '%ganará%' OR "
                "old_text LIKE '%perderá%' OR "
                "old_text LIKE '%convierten%' OR "
                "old_text LIKE '%proponerte%' OR "
                "old_text LIKE '%proponerse%' OR "
                "old_text LIKE '%hacerlo%' OR "
                "old_text LIKE '%quedaste%' OR "
                "old_text LIKE '%quedó%' OR "
                "old_text LIKE '%apoyas%' OR "
                "old_text LIKE '%apoya%' OR "
                "old_text LIKE '%perdiste%' OR "
                "old_text LIKE '%perdió%')"
            ),
            scope_description="Blocks Select_CString literal alternatives that still contain Spanish text.",
            default_threshold=None,
            priority=36,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_negative": 28,
                "known_markers": ["deber", "ganará", "perderá", "convierten", "apoyas", "perdiste"],
                "next_step": "Collect corrected_text pairs before this becomes an autofix subpolicy.",
            },
        ),
        AgentSpec(
            agent_key="dynamic_token_literal_payload_repair_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="select_cstring_residual_spanish_guard",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_repair_checkpoint",
            scope_group="issue_partial_coverage_dynamic_literals",
            scope_sql=(
                "(old_text LIKE '%Select_CString%' OR old_text LIKE '%Concept(%') AND ("
                "old_text LIKE '%sois%' OR "
                "old_text LIKE '%son%' OR "
                "old_text LIKE '%encarcelad%')"
            ),
            scope_description=(
                "Repairs Spanish visible literals inside CK3 dynamic token payloads while keeping the "
                "Select_CString/Concept signature stable."
            ),
            default_threshold=None,
            priority=37,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_dynamic_token_literal_repair_checkpoint_runs",
                "latest_checkpoint_run_id": 3,
                "source_decision_run_id": 56,
                "source_queue_run_id": 37,
                "candidate_count": 59,
                "allowed_shadow_repairs": 35,
                "blocked_shadow_repairs": 24,
                "known_pattern": "Select_CString/Concept visible Spanish payloads translated inside CK3 dynamic tokens.",
                "token_status": "same_structural_tokens_or_dynamic_literal_payload_only",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, no production apply.",
                "next_step": "Split blocked payloads into smaller sentence, label, encoding, and title semantic neurons.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_auxiliary_sentence_rewrite_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="dynamic_token_literal_payload_repair_shadow",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_rewrite_checkpoint",
            scope_group="issue_partial_coverage_select_cstring_auxiliary",
            scope_sql=(
                "old_text LIKE '%Select_CString%' AND ("
                "old_text LIKE '%''has'', ''ha''%' OR "
                "old_text LIKE '%''te has'', ''se ha''%' OR "
                "old_text LIKE '%''recibiste'', ''recibi%' OR "
                "old_text LIKE '%''ajustaste'', ''ajust%' OR "
                "old_text LIKE '%''abatiste'', ''abati%' OR "
                "old_text LIKE '%''te pasas'', ''se pasa''%' OR "
                "old_text LIKE '%''sueles'', ''suele''%')"
            ),
            scope_description=(
                "Neutralizes or normalizes Spanish auxiliary Select_CString fragments when the surrounding "
                "Portuguese sentence already carries the verb meaning."
            ),
            default_threshold=None,
            priority=38,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_auxiliary_rewrite_checkpoint_runs",
                "latest_checkpoint_run_id": 2,
                "source_checkpoint_run_id": 3,
                "candidate_count": 16,
                "allowed_shadow_repairs": 15,
                "blocked_shadow_repairs": 1,
                "known_patterns": [
                    "has/ha -> empty payload before Portuguese past-tense verb",
                    "recibiste/recibio -> empty before sofreu or recebeu before uma ferida",
                    "ajustaste/ajusto -> ajustou",
                    "tu -> voce when paired with GetSheHe in a neutralized auxiliary sentence",
                ],
                "blocked_pattern": "relationship sentence with te/le + seguiste/siguio + tus ancestros needs a multi-token relation rewrite.",
                "token_status": "same_structural_tokens",
                "safety_note": "Shadow-only: preserves exact CK3 token shape, writes no source/output, promotes no production policy.",
                "next_step": "Split the remaining multi-token relation rewrite into a relation-context neuron if it reappears.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_antpath_relation_rewrite_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="select_cstring_auxiliary_sentence_rewrite_shadow",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_relation_rewrite_checkpoint",
            scope_group="issue_partial_coverage_select_cstring_relation_context",
            scope_sql=(
                "relative_path = 'relationship_reasons_filippa_l_spanish.yml' AND "
                "source_key IN ("
                "'friend_humored_antpath_superstition', "
                "'friend_humored_antpath_superstition_corresponding')"
            ),
            scope_description=(
                "Rewrites paired relationship reasons where Spanish Select_CString payloads te/le, "
                "seguiste/siguio and tus ancestros encode an ant-path superstition sentence."
            ),
            default_threshold=None,
            priority=39,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs",
                "latest_checkpoint_run_id": 2,
                "source_auxiliary_checkpoint_run_id": 2,
                "candidate_count": 2,
                "allowed_shadow_repairs": 2,
                "blocked_shadow_repairs": 0,
                "token_status": "same_structural_tokens",
                "parent_blocker_closed": "auxiliary run 2 no_auxiliary_rewrite_rule segment 234691",
                "sibling_pattern": "friend_humored_antpath_superstition_corresponding",
                "safety_note": "Shadow-only: preserves normalized CK3 token shell and writes no source/output.",
                "next_step": "Create a lifecycle bridge only if this relationship-context pattern recurs or production policy needs it.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_same_token_lifecycle_policy",
            agent_type="lifecycle_policy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="governed_shadow_release",
            scope_group="issue_select_cstring_same_token_shadow_lifecycle",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Aggregates mature Select_CString shadow repairs that preserve the normalized CK3 token shell, "
                "blocks residual Spanish and non-same-token payloads, and keeps production release disabled."
            ),
            default_threshold=None,
            priority=40,
            dashboard_group="Governance",
            notes={
                "lifecycle_table": "ml_issue_select_cstring_same_token_lifecycle_runs",
                "latest_lifecycle_run_id": 1,
                "source_run_ids": {
                    "dynamic_literal_payload": 3,
                    "auxiliary_sentence": 2,
                    "visible_label": 1,
                    "object_sentence": 1,
                    "antpath_relation": 2,
                },
                "candidate_count": 56,
                "released_shadow": 44,
                "blocked": 12,
                "block_reasons": {
                    "token_status_not_same_structural_tokens": 5,
                    "blocking_validation_issue": 7,
                },
                "production_release_allowed": 0,
                "safety_note": "Lifecycle observation only: no source/output reads and no output writes.",
                "next_step": "Split the 7 residual Spanish blockers into targeted literal cleanup neurons before any production bridge.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_residual_literal_cleanup_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="select_cstring_same_token_lifecycle_policy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="second_layer_cleanup_checkpoint",
            scope_group="issue_select_cstring_residual_literal_cleanup",
            scope_sql=(
                "old_text LIKE '%Select_CString%' AND ("
                "old_text LIKE '%''tú''%' OR "
                "old_text LIKE '%''te''%' OR "
                "old_text LIKE '%''le''%' OR "
                "old_text LIKE '%''ti''%')"
            ),
            scope_description=(
                "Second-layer cleanup for Select_CString rows that passed a first dynamic literal repair but "
                "still retained Spanish pronoun literals or Spanish residue in the proposed text."
            ),
            default_threshold=None,
            priority=41,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_lifecycle_run_id": 1,
                "candidate_count": 7,
                "allowed_shadow_repairs": 7,
                "blocked_shadow_repairs": 0,
                "token_status": "same_structural_tokens",
                "composition_parent": "select_cstring_same_token_lifecycle_policy",
                "safety_note": "Shadow-only second-layer cleanup; no source/output reads and no output writes.",
                "next_step": "Rerun or extend the same-token lifecycle to account for these 7 composed repairs.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_same_token_composition_overlay",
            agent_type="composition_policy",
            parent_agent_key="select_cstring_same_token_lifecycle_policy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="measure_composed_shadow_release",
            scope_group="issue_select_cstring_same_token_composition",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Composition overlay that combines baseline same-token lifecycle releases with second-layer "
                "cleanup checkpoints to measure aggregate release lift before any production bridge."
            ),
            default_threshold=None,
            priority=42,
            dashboard_group="Governance",
            notes={
                "overlay_table": "ml_issue_select_cstring_same_token_composition_overlay_runs",
                "latest_overlay_run_id": 1,
                "source_lifecycle_run_id": 1,
                "source_cleanup_checkpoint_run_id": 1,
                "candidate_count": 56,
                "baseline_released_shadow": 44,
                "cleanup_lift_shadow": 7,
                "composed_released_shadow": 51,
                "blocked_after_composition": 5,
                "production_release_allowed": 0,
                "safety_note": "Shadow-only composition measurement; no source/output reads and no output writes.",
                "next_step": "Split or policy the 5 remaining dynamic_literal_payload_only token-delta blockers.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_dynamic_literal_payload_delta_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="select_cstring_same_token_composition_overlay",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="validate_dynamic_literal_payload_delta",
            scope_group="issue_select_cstring_dynamic_literal_payload_delta",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Validates Select_CString token deltas when the only structural difference is inside dynamic "
                "literal payloads and the final text has no blocking local validation issues."
            ),
            default_threshold=None,
            priority=43,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_composition_overlay_run_id": 1,
                "candidate_count": 5,
                "allowed_shadow_repairs": 4,
                "blocked_shadow_repairs": 1,
                "block_reasons": {"blocking_validation_issue": 1},
                "token_status": "dynamic_literal_payload_only_validated",
                "production_release_allowed": 0,
                "safety_note": "Shadow-only token-delta validator; no source/output reads and no output writes.",
                "next_step": "Compose this checkpoint into the overlay to measure 51 to 55 shadow coverage lift.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_multistage_composition_overlay",
            agent_type="composition_coordinator",
            parent_agent_key="select_cstring_same_token_composition_overlay",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="measure_multistage_composed_shadow_release",
            scope_group="issue_select_cstring_multistage_composition",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Coordinator overlay that measures combined shadow coverage from baseline same-token lifecycle, "
                "residual literal cleanup, and dynamic literal payload delta neurons."
            ),
            default_threshold=None,
            priority=44,
            dashboard_group="Governance",
            notes={
                "overlay_table": "ml_issue_select_cstring_multistage_composition_overlay_runs",
                "latest_overlay_run_id": 1,
                "source_lifecycle_run_id": 1,
                "source_cleanup_checkpoint_run_id": 1,
                "source_dynamic_payload_checkpoint_run_id": 1,
                "candidate_count": 56,
                "baseline_released_shadow": 44,
                "cleanup_lift_shadow": 7,
                "dynamic_payload_lift_shadow": 4,
                "composed_released_shadow": 55,
                "blocked_after_composition": 1,
                "composed_shadow_rate": 0.9821,
                "production_release_allowed": 0,
                "safety_note": "Shadow-only multistage composition measurement; no source/output reads and no output writes.",
                "next_step": "Split final nick_the_repulsive_desc residual literal blocker or promote governed bridge after more regression checks.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_local_player_pronoun_literal_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="select_cstring_multistage_composition_overlay",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="repair_local_player_pronoun_literal",
            scope_group="issue_select_cstring_local_player_pronoun_literal",
            scope_sql="old_text LIKE '%Select_CString%' AND old_text LIKE '%GetSheHe%'",
            scope_description=(
                "Repairs Spanish local-player pronoun literals inside Select_CString patterns where "
                "X.IsLocalPlayer selects 'tú' and the non-player branch uses X.GetSheHe."
            ),
            default_threshold=None,
            priority=45,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_local_player_pronoun_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_dynamic_payload_checkpoint_run_id": 1,
                "candidate_count": 1,
                "allowed_shadow_repairs": 1,
                "blocked_shadow_repairs": 0,
                "token_status": "local_player_pronoun_literal_validated",
                "production_release_allowed": 0,
                "safety_note": "Shadow-only literal repair; no source/output reads and no output writes.",
                "next_step": "Compose into multistage overlay to measure 56/56 shadow coverage.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_final_composition_overlay",
            agent_type="composition_coordinator",
            parent_agent_key="select_cstring_multistage_composition_overlay",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="measure_final_composed_shadow_release",
            scope_group="issue_select_cstring_final_composition",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Final Select_CString shadow coordinator combining baseline lifecycle, residual cleanup, "
                "dynamic literal payload delta, and local-player pronoun literal neurons."
            ),
            default_threshold=None,
            priority=46,
            dashboard_group="Governance",
            notes={
                "overlay_table": "ml_issue_select_cstring_final_composition_overlay_runs",
                "latest_overlay_run_id": 1,
                "source_lifecycle_run_id": 1,
                "source_cleanup_checkpoint_run_id": 1,
                "source_dynamic_payload_checkpoint_run_id": 1,
                "source_local_pronoun_checkpoint_run_id": 1,
                "candidate_count": 56,
                "baseline_released_shadow": 44,
                "cleanup_lift_shadow": 7,
                "dynamic_payload_lift_shadow": 4,
                "local_pronoun_lift_shadow": 1,
                "composed_released_shadow": 56,
                "blocked_after_composition": 0,
                "composed_shadow_rate": 1.0,
                "production_release_allowed": 0,
                "safety_note": "Shadow-only final composition measurement; no source/output reads and no output writes.",
                "next_step": "Run regression checks before proposing a governed production bridge.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_final_composition_maturity_auditor",
            agent_type="governance_auditor",
            parent_agent_key="select_cstring_final_composition_overlay",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="audit_bridge_readiness",
            scope_group="issue_select_cstring_final_composition_maturity",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Audits final Select_CString composition for duplicate segments, broken source references, "
                "unexpected sources, unresolved blockers, and mechanical bridge readiness."
            ),
            default_threshold=None,
            priority=47,
            dashboard_group="Governance",
            notes={
                "audit_table": "ml_issue_select_cstring_final_composition_maturity_audit_runs",
                "latest_audit_run_id": 1,
                "source_final_overlay_run_id": 1,
                "candidate_count": 56,
                "composed_released_shadow": 56,
                "blocked": 0,
                "issue_count": 0,
                "maturity_status": "bridge_candidate_shadow",
                "bridge_candidate": 1,
                "production_release_allowed": 0,
                "safety_note": "Audit-only maturity signal; no source/output reads and no output writes.",
                "next_step": "Design governed production bridge with regression guardrails before enabling writes.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_governed_bridge_proposal",
            agent_type="governance_bridge",
            parent_agent_key="select_cstring_final_composition_maturity_auditor",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="propose_governed_bridge_candidates",
            scope_group="issue_select_cstring_governed_bridge",
            scope_sql="old_text LIKE '%Select_CString%'",
            scope_description=(
                "Shadow-only governed bridge proposal for the final Select_CString composition chain. "
                "It materializes ready candidates and required guardrails but does not promote confirmations or write output."
            ),
            default_threshold=None,
            priority=48,
            dashboard_group="Governance",
            notes={
                "proposal_table": "ml_issue_select_cstring_governed_bridge_proposal_runs",
                "latest_proposal_run_id": 1,
                "source_maturity_audit_run_id": 1,
                "source_final_overlay_run_id": 1,
                "candidate_count": 56,
                "ready_count": 56,
                "blocked_count": 0,
                "same_token_count": 51,
                "dynamic_payload_delta_count": 5,
                "production_release_allowed": 0,
                "safety_note": "Proposal-only bridge; no source/output reads and no output writes.",
                "required_runtime_guards": [
                    "learning_status.production_safe == true",
                    "maturity audit remains bridge_candidate_shadow",
                    "source checkpoint references remain allowed and unblocked",
                    "current/corrected text hashes match proposal",
                    "local validation has no blocking issues",
                ],
                "next_step": "Ask production chat to implement a guarded bridge runner only after we approve the proposal.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_label_noun_rewrite_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="dynamic_token_literal_payload_repair_shadow",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_label_rewrite_checkpoint",
            scope_group="issue_partial_coverage_select_cstring_label_nouns",
            scope_sql=(
                "old_text LIKE '%Select_CString%' AND ("
                "old_text LIKE '%ermita%' OR "
                "old_text LIKE '%eunuc%' OR "
                "old_text LIKE '%cazador%')"
            ),
            scope_description=(
                "Rewrites short visible Select_CString labels and nouns when the surrounding segment is only a "
                "label/title surface and the CK3 structural token set stays stable."
            ),
            default_threshold=None,
            priority=39,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_label_rewrite_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_checkpoint_run_id": 3,
                "candidate_count": 6,
                "allowed_shadow_repairs": 3,
                "blocked_shadow_repairs": 3,
                "known_patterns": [
                    "La ermitana/El ermitano with duplicate outer O Eremita -> A Eremita/O Eremita",
                    "la eunuca/el eunuco -> a eunuca/o eunuco",
                    "Esta cazadora/Este cazador -> Esta cacadora/Este cacador",
                ],
                "blocked_patterns": [
                    "forced object sentence with mi persona needs object-pronoun rewrite",
                    "long quoted LAAMP scene needs long-text surface neuron",
                    "Pauper King nickname needs title semantic neuron",
                ],
                "token_status": "same_structural_tokens",
                "safety_note": "Shadow-only: no source/output read, no production write, no confirmation promotion.",
                "next_step": "Keep as a narrow label neuron; do not expand into sentence or title semantics.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_object_sentence_rewrite_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="dynamic_token_literal_payload_repair_shadow",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_sentence_rewrite_checkpoint",
            scope_group="issue_partial_coverage_select_cstring_object_sentence",
            scope_sql=(
                "old_text LIKE '%Select_CString%' AND "
                "old_text LIKE '%mi persona%' AND "
                "english_text LIKE '%forced%' AND "
                "english_text LIKE '%position of forfeit%'"
            ),
            scope_description=(
                "Rewrites the exact tournament forced-object sentence where Spanish Select_CString literals "
                "encode first-person verb and local-player object text."
            ),
            default_threshold=None,
            priority=40,
            dashboard_group="Token Gate",
            notes={
                "checkpoint_table": "ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_checkpoint_run_id": 1,
                "source_parent_checkpoint": "ml_issue_select_cstring_label_rewrite_checkpoint_runs",
                "candidate_count": 1,
                "allowed_shadow_repairs": 1,
                "blocked_shadow_repairs": 0,
                "observed_mi_persona_family_count": 15,
                "known_pattern": "forced object sentence: obrigue/obligo + mi persona -> forcei/forcou + voce with same CK3 token set.",
                "token_status": "same_structural_tokens",
                "safety_note": "Shadow-only: exact pattern only, no source/output read, no production write, no confirmation promotion.",
                "next_step": "Study the broader 15-row mi persona family separately before generalizing this neuron.",
            },
        ),
        AgentSpec(
            agent_key="nickname_pauper_king_title_semantic_shadow",
            agent_type="symbolic_subpolicy",
            parent_agent_key="nickname_select_cstring_literal_specialist",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_title_semantic_checkpoint",
            scope_group="issue_partial_coverage_nickname_title_semantic",
            scope_sql=(
                "relative_path = 'nicknames_l_spanish.yml' "
                "AND source_key = 'nick_pauper_king' "
                "AND english_text = 'the Pauper King'"
            ),
            scope_description=(
                "Repairs the Pauper King nickname semantic mismatch using the internal The Pauper King -> "
                "O Rei Mendigo reference while marking the ES_XA -> ES_OA gender-helper delta."
            ),
            default_threshold=None,
            priority=41,
            dashboard_group="Titles",
            notes={
                "checkpoint_table": "ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_checkpoint_run_id": 1,
                "source_parent_checkpoint": "ml_issue_select_cstring_label_rewrite_checkpoint_runs",
                "candidate_count": 1,
                "allowed_shadow_repairs": 1,
                "blocked_shadow_repairs": 0,
                "reference_key": "journey_pauper_king_modifier",
                "reference_translation": "O Rei Mendigo",
                "token_status": "expected_gender_helper_change_es_xa_to_es_oa",
                "production_release_allowed": 0,
                "known_pattern": "nick_pauper_king: la Reina/el Rey Convidador[ES_XA] -> a Rainha/o Rei Mendig[ES_OA].",
                "safety_note": "Shadow-only: semantic fix is staged, but helper change requires token-policy awareness before production.",
                "next_step": "Bridge this title semantic repair into the controlled token-policy layer before production application.",
            },
        ),
        AgentSpec(
            agent_key="gender_visible_spanish_residual_guard",
            agent_type="symbolic_guard",
            parent_agent_key="gender_form_swap_subpolicy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="issue_review_gender",
            scope_sql=(
                "(old_text LIKE '%Custom(''ES_OA'')%' OR old_text LIKE '%Custom(''ES_ElLa'')%') AND ("
                "old_text LIKE '%Bajo el gobierno%' OR "
                "old_text LIKE '%Jurar lealtad%' OR "
                "old_text LIKE '% demasiado buena%' OR "
                "old_text LIKE '% con una %' OR "
                "old_text LIKE '% es [%')"
            ),
            scope_description="Blocks Spanish residue around visible CK3 gender helper tokens outside Select_CString.",
            default_threshold=None,
            priority=37,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_negative": 0,
                "known_markers": ["Bajo el gobierno", "Jurar lealtad", "demasiado buena", "con una"],
                "next_step": "Watch as sibling guard; may merge with Select_CString residual if evidence stays small.",
            },
        ),
        AgentSpec(
            agent_key="gender_surface_boundary_guard",
            agent_type="symbolic_guard",
            parent_agent_key="gender_form_swap_subpolicy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="issue_review_gender",
            scope_sql=(
                "old_text LIKE '%Custom(''ES_%' AND ("
                "old_text LIKE '% ,%' OR "
                "old_text LIKE '% :%' OR "
                "old_text LIKE '%[ %')"
            ),
            scope_description="Blocks surface punctuation/spacing defects around CK3 gender helper contexts.",
            default_threshold=None,
            priority=38,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_negative": 3,
                "known_patterns": ["space_before_punctuation", "missing_space_after_token"],
                "next_step": "Keep as boundary; repairs should be handled by surface repair policy.",
            },
        ),
        AgentSpec(
            agent_key="gender_false_reopen_safe_candidate",
            agent_type="symbolic_subpolicy",
            parent_agent_key="gender_form_swap_subpolicy",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_release_candidate",
            scope_group="issue_review_gender",
            scope_sql=(
                "old_text LIKE '%GetSheHe%' OR "
                "old_text LIKE '%Select_CString%' OR "
                "old_text LIKE '%scales_of_power%'"
            ),
            scope_description="Observes reviewed gender-token false reopens that appear safe under the active policy.",
            default_threshold=None,
            priority=39,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_positive": 4,
                "auto_apply_allowed": 0,
                "next_step": "Needs checkpoint before any guarded release integration.",
            },
        ),
        AgentSpec(
            agent_key="gender_context_router",
            agent_type="subcoordinator",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="route_and_arbitrate",
            scope_group="issue_review_gender",
            scope_sql=(
                "old_text LIKE '%Custom(''ES_%' OR "
                "old_text LIKE '%Select_CString%' OR "
                "old_text LIKE '%LocalPlayerString%'"
            ),
            scope_description="Routes broad gender-token cases to domain/context specialists instead of treating them as direct releases.",
            default_threshold=None,
            priority=40,
            dashboard_group="Token Gate",
            notes={
                "source_queue": "ml_issue_review_queue_runs:4",
                "evidence_context": 75,
                "next_step": "Use context router output to seed narrower repair/correction queues.",
            },
        ),
        AgentSpec(
            agent_key="short_label_single_issue_low_risk_sampler",
            agent_type="review_sampler",
            parent_agent_key="micro_short_label_style",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="profile_and_sample",
            scope_group="issue_short_label_single_issue_opportunity",
            scope_sql=None,
            scope_description=(
                "Profiles short-label single-issue reopen candidates into low-risk review buckets "
                "so future checkpoints can learn reusable patterns instead of closing segments one by one."
            ),
            default_threshold=None,
            priority=41,
            dashboard_group="Issue Network",
            notes={
                "diagnostic_table": "ml_issue_short_label_single_issue_opportunity_runs",
                "latest_diagnostic_run_id": 1,
                "partial_coverage_run_id": 59,
                "candidate_count": 4728,
                "reviewable_count": 4119,
                "held_count": 609,
                "review_queue_run_id": 36,
                "reviewed_count": 125,
                "checkpointed_positive_count": 103,
                "retained_for_learning_count": 22,
                "decision_runs": [47, 48, 49, 50, 51],
                "checkpoint_runs": [13, 14, 15, 16, 17],
                "reviewable_profiles": {
                    "single_low_token_short_text": 1135,
                    "single_medium_token_short_ui": 1128,
                    "single_activity_low_token": 709,
                    "single_rules_tooltip_low_token": 656,
                    "single_no_token_short_label": 491,
                },
                "reviewed_profile_results": {
                    "single_rules_tooltip_low_token": {
                        "safe": 22,
                        "retained": 3,
                        "estimated_safe_rate": 0.88,
                    },
                    "single_no_token_short_label": {
                        "safe": 21,
                        "retained": 4,
                        "estimated_safe_rate": 0.84,
                    },
                    "single_low_token_short_text": {
                        "safe": 21,
                        "retained": 4,
                        "estimated_safe_rate": 0.84,
                    },
                    "single_medium_token_short_ui": {
                        "safe": 20,
                        "retained": 5,
                        "estimated_safe_rate": 0.80,
                    },
                    "single_activity_low_token": {
                        "safe": 19,
                        "retained": 6,
                        "estimated_safe_rate": 0.76,
                    },
                },
                "observed_retention_causes": {
                    "semantic_compression_or_drift": 5,
                    "surface_repair": 8,
                    "domain_or_custom_loc_context": 2,
                    "dynamic_gender_or_token_recovery": 4,
                    "activity_semantic_drift": 3,
                },
                "guardrail": "Shadow sampler only; no coverage close, no repair, no source/output read, no production write.",
                "next_step": "Promote only profile-specific guarded policies after repeated samples confirm low false-safe rate; route activity semantic drift and dynamic gender/token recovery to repair microagents.",
            },
        ),
        AgentSpec(
            agent_key="title_preserved_direction_residual_guard",
            agent_type="symbolic_guard",
            parent_agent_key="titles",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="title_preserved_old_output",
            scope_sql=(
                "relative_path = 'titles_l_spanish.yml' "
                "AND ("
                "spanish_text LIKE '%Noreste%' OR "
                "spanish_text LIKE '%Sureste%' OR "
                "spanish_text LIKE '%Occidental%' OR "
                "spanish_text LIKE 'Ruta de %' OR "
                "old_text LIKE '%Noreste%' OR "
                "old_text LIKE '%Sureste%' OR "
                "old_text LIKE '%Occidental%' OR "
                "old_text LIKE 'Ruta de %'"
                ")"
            ),
            scope_description=(
                "Blocks preserved title strings where Spanish direction words or route labels remain visible."
            ),
            default_threshold=None,
            priority=36,
            dashboard_group="Titles",
            notes={
                "source_queue": "reports/20260603_165420_title_preserved_regression_queue.txt",
                "evidence_positive": 0,
                "evidence_negative": 10,
                "known_markers": ["Noreste", "Sureste", "Occidental", "Ruta de"],
                "next_step": "Create a shadow audit queue and require zero false blocks before active guard use.",
            },
        ),
        AgentSpec(
            agent_key="title_preserved_exonym_residual_guard",
            agent_type="symbolic_guard",
            parent_agent_key="titles",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="title_preserved_old_output",
            scope_sql=(
                "relative_path = 'titles_l_spanish.yml' "
                "AND ("
                "spanish_text IN ('Bruselas', 'Egipto', 'Rusia', 'Venecia', 'Polonia', 'Italia', 'Nankin') OR "
                "old_text IN ('Bruselas', 'Egipto', 'Rusia', 'Venecia', 'Polonia', 'Italia', 'Nankin')"
                ")"
            ),
            scope_description=(
                "Blocks common Spanish exonyms that should not be released only because old/output preserved Spanish."
            ),
            default_threshold=None,
            priority=37,
            dashboard_group="Titles",
            notes={
                "source_queue": "reports/20260603_165420_title_preserved_regression_queue.txt",
                "evidence_positive": 0,
                "evidence_negative": 9,
                "known_markers": ["Bruselas", "Egipto", "Rusia", "Venecia", "Polonia", "Italia", "Nankin"],
                "next_step": "Expand marker list from reviewed negatives and keep as guard, not release policy.",
            },
        ),
        AgentSpec(
            agent_key="title_preserved_demonym_suffix_guard",
            agent_type="symbolic_guard",
            parent_agent_key="title_adjectives",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="title_preserved_old_output",
            scope_sql=(
                "relative_path = 'titles_l_spanish.yml' "
                "AND source_key LIKE '%\\_adj%' ESCAPE '\\' "
                "AND (source_key LIKE '%norman%' OR source_key LIKE '%anglo_saxon%')"
            ),
            scope_description=(
                "Blocks preserved cultural demonyms where Spanish suffix style appears inside title adjective keys."
            ),
            default_threshold=None,
            priority=38,
            dashboard_group="Titles",
            notes={
                "source_queue": "reports/20260603_165420_title_preserved_regression_queue.txt",
                "evidence_positive": 0,
                "evidence_negative": 3,
                "known_examples": ["norfolques", "bedfordes", "oestewalano"],
                "next_step": "Collect more adjective negatives before creating any positive adjective release.",
            },
        ),
        AgentSpec(
            agent_key="title_preserved_safe_exonym_release",
            agent_type="symbolic_subpolicy",
            parent_agent_key="title_names",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="guarded_release_candidate",
            scope_group="title_preserved_old_output",
            scope_sql=(
                "relative_path = 'titles_l_spanish.yml' "
                "AND source_key IN ("
                "'k_bavaria', 'k_wales', 'k_trebizond', 'k_moldova', 'k_krete', 'k_england', "
                "'d_danes', 'd_brabant', 'd_turkmens', 'c_oldenburg', 'c_tubingen', 'b_combourg'"
                ")"
            ),
            scope_description=(
                "Releases only reviewed PT-BR safe exonyms from preserved title output, after negative guards run first."
            ),
            default_threshold=None,
            priority=39,
            dashboard_group="Titles",
            notes={
                "source_queue": "reports/20260603_165420_title_preserved_regression_queue.txt",
                "evidence_positive": 12,
                "evidence_negative": 0,
                "requires_guards": [
                    "title_preserved_direction_residual_guard",
                    "title_preserved_exonym_residual_guard",
                    "title_preserved_demonym_suffix_guard",
                ],
                "next_step": "Promote only through coordinator after repeated reviewed positives and zero guard conflict.",
            },
        ),
        AgentSpec(
            agent_key="glossary_historical_label_boundary",
            agent_type="subspecialist",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="experimental",
            operational_state="dry_run",
            decision_role="needs_policy_evidence",
            scope_group="token_policy_glossary_labels",
            scope_sql=None,
            scope_description=(
                "Separates historical event glossary labels from broad same-key glossary label translations."
            ),
            default_threshold=None,
            priority=42,
            dashboard_group="Token Gate",
            notes={
                "reason": "Same-key Glossary label translations are often safe, but historical event labels can carry narrative exceptions.",
                "target_rule_family": "glossary_historical_rebellion_label_needs_subpolicy",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_direct_name_reference_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_dynamic_spanish_literal_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="token_policy_select_cstring_direct_name",
            scope_sql=None,
            scope_description=(
                "Accepts only reviewed Select_CString removals where the confirmed branch selects between a local "
                "literal and the same scope GetShortUIName, while English and corrected PT-BR both use the direct name."
            ),
            default_threshold=None,
            priority=41,
            dashboard_group="Token Gate",
            notes={
                "bridge_run_id": 3,
                "shadow_run_id": 5,
                "checkpoint_run_id": 6,
                "lifecycle_policy_run_id": 2,
                "shadow_ready": 1,
                "checkpoint_allowed": 1,
                "checkpoint_blocked": 0,
                "maturity_status": "shadow_validated_microcase",
                "safe_scope": "host.GetShortUIName replacing Select_CString(host.IsLocalPlayer, literal, host.GetShortUIName)",
            },
        ),
        AgentSpec(
            agent_key="localplayerstring_war_join_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_dynamic_spanish_literal_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="token_policy_localplayerstring_war_join",
            scope_sql=None,
            scope_description=(
                "Accepts only liege war-join messages where Character.LocalPlayerString is removed and the PT-BR "
                "sentence becomes neutral with Character.GetShortUIName plus 'se juntara/nao se juntara'."
            ),
            default_threshold=None,
            priority=41,
            dashboard_group="Token Gate",
            notes={
                "bridge_run_id": 3,
                "shadow_run_id": 6,
                "checkpoint_run_id": 5,
                "lifecycle_policy_run_id": 2,
                "shadow_ready": 2,
                "checkpoint_allowed": 2,
                "checkpoint_blocked": 0,
                "maturity_status": "shadow_validated_microcase",
                "safe_keys": ["liege_will_join_war_message", "liege_will_not_join_war_message"],
            },
        ),
        AgentSpec(
            agent_key="same_token_boundary_repair_production_auditor",
            agent_type="guard",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state="dry_run",
            decision_role="controlled_production_readiness_audit",
            scope_group="lifecycle_auto_confirmation_same_token_repair",
            scope_sql=None,
            scope_description=(
                "Audits same-token boundary repair lifecycle rows before production consumes them, separating "
                "real repairs from no-op observations and estimating closed-state gain without applying output."
            ),
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "audit_run_id": 2,
                "lifecycle_run_id": 10,
                "segment_state_run_id": 129,
                "candidate_count": 116,
                "eligible_controlled_production": 71,
                "estimated_closed_gain": 71,
                "real_repair_count": 70,
                "noop_observation_count": 46,
                "blocked_count": 1,
                "required_steps": [
                    "promote_corrected_text_to_confirmation",
                    "write_corrected_text_to_output",
                    "include_same_token_boundary_repair_lifecycle_in_segment_state",
                ],
                "safety_note": "Read-only audit: no confirmation, output, or segment-state writes.",
            },
        ),
        AgentSpec(
            agent_key="gender_pronoun_set_boundary",
            agent_type="subspecialist",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="needs_policy_evidence",
            scope_group="token_policy_pronoun_sets",
            scope_sql=None,
            scope_description=(
                "Studies cases where gender article/suffix tokens are replaced by a set of "
                "subject, object, and possessive pronouns."
            ),
            default_threshold=None,
            priority=43,
            dashboard_group="Token Gate",
            notes={
                "reason": "The current pronoun-added policy is safe for single pronouns, but pronoun sets mix grammar roles.",
                "target_rule_family": "single_scope_missing_gender_to_pronoun_set_english_aligned",
            },
        ),
        AgentSpec(
            agent_key="multi_scope_gender_pronoun_boundary",
            agent_type="subspecialist",
            parent_agent_key="gender_pronoun_set_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_pronoun_sets",
            scope_sql=None,
            scope_description=(
                "Separates multi-scope ROOT/actor gender suffix and pronoun additions from safe single-scope pronoun rules."
            ),
            default_threshold=None,
            priority=44,
            dashboard_group="Token Gate",
            notes={
                "reason": "Multi-scope gender suffix plus pronoun additions mix speaker state, target state, and narrative role.",
                "target_rule_family": "multi_scope_gender_suffix_pronoun_added",
            },
        ),
        AgentSpec(
            agent_key="contextual_gender_pronoun_boundary",
            agent_type="subspecialist",
            parent_agent_key="gender_pronoun_set_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_pronoun_contextual",
            scope_sql=None,
            scope_description=(
                "Separates contextual ROOT/actor gender suffix plus pronoun rewrites by scene before any broad policy is considered."
            ),
            default_threshold=None,
            priority=45,
            dashboard_group="Token Gate",
            notes={
                "reason": "These rewrites combine fluency, referent binding, and speaker/target gender, so mixed evidence must be scene-bounded.",
                "target_rule_family": "missing_gender_plus_contextual_pronoun_rewrite",
            },
        ),
        AgentSpec(
            agent_key="multi_character_name_pronoun_boundary",
            agent_type="subspecialist",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_mixed_name_gender",
            scope_sql=None,
            scope_description=(
                "Separates multi-character name, gender, and pronoun rewrites into scene-level blockers before release."
            ),
            default_threshold=None,
            priority=46,
            dashboard_group="Token Gate",
            notes={
                "reason": "Multi-character rewrites often change referent binding, narrator fluency, and name presentation together.",
                "target_rule_family": "multi_character_name_pronoun_needs_subpolicy",
            },
        ),
        AgentSpec(
            agent_key="auto_confirmation_reopen_reconciler",
            agent_type="subcoordinator",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state="operational",
            decision_role="lifecycle_reopen_triage",
            scope_group="lifecycle_auto_confirmation",
            scope_sql=None,
            scope_description=(
                "Triage auto-confirmed segments reopened by current ML autofix signals into weak auto confirmations, "
                "mechanical confirmations, curated confirmations, and true repair candidates."
            ),
            default_threshold=None,
            priority=47,
            dashboard_group="Lifecycle",
            notes={
                "reason": "The largest remaining pending block is auto-confirmed output matching its confirmation while the current model requests autofix.",
                "target_final_state": "reopen_auto_confirmed_autofix",
                "audit_table": "auto_confirmation_reopen_audit_runs",
                "queue_table": "auto_confirmation_reopen_guarded_queue_runs",
                "current_guarded_formatting_candidates": 204,
                "current_lifecycle_policy_releases": 204,
                "remaining_formatting_boundaries": 2,
                "current_text_replacement_samples": 146,
                "current_text_replacement_diagnostic_run": 1,
                "text_replacement_needs_subagent": 121,
                "text_replacement_short_policy_candidates": 12,
                "text_replacement_manual_sampling": 12,
                "text_replacement_manual_boundary_review": 1,
                "latest_text_replacement_queue_run_id": 9,
                "latest_text_replacement_diagnostic_run_id": 6,
                "latest_text_replacement_queue_selected": 25,
                "latest_text_replacement_needs_subagent": 23,
                "latest_text_replacement_manual_sampling": 2,
                "latest_text_replacement_top_subfamily": "nickname_dynamic_literal_replacement",
                "requires_no_output_write": True,
            },
        ),
        AgentSpec(
            agent_key="weak_auto_confirmation_reconciler",
            agent_type="subcoordinator",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="weak_auto_sampling_router",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description=(
                "Routes weak auto-confirmed reopen cases into issue boundaries, residual-Spanish boundaries, "
                "dynamic Select_CString hazards, and low-risk confirmation samplers."
            ),
            default_threshold=None,
            priority=48,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "source_guarded_queue_run_id": 7,
                "diagnostic_run_id": 5,
                "sampled_rows": 240,
                "high_risk_rows": 112,
                "medium_risk_rows": 53,
                "low_risk_rows": 75,
                "review_queue_run_ids": [17, 18, 19, 20],
                "latest_guarded_queue_run_id": 10,
                "latest_diagnostic_run_id": 8,
                "latest_sampled_rows": 260,
                "latest_high_risk_rows": 114,
                "latest_medium_risk_rows": 70,
                "latest_low_risk_rows": 76,
                "latest_candidate_short_ui_policy_sampling": 87,
                "latest_needs_subagent_before_policy": 10,
                "latest_spanish_hint_expansion": [
                    "aportaste/aporto",
                    "decidiste/decidio",
                    "deseaste/deseo",
                    "ganas/gana",
                    "intentaras/intentara",
                    "ridiculizaste/ridiculizo",
                ],
                "reason": "weak_auto_confirmation is too broad to promote directly; it needs a router before any lifecycle closure.",
                "promotion_requirement": "Collect reviewed evidence per branch and promote only clean branches to shadow.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="manual_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description=(
                "Inspects weak auto confirmations where validation already detected an issue signal, "
                "without assuming the confirmed text is safe."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "latest_diagnostic_run_id": 20,
                "diagnostic_subfamily": "weak_auto_issue_signal",
                "evidence_count": 73,
                "review_queue_run_id": 47,
                "queued_count": 40,
                "review_decision_run_id": 39,
                "reviewed_count": 40,
                "known_positive_examples": 1,
                "known_negative_examples": 39,
                "latest_boundary_policy_run_id": 11,
                "latest_boundary_rows": 203,
                "latest_issue_boundary_rows": 54,
                "latest_issue_boundary_unclassified": 0,
                "latest_same_token_repair_queue_run_id": 12,
                "latest_same_token_repair_candidates": 116,
                "latest_token_change_repair_queue_run_id": 11,
                "latest_token_change_candidates": 3,
                "latest_token_policy_bridge_run_id": 3,
                "latest_token_policy_bridge_ready": 3,
                "latest_token_subpolicy_lifecycle_run_id": 2,
                "latest_token_subpolicy_lifecycle_released_shadow": 3,
                "latest_token_subpolicy_lifecycle_blocked": 0,
                "latest_token_subpolicy_shadow_runs": {
                    "select_cstring_direct_name_reference": 5,
                    "localplayerstring_war_join_neutralization": 6,
                },
                "latest_token_subpolicy_checkpoint_runs": {
                    "select_cstring_direct_name_reference": 6,
                    "localplayerstring_war_join_neutralization": 5,
                },
                "latest_repair_shadow_run_id": 10,
                "latest_shadow_ready": 116,
                "latest_shadow_repair_ready": 70,
                "latest_shadow_noop_ready": 46,
                "latest_shadow_blocked": 0,
                "latest_shadow_quality_blocked": 0,
                "latest_repair_checkpoint_run_id": 11,
                "latest_repair_lifecycle_run_id": 10,
                "latest_quality_issue_refinement_decision_run_id": 40,
                "latest_token_change_refinement_decision_run_id": 41,
                "learned_boundaries": [
                    "es_helper_narrative",
                    "hostage_context",
                    "dynamic_spanish_literal",
                    "inline_spanish_literal",
                    "credit_spanish_label",
                    "concept_spanish_label",
                ],
                "reason": "Issue signals include token grammar, spacing, literal residue, and other mixed validation concerns.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_es_helper_narrative_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_issue",
            scope_sql=None,
            scope_description="Blocks issue-signal rows where Spanish ES_* gender helpers are embedded in narrative PT-BR prose.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 47,
                "source_review_decision_run_id": 39,
                "negative_examples": 17,
                "latest_boundary_policy_run_id": 9,
                "latest_boundary_examples": 14,
                "repair_route": "manual_or_contextual_gender_rewrite",
                "known_pattern": "Portuguese prose glued to ES_OA/ES_XA/ES_ElLa/ES_DelDela helpers.",
            },
        ),
        AgentSpec(
            agent_key="es_helper_narrative_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_es_helper_narrative_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_es_helper_narrative",
            scope_sql=None,
            scope_description=(
                "Observes reviewed narrative rewrites that remove Spanish ES_* gender helpers from PT-BR prose "
                "while keeping the correction under token-policy governance."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "es_helper_narrative_neutralization",
                "source_boundary_policy": "weak_auto_issue_es_helper_narrative",
                "allowed_buckets": [
                    "review_dynamic_scope_change",
                    "review_gender_token_change",
                    "review_mixed_token_change",
                ],
                "source_review_decision_run_id": 43,
                "seed_examples": 13,
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_hostage_context_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_issue",
            scope_sql=None,
            scope_description="Blocks hostage/captive issue-signal rows where relation terms and ES helpers need contextual PT-BR review.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 47,
                "source_review_decision_run_id": 39,
                "negative_examples": 10,
                "latest_boundary_policy_run_id": 9,
                "latest_boundary_examples": 17,
                "repair_route": "manual_or_contextual_hostage_rewrite",
                "known_pattern": "hostage/cativo/refem wording with dynamic relation and ES_OA article helpers.",
            },
        ),
        AgentSpec(
            agent_key="hostage_context_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_hostage_context_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_hostage_context",
            scope_sql=None,
            scope_description=(
                "Observes reviewed hostage/captive rewrites that remove Spanish ES_* helpers and neutralize "
                "unsafe relation-gender wording with PT-BR forms such as refem or family-neutral phrasing."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "hostage_context_neutralization",
                "source_boundary_policy": "weak_auto_issue_hostage_context",
                "allowed_buckets": [
                    "review_dynamic_scope_change",
                    "review_mixed_token_change",
                ],
                "source_review_decision_run_id": 44,
                "seed_examples": 9,
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_dynamic_spanish_literal_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_issue",
            scope_sql=None,
            scope_description="Blocks issue-signal Select_CString/LocalPlayerString branches that still expose Spanish visible text.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 47,
                "source_review_decision_run_id": 39,
                "negative_examples": 5,
                "latest_boundary_policy_run_id": 11,
                "latest_boundary_examples": 12,
                "latest_token_change_candidates": 3,
                "latest_token_policy_bridge_run_id": 3,
                "latest_token_policy_bridge_ready": 3,
                "latest_select_cstring_direct_name_shadow_run_id": 5,
                "latest_select_cstring_direct_name_ready": 1,
                "latest_war_join_shadow_run_id": 6,
                "latest_war_join_ready": 2,
                "latest_token_subpolicy_lifecycle_run_id": 2,
                "latest_token_subpolicy_lifecycle_released_shadow": 3,
                "latest_token_subpolicy_lifecycle_blocked": 0,
                "repair_route": "token_policy_or_manual_dynamic_rewrite",
                "known_patterns": ["ti/host odio", "Tus/Sus", "te/se war messages", "viste/vio"],
                "learned_safe_repair_subcases": [
                    "Select_CString(host.IsLocalPlayer, literal, host.GetShortUIName) -> host.GetShortUIName when English also uses host.GetShortUIName.",
                    "Character.LocalPlayerString('te','se') removed in liege war join messages with neutral PT-BR 'se juntara/nao se juntara'.",
                ],
            },
        ),
        AgentSpec(
            agent_key="legend_holy_warrior_complex_narrative_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_dynamic_spanish_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="manual_narrative_boundary",
            scope_group="boundary_token_policy_legend_complex_narrative",
            scope_sql=(
                "relative_path = 'dlc/ce1/legends_l_spanish.yml' "
                "AND source_key = 'legend_chapter_famous_deed_holy_warrior'"
            ),
            scope_description=(
                "Legend holy-warrior prose where ES helpers, hero Select_CString, style modifiers, "
                "faith/title tokens, and narrative neutralization are changed together."
            ),
            default_threshold=None,
            priority=57,
            dashboard_group="Token Gate",
            notes={
                "bridge_item_id": 134,
                "segment_id": 38917,
                "boundary_policy": "weak_auto_issue_dynamic_spanish_literal",
                "policy_bucket": "review_select_cstring_change",
                "review_label": "negative_boundary",
                "same_family_active_rows": 116,
                "same_family_reviewed_negative_rows": 5,
                "known_risk": (
                    "Multiple simultaneous changes: ES_XA/ES_OA/ES_ElLa removal, hero Select_CString removal, "
                    "style-variant token normalization, faith/title references, and semantic prose rewrite."
                ),
                "maturity_status": "manual_narrative_boundary",
                "promotion_requirement": (
                    "Do not promote from this single case. Needs a dedicated legend narrative queue with "
                    "separate positive/negative evidence before any subpolicy shadow."
                ),
                "safety_note": "Boundary only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_inline_spanish_literal_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_issue",
            scope_sql=None,
            scope_description="Blocks issue-signal rows with visible Spanish literal words inside otherwise PT-BR text.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 47,
                "source_review_decision_run_id": 39,
                "negative_examples": 3,
                "latest_boundary_policy_run_id": 9,
                "latest_boundary_examples": 7,
                "repair_route": "same_token_boundary_repair_when_tokens_match",
                "known_patterns": ["casi", "demasiado", "perfecto/fuerza"],
            },
        ),
        AgentSpec(
            agent_key="laamp_hanger_on_complex_narrative_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_inline_spanish_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="manual_narrative_boundary",
            scope_group="boundary_token_policy_laamp_complex_narrative",
            scope_sql=(
                "relative_path = 'laamp_contract_schemes_l_spanish.yml' "
                "AND source_key = 'laamp_base_contract_schemes.0021.desc'"
            ),
            scope_description=(
                "LAAMP contract prose where employer ES helpers, Root Select_CString hanger-on text, "
                "gendered role tokens, pronouns, and narrative style are rewritten together."
            ),
            default_threshold=None,
            priority=57,
            dashboard_group="Token Gate",
            notes={
                "bridge_item_id": 162,
                "segment_id": 159044,
                "boundary_policy": "weak_auto_issue_inline_spanish_literal",
                "policy_bucket": "review_select_cstring_change",
                "review_label": "negative_boundary",
                "same_family_active_rows": 1595,
                "same_family_reviewed_negative_rows": 4,
                "known_risk": (
                    "Multiple simultaneous changes: ES_ElLa/ES_XA/ES_OA removal, Spanish #EMP quiera#! repair, "
                    "otra gorrona/otro gorron neutralization, employer pronoun rebinding, and fluency rewrite."
                ),
                "maturity_status": "manual_narrative_boundary",
                "promotion_requirement": (
                    "Do not promote from this single case. Needs a LAAMP narrative queue with scene-role "
                    "subfamilies before any automatic token subpolicy."
                ),
                "safety_note": "Boundary only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_credit_spanish_label_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_issue",
            scope_sql=None,
            scope_description="Blocks credits labels that still use Spanish wording or Spanish credit-role syntax.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 47,
                "source_review_decision_run_id": 39,
                "negative_examples": 3,
                "latest_boundary_policy_run_id": 9,
                "latest_boundary_examples": 3,
                "repair_route": "same_token_boundary_repair",
                "known_patterns": ["JEFES DE PROYECTO", "Artistas de personaje", "Gestion de relaciones tecnologicas"],
            },
        ),
        AgentSpec(
            agent_key="weak_auto_issue_concept_spanish_label_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_issue",
            scope_sql=None,
            scope_description="Blocks concept links whose visible label is still Spanish even though the CK3 concept token is valid.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 47,
                "source_review_decision_run_id": 39,
                "negative_examples": 1,
                "latest_boundary_policy_run_id": 9,
                "latest_boundary_examples": 1,
                "repair_route": "same_token_boundary_repair",
                "known_pattern": "Concept('culture_tradition', 'tradicion') -> visible PT-BR label.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_dynamic_spanish_literal_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Checks weak auto confirmations where Select_CString or LocalPlayerString still contains Spanish visible text.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "latest_diagnostic_run_id": 8,
                "diagnostic_subfamily": "weak_auto_dynamic_spanish_literal",
                "evidence_count": 20,
                "review_queue_run_id": 20,
                "queued_count": 17,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "latest_hint_expansion_reclassified_low_risk": 5,
                "reason": "Dynamic branches can look structurally valid while still displaying Spanish strings in game.",
            },
        ),
        AgentSpec(
            agent_key="tour_title_gendered_possessive_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_dynamic_spanish_literal_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_tour_title_gendered_possessive",
            scope_sql=None,
            scope_description=(
                "Observes the reviewed tour title where plain 'Meu' becomes a visiting_liege.IsFemale "
                "Select_CString possessive while the title and Hunter/Cacador label remain preserved."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "tour_title_gendered_possessive_neutralization",
                "source_boundary_policy": "weak_auto_dynamic_select_cstring_spanish_literal",
                "allowed_bucket": "review_select_cstring_change",
                "seed_examples": 1,
                "safe_keys": ["tour_grounds_events.3004.title"],
                "known_pattern": "Meu + visiting_liege title becomes Select_CString(visiting_liege.IsFemale, Minha, Meu).",
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_residual_spanish_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Checks weak auto confirmations with visible Spanish residue outside dynamic token branches.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "diagnostic_subfamily": "weak_auto_residual_spanish_literal",
                "evidence_count": 10,
                "review_queue_run_id": 18,
                "queued_count": 10,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "reason": "Residual Spanish is usually a blocker, but glossary/proper-name false positives must be separated before hard blocking.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_exact_sampler",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="positive_sampler_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Samples tiny exact-match weak auto confirmations that may become a safe lifecycle closure branch.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "latest_diagnostic_run_id": 8,
                "diagnostic_subfamily": "weak_auto_tiny_exact",
                "evidence_count": 38,
                "review_queue_run_id": 30,
                "queued_count": 20,
                "known_positive_examples": 10,
                "known_negative_examples": 7,
                "known_context_examples": 1,
                "known_manual_exception_examples": 2,
                "maturity_status": "blocked_boundary",
                "latest_review_queue_run_id": 49,
                "latest_review_decision_run_id": 38,
                "latest_reviewed_count": 40,
                "latest_positive_count": 26,
                "latest_negative_count": 7,
                "latest_context_count": 1,
                "latest_manual_exception_count": 6,
                "latest_negative_rate": 0.175,
                "latest_unclassified_boundary_policy_run_id": 8,
                "latest_unclassified_before": 19,
                "latest_unclassified_after": 0,
                "learned_boundary": (
                    "Tiny exact rows are not a single safe family: local-player 'sua pessoa', Spanish runtime verbs, "
                    "Spanish geographic directions, English interjections, semantic short labels, custom-loc article helpers, "
                    "style labels, credit titles, possessives, and direct-object preposition cases must be separated."
                ),
                "reason": "Tiny exact rows are the likely positive control group, but still need reviewed precision before release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_exact_clean_ui_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="dry_run",
            decision_role="guarded_positive_candidate",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description=(
                "Tracks reviewed tiny exact rows with visible PT-BR UI text, no issue signal, no Spanish residue, "
                "and no helper/name/cultural-context boundary."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 49,
                "source_review_decision_run_id": 38,
                "reviewed_count": 40,
                "positive_seed_count": 26,
                "negative_boundary_count": 7,
                "manual_exception_count": 6,
                "context_count": 1,
                "maturity_status": "positive_candidate_needs_shadow_policy",
                "shadow_policy_name": "weak_auto_short_exact_clean_ui_shadow_v1",
                "excluded_boundaries": [
                    "custom_loc_helpers",
                    "proper_names",
                    "cultural_title_names",
                    "local_player_sua_pessoa",
                    "dynamic_spanish_verbs",
                    "spanish_geo_directions",
                    "english_interjections",
                    "semantic_label_mismatch",
                ],
                "promotion_requirement": (
                    "Run shadow only first; do not promote until the positive branch is proven after the negative "
                    "children remain blocked or repaired."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_local_player_sua_pessoa_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short local-player strings that render as 'sua pessoa' and require a PT-BR second-person rewrite.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 49,
                "negative_examples": 2,
                "repair_route": "same_token_boundary_repair",
                "recommended_correction": "Prefer 'voce' semantics for local-player display when tokens permit.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_dynamic_spanish_verb_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks tiny exact rows where Select_CString runtime alternatives still contain Spanish verbs.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 49,
                "negative_examples": 18,
                "latest_boundary_policy_run_id": 8,
                "repair_route": "same_token_or_manual_dynamic_repair",
                "known_patterns": ["ablandaste/ablandó", "assassinated local-player phrasing without safe correction"],
            },
        ),
        AgentSpec(
            agent_key="short_dynamic_spanish_verb_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_dynamic_spanish_verb_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_short_dynamic_spanish_verb",
            scope_sql=None,
            scope_description=(
                "Observes reviewed short Select_CString rewrites where Spanish runtime verbs are removed and "
                "the PT-BR correction keeps the required CK3 references."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "short_dynamic_spanish_verb_neutralization",
                "source_boundary_policy": "weak_auto_short_dynamic_spanish_verb",
                "allowed_bucket": "review_select_cstring_change",
                "seed_examples": 2,
                "known_patterns": [
                    "Character.IsLocalPlayer aportaste/aporto -> passive funded-by PT-BR sentence.",
                    "actor.IsLocalPlayer ganas/gana -> invariant PT-BR 'ganha' with title/vassal tokens preserved.",
                ],
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="hunt_activity_select_cstring_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_dynamic_spanish_literal_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_hunt_activity_select_cstring",
            scope_sql=None,
            scope_description=(
                "Observes reviewed hunt activity tooltips where redundant host.IsLocalPlayer Select_CString text "
                "is collapsed to stable PT-BR while activity and danger tokens are preserved."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "hunt_activity_select_cstring_neutralization",
                "source_boundary_policy": "weak_auto_issue_dynamic_spanish_literal",
                "allowed_bucket": "review_select_cstring_change",
                "seed_examples": 2,
                "safe_keys": ["hunt_activity_chase_tt", "hunt_activity_captive_tt"],
                "known_pattern": "host.IsLocalPlayer tentar/tentara redundant selector with hunt animal article neutralization.",
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="coronation_title_es_helper_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_issue_inline_spanish_literal_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_coronation_title_es_helper",
            scope_sql=None,
            scope_description=(
                "Observes reviewed coronation dialogue where Spanish ES title helpers are removed and "
                "the PT-BR correction preserves ROOT.Char title references with fluent phrasing."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "coronation_title_es_helper_neutralization",
                "source_boundary_policy": "weak_auto_issue_inline_spanish_literal",
                "allowed_bucket": "review_gender_token_change",
                "seed_examples": 1,
                "safe_keys": ["guest_intent_coronation_events.0100.desc.magnificence_loss_subtle"],
                "known_pattern": "ROOT.Char ES_OA/ES_ElLa/ES_AlAla removed while title tokens remain aligned to English.",
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_geo_spanish_direction_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short title/location labels whose Spanish direction term should be localized to PT-BR.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 49,
                "negative_examples": 1,
                "known_pattern": "Huaiyuan Sur -> Huaiyuan do Sul",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_english_interjection_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short English interjections that survived as exact strings and need PT-BR flavor.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 49,
                "negative_examples": 1,
                "known_pattern": "ahem... -> ha...",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_semantic_label_mismatch_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short labels that are structurally valid but semantically wrong in PT-BR game UI.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 49,
                "negative_examples": 1,
                "known_pattern": "Intervencao Legal -> Intromissao Legal for legal meddling interaction.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_custom_loc_article_helper_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks ES custom-localization article helper definitions that are structural helpers rather than PT-BR safe text.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_boundary_policy_run_id": 8,
                "negative_examples": 2,
                "known_pattern": "Loc_ES_el_GetShortUIName article-helper rows.",
                "promotion_requirement": "Keep as boundary/no-op; do not learn as positive static-token evidence.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_adjective_label_style_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short adjective/personality labels that are literal or awkward even when structurally valid.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_boundary_policy_run_id": 8,
                "negative_examples": 2,
                "known_pattern": "boldness_strong_neg_adj style boundary.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_credit_title_style_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks credit title strings where word order needs polished PT-BR instead of exact reuse.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_boundary_policy_run_id": 8,
                "negative_examples": 2,
                "known_pattern": "TITLE_LEAD_AI_PROGRAMMERS word-order boundary.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_possessive_phrase_style_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short possessive phrases whose CK3 dynamic possessive wording is awkward or unsafe in PT-BR.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_boundary_policy_run_id": 8,
                "negative_examples": 1,
                "known_pattern": "diarch loyalty vegetarianism possessive phrase boundary.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_direct_object_preposition_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_short_exact_sampler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_short_exact",
            scope_sql=None,
            scope_description="Blocks short event strings where direct object grammar wrongly carries preposition/crasis.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_boundary_policy_run_id": 8,
                "negative_examples": 2,
                "known_pattern": "child_is_excited: alegrar a menina/o menino instead of a prepositional object.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_confirmation_sampler",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="manual_sampler_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Samples general exact-match weak auto confirmations that do not fit a narrower branch yet.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "diagnostic_subfamily": "weak_auto_general_exact",
                "evidence_count": 39,
                "review_queue_run_id": 22,
                "queued_count": 25,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "reason": "General exact matches need manual sampling before deciding whether they are safe or need more specific subagents.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_escape_delta_sampler",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="positive_sampler_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Samples weak auto confirmations whose only observed delta is display-equivalent escaping.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "diagnostic_subfamily": "weak_auto_escape_delta_equivalence",
                "evidence_count": 15,
                "review_queue_run_id": 21,
                "queued_count": 15,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "reason": "Escape deltas are likely mechanical positives but need sampled proof before lifecycle closure.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_empty_or_token_sampler",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="positive_sampler_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Samples blank, empty, or token-only weak auto confirmations that may be safely closed.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "latest_diagnostic_run_id": 16,
                "diagnostic_subfamily": "weak_auto_empty_or_token_exact",
                "evidence_count": 9,
                "review_queue_run_id": 31,
                "queued_count": 9,
                "known_positive_examples": 6,
                "known_negative_examples": 1,
                "known_manual_exception_examples": 2,
                "maturity_status": "blocked_boundary",
                "strict_static_child_positive_examples": 169,
                "strict_static_child_shadow_ready": 169,
                "strict_static_child_shadow_blocked": 0,
                "reason": (
                    "Empty/token-only rows are strong positive candidates, but the broad parent remains a boundary "
                    "because embedded literals, source-visible semantic deltas, and runtime helpers are separate risks."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_static_token_only_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_empty_or_token_sampler",
            model_kind=None,
            status="active",
            operational_state="operational",
            decision_role="guarded_lifecycle_policy",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description=(
                "Separates reviewed weak-auto empty/token rows whose visible text outside CK3 tokens contains no letters."
            ),
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 16,
                "source_review_queue_run_ids": [39, 40],
                "known_positive_examples": 169,
                "known_negative_examples": 0,
                "known_manual_exception_examples": 0,
                "shadow_policy_name": "weak_auto_static_token_only_shadow_v1",
                "latest_shadow_policy_run_id": 16,
                "latest_shadow_ready": 169,
                "latest_shadow_blocked": 0,
                "latest_checkpoint_run_id": 1,
                "latest_checkpoint_name": "weak_auto_static_token_only_guarded_checkpoint_v1",
                "latest_checkpoint_allowed": 169,
                "latest_checkpoint_blocked": 0,
                "checkpoint_status": "ready_for_guarded_lifecycle_policy",
                "latest_lifecycle_policy_run_id": 2,
                "latest_lifecycle_policy_name": "guarded_static_token_only_reopen_closure",
                "latest_lifecycle_released": 169,
                "latest_lifecycle_blocked": 0,
                "latest_segment_state_run_id": 124,
                "latest_segment_state_guarded_lifecycle_closed_total": 373,
                "promotion_status": "released_to_guarded_lifecycle",
                "production_release_allowed": True,
                "excluded_surface_token_only_boundaries": {
                    "embedded_literal_token": 65,
                    "source_visible_text": 48,
                    "es_custom_helper": 10,
                },
                "reason": (
                    "This strict branch now covers weak-auto rows that are token/static in English, Spanish, and PT-BR, "
                    "with no embedded literal constructors and no ES custom-localization runtime helpers."
                ),
                "promotion_requirement": (
                    "Continue monitoring production snapshots; embedded literals, source-visible deltas, and ES helpers "
                    "must remain routed to their boundary agents before any broader weak-auto release."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_embedded_literal_token_specialist",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="embedded_token_literal_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description=(
                "Separates weak-auto rows with no visible letters outside CK3 tokens but with translatable "
                "literals hidden inside Select_CString, LocalPlayerString, or Concept constructors."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 18,
                "boundary_queue_run_id": 20,
                "review_queue_run_id": 45,
                "review_decision_run_id": 34,
                "specialist_audit_run_id": 40,
                "aggregate_specialist_audit_run_id": 44,
                "candidate_count": 38,
                "queued_count": 38,
                "reviewed_count": 38,
                "positive_count": 18,
                "negative_count": 20,
                "negative_rate": 0.5263,
                "diagnostic_subfamilies": [
                    "weak_auto_embedded_literal_token_exact",
                    "weak_auto_embedded_spanish_literal_token",
                ],
                "maturity_status": "blocked_boundary",
                "known_risk": (
                    "Token-only surface can still hide Spanish or PT-BR grammar inside dynamic token arguments; "
                    "these rows must not be promoted by the strict static-token-only policy."
                ),
                "learned_boundary": (
                    "PT-BR embedded literals such as seu/sua, Examinando/Examinanda, and preserved glossary names "
                    "can be positive evidence; Spanish visible labels inside Glossary or Select_CString remain "
                    "negative even when the outer token shape is structurally intact."
                ),
                "recommended_action": (
                    "Split into embedded_possessive_runtime_safe, embedded_glossary_visible_label, and "
                    "embedded_select_cstring_spanish_literal subagents before any release policy."
                ),
                "reason": (
                    "The expanded static-token audit found rows such as Select_CString branches with verbs "
                    "inside tokens, so this subagent owns that boundary before any lifecycle closure."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_embedded_possessive_runtime_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_embedded_literal_token_specialist",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="guarded_positive_candidate",
            scope_group="lifecycle_auto_confirmation_weak_auto_embedded_literal",
            scope_sql=None,
            scope_description=(
                "Tracks embedded Select_CString possessive/runtime literals that are already PT-BR "
                "and preserve CK3 runtime context without Spanish residue."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 45,
                "source_review_decision_run_id": 34,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 13,
                "positive_count": 13,
                "negative_count": 0,
                "maturity_status": "positive_candidate_needs_shadow_policy",
                "latest_shadow_policy_run_id": 18,
                "latest_shadow_policy_name": "weak_auto_embedded_possessive_runtime_shadow_v1",
                "latest_shadow_ready": 13,
                "latest_shadow_blocked": 25,
                "positive_pattern": "Select_CString local-player possessive variants such as seu/sua/do seu senhor.",
                "promotion_requirement": (
                    "Generate a scoped shadow policy and confirm zero token or Spanish-literal blockers before "
                    "considering guarded lifecycle release."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_embedded_glossary_visible_label_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_embedded_literal_token_specialist",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_embedded_literal",
            scope_sql=None,
            scope_description=(
                "Reviews visible labels inside Glossary/Concept-like tokens where the outer CK3 token is safe "
                "but the displayed label may still be Spanish or culturally wrong."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 45,
                "source_review_decision_run_id": 34,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 18,
                "positive_count": 2,
                "negative_count": 16,
                "negative_rate": 0.8889,
                "maturity_status": "blocked_boundary",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_embedded_glossary_visible_label_boundary_v1",
                "latest_boundary_policy_count": 16,
                "latest_boundary_repair_queue_run_ids": [1, 2, 12],
                "latest_boundary_repair_same_token": 16,
                "latest_boundary_repair_token_change": 14,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 14,
                "latest_token_policy_bridge_primary_bucket": "review_mixed_token_change",
                "latest_token_subpolicy_shadow_run_id": 2,
                "latest_token_subpolicy_shadow_name": "glossary_visible_label_ptbr_translation",
                "latest_token_subpolicy_shadow_ready": 14,
                "latest_token_subpolicy_shadow_blocked": 0,
                "latest_token_subpolicy_checkpoint_run_id": 2,
                "latest_token_subpolicy_checkpoint_allowed": 14,
                "latest_token_subpolicy_checkpoint_blocked": 0,
                "latest_same_token_repair_shadow_run_id": 10,
                "latest_same_token_repair_shadow_ready": 16,
                "latest_same_token_repair_shadow_blocked": 0,
                "latest_same_token_repair_noop_ready": 16,
                "latest_same_token_repair_lifecycle_run_id": 10,
                "latest_same_token_repair_lifecycle_released": 16,
                "latest_same_token_repair_lifecycle_blocked": 0,
                "latest_current_same_token_repair_ready": 0,
                "latest_current_same_token_noop_ready": 16,
                "learned_safe_noop_subcase": "merit_rank Glossary labels where PT-BR overlaps Spanish but English confirms lower-upper/upper-upper semantics.",
                "negative_patterns": [
                    "Spanish rank labels such as inferior-medio and superior-bajo",
                    "Spanish I Ching labels such as Cielo, Trueno, Agua and Montana",
                    "Historical/proper labels that need PT-BR convention review",
                ],
                "learned_safe_repair_subcase": (
                    "Glossary visible-label token deltas where only the human-facing label is translated to PT-BR "
                    "and the internal glossary ID is preserved."
                ),
                "recommended_action": (
                    "Keep the parent as boundary, but monitor the glossary_visible_label_ptbr_translation "
                    "subpolicy as a narrow shadow repair path before any lifecycle release."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_embedded_select_cstring_spanish_literal_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_embedded_literal_token_specialist",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_embedded_literal",
            scope_sql=None,
            scope_description=(
                "Blocks Select_CString/LocalPlayerString rows whose visible runtime alternatives still contain "
                "Spanish verbs, adjectives, or spelling."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 45,
                "source_review_decision_run_id": 34,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 4,
                "positive_count": 0,
                "negative_count": 4,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_embedded_select_cstring_spanish_literal_boundary_v1",
                "latest_boundary_policy_count": 4,
                "latest_boundary_repair_queue_run_ids": [1, 2],
                "latest_boundary_repair_same_token": 2,
                "latest_boundary_repair_token_change": 2,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 2,
                "latest_token_policy_bridge_primary_bucket": "review_select_cstring_change",
                "latest_token_subpolicy_shadow_run_id": 1,
                "latest_token_subpolicy_shadow_ready": 2,
                "latest_token_subpolicy_shadow_blocked": 0,
                "latest_token_subpolicy_checkpoint_run_id": 1,
                "latest_token_subpolicy_checkpoint_allowed": 2,
                "latest_token_subpolicy_checkpoint_blocked": 0,
                "latest_same_token_repair_shadow_run_id": 2,
                "latest_same_token_repair_shadow_ready": 2,
                "latest_same_token_repair_shadow_blocked": 0,
                "latest_same_token_repair_lifecycle_released": 2,
                "latest_same_token_repair_lifecycle_blocked": 0,
                "negative_patterns": [
                    "resultas/resulta inside injury tooltips",
                    "Exhibido inside artifact equipped labels",
                ],
                "promotion_requirement": "This is a blocker path, not a safe release path.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_embedded_concept_label_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_embedded_literal_token_specialist",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="guarded_positive_candidate",
            scope_group="lifecycle_auto_confirmation_weak_auto_embedded_literal",
            scope_sql=None,
            scope_description=(
                "Tracks Concept labels whose visible embedded text is already PT-BR and whose CK3 concept key "
                "is preserved."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 45,
                "source_review_decision_run_id": 34,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 2,
                "positive_count": 2,
                "negative_count": 0,
                "maturity_status": "positive_seed_needs_more_evidence",
                "positive_pattern": "Concept labels such as Examinando/Examinanda and Lider desafiado with preserved concept keys.",
                "promotion_requirement": "Collect more examples; Concept labels can drift semantically even when tokens are intact.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_embedded_constant_runtime_literal_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_embedded_literal_token_specialist",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="guarded_positive_candidate",
            scope_group="lifecycle_auto_confirmation_weak_auto_embedded_literal",
            scope_sql=None,
            scope_description=(
                "Tracks runtime string branches that intentionally collapse to the same PT-BR literal in both "
                "local-player and third-person contexts."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 45,
                "source_review_decision_run_id": 34,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 1,
                "positive_count": 1,
                "negative_count": 0,
                "maturity_status": "positive_seed_needs_more_evidence",
                "positive_pattern": "Select_CString branches intentionally both resolve to PT-BR e.",
                "promotion_requirement": "Collect more examples before considering a scoped shadow policy.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_token_surface_semantic_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="semantic_delta_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description=(
                "Flags weak-auto rows whose candidate text has only tokens/punctuation while English or Spanish "
                "still has visible semantic text outside tokens."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 17,
                "boundary_queue_run_id": 19,
                "review_queue_run_id": 44,
                "review_decision_run_id": 35,
                "specialist_audit_run_id": 43,
                "aggregate_specialist_audit_run_id": 44,
                "candidate_count": 48,
                "queued_count": 48,
                "reviewed_count": 48,
                "positive_count": 9,
                "negative_count": 39,
                "negative_rate": 0.8125,
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_boundary_policy_all_v1",
                "latest_boundary_policy_candidates": 64,
                "latest_boundary_policy_repair_candidates": 64,
                "latest_boundary_policy_unclassified": 0,
                "latest_boundary_repair_queue_run_ids": [1, 2],
                "latest_boundary_repair_same_token": 20,
                "latest_boundary_repair_token_change": 44,
                "latest_same_token_repair_shadow_run_id": 2,
                "latest_same_token_repair_shadow_ready": 20,
                "latest_same_token_repair_shadow_blocked": 0,
                "latest_same_token_repair_noop_ready": 2,
                "latest_same_token_repair_checkpoint_run_id": 2,
                "latest_same_token_repair_checkpoint_allowed": 20,
                "latest_same_token_repair_checkpoint_blocked": 0,
                "latest_same_token_repair_lifecycle_run_id": 2,
                "latest_same_token_repair_lifecycle_status": "shadow",
                "latest_same_token_repair_lifecycle_released": 20,
                "latest_same_token_repair_lifecycle_blocked": 0,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_candidates": 44,
                "latest_token_policy_bridge_review_required": 44,
                "latest_token_policy_bridge_blocked": 0,
                "latest_token_policy_bridge_buckets": {
                    "review_select_cstring_change": 20,
                    "review_mixed_token_change": 14,
                    "review_gender_token_change": 7,
                    "review_dynamic_scope_change": 3,
                },
                "latest_token_subpolicy_shadow_count": 4,
                "latest_token_subpolicy_shadow_ready": 44,
                "latest_token_subpolicy_shadow_blocked": 0,
                "latest_token_subpolicy_checkpoint_count": 4,
                "latest_token_subpolicy_checkpoint_allowed": 44,
                "latest_token_subpolicy_checkpoint_blocked": 0,
                "latest_token_subpolicy_lifecycle_run_id": 1,
                "latest_token_subpolicy_lifecycle_status": "shadow",
                "latest_token_subpolicy_lifecycle_released": 44,
                "latest_token_subpolicy_lifecycle_blocked": 0,
                "latest_token_subpolicy_production_audit_run_id": 1,
                "latest_token_subpolicy_production_audit_status": "ready_for_controlled_production_design",
                "latest_token_subpolicy_production_audit_eligible": 44,
                "latest_token_subpolicy_production_audit_estimated_closed_gain": 44,
                "latest_token_subpolicy_production_audit_blocked": 0,
                "latest_token_subpolicy_production_audit_requires_confirmation_promotion": 44,
                "latest_token_subpolicy_production_audit_requires_output_apply": 44,
                "latest_token_subpolicy_production_audit_requires_segment_state_lifecycle_integration": 44,
                "latest_token_subpolicy_production_audit_required_steps": [
                    "promote_corrected_text_to_confirmation",
                    "write_corrected_text_to_output",
                    "include_token_subpolicy_lifecycle_in_segment_state",
                ],
                "latest_token_policy_bridge_remaining_ungoverned": 0,
                "latest_token_policy_bridge_remaining_buckets": {},
                "diagnostic_subfamily": "weak_auto_token_surface_semantic_delta",
                "maturity_status": "blocked_boundary",
                "known_risk": (
                    "A token-only output can be a real semantic deletion, such as a sentence collapsed to punctuation "
                    "or a possessive expression losing its visible connector."
                ),
                "learned_boundary": (
                    "This family is mostly negative: Spanish runtime verbs, removed de/of connectors, ES_DelDela "
                    "helpers, and punctuation-only collapses must block lifecycle closure. Compact UI token stacks, "
                    "explicit PT-BR gendered Select_CString labels, ES_DelDela -> literal de repairs, and narrow "
                    "dynamic helper neutralizations can be positive subcases when scoped by narrow subpolicies."
                ),
                "recommended_action": (
                    "Split into visible_runtime_spanish_verbs, visible_possessive_connector_loss, "
                    "visible_sentence_collapse, visible_semantic_sentence_loss, and compact_ui_token_stack_safe subagents."
                ),
                "reason": (
                    "The strict static-token queue found surface-token candidates that were not genuinely static "
                    "because the source strings still had visible semantic text."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_visible_runtime_spanish_verb_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_token_surface_semantic_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_source_visible",
            scope_sql=None,
            scope_description=(
                "Blocks weak-auto rows where visible gameplay text still contains Spanish conjugated verbs "
                "inside or around CK3 tokens."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 44,
                "source_review_decision_run_id": 35,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 18,
                "positive_count": 0,
                "negative_count": 18,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_visible_runtime_spanish_verb_boundary_v1",
                "latest_boundary_policy_count": 18,
                "latest_boundary_repair_queue_run_id": 2,
                "latest_boundary_repair_token_change": 18,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 18,
                "latest_token_policy_bridge_primary_bucket": "review_select_cstring_change",
                "latest_token_subpolicy_shadow_run_id": 1,
                "latest_token_subpolicy_shadow_ready": 18,
                "latest_token_subpolicy_shadow_blocked": 0,
                "latest_token_subpolicy_checkpoint_run_id": 1,
                "latest_token_subpolicy_checkpoint_allowed": 18,
                "latest_token_subpolicy_checkpoint_blocked": 0,
                "latest_current_same_token_repair_queue_run_id": 12,
                "latest_current_same_token_repair_candidates": 10,
                "latest_current_same_token_repair_shadow_run_id": 10,
                "latest_current_same_token_repair_ready": 0,
                "latest_current_same_token_noop_ready": 10,
                "latest_current_same_token_lifecycle_run_id": 10,
                "latest_current_same_token_lifecycle_released": 10,
                "latest_current_same_token_lifecycle_blocked": 0,
                "negative_patterns": [
                    "ganas/gana",
                    "pierdes/pierde",
                    "posees/posee",
                    "gastas/gasta",
                    "despides/despide",
                    "abandonaras/abandonara",
                ],
                "recommended_action": "Turn into a deterministic blocker or targeted repair queue before any release.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_visible_possessive_connector_loss_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_token_surface_semantic_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_source_visible",
            scope_sql=None,
            scope_description=(
                "Blocks rows where source-visible possessive/prepositional meaning was lost, especially de/of "
                "connectors, adventure-name patterns, and ES_DelDela helpers."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 44,
                "source_review_decision_run_id": 35,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 18,
                "positive_count": 0,
                "negative_count": 18,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_visible_possessive_connector_loss_boundary_v1",
                "latest_boundary_policy_count": 17,
                "latest_boundary_repair_queue_run_ids": [1, 2, 12],
                "latest_boundary_repair_same_token": 17,
                "latest_boundary_repair_token_change": 3,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 3,
                "latest_token_policy_bridge_primary_bucket": "review_gender_token_change",
                "latest_token_subpolicy_shadow_run_id": 3,
                "latest_token_subpolicy_shadow_name": "es_deldela_literal_de_ptbr_repair",
                "latest_token_subpolicy_shadow_ready_for_agent": 3,
                "latest_token_subpolicy_checkpoint_run_id": 3,
                "latest_token_subpolicy_checkpoint_allowed_for_agent": 3,
                "latest_token_subpolicy_checkpoint_blocked_for_agent": 0,
                "latest_same_token_repair_shadow_run_id": 10,
                "latest_same_token_repair_shadow_ready": 17,
                "latest_same_token_repair_shadow_blocked": 0,
                "latest_same_token_repair_checkpoint_run_id": 11,
                "latest_same_token_repair_lifecycle_run_id": 10,
                "latest_same_token_repair_lifecycle_released": 17,
                "latest_same_token_repair_lifecycle_blocked": 0,
                "latest_current_same_token_repair_ready": 14,
                "latest_current_same_token_noop_ready": 3,
                "negative_patterns": [
                    "ES_DelDela preserved in PT-BR candidate",
                    "English of mapped to missing PT-BR de connector",
                    "Adventure-name token stacks losing de between name and god/culture",
                ],
                "learned_safe_repair_subcase": "ES_DelDela helper removed and replaced by literal de before the same dynamic scope.",
                "recommended_action": (
                    "Keep parent as boundary; route the narrow ES_DelDela -> de pattern through "
                    "es_deldela_literal_de_ptbr_repair before any lifecycle release."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_visible_copula_token_form_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_token_surface_semantic_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_source_visible",
            scope_sql=None,
            scope_description=(
                "Blocks rows where an English CharAreIs/a structure was only partially converted and the resulting "
                "PT-BR token form or copula text is suspicious."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 44,
                "source_review_decision_run_id": 35,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 1,
                "positive_count": 0,
                "negative_count": 1,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_visible_copula_token_form_boundary_v1",
                "latest_boundary_policy_count": 1,
                "latest_boundary_repair_queue_run_id": 2,
                "latest_boundary_repair_token_change": 1,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 1,
                "latest_token_policy_bridge_primary_bucket": "review_dynamic_scope_change",
                "latest_token_subpolicy_shadow_run_id": 4,
                "latest_token_subpolicy_shadow_name": "dynamic_scope_ptbr_helper_neutralization",
                "latest_token_subpolicy_shadow_ready_for_agent": 1,
                "latest_token_subpolicy_checkpoint_run_id": 4,
                "latest_token_subpolicy_checkpoint_allowed_for_agent": 1,
                "latest_token_subpolicy_checkpoint_blocked_for_agent": 0,
                "negative_pattern": "Partially converted CharAreIs/a sentence with suspicious GetShortUINameU token form.",
                "learned_safe_repair_subcase": (
                    "LocalPlayerString with identical PT-BR alternatives can be neutralized to a literal copula "
                    "when GetShortUINameU is normalized to GetShortUIName|U and the semantic label is preserved."
                ),
                "recommended_action": (
                    "Keep parent as boundary; monitor dynamic_scope_ptbr_helper_neutralization as a narrow repair route."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_visible_sentence_collapse_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_token_surface_semantic_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_source_visible",
            scope_sql=None,
            scope_description=(
                "Blocks rows where a source sentence or tooltip was collapsed into punctuation, a bare name, "
                "or a token stack that loses semantic text."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 44,
                "source_review_decision_run_id": 35,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 2,
                "positive_count": 0,
                "negative_count": 2,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_visible_sentence_collapse_boundary_v1",
                "latest_boundary_policy_count": 2,
                "latest_boundary_repair_queue_run_ids": [1, 2],
                "latest_boundary_repair_same_token": 1,
                "latest_boundary_repair_token_change": 1,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 1,
                "latest_token_policy_bridge_primary_bucket": "review_gender_token_change",
                "latest_token_subpolicy_shadow_run_id": 3,
                "latest_token_subpolicy_shadow_name": "es_deldela_literal_de_ptbr_repair",
                "latest_token_subpolicy_shadow_ready_for_agent": 1,
                "latest_token_subpolicy_checkpoint_run_id": 3,
                "latest_token_subpolicy_checkpoint_allowed_for_agent": 1,
                "latest_token_subpolicy_checkpoint_blocked_for_agent": 0,
                "latest_same_token_repair_shadow_run_id": 2,
                "latest_same_token_repair_shadow_ready": 1,
                "latest_same_token_repair_shadow_blocked": 0,
                "latest_same_token_repair_lifecycle_released": 1,
                "latest_same_token_repair_lifecycle_blocked": 0,
                "negative_patterns": [
                    "Full sentence collapsed to !",
                    "Visible sentence collapsed to punctuation or a bare token/name stack",
                ],
                "learned_safe_repair_subcase": (
                    "One collapsed ES_DelDela sentence can be repaired when the PT-BR sentence is restored and "
                    "the missing helper is replaced by literal de before the same dynamic scope."
                ),
                "recommended_action": "Keep as manual/repair queue until enough examples support a correction generator.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_visible_semantic_sentence_loss_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_token_surface_semantic_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_source_visible",
            scope_sql=None,
            scope_description=(
                "Blocks rows where source-visible semantic text around a preserved CK3 token was removed, "
                "such as losing an entire predicate while keeping only a name and Concept token."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 44,
                "source_review_decision_run_id": 35,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 1,
                "positive_count": 0,
                "negative_count": 1,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_visible_semantic_sentence_loss_boundary_v1",
                "latest_boundary_policy_count": 1,
                "latest_boundary_repair_queue_run_id": 1,
                "latest_boundary_repair_same_token": 1,
                "latest_same_token_repair_shadow_run_id": 2,
                "latest_same_token_repair_shadow_ready": 1,
                "latest_same_token_repair_shadow_blocked": 0,
                "latest_same_token_repair_lifecycle_released": 1,
                "latest_same_token_repair_lifecycle_blocked": 0,
                "negative_pattern": "No longer considered sentence lost its visible PT-BR predicate around a Concept token.",
                "recommended_action": (
                    "Route to same-token semantic repair review; do not treat preserved tokens as sufficient evidence."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_compact_ui_token_stack_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_token_surface_semantic_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="guarded_positive_candidate",
            scope_group="lifecycle_auto_confirmation_weak_auto_source_visible",
            scope_sql=None,
            scope_description=(
                "Tracks compact UI token stacks that preserve the English semantic structure and avoid Spanish "
                "connectors or verbs."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 44,
                "source_review_decision_run_id": 35,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 9,
                "positive_count": 9,
                "negative_count": 0,
                "maturity_status": "positive_candidate_needs_shadow_policy",
                "latest_shadow_policy_run_id": 17,
                "latest_shadow_policy_name": "weak_auto_compact_ui_token_stack_shadow_v1",
                "latest_shadow_ready": 9,
                "latest_shadow_blocked": 39,
                "positive_patterns": [
                    "PT-BR gendered Select_CString title labels",
                    "Compact parameter labels with equivalent CK3 token sequence",
                    "English token/punctuation exact semantics preserved",
                ],
                "promotion_requirement": (
                    "Create a scoped shadow policy and ensure no hidden Spanish dynamic literals before guarded release."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_custom_loc_helper_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="custom_localization_runtime_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description=(
                "Reviews weak-auto rows whose surface is token-only but depends on ES custom-localization helpers "
                "that may emit visible runtime grammar."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 19,
                "boundary_queue_run_id": 21,
                "review_queue_run_id": 46,
                "review_decision_run_id": 33,
                "specialist_audit_run_id": 38,
                "aggregate_specialist_audit_run_id": 44,
                "candidate_count": 6,
                "queued_count": 6,
                "reviewed_count": 6,
                "positive_count": 1,
                "negative_count": 5,
                "diagnostic_subfamily": "weak_auto_custom_loc_helper_token",
                "maturity_status": "blocked_boundary",
                "known_risk": (
                    "Custom('ES_...') calls can be correct runtime grammar or manual exceptions, but they are not "
                    "pure static-token rows."
                ),
                "learned_boundary": (
                    "ES_DelDela and ES_only_el_* helpers are negative PT-BR boundaries; a helper body that only "
                    "returns GetShortUIName can be safe but does not justify promoting the whole custom_loc family."
                ),
                "reason": (
                    "The strict static-token expansion found ES helper calls such as ES_DelDela and "
                    "ES_only_el_GetShortUIName; they need a runtime-helper boundary before promotion."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_custom_loc_es_helper_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_custom_loc_helper_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_weak_auto_custom_loc",
            scope_sql=None,
            scope_description=(
                "Blocks ES custom-localization helpers that emit Spanish article or contraction grammar at runtime."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 46,
                "source_review_decision_run_id": 33,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 5,
                "positive_count": 0,
                "negative_count": 5,
                "negative_rate": 1.0,
                "maturity_status": "hard_boundary_candidate",
                "latest_boundary_policy_run_id": 2,
                "latest_boundary_policy_name": "weak_auto_custom_loc_es_helper_boundary_v1",
                "latest_boundary_policy_count": 5,
                "latest_boundary_repair_queue_run_id": 2,
                "latest_boundary_repair_token_change": 5,
                "latest_token_policy_bridge_run_id": 1,
                "latest_token_policy_bridge_review_required": 5,
                "latest_token_policy_bridge_buckets": {
                    "review_gender_token_change": 3,
                    "review_dynamic_scope_change": 2,
                },
                "latest_token_subpolicy_shadow_run_id": 3,
                "latest_token_subpolicy_shadow_name": "es_deldela_literal_de_ptbr_repair",
                "latest_token_subpolicy_shadow_ready_for_agent": 3,
                "latest_token_subpolicy_checkpoint_run_id": 3,
                "latest_token_subpolicy_checkpoint_allowed_for_agent": 3,
                "latest_token_subpolicy_checkpoint_blocked_for_agent": 0,
                "latest_dynamic_scope_subpolicy_shadow_run_id": 4,
                "latest_dynamic_scope_subpolicy_shadow_name": "dynamic_scope_ptbr_helper_neutralization",
                "latest_dynamic_scope_subpolicy_shadow_ready_for_agent": 2,
                "latest_dynamic_scope_subpolicy_checkpoint_run_id": 4,
                "latest_dynamic_scope_subpolicy_checkpoint_allowed_for_agent": 2,
                "latest_dynamic_scope_subpolicy_checkpoint_blocked_for_agent": 0,
                "latest_token_policy_bridge_remaining_ungoverned_for_agent": 0,
                "negative_patterns": [
                    "Custom('ES_DelDela')",
                    "Custom('ES_only_el_GetShortUIName')",
                    "Custom('ES_only_el_GetShortUIName_U')",
                ],
                "learned_safe_repair_subcase": (
                    "Custom('ES_DelDela') can be repaired as literal de when the same target scope is preserved; "
                    "ES_only_el_GetShortUIName helpers can be neutralized to the character name when the Spanish "
                    "article helper disappears and the target scope/style is preserved."
                ),
                "recommended_action": (
                    "Keep ES helpers as boundary; route only the covered literal-de and name-neutralization "
                    "subcases through their shadow checkpoints."
                ),
            },
        ),
        AgentSpec(
            agent_key="weak_auto_custom_loc_plain_name_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_custom_loc_helper_boundary",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="guarded_positive_candidate",
            scope_group="lifecycle_auto_confirmation_weak_auto_custom_loc",
            scope_sql=None,
            scope_description=(
                "Tracks custom localization helper bodies that only return a CK3 name token with no visible "
                "Spanish grammar helper."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "source_review_queue_run_id": 46,
                "source_review_decision_run_id": 33,
                "parent_specialist_audit_run_id": 44,
                "reviewed_count": 1,
                "positive_count": 1,
                "negative_count": 0,
                "maturity_status": "positive_seed_needs_more_evidence",
                "positive_pattern": "Helper body is only GetShortUIName with no article/contraction grammar.",
                "promotion_requirement": "Collect more examples before creating a release policy.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_dynamic_issue_boundary",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="manual_boundary_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Checks weak auto confirmations with dynamic text and validation issue signals.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "diagnostic_subfamily": "weak_auto_dynamic_issue_signal",
                "evidence_count": 8,
                "review_queue_run_id": 25,
                "queued_count": 8,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "reason": "Dynamic issue rows can hide Spanish branches, pronoun inversions, or token-adjacent grammar issues.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_short_concept_sampler",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="positive_sampler_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Samples short concept-link weak auto confirmations for safe UI lifecycle closure.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "diagnostic_subfamily": "weak_auto_short_concept_exact",
                "evidence_count": 7,
                "review_queue_run_id": 26,
                "queued_count": 7,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "reason": "Short concept rows are structurally attractive but concept labels can still be semantically wrong.",
            },
        ),
        AgentSpec(
            agent_key="weak_auto_dynamic_context_sampler",
            agent_type="subspecialist",
            parent_agent_key="weak_auto_confirmation_reconciler",
            model_kind=None,
            status="experimental",
            operational_state="candidate",
            decision_role="manual_sampler_review",
            scope_group="lifecycle_auto_confirmation_weak_auto",
            scope_sql=None,
            scope_description="Samples dynamic exact-match weak auto confirmations that lack explicit issue signals.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 5,
                "diagnostic_subfamily": "weak_auto_dynamic_exact",
                "evidence_count": 5,
                "review_queue_run_id": 23,
                "queued_count": 5,
                "known_positive_examples": 0,
                "known_negative_examples": 0,
                "reason": "Dynamic exact rows may be safe, but role/context binding must be reviewed before promotion.",
            },
        ),
        AgentSpec(
            agent_key="nickname_select_cstring_literal_specialist",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="literal_boundary_review",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description=(
                "Studies nickname descriptions with Select_CString branches and Spanish literal hints before any "
                "auto-confirmed text-replacement lifecycle policy is released."
            ),
            default_threshold=None,
            priority=48,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "nickname_dynamic_literal_replacement",
                "evidence_count": 61,
                "high_risk_count": 6,
                "review_queue_count": 61,
                "reviewed_count": 61,
                "positive_count": 20,
                "negative_count": 41,
                "negative_rate": 0.67,
                "maturity_status": "blocked_boundary",
                "review_decision_run_ids": [2, 3],
                "last_specialist_audit_run_id": 5,
                "latest_text_diagnostic_run_id": 6,
                "latest_new_sample_count": 18,
                "latest_new_sample_action": "needs_subagent_before_policy",
                "all_nickname_cases_reviewed": True,
                "reason": "Nickname descriptions mix dynamic gender branches, literal Spanish hints, and valid stylistic exceptions.",
                "promotion_requirement": "Split into smaller boundaries before any lifecycle closure can be considered.",
            },
        ),
        AgentSpec(
            agent_key="nickname_residual_spanish_select_cstring_boundary",
            agent_type="subspecialist",
            parent_agent_key="nickname_select_cstring_literal_specialist",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks nickname Select_CString branches that still contain Spanish verbs or visible Spanish residue.",
            default_threshold=None,
            priority=48,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [1, 2, 3, 21, 22],
                "known_negative_examples": 31,
                "issue_ledger_agent_key": "nickname_residual_spanish_select_cstring_boundary",
                "latest_issue_ledger_run_id": 3,
                "latest_issue_review_queue_run_id": 10,
                "latest_issue_review_decision_run_id": 22,
                "latest_issue_review_examples": 13,
                "maturity_status": "shadow_boundary_with_issue_ledger_route",
                "reason": (
                    "Reviewed nickname evidence found residual Spanish such as posees, inspiras, hacerte, termina, "
                    "comportamiento, cielos, mirada, habláis, hablan, hablas, luchas, puedes, te/le, comes and apareciste."
                ),
            },
        ),
        AgentSpec(
            agent_key="nickname_possessive_gender_mismatch_boundary",
            agent_type="subspecialist",
            parent_agent_key="nickname_select_cstring_literal_specialist",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks nickname text where dynamic possessives produce wrong PT-BR gender or broken article sequences.",
            default_threshold=None,
            priority=48,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [2, 3],
                "known_negative_examples": 24,
                "reason": "Reviewed examples include sua destino, sua arco, sua estandarte, sua lema, sua patronato, sua conhecimento and ao sua passo.",
            },
        ),
        AgentSpec(
            agent_key="nickname_demonym_gender_boundary",
            agent_type="symbolic_subpolicy",
            parent_agent_key="nickname_select_cstring_literal_specialist",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="guarded_context_checkpoint",
            scope_group="issue_partial_coverage_nickname_demonym_gender",
            scope_sql=(
                "relative_path = 'nicknames_l_spanish.yml' "
                "AND source_key LIKE 'nick_the_%'"
            ),
            scope_description=(
                "Explains short nickname demonym labels that use compact PT-BR article/suffix gender text "
                "with CHARACTER.Custom('ES_AnAna') after structural, dynamic, gender and short-label guards agree."
            ),
            default_threshold=None,
            priority=48,
            dashboard_group="Issue Network",
            notes={
                "checkpoint_table": "ml_issue_nickname_demonym_gender_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "source_partial_coverage_run_id": 52,
                "seed_segment": "nick_the_german",
                "allowed_shadow_items": 2,
                "blocked_shadow_items": 0,
                "known_pattern": "o/a <demonym masculine>/ã[CHARACTER.Custom('ES_AnAna')]",
                "target_families": ["nickname_name_policy", "semantic_review_router"],
                "required_covered_families": [
                    "dynamic_ck3_expression_microagent",
                    "gender_token_microagent",
                    "short_label_style_microagent",
                    "structural_token_gate",
                ],
                "maturity_status": "new_shadow_checkpoint",
                "safety_note": "Shadow-only: no source/output read, no production write, no correction application.",
            },
        ),
        AgentSpec(
            agent_key="nickname_redundant_select_cstring_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="nickname_select_cstring_literal_specialist",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="positive_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Collects safe nickname cases where Select_CString branches are redundant but the PT-BR text is fluent.",
            default_threshold=None,
            priority=48,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [2, 3],
                "known_positive_examples": 20,
                "reason": "A positive bucket exists for redundant Select_CString branches where the PT-BR sentence remains fluent, but it must stay separated from dynamic possessive hazards.",
            },
        ),
        AgentSpec(
            agent_key="nickname_whisperer_select_cstring_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="nickname_redundant_select_cstring_safe_candidate",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_nickname_whisperer_select_cstring",
            scope_sql=None,
            scope_description=(
                "Observes the reviewed Whisperer nickname rewrite where ES_OA and a redundant CHARACTER.IsLocalPlayer "
                "Select_CString are removed while the character display-name token is preserved."
            ),
            default_threshold=None,
            priority=48,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "nickname_whisperer_select_cstring_neutralization",
                "source_boundary_policy": "weak_auto_issue_dynamic_spanish_literal",
                "allowed_bucket": "review_select_cstring_change",
                "seed_examples": 1,
                "safe_keys": ["nick_the_whisperer_desc"],
                "known_pattern": "CHARACTER ES_OA plus redundant passa/passa selector becomes a fluent PT-BR nickname sentence.",
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="hold_court_dialogue_literal_specialist",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="literal_boundary_review",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description=(
                "Separates hold-court dialogue and narrative literal replacements from broad text-replacement signals."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "hold_court_dialogue_literal_replacement",
                "evidence_count": 39,
                "review_queue_count": 39,
                "reviewed_count": 39,
                "positive_count": 38,
                "negative_count": 1,
                "negative_rate": 0.026,
                "maturity_status": "split_required",
                "review_decision_run_ids": [5],
                "last_specialist_audit_run_id": 8,
                "shadow_policy_run_ids": [1],
                "reason": "Court scenes mostly contain a safe aptitude tooltip pattern, but one residual Spanish title-retention case blocks broad release.",
                "promotion_requirement": "Promote only the aptitude-tooltip subpattern; keep title-retention dialogue behind a residual-Spanish boundary.",
            },
        ),
        AgentSpec(
            agent_key="hold_court_aptitude_tooltip_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="hold_court_dialogue_literal_specialist",
            model_kind=None,
            status="experimental",
            operational_state="dry_run",
            decision_role="shadow_positive_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description=(
                "Collects hold-court court-position aptitude tooltip cases where GetHerHisMy maps safely to "
                "Select_CString Minha/Sua before a feminine aptitude concept."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [5],
                "known_positive_examples": 38,
                "known_negative_examples": 0,
                "shadow_policy_table": "auto_confirmation_reopen_text_shadow_policy_runs",
                "shadow_policy_run_id": 1,
                "shadow_ready_count": 38,
                "shadow_blocked_count": 1,
                "shadow_apply_allowed": 0,
                "policy_name": "hold_court_aptitude_tooltip_shadow_v1",
                "reason": "Reviewed evidence shows a stable tooltip formula: Minha/Sua aptidao e [candidate.GetCourtPositionAptitude(...)] with optional old_holder comparison.",
                "promotion_requirement": "Keep in shadow until the production flow confirms this pattern on a fresh package without false-safe rows.",
            },
        ),
        AgentSpec(
            agent_key="hold_court_title_retention_spanish_boundary",
            agent_type="subspecialist",
            parent_agent_key="hold_court_dialogue_literal_specialist",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks hold-court title-retention dialogue still carrying Spanish verb branches such as retengo/retiene.",
            default_threshold=None,
            priority=49,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [5],
                "known_negative_examples": 1,
                "reason": "The title-retention line still uses Spanish Select_CString verb forms and should not contaminate the aptitude-tooltip policy.",
            },
        ),
        AgentSpec(
            agent_key="visible_concept_literal_boundary",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="literal_boundary_review",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Checks visible concept/glossary literal replacements where UI concepts may be intentionally preserved.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "visible_concept_or_glossary_literal",
                "evidence_count": 21,
                "review_queue_count": 21,
                "reviewed_count": 21,
                "positive_count": 13,
                "negative_count": 8,
                "negative_rate": 0.381,
                "maturity_status": "blocked_boundary",
                "review_decision_run_ids": [6],
                "last_specialist_audit_run_id": 9,
                "latest_text_diagnostic_run_id": 6,
                "latest_new_sample_count": 5,
                "latest_new_sample_action": "needs_subagent_before_policy",
                "reason": "Concept links and visible glossary terms are structurally safe but semantically easy to overgeneralize.",
                "promotion_requirement": "Split preserved-token UI positives from semantic drift, dynamic pronoun, and residual Spanish trigger boundaries.",
            },
        ),
        AgentSpec(
            agent_key="visible_concept_preserved_token_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="visible_concept_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="positive_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Collects visible concept/glossary cases where concept links and CK3 tokens are preserved and the PT-BR text remains coherent.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [6],
                "known_positive_examples": 13,
                "known_negative_examples": 0,
                "reason": "Positive examples include preserved concepts, compact UI labels, reward tooltips and event prose without semantic drift.",
                "promotion_requirement": "Needs narrower buckets before any shadow policy because positives span event prose, triggers, effects and activity rewards.",
            },
        ),
        AgentSpec(
            agent_key="visible_concept_semantic_drift_boundary",
            agent_type="subspecialist",
            parent_agent_key="visible_concept_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks visible concept/glossary replacements that change relationship meaning, pronoun role, grammar binding, or UI label semantics.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [6],
                "known_negative_examples": 6,
                "reason": "Reviewed negatives include a/as wars, inverted object pronoun, fixed candidate pronouns, bully as verb, missing nemesis de, and flatulacionista literalism.",
            },
        ),
        AgentSpec(
            agent_key="visible_concept_residual_spanish_trigger_boundary",
            agent_type="subspecialist",
            parent_agent_key="visible_concept_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks visible trigger/condition rows where Spanish No/no remains visible in Portuguese UI.",
            default_threshold=None,
            priority=50,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [6],
                "known_negative_examples": 2,
                "reason": "Reviewed trigger rows still showed #bold No#! or #bold no#! instead of Portuguese Nao/nao.",
            },
        ),
        AgentSpec(
            agent_key="short_ui_relation_score_literal_specialist",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="active",
            operational_state="dry_run",
            decision_role="guarded_shadow_text_policy",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Evaluates short relationship/opinion score UI strings as candidates for a narrower text policy.",
            default_threshold=None,
            priority=51,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "short_ui_relation_score_literal",
                "evidence_count": 9,
                "review_queue_count": 9,
                "reviewed_count": 9,
                "positive_count": 9,
                "negative_count": 0,
                "negative_rate": 0.0,
                "maturity_status": "ready_for_shadow",
                "review_decision_run_ids": [7],
                "last_specialist_audit_run_id": 14,
                "latest_shadow_policy_run_id": 2,
                "latest_shadow_policy_name": "short_ui_relation_score_shadow_v1",
                "latest_shadow_ready": 9,
                "latest_shadow_blocked": 0,
                "promotion_decision": "promoted_to_guarded_shadow_candidate",
                "production_release_allowed": False,
                "reason": "All currently eligible short diarchy score labels passed manual evidence and shadow guards, with no negative examples or blockers.",
                "promotion_requirement": "Add a dedicated guarded text lifecycle policy before this subagent may close segment-state in production.",
            },
        ),
        AgentSpec(
            agent_key="short_tooltip_logic_literal_specialist",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="short_ui_policy_sampling",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Evaluates short tooltip/logic snippets as candidates for a compact guarded text policy.",
            default_threshold=None,
            priority=52,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "short_tooltip_logic_literal",
                "evidence_count": 3,
                "review_queue_count": 3,
                "reviewed_count": 3,
                "positive_count": 2,
                "negative_count": 1,
                "negative_rate": 0.333,
                "maturity_status": "blocked_boundary",
                "review_decision_run_ids": [11],
                "last_specialist_audit_run_id": 13,
                "reason": "Short tooltip fragments include safe mechanical value/trigger rows, but a religious title style row blocks broad promotion.",
                "promotion_requirement": "Split mechanical short tooltips from religious title style before any shadow policy.",
            },
        ),
        AgentSpec(
            agent_key="short_tooltip_mechanical_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="short_tooltip_logic_literal_specialist",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="positive_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Collects compact mechanical tooltip/value/trigger rows where token-preserving PT-BR is fluent.",
            default_threshold=None,
            priority=52,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [11],
                "known_positive_examples": 2,
                "known_negative_examples": 0,
                "reason": "Reviewed positives cover honor value and character inequality trigger rows.",
                "promotion_requirement": "Needs more evidence before shadow because only two rows are known.",
            },
        ),
        AgentSpec(
            agent_key="short_tooltip_religious_title_style_boundary",
            agent_type="subspecialist",
            parent_agent_key="short_tooltip_logic_literal_specialist",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks compact tooltip rows where religious/title style needs PT-BR terminology standardization.",
            default_threshold=None,
            priority=52,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [11],
                "known_negative_examples": 1,
                "reason": "Reviewed God King tooltip was semantically close but 'rei deus' is not a safe polished PT-BR title style.",
            },
        ),
        AgentSpec(
            agent_key="event_narrative_literal_boundary",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="manual_sampling",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Keeps event narrative literal replacements in manual sampling until scene-specific evidence exists.",
            default_threshold=None,
            priority=53,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "event_narrative_literal_replacement",
                "evidence_count": 8,
                "review_queue_count": 8,
                "reviewed_count": 8,
                "positive_count": 5,
                "negative_count": 3,
                "negative_rate": 0.375,
                "maturity_status": "blocked_boundary",
                "review_decision_run_ids": [8],
                "last_specialist_audit_run_id": 11,
                "latest_text_diagnostic_run_id": 6,
                "latest_new_sample_count": 2,
                "latest_new_sample_action": "manual_sampling_before_policy",
                "reason": "Narrative event prose contains safe compact logs, but dynamic gender/number and possessive referents create hard boundaries.",
                "promotion_requirement": "Split event narrative into activity-log positives and dynamic referent boundaries before any shadow policy.",
            },
        ),
        AgentSpec(
            agent_key="event_narrative_activity_log_safe_candidate",
            agent_type="subspecialist",
            parent_agent_key="event_narrative_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="positive_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Collects compact event/activity logs where the literal replacement is fluent and preserves character tokens.",
            default_threshold=None,
            priority=53,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [8],
                "known_positive_examples": 5,
                "known_negative_examples": 0,
                "reason": "Reviewed positives include tournament resignation and hunt activity logs with stable token preservation.",
                "promotion_requirement": "Needs more compact log evidence before a shadow policy can separate it from broader event prose.",
            },
        ),
        AgentSpec(
            agent_key="event_narrative_dynamic_referent_boundary",
            agent_type="subspecialist",
            parent_agent_key="event_narrative_literal_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks event prose where dynamic gender/number or possessive referents can create ungrammatical or ambiguous PT-BR.",
            default_threshold=None,
            priority=53,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [8],
                "known_negative_examples": 3,
                "reason": "Reviewed negatives include dois/candidatas, as/dois, local-player possessive grammar, and redundant sua/sua referent loss.",
            },
        ),
        AgentSpec(
            agent_key="auto_confirmation_text_replacement_reconciler",
            agent_type="subcoordinator",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="manual_sampling",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Collects miscellaneous text-replacement reopen cases that do not fit a stable subfamily yet.",
            default_threshold=None,
            priority=54,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "other_text_literal_replacement",
                "evidence_count": 4,
                "review_queue_count": 4,
                "reviewed_count": 4,
                "positive_count": 0,
                "negative_count": 4,
                "negative_rate": 1.0,
                "maturity_status": "blocked_boundary",
                "review_decision_run_ids": [10],
                "last_specialist_audit_run_id": 12,
                "reason": "Small residual bucket was fully negative: artifact history/debug and illegitimacy secret prose are contextual boundaries, not generic literal replacements.",
                "promotion_requirement": "Keep residual closed; route future rows into artifact-history or secret-kinship boundary specialists.",
            },
        ),
        AgentSpec(
            agent_key="artifact_history_debug_text_boundary",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_text_replacement_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks artifact history/debug strings where literal Select_CString branches create impossible PT-BR possessive phrasing.",
            default_threshold=None,
            priority=54,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [10],
                "known_negative_examples": 1,
                "reason": "Reviewed debug artifact text could produce 'Roubado por seus/as das maos de', so it must not be learned as safe literal text.",
            },
        ),
        AgentSpec(
            agent_key="secret_illegitimacy_dynamic_kinship_boundary",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_text_replacement_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks secret/heritage prose where mother, child, target and real father relations require contextual restructuring.",
            default_threshold=None,
            priority=54,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_run_id": 1,
                "source_review_run_ids": [10],
                "known_negative_examples": 3,
                "reason": "Reviewed illegitimacy secret rows can form unstable phrases such as 'Voce, sua filha, e filho ilegitimo seu'.",
            },
        ),
        AgentSpec(
            agent_key="auto_confirmation_text_issue_boundary",
            agent_type="subspecialist",
            parent_agent_key="auto_confirmation_reopen_reconciler",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="manual_boundary_review",
            scope_group="lifecycle_auto_confirmation_text_replacement",
            scope_sql=None,
            scope_description="Blocks known issue-boundary cases from being learned as safe lifecycle closure.",
            default_threshold=None,
            priority=55,
            dashboard_group="Lifecycle",
            notes={
                "diagnostic_table": "auto_confirmation_reopen_text_diagnostic_runs",
                "diagnostic_run_id": 1,
                "diagnostic_subfamily": "manual_issue_boundary",
                "evidence_count": 1,
                "high_risk_count": 1,
                "reason": "Known issue boundary must stay separated from policy candidates.",
            },
        ),
        AgentSpec(
            agent_key="select_cstring_scene_router",
            agent_type="subcoordinator",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_boundary",
            scope_group="token_policy_select_cstring",
            scope_sql=None,
            scope_description=(
                "Routes complex Select_CString rewrites by scene role before any broad guarded policy is considered."
            ),
            default_threshold=None,
            priority=48,
            dashboard_group="Token Gate",
            notes={
                "reason": "EP3 LAAMP Select_CString rewrites are scene-specific and should not be generalized as one rule.",
                "target_rule_families": [
                    "select_cstring_multi_dynamic_rewrite_candidate_rule",
                    "multi_gender_select_cstring_narrative_rewrite",
                ],
            },
        ),
        AgentSpec(
            agent_key="select_cstring_ep3_laamp_roles",
            agent_type="subspecialist",
            parent_agent_key="select_cstring_scene_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_select_cstring_ep3_laamp",
            scope_sql=(
                "relative_path = 'dlc/ep3/ep3_laamp_decision_events_l_spanish.yml' "
                "AND (english_text LIKE '%Select_CString%' "
                "OR spanish_text LIKE '%Select_CString%' "
                "OR old_text LIKE '%Select_CString%')"
            ),
            scope_description="Tracks EP3 LAAMP role-specific Select_CString rewrites such as town crier, healer, clergy and guards.",
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={"reason": "Repeated role scenes have stable evidence boundaries but not enough safe generalization yet."},
        ),
        AgentSpec(
            agent_key="select_cstring_ep3_laamp_spanish_role_boundary",
            agent_type="subspecialist",
            parent_agent_key="select_cstring_ep3_laamp_roles",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="negative_boundary",
            scope_group="token_policy_select_cstring_ep3_laamp_spanish_roles",
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
            scope_description=(
                "Blocks EP3 LAAMP Select_CString role branches that still carry Spanish articles, "
                "prepositions, or plural adjectives inside the dynamic alternatives."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "reason": "This boundary is a small, high-signal child neuron for residual Spanish inside gendered role options."
            },
        ),
        AgentSpec(
            agent_key="select_cstring_ep3_laamp_role_correction_candidate",
            agent_type="subspecialist",
            parent_agent_key="select_cstring_ep3_laamp_spanish_role_boundary",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="correction_candidate",
            scope_group="token_policy_select_cstring_ep3_laamp_role_corrections",
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
            scope_description=(
                "Learns reviewed PT-BR corrections for EP3 LAAMP Select_CString role branches "
                "after the Spanish-role boundary blocks the unsafe candidate."
            ),
            default_threshold=None,
            priority=49,
            dashboard_group="Token Gate",
            notes={
                "reason": "Correction examples are kept separate from the negative boundary so the coordinator can learn both block and repair paths."
            },
        ),
        AgentSpec(
            agent_key="select_cstring_pronoun_scene_boundary",
            agent_type="subspecialist",
            parent_agent_key="select_cstring_scene_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_select_cstring_pronoun",
            scope_sql=None,
            scope_description=(
                "Separates Select_CString-to-pronoun rewrites by scene role, preserving manual exceptions and blocking inverted rewrites."
            ),
            default_threshold=None,
            priority=50,
            dashboard_group="Token Gate",
            notes={
                "reason": "Select_CString pronoun rewrites mix safe manual fluency, scene-specific needs, and negative inverted changes.",
                "target_rule_family": "select_cstring_to_pronoun_rewrite_candidate_rule",
            },
        ),
        AgentSpec(
            agent_key="dynamic_scope_context_router",
            agent_type="subcoordinator",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_boundary",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description=(
                "Routes dynamic scope/name rewrites into narrower boundaries before any "
                "guarded release is considered."
            ),
            default_threshold=None,
            priority=51,
            dashboard_group="Token Gate",
            notes={
                "reason": "Large dynamic_scope families mix manual exceptions, cleanup, and true subpolicy needs.",
                "target_rule_families": [
                    "dynamic_name_form_rewrite",
                    "dynamic_name_plus_pronoun_contextual_rewrite",
                    "dynamic_gender_pronoun_rewrite_candidate_rule",
                ],
            },
        ),
        AgentSpec(
            agent_key="dynamic_manual_exception_boundary",
            agent_type="subspecialist",
            parent_agent_key="dynamic_scope_context_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description="Keeps individually approved dynamic rewrites as manual exceptions without generalizing them.",
            default_threshold=None,
            priority=52,
            dashboard_group="Token Gate",
            notes={"reason": "Several reviewed dynamic rewrites are valid for one segment but unsafe as a broad rule."},
        ),
        AgentSpec(
            agent_key="dynamic_encoding_cleanup_boundary",
            agent_type="subspecialist",
            parent_agent_key="dynamic_scope_context_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="cleanup_gate",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description="Separates structurally plausible dynamic rewrites blocked only by mojibake or text hygiene.",
            default_threshold=None,
            priority=53,
            dashboard_group="Token Gate",
            notes={"reason": "Encoding cleanup evidence should not be learned as a positive token policy."},
        ),
        AgentSpec(
            agent_key="dynamic_name_pronoun_boundary",
            agent_type="subspecialist",
            parent_agent_key="dynamic_scope_context_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="needs_policy_evidence",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description="Studies contextual name/pronoun rewrites before deciding whether a guarded pattern exists.",
            default_threshold=None,
            priority=54,
            dashboard_group="Token Gate",
            notes={"reason": "Name-to-pronoun rewrites are common but currently too contextual for automatic release."},
        ),
        AgentSpec(
            agent_key="single_combat_victor_name_pronoun_neutralization_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="dynamic_name_pronoun_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_single_combat_victor_name",
            scope_sql=None,
            scope_description=(
                "Observes the reviewed single-combat rewrite where English sc_victor pronouns are rendered "
                "as repeated sc_victor display names to avoid PT-BR ambiguity."
            ),
            default_threshold=None,
            priority=54,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "single_combat_victor_name_pronoun_neutralization",
                "source_boundary_policy": "weak_auto_residual_spanish_literal_repair",
                "allowed_bucket": "review_dynamic_scope_change",
                "seed_examples": 1,
                "safe_keys": ["single_combat.0031.desc.sc_attacker.mocking_boast"],
                "known_pattern": "sc_victor GetSheHe/GetHerHim in English becomes two GetFirstNameNoTooltip references in PT-BR.",
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="ep3_travel_title_adjective_alignment_subpolicy",
            agent_type="subspecialist",
            parent_agent_key="dynamic_entity_rebinding_boundary",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="token_subpolicy_shadow",
            scope_group="boundary_token_policy_ep3_travel_title_adjective",
            scope_sql=None,
            scope_description=(
                "Observes the reviewed EP3 travel rewrite where a Spanish ES_ElLa title phrase is replaced "
                "by the English-aligned primary-title adjective plus title-name order."
            ),
            default_threshold=None,
            priority=56,
            dashboard_group="Token Gate",
            notes={
                "subpolicy_name": "ep3_travel_title_adjective_alignment",
                "source_boundary_policy": "weak_auto_residual_spanish_literal_repair",
                "allowed_bucket": "review_dynamic_scope_change",
                "seed_examples": 1,
                "safe_keys": ["ep3_travel_events.3001.islamic_arabic"],
                "known_pattern": "lords_liege ES_ElLa + title-of-name becomes primary-title adjective + title name, aligned to English.",
                "maturity_status": "new_shadow_candidate",
                "safety_note": "Shadow-only: no output write, no confirmation promotion, and no production release.",
            },
        ),
        AgentSpec(
            agent_key="dynamic_same_scope_pronoun_boundary",
            agent_type="subspecialist",
            parent_agent_key="dynamic_scope_context_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="boundary_only",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description="Separates same-scope name-to-pronoun rewrites by scene family and pronoun role.",
            default_threshold=None,
            priority=55,
            dashboard_group="Token Gate",
            notes={
                "reason": "Same-scope dynamic name/pronoun changes include stable manual exceptions and unresolved subject/object rewrites.",
                "target_rule_families": [
                    "same_scope_name_to_pronoun_contextual_boundary",
                    "same_scope_gendered_name_to_pronoun_contextual_boundary",
                ],
            },
        ),
        AgentSpec(
            agent_key="dynamic_entity_rebinding_boundary",
            agent_type="subspecialist",
            parent_agent_key="dynamic_scope_context_router",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="needs_policy_evidence",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description="Tracks cross-scope entity rebinding, title, house, dynasty and court-position rewrites.",
            default_threshold=None,
            priority=56,
            dashboard_group="Token Gate",
            notes={
                "reason": "Cross-scope name/title rewrites often change semantic referents and need explicit subpolicy evidence.",
                "target_rule_families": [
                    "dynamic_name_pronoun_cross_scope_needs_subpolicy",
                    "dynamic_title_entity_cross_scope_needs_subpolicy",
                ],
            },
        ),
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
            agent_key="religion_possessive_acham_paganism_boundary",
            agent_type="subspecialist",
            parent_agent_key="religion_possessive_gods",
            model_kind=None,
            status="active",
            operational_state="operational",
            decision_role="negative_boundary",
            scope_group="religion_possessives",
            scope_sql=(
                "relative_path = 'religion/religion_paganism_l_spanish.yml' "
                "AND source_key LIKE 'acham_%_possessive'"
            ),
            scope_description=(
                "Acham pagan possessive deity names and token-reference rows that need a separate boundary."
            ),
            default_threshold=None,
            priority=31,
            dashboard_group="Religion",
            notes={
                "source_model_run_id": 291,
                "source_dataset_run_id": 348,
                "source_holdout_eval_run_id": 1,
                "known_false_safe_examples": 7,
                "active_scope_count": 16,
                "reviewed_scope_target": 16,
                "latest_source_model_run_id": 311,
                "latest_source_holdout_false_safe_count": 7,
                "reviewed_positive_examples": 7,
                "reviewed_negative_examples": 10,
                "reason": "Holdout exposed Acham deity possessive rows as false-safe for the broad religion model; keep this family separate before broad promotion.",
            },
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
                ") AND NOT ("
                "source_key LIKE '%_god_name_possessive' OR "
                "source_key LIKE '%_deity_name_possessive' OR "
                "source_key LIKE '%_devil_name_possessive' OR "
                "source_key LIKE '%_death_deity_%' OR "
                "source_key LIKE '%_name_possessive' OR "
                "source_key LIKE '%_possessive'"
                ")"
            ),
            scope_description=(
                "Small religion-specific terms that may be preserved instead of translated, "
                "excluding possessive fields routed to religion_possessive_gods."
            ),
            default_threshold=0.97,
            priority=35,
            dashboard_group="Religion",
            notes={"reason": "Recent reviews found specialized terms generalized into common PT-BR words."},
        ),
        AgentSpec(
            agent_key="religion_divine_realm_contextual_boundary",
            agent_type="subspecialist",
            parent_agent_key="religion_preserved_terms",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="contextual_boundary",
            scope_group="religion_divine_realm",
            scope_sql="relative_path LIKE 'religion/%' AND source_key LIKE '%divine_realm%'",
            scope_description=(
                "Divine-realm localization fields where aliases, preserved sacred names, and PT-BR contextual "
                "translations such as Ceu/Paraiso must be separated before broad automation."
            ),
            default_threshold=None,
            priority=36,
            dashboard_group="Religion",
            notes={
                "reason": "Recent religion_preserved_terms shadow run left dab_qhuas_divine_realm as the only pending row.",
                "active_scope_count": 105,
                "review_focus": "base divine_realm rows first; token aliases can be sampled later.",
                "promotion_requirement": "Needs more negative/corrected evidence before becoming a trainable specialist.",
            },
        ),
        AgentSpec(
            agent_key="religion_dab_qhuas_terms",
            agent_type="subspecialist",
            parent_agent_key="religion_preserved_terms",
            model_kind=None,
            status="planned",
            operational_state="candidate",
            decision_role="route_then_vote",
            scope_group="religion_terms",
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
            scope_description=(
                "Dab Qhuas preserved terms where specific Hmong religious names must be separated "
                "from generic PT-BR replacements."
            ),
            default_threshold=0.97,
            priority=37,
            dashboard_group="Religion",
            notes={
                "reason": "Preserved-terms evidence shows Dab Qhuas has the strongest negative signal inside the parent scope.",
                "reviewed_scope_target": 50,
                "known_negative_or_corrected_examples": 7,
                "exclude_child_boundary": "religion_divine_realm_contextual_boundary",
                "promotion_requirement": "Train and score cleanly before considering shadow promotion.",
            },
        ),
    ]


def recommendation_subagents() -> list[AgentSpec]:
    return [
        spec
        for spec in planned_subagents()
        if spec.agent_key not in OPERATIONAL_SPECIALIST_KEYS
        and not (spec.status == "active" and spec.operational_state == "operational")
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
            agent_key="glossary_label_translation_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_glossary_labels",
            scope_sql=None,
            scope_description="Guarded same-key Glossary label translations after historical-event boundaries are excluded.",
            default_threshold=None,
            priority=34,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_glossary_label_policy"},
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
            agent_key="token_style_modifier_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_style_modifier",
            scope_sql=None,
            scope_description="Guarded same-token style modifier additions when the token identity is unchanged.",
            default_threshold=None,
            priority=40,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_token_style_modifier_policy"},
        ),
        AgentSpec(
            agent_key="dynamic_scope_neutralization_subpolicy",
            agent_type="symbolic_subpolicy",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="active",
            operational_state=state,
            decision_role="guarded_release",
            scope_group="token_policy_dynamic_scope",
            scope_sql=None,
            scope_description="Guarded neutral PT-BR rewrites where a dynamic LocalPlayerString is unnecessary.",
            default_threshold=None,
            priority=41,
            dashboard_group="Token Gate",
            notes={**common_notes, "release_action": "dry_run_release_to_guarded_dynamic_scope_neutralization_policy"},
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
            priority=42,
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
        AgentSpec(
            agent_key="composition_coordinator_v1",
            agent_type="coordinator",
            parent_agent_key="coordinator_ensemble_v1",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="route_multiagent_segment_closure",
            scope_group="issue_multiagent_composition",
            scope_sql=None,
            scope_description=(
                "Coordinates segments with multiple open issue families, measuring when existing "
                "microagents can jointly close a segment and which missing family blocks closure."
            ),
            default_threshold=None,
            priority=16,
            dashboard_group="Coordinator",
            notes={
                "queue_table": "ml_issue_review_queue_runs",
                "latest_queue_run_id": 37,
                "queue_strategy": "multi_issue_composition_opportunity",
                "source_partial_coverage_run_id": 59,
                "candidate_segments": 6688,
                "selected_segments": 120,
                "reviewed_segments": 60,
                "all_mature_selected": 100,
                "unknown_blocker_selected": 20,
                "reviewed_all_mature_2_issue": {
                    "decision_run_id": 52,
                    "sample_size": 20,
                    "composition_ready": 8,
                    "needs_repair": 11,
                    "needs_domain_context": 1,
                    "observed_ready_rate": 0.40,
                },
                "reviewed_all_mature_3_4_issue": {
                    "decision_run_id": 53,
                    "sample_size": 20,
                    "composition_ready": 2,
                    "needs_repair": 17,
                    "needs_new_microagent": 1,
                    "observed_ready_rate": 0.10,
                },
                "reviewed_long_text_composer_blocker": {
                    "decision_run_id": 54,
                    "sample_size": 20,
                    "composition_ready": 1,
                    "needs_repair": 19,
                    "observed_ready_rate": 0.05,
                    "main_repair_routes": {
                        "spanish_select_cstring_literal": 5,
                        "quote_surface_normalization": 5,
                        "object_pronoun_token_repair": 3,
                        "lexical_gender_split": 2,
                        "glossary_visible_label_translation": 2,
                        "gender_suffix_invariant_word_repair": 1,
                        "concept_paragraph_spanish_residual": 1,
                    },
                },
                "long_text_repair_route_checkpoint": {
                    "checkpoint_table": "ml_issue_long_text_repair_route_checkpoint_runs",
                    "latest_checkpoint_run_id": 2,
                    "lifecycle_table": "ml_issue_long_text_repair_route_lifecycle_runs",
                    "latest_lifecycle_run_id": 1,
                    "structural_subpolicy_shadow_table": "ml_issue_long_text_structural_subpolicy_shadow_runs",
                    "latest_structural_subpolicy_shadow_run_id": 1,
                    "structural_subpolicy_checkpoint_table": "ml_issue_long_text_structural_subpolicy_checkpoint_runs",
                    "latest_structural_subpolicy_checkpoint_run_id": 1,
                    "structural_subpolicy_lifecycle_table": "ml_issue_long_text_structural_subpolicy_lifecycle_runs",
                    "latest_structural_subpolicy_lifecycle_run_id": 1,
                    "mixed_structural_split_table": "ml_issue_long_text_mixed_structural_split_runs",
                    "latest_mixed_structural_split_run_id": 1,
                    "mixed_structural_split_checkpoint_table": "ml_issue_long_text_mixed_structural_split_checkpoint_runs",
                    "latest_mixed_structural_split_checkpoint_run_id": 1,
                    "mixed_structural_split_lifecycle_table": "ml_issue_long_text_mixed_structural_split_lifecycle_runs",
                    "latest_mixed_structural_split_lifecycle_run_id": 1,
                    "mixed_structural_token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                    "latest_mixed_structural_token_policy_shadow_run_id": 1,
                    "mixed_structural_token_policy_checkpoint_table": "ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs",
                    "latest_mixed_structural_token_policy_checkpoint_run_id": 1,
                    "mixed_structural_token_policy_lifecycle_table": "ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs",
                    "latest_mixed_structural_token_policy_lifecycle_run_id": 1,
                    "mixed_structural_token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                    "latest_mixed_structural_token_policy_blocker_subsplit_run_id": 1,
                    "subject_pronoun_form_checkpoint_table": "ml_issue_long_text_subject_pronoun_form_checkpoint_runs",
                    "latest_subject_pronoun_form_checkpoint_run_id": 1,
                    "subject_pronoun_form_lifecycle_table": "ml_issue_long_text_subject_pronoun_form_lifecycle_runs",
                    "latest_subject_pronoun_form_lifecycle_run_id": 1,
                    "partial_composition_impact_table": "ml_issue_long_text_partial_composition_impact_runs",
                    "latest_partial_composition_impact_run_id": 1,
                    "composition_recheck_queue_table": "ml_issue_long_text_composition_recheck_queue_runs",
                    "latest_composition_recheck_queue_run_id": 1,
                    "composition_recheck_review_queue_run_id": 38,
                    "policy_name": "long_text_repair_route_shadow_v1",
                    "lifecycle_policy_name": "long_text_repair_route_shadow_lifecycle_v1",
                    "structural_subpolicy_policy_name": "long_text_structural_subpolicy_shadow_v1",
                    "structural_subpolicy_checkpoint_name": "long_text_structural_atomic_repair_checkpoint_v1",
                    "structural_subpolicy_lifecycle_policy_name": "long_text_structural_atomic_repair_shadow_lifecycle_v1",
                    "mixed_structural_split_policy_name": "long_text_mixed_structural_split_shadow_v1",
                    "mixed_structural_split_checkpoint_name": "long_text_mixed_structural_split_ready_checkpoint_v1",
                    "mixed_structural_split_lifecycle_policy_name": "long_text_mixed_structural_partial_component_shadow_lifecycle_v1",
                    "mixed_structural_token_policy_shadow_name": "long_text_mixed_structural_token_policy_shadow_v1",
                    "mixed_structural_token_policy_checkpoint_name": "long_text_mixed_structural_token_policy_partial_checkpoint_v1",
                    "mixed_structural_token_policy_lifecycle_policy_name": "long_text_mixed_structural_token_policy_partial_shadow_lifecycle_v1",
                    "mixed_structural_token_policy_blocker_subsplit_policy_name": "long_text_mixed_structural_token_policy_blocker_subsplit_v1",
                    "subject_pronoun_form_checkpoint_name": "long_text_subject_pronoun_form_partial_checkpoint_v1",
                    "subject_pronoun_form_lifecycle_policy_name": "long_text_subject_pronoun_form_partial_shadow_lifecycle_v1",
                    "partial_composition_impact_policy_name": "long_text_partial_composition_impact_shadow_v1",
                    "source_decision_run_id": 54,
                    "candidate_count": 20,
                    "allowed_shadow_repairs": 8,
                    "blocked_repairs": 11,
                    "lifecycle_released_shadow": 8,
                    "lifecycle_blocked": 0,
                    "structural_subpolicy_candidates": 11,
                    "structural_subpolicy_shadow_ready": 3,
                    "structural_subpolicy_blocked": 8,
                    "structural_subpolicy_checkpoint_allowed": 3,
                    "structural_subpolicy_checkpoint_blocked": 0,
                    "structural_subpolicy_lifecycle_released": 3,
                    "structural_subpolicy_lifecycle_blocked": 0,
                    "mixed_structural_split_blockers": 8,
                    "mixed_structural_split_units": 15,
                    "mixed_structural_split_ready": 4,
                    "mixed_structural_split_needs_token_policy": 8,
                    "mixed_structural_split_needs_semantic_review": 2,
                    "mixed_structural_split_needs_more_evidence": 1,
                    "mixed_structural_split_checkpoint_allowed": 4,
                    "mixed_structural_split_checkpoint_blocked": 0,
                    "mixed_structural_split_checkpoint_partial_only": 4,
                    "mixed_structural_split_lifecycle_released": 4,
                    "mixed_structural_split_lifecycle_blocked": 0,
                    "mixed_structural_split_lifecycle_partial_only": 4,
                    "mixed_structural_token_policy_shadow_ready": 2,
                    "mixed_structural_token_policy_superseded": 2,
                    "mixed_structural_token_policy_blocked": 4,
                    "mixed_structural_token_policy_checkpoint_allowed": 2,
                    "mixed_structural_token_policy_checkpoint_blocked": 0,
                    "mixed_structural_token_policy_checkpoint_partial_only": 2,
                    "mixed_structural_token_policy_lifecycle_released": 2,
                    "mixed_structural_token_policy_lifecycle_blocked": 0,
                    "mixed_structural_token_policy_lifecycle_partial_only": 2,
                    "mixed_structural_token_policy_blocker_subsplit_source_blocked": 4,
                    "mixed_structural_token_policy_blocker_subsplit_duplicate_inputs": 1,
                    "mixed_structural_token_policy_blocker_subsplit_units": 4,
                    "mixed_structural_token_policy_blocker_subsplit_ready": 1,
                    "mixed_structural_token_policy_blocker_subsplit_needs_phrase_mapping": 1,
                    "mixed_structural_token_policy_blocker_subsplit_needs_context_review": 1,
                    "mixed_structural_token_policy_blocker_subsplit_needs_semantic_review": 1,
                    "subject_pronoun_form_checkpoint_allowed": 1,
                    "subject_pronoun_form_checkpoint_blocked": 0,
                    "subject_pronoun_form_checkpoint_partial_only": 1,
                    "subject_pronoun_form_lifecycle_released": 1,
                    "subject_pronoun_form_lifecycle_blocked": 0,
                    "subject_pronoun_form_lifecycle_partial_only": 1,
                    "partial_composition_candidate_segments": 20,
                    "partial_composition_released_components": 18,
                    "partial_composition_component_segments": 17,
                    "partial_composition_candidate_recheck_segments": 16,
                    "partial_composition_potential_close_after_recheck": 17,
                    "partial_composition_blocker_segments": 3,
                    "partial_composition_unmapped_needs_repair": 0,
                    "composition_recheck_selected": 16,
                    "composition_recheck_open": 16,
                    "composition_recheck_reviewed": 0,
                    "composition_recheck_released_components": 17,
                    "partial_composition_coverage_states": {
                        "blocked_no_released_component": 2,
                        "candidate_composition_recheck": 16,
                        "partial_covered_blocked": 1,
                        "review_closed_baseline": 1,
                    },
                    "mixed_structural_token_policy_blocker_subsplit_microagents": {
                        "long_text_subject_pronoun_form_microagent": 1,
                        "long_text_lexical_select_cstring_bundle_guard": 1,
                        "long_text_speaker_scope_retarget_guard": 1,
                        "long_text_concept_link_reference_guard": 1,
                    },
                    "mixed_structural_token_policy_ready_microagents": {
                        "long_text_article_gender_token_microagent": 1,
                        "long_text_lexical_gender_select_cstring_microagent": 1,
                    },
                    "mixed_structural_token_policy_blocked_reasons": {
                        "complex_lexical_gender_multi_token_requires_subsplit": 2,
                        "speaker_gender_scope_change_requires_context": 1,
                        "concept_reference_added_requires_semantic_context": 1,
                    },
                    "mixed_structural_split_microagents": {
                        "long_text_lexical_gender_select_cstring_microagent": 2,
                        "long_text_object_pronoun_case_microagent": 2,
                        "long_text_select_cstring_literal_microagent": 2,
                        "long_text_preposition_surface_microagent": 1,
                        "long_text_quote_surface_microagent": 1,
                        "long_text_speaker_gender_alignment_microagent": 1,
                        "long_text_concept_paragraph_semantic_microagent": 1,
                    },
                    "composition_ready_observations": 1,
                    "allowed_routes": {
                        "quote_surface_normalization": 3,
                        "spanish_select_cstring_literal": 3,
                        "glossary_visible_label_translation": 2,
                    },
                    "blocked_routes": {
                        "object_pronoun_token_repair": 3,
                        "lexical_gender_split": 2,
                        "quote_surface_normalization": 2,
                        "spanish_select_cstring_literal": 2,
                        "gender_suffix_invariant_word_repair": 1,
                        "concept_paragraph_spanish_residual": 1,
                    },
                },
                "known_blocker": "long_text_composer",
                "false_comfort_signal": (
                    "All-families-mature and long-text maturity are not enough for release: 60 reviewed "
                    "composition samples found only 18.33% composition_ready. Failures concentrate in dynamic "
                    "Spanish payloads, gender suffix stems, residual UI helpers, possessive/object pronoun "
                    "tokens, glossary labels, and long-text surface repairs."
                ),
                "selected_buckets": {
                    "all_mature_high_issue": 60,
                    "all_mature_2_issue": 20,
                    "all_mature_3_4_issue": 20,
                    "long_text_composer_blocker": 20,
                },
                "cluster_mix": {
                    "general_cluster": 46,
                    "nickname_cluster": 29,
                    "activity_cluster": 19,
                    "long_text_cluster": 11,
                    "laamp_contract_cluster": 10,
                    "event_cluster": 5,
                },
                "maturity_signal": (
                    "60-row composition audit shows only 18.33% composition_ready: 11 ready, "
                    "47 repair-needed, 1 domain-context, and 1 new-microagent. Long text is a repair "
                    "router, not a release specialist."
                ),
                "guardrail": "Shadow queue only; no source/output read, no production write, no segment closure promotion yet.",
                "next_step": (
                    "The 8 simple route repairs and 3 structural atomic repairs are lifecycle-observed "
                    "in shadow. The 8 mixed blockers are split into 15 partial units, and 4 partial "
                    "components are now lifecycle-observed. Token-policy shadow separated 2 observable "
                    "components, 2 superseded duplicates, and 4 real blockers; the 2 observable token "
                    "components are now lifecycle-observed. The 4 real blockers are split into 4 narrower "
                    "units: 1 ready subject-pronoun component, 1 phrase-mapping bundle, 1 speaker-scope "
                    "context guard, and 1 concept semantic guard. The ready subject-pronoun component "
                    "is lifecycle-observed in shadow. Partial composition impact now shows 17/20 "
                    "potential closes after recheck, including 16 new composition recheck candidates. "
                    "The recheck queue is created as review_queue_run 38; next review it and split "
                    "results into composition_ready checkpoints, concrete repairs, or new microagent evidence."
                ),
            },
        ),
        AgentSpec(
            agent_key="long_text_repair_router",
            agent_type="subcoordinator",
            parent_agent_key="composition_coordinator_v1",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="repair_route_checkpoint",
            scope_group="issue_long_text_repair_route",
            scope_sql=None,
            scope_description=(
                "Classifies reviewed long-text composition failures into reusable repair routes and separates "
                "same-token/dynamic-literal shadow repairs from structural token changes."
            ),
            default_threshold=None,
            priority=17,
            dashboard_group="Issue Network",
            notes={
                "checkpoint_table": "ml_issue_long_text_repair_route_checkpoint_runs",
                "latest_checkpoint_run_id": 2,
                "lifecycle_table": "ml_issue_long_text_repair_route_lifecycle_runs",
                "latest_lifecycle_run_id": 1,
                "structural_subpolicy_shadow_table": "ml_issue_long_text_structural_subpolicy_shadow_runs",
                "latest_structural_subpolicy_shadow_run_id": 1,
                "structural_subpolicy_checkpoint_table": "ml_issue_long_text_structural_subpolicy_checkpoint_runs",
                "latest_structural_subpolicy_checkpoint_run_id": 1,
                "structural_subpolicy_lifecycle_table": "ml_issue_long_text_structural_subpolicy_lifecycle_runs",
                "latest_structural_subpolicy_lifecycle_run_id": 1,
                "mixed_structural_split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_mixed_structural_split_run_id": 1,
                "mixed_structural_split_checkpoint_table": "ml_issue_long_text_mixed_structural_split_checkpoint_runs",
                "latest_mixed_structural_split_checkpoint_run_id": 1,
                "mixed_structural_split_lifecycle_table": "ml_issue_long_text_mixed_structural_split_lifecycle_runs",
                "latest_mixed_structural_split_lifecycle_run_id": 1,
                "mixed_structural_token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_mixed_structural_token_policy_shadow_run_id": 1,
                "mixed_structural_token_policy_checkpoint_table": "ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs",
                "latest_mixed_structural_token_policy_checkpoint_run_id": 1,
                "mixed_structural_token_policy_lifecycle_table": "ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs",
                "latest_mixed_structural_token_policy_lifecycle_run_id": 1,
                "mixed_structural_token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_mixed_structural_token_policy_blocker_subsplit_run_id": 1,
                    "subject_pronoun_form_checkpoint_table": "ml_issue_long_text_subject_pronoun_form_checkpoint_runs",
                    "latest_subject_pronoun_form_checkpoint_run_id": 1,
                    "subject_pronoun_form_lifecycle_table": "ml_issue_long_text_subject_pronoun_form_lifecycle_runs",
                    "latest_subject_pronoun_form_lifecycle_run_id": 1,
                    "partial_composition_impact_table": "ml_issue_long_text_partial_composition_impact_runs",
                    "latest_partial_composition_impact_run_id": 1,
                    "composition_recheck_queue_table": "ml_issue_long_text_composition_recheck_queue_runs",
                    "latest_composition_recheck_queue_run_id": 1,
                    "composition_recheck_review_queue_run_id": 38,
                    "policy_name": "long_text_repair_route_shadow_v1",
                "lifecycle_policy_name": "long_text_repair_route_shadow_lifecycle_v1",
                "structural_subpolicy_policy_name": "long_text_structural_subpolicy_shadow_v1",
                "structural_subpolicy_checkpoint_name": "long_text_structural_atomic_repair_checkpoint_v1",
                "structural_subpolicy_lifecycle_policy_name": "long_text_structural_atomic_repair_shadow_lifecycle_v1",
                "mixed_structural_split_policy_name": "long_text_mixed_structural_split_shadow_v1",
                "mixed_structural_split_checkpoint_name": "long_text_mixed_structural_split_ready_checkpoint_v1",
                "mixed_structural_split_lifecycle_policy_name": "long_text_mixed_structural_partial_component_shadow_lifecycle_v1",
                "mixed_structural_token_policy_shadow_name": "long_text_mixed_structural_token_policy_shadow_v1",
                "mixed_structural_token_policy_checkpoint_name": "long_text_mixed_structural_token_policy_partial_checkpoint_v1",
                "mixed_structural_token_policy_lifecycle_policy_name": "long_text_mixed_structural_token_policy_partial_shadow_lifecycle_v1",
                    "mixed_structural_token_policy_blocker_subsplit_policy_name": "long_text_mixed_structural_token_policy_blocker_subsplit_v1",
                    "subject_pronoun_form_checkpoint_name": "long_text_subject_pronoun_form_partial_checkpoint_v1",
                    "subject_pronoun_form_lifecycle_policy_name": "long_text_subject_pronoun_form_partial_shadow_lifecycle_v1",
                    "partial_composition_impact_policy_name": "long_text_partial_composition_impact_shadow_v1",
                    "source_queue_run_id": 37,
                "source_decision_run_id": 54,
                "candidate_count": 20,
                "allowed_shadow_repairs": 8,
                "blocked_repairs": 11,
                "lifecycle_released_shadow": 8,
                "lifecycle_blocked": 0,
                "structural_subpolicy_candidates": 11,
                "structural_subpolicy_shadow_ready": 3,
                "structural_subpolicy_blocked": 8,
                "structural_subpolicy_checkpoint_allowed": 3,
                "structural_subpolicy_checkpoint_blocked": 0,
                "structural_subpolicy_lifecycle_released": 3,
                "structural_subpolicy_lifecycle_blocked": 0,
                "mixed_structural_split_blockers": 8,
                "mixed_structural_split_units": 15,
                "mixed_structural_split_ready": 4,
                "mixed_structural_split_needs_token_policy": 8,
                "mixed_structural_split_needs_semantic_review": 2,
                "mixed_structural_split_needs_more_evidence": 1,
                "mixed_structural_split_checkpoint_allowed": 4,
                "mixed_structural_split_checkpoint_blocked": 0,
                "mixed_structural_split_checkpoint_partial_only": 4,
                "mixed_structural_split_lifecycle_released": 4,
                "mixed_structural_split_lifecycle_blocked": 0,
                "mixed_structural_split_lifecycle_partial_only": 4,
                "mixed_structural_token_policy_shadow_ready": 2,
                "mixed_structural_token_policy_superseded": 2,
                "mixed_structural_token_policy_blocked": 4,
                "mixed_structural_token_policy_checkpoint_allowed": 2,
                "mixed_structural_token_policy_checkpoint_blocked": 0,
                "mixed_structural_token_policy_checkpoint_partial_only": 2,
                "mixed_structural_token_policy_lifecycle_released": 2,
                "mixed_structural_token_policy_lifecycle_blocked": 0,
                "mixed_structural_token_policy_lifecycle_partial_only": 2,
                "mixed_structural_token_policy_blocker_subsplit_source_blocked": 4,
                "mixed_structural_token_policy_blocker_subsplit_duplicate_inputs": 1,
                "mixed_structural_token_policy_blocker_subsplit_units": 4,
                "mixed_structural_token_policy_blocker_subsplit_ready": 1,
                "mixed_structural_token_policy_blocker_subsplit_needs_phrase_mapping": 1,
                "mixed_structural_token_policy_blocker_subsplit_needs_context_review": 1,
                "mixed_structural_token_policy_blocker_subsplit_needs_semantic_review": 1,
                    "subject_pronoun_form_checkpoint_allowed": 1,
                    "subject_pronoun_form_checkpoint_blocked": 0,
                    "subject_pronoun_form_checkpoint_partial_only": 1,
                    "subject_pronoun_form_lifecycle_released": 1,
                    "subject_pronoun_form_lifecycle_blocked": 0,
                    "subject_pronoun_form_lifecycle_partial_only": 1,
                    "partial_composition_candidate_segments": 20,
                    "partial_composition_released_components": 18,
                    "partial_composition_component_segments": 17,
                    "partial_composition_candidate_recheck_segments": 16,
                    "partial_composition_potential_close_after_recheck": 17,
                    "partial_composition_blocker_segments": 3,
                    "partial_composition_unmapped_needs_repair": 0,
                    "composition_recheck_selected": 16,
                    "composition_recheck_open": 16,
                    "composition_recheck_reviewed": 0,
                    "composition_recheck_released_components": 17,
                    "partial_composition_coverage_states": {
                        "blocked_no_released_component": 2,
                        "candidate_composition_recheck": 16,
                        "partial_covered_blocked": 1,
                        "review_closed_baseline": 1,
                    },
                "mixed_structural_token_policy_blocker_subsplit_microagents": {
                    "long_text_subject_pronoun_form_microagent": 1,
                    "long_text_lexical_select_cstring_bundle_guard": 1,
                    "long_text_speaker_scope_retarget_guard": 1,
                    "long_text_concept_link_reference_guard": 1,
                },
                "mixed_structural_token_policy_ready_microagents": {
                    "long_text_article_gender_token_microagent": 1,
                    "long_text_lexical_gender_select_cstring_microagent": 1,
                },
                "mixed_structural_token_policy_blocked_reasons": {
                    "complex_lexical_gender_multi_token_requires_subsplit": 2,
                    "speaker_gender_scope_change_requires_context": 1,
                    "concept_reference_added_requires_semantic_context": 1,
                },
                "mixed_structural_split_microagents": {
                    "long_text_lexical_gender_select_cstring_microagent": 2,
                    "long_text_object_pronoun_case_microagent": 2,
                    "long_text_select_cstring_literal_microagent": 2,
                    "long_text_preposition_surface_microagent": 1,
                    "long_text_quote_surface_microagent": 1,
                    "long_text_speaker_gender_alignment_microagent": 1,
                    "long_text_concept_paragraph_semantic_microagent": 1,
                },
                "composition_ready_observations": 1,
                "allowed_routes": {
                    "quote_surface_normalization": 3,
                    "spanish_select_cstring_literal": 3,
                    "glossary_visible_label_translation": 2,
                },
                "released_routes": {
                    "quote_surface_normalization": 3,
                    "spanish_select_cstring_literal": 3,
                    "glossary_visible_label_translation": 2,
                },
                "blocked_routes": {
                    "object_pronoun_token_repair": 3,
                    "lexical_gender_split": 2,
                    "quote_surface_normalization": 2,
                    "spanish_select_cstring_literal": 2,
                    "gender_suffix_invariant_word_repair": 1,
                    "concept_paragraph_spanish_residual": 1,
                },
                "structural_subpolicy_shadow_ready_names": {
                    "long_text_invariant_word_gender_token_removal": 1,
                    "long_text_object_pronoun_case_repair": 1,
                    "long_text_visible_ele_ela_subject_token": 1,
                },
                "structural_subpolicy_blocked_names": {
                    "long_text_lexical_gender_select_cstring_split": 2,
                    "long_text_mixed_quote_token_surface_repair": 2,
                    "long_text_select_cstring_structural_literal_repair": 2,
                    "long_text_concept_paragraph_residual_rewrite": 1,
                    "long_text_mixed_object_pronoun_surface_repair": 1,
                },
                "maturity_status": "shadow_lifecycle_observing",
                "guardrail": "No source/output read, no production write, no segment closure promotion.",
                "next_step": (
                    "The 8 simple route repairs and 3 structural atomic repairs are lifecycle-observed "
                    "in shadow. The 8 mixed blockers are split into 15 partial units, and 4 partial "
                    "components are now lifecycle-observed. Token-policy shadow separated 2 observable "
                    "components, 2 superseded duplicates, and 4 real blockers; the 2 observable token "
                    "components are now lifecycle-observed. The 4 real blockers are split into 4 narrower "
                    "units: 1 ready subject-pronoun component, 1 phrase-mapping bundle, 1 speaker-scope "
                    "context guard, and 1 concept semantic guard. The ready subject-pronoun component "
                    "is lifecycle-observed in shadow. Partial composition impact now shows 17/20 "
                    "potential closes after recheck, including 16 new composition recheck candidates. "
                    "The recheck queue is created as review_queue_run 38; next review it and split "
                    "results into composition_ready checkpoints, concrete repairs, or new microagent evidence."
                ),
            },
        ),
        AgentSpec(
            agent_key="long_text_article_gender_token_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_repair_builder",
            scope_group="issue_long_text_mixed_structural_token_policy_shadow",
            scope_sql=None,
            scope_description=(
                "Observes narrow article-gender token swaps in long texts when a partial repair changes "
                "ES_OA/ES_XA article helpers without claiming whole-segment closure."
            ),
            default_threshold=None,
            priority=18,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "token_policy_checkpoint_table": "ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs",
                "latest_token_policy_checkpoint_run_id": 1,
                "token_policy_lifecycle_table": "ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs",
                "latest_token_policy_lifecycle_run_id": 1,
                "split_units": 1,
                "token_policy_shadow_ready": 1,
                "token_policy_checkpoint_allowed": 1,
                "token_policy_checkpoint_blocked": 0,
                "token_policy_lifecycle_released": 1,
                "token_policy_lifecycle_blocked": 0,
                "token_policy_blocked": 0,
                "shadow_action": "observe_article_es_oa_to_es_xa_partial_shadow",
                "checkpoint_action": "stage_article_es_oa_to_es_xa_partial_token_shadow",
                "lifecycle_policy": "long_text_mixed_structural_token_policy_partial_shadow_lifecycle_v1",
                "partial_component_only": True,
                "source_microagent_key": "long_text_article_gender_token_microagent",
                "next_step": "Observe as partial lifecycle evidence and measure composition impact only after sibling blockers mature.",
            },
        ),
        AgentSpec(
            agent_key="long_text_lexical_gender_select_cstring_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_repair_builder",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Builds lexical gender whole-word splits with Select_CString when ES_OA/ES_XA suffix helpers "
                "cannot safely form natural PT-BR nouns."
            ),
            default_threshold=None,
            priority=18,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "token_policy_checkpoint_table": "ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs",
                "latest_token_policy_checkpoint_run_id": 1,
                "token_policy_lifecycle_table": "ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs",
                "latest_token_policy_lifecycle_run_id": 1,
                "split_units": 2,
                "split_status": "needs_token_policy",
                "token_policy_shadow_ready": 1,
                "token_policy_checkpoint_allowed": 1,
                "token_policy_checkpoint_blocked": 0,
                "token_policy_lifecycle_released": 1,
                "token_policy_lifecycle_blocked": 0,
                "token_policy_blocked": 1,
                "token_policy_blocked_reason": "complex_lexical_gender_multi_token_requires_subsplit",
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "token_policy_blocker_subsplit_phrase_mapping": 1,
                "shadow_action": "observe_single_select_cstring_lexical_gender_phrase_shadow",
                "checkpoint_action": "stage_single_select_cstring_lexical_gender_phrase_partial_shadow",
                "lifecycle_policy": "long_text_mixed_structural_token_policy_partial_shadow_lifecycle_v1",
                "source_microagent_key": "long_text_lexical_gender_select_cstring_microagent",
                "next_step": "Observe one simple Select_CString phrase as lifecycle evidence; send the complex bundle to phrase mapping review.",
            },
        ),
        AgentSpec(
            agent_key="long_text_lexical_select_cstring_bundle_guard",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_phrase_mapping_guard",
            scope_group="issue_long_text_mixed_structural_token_policy_blocker_subsplit",
            scope_sql=None,
            scope_description=(
                "Guards multi-token Select_CString lexical bundles that cannot be mapped safely from token delta alone."
            ),
            default_threshold=None,
            priority=19,
            dashboard_group="Issue Network",
            notes={
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "subsplit_units": 1,
                "subsplit_status": "needs_phrase_mapping_review",
                "block_reason": "multi_select_cstring_bundle_requires_phrase_mapping",
                "review_route": "phrase_mapping_review",
                "partial_component_only": True,
                "source_microagents": [
                    "long_text_lexical_gender_select_cstring_microagent",
                    "long_text_subject_reference_token_microagent",
                ],
                "next_step": "Collect phrase-aligned evidence before any checkpoint; token delta alone is not enough.",
            },
        ),
        AgentSpec(
            agent_key="long_text_select_cstring_literal_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="dynamic_literal_repair_builder",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Handles Spanish visible literals inside Select_CString payloads when a long text also needs "
                "structural token review."
            ),
            default_threshold=None,
            priority=19,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "split_units": 2,
                "split_status": "needs_token_policy",
                "token_policy_superseded": 2,
                "superseded_by": [
                    "long_text_article_gender_token_microagent",
                    "long_text_object_pronoun_case_microagent",
                ],
                "source_microagent_key": "long_text_select_cstring_literal_microagent",
                "next_step": "Collect true Select_CString literal examples; current two rows belong to sibling token microagents.",
            },
        ),
        AgentSpec(
            agent_key="long_text_subject_reference_token_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_repair_builder",
            scope_group="issue_long_text_mixed_structural_token_policy_shadow",
            scope_sql=None,
            scope_description=(
                "Explains subject-reference token edits inside long Select_CString passages where multiple "
                "gendered references must be split before the repair can be trusted."
            ),
            default_threshold=None,
            priority=20,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "split_units": 1,
                "token_policy_blocked": 1,
                "token_policy_blocked_reason": "complex_lexical_gender_multi_token_requires_subsplit",
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "token_policy_blocker_subsplit_ready": 1,
                "token_policy_blocker_subsplit_ready_microagent": "long_text_subject_pronoun_form_microagent",
                "partial_component_only": True,
                "source_microagent_key": "long_text_subject_reference_token_microagent",
                "next_step": "Use the narrower subject-pronoun microagent for checkpoint; keep this broad agent as source evidence.",
            },
        ),
        AgentSpec(
            agent_key="long_text_subject_pronoun_form_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_repair_builder",
            scope_group="issue_long_text_mixed_structural_token_policy_blocker_subsplit",
            scope_sql=None,
            scope_description=(
                "Extracts same-scope GetWomanMan to GetSheHe subject-pronoun form swaps from broader mixed token blockers."
            ),
            default_threshold=None,
            priority=20,
            dashboard_group="Issue Network",
            notes={
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "subsplit_units": 1,
                "subsplit_ready": 1,
                "subsplit_status": "subsplit_ready",
                "subsplit_action": "extract_subject_getwomanman_to_getshehe_same_scope_shadow",
                "checkpoint_table": "ml_issue_long_text_subject_pronoun_form_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "checkpoint_allowed": 1,
                "checkpoint_blocked": 0,
                "checkpoint_action": "stage_subject_getwomanman_to_getshehe_same_scope_partial_shadow",
                "lifecycle_table": "ml_issue_long_text_subject_pronoun_form_lifecycle_runs",
                "latest_lifecycle_run_id": 1,
                "lifecycle_released": 1,
                "lifecycle_blocked": 0,
                "lifecycle_policy": "long_text_subject_pronoun_form_partial_shadow_lifecycle_v1",
                "partial_component_only": True,
                "source_microagents": [
                    "long_text_lexical_gender_select_cstring_microagent",
                    "long_text_subject_reference_token_microagent",
                ],
                "next_step": "Measure composition impact only after phrase-mapping, speaker-scope context and concept-reference blockers mature.",
            },
        ),
        AgentSpec(
            agent_key="long_text_object_pronoun_case_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_existing_policy_router",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Extracts GetSheHe to GetHerHim object-pronoun repairs from mixed long-text rows and routes "
                "them to the existing atomic object-pronoun policy."
            ),
            default_threshold=None,
            priority=20,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "split_units": 2,
                "split_ready": 2,
                "split_status": "split_ready",
                "token_policy_supersedes_select_cstring_literal": 1,
                "checkpoint_table": "ml_issue_long_text_mixed_structural_split_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "checkpoint_allowed": 2,
                "checkpoint_blocked": 0,
                "checkpoint_scope": "partial_component_only",
                "lifecycle_table": "ml_issue_long_text_mixed_structural_split_lifecycle_runs",
                "latest_lifecycle_run_id": 1,
                "lifecycle_released": 2,
                "lifecycle_blocked": 0,
                "lifecycle_scope": "partial_component_only",
                "source_microagent_key": "long_text_object_pronoun_case_microagent",
                "next_step": "Measure composition impact after sibling token/semantic components mature; still no whole-segment release.",
            },
        ),
        AgentSpec(
            agent_key="long_text_quote_surface_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_surface_repair_builder",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Extracts quote/guillemet and punctuation normalization from mixed long-text rows before "
                "token-sensitive pieces are evaluated."
            ),
            default_threshold=None,
            priority=21,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "split_units": 1,
                "split_ready": 1,
                "split_status": "split_ready",
                "checkpoint_table": "ml_issue_long_text_mixed_structural_split_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "checkpoint_allowed": 1,
                "checkpoint_blocked": 0,
                "checkpoint_scope": "partial_component_only",
                "lifecycle_table": "ml_issue_long_text_mixed_structural_split_lifecycle_runs",
                "latest_lifecycle_run_id": 1,
                "lifecycle_released": 1,
                "lifecycle_blocked": 0,
                "lifecycle_scope": "partial_component_only",
                "source_microagent_key": "long_text_quote_surface_microagent",
                "next_step": "Measure composition impact after sibling token/semantic components mature; still no whole-segment release.",
            },
        ),
        AgentSpec(
            agent_key="long_text_preposition_surface_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_surface_repair_builder",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Extracts narrow preposition surface normalization from mixed long-text rows while other "
                "token-sensitive gender pieces remain blocked."
            ),
            default_threshold=None,
            priority=21,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "split_units": 1,
                "split_ready": 1,
                "split_status": "split_ready",
                "checkpoint_table": "ml_issue_long_text_mixed_structural_split_checkpoint_runs",
                "latest_checkpoint_run_id": 1,
                "checkpoint_allowed": 1,
                "checkpoint_blocked": 0,
                "checkpoint_scope": "partial_component_only",
                "lifecycle_table": "ml_issue_long_text_mixed_structural_split_lifecycle_runs",
                "latest_lifecycle_run_id": 1,
                "lifecycle_released": 1,
                "lifecycle_blocked": 0,
                "lifecycle_scope": "partial_component_only",
                "source_microagent_key": "long_text_preposition_surface_microagent",
                "next_step": "Measure composition impact after sibling token/semantic components mature; still no whole-segment release.",
            },
        ),
        AgentSpec(
            agent_key="long_text_speaker_gender_alignment_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_alignment_builder",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Explains speaker/listener gender-token scope alignment inside dialogue-heavy long texts."
            ),
            default_threshold=None,
            priority=22,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "split_units": 1,
                "split_status": "needs_token_policy",
                "token_policy_blocked": 1,
                "token_policy_blocked_reason": "speaker_gender_scope_change_requires_context",
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "token_policy_blocker_subsplit_context_review": 1,
                "source_microagent_key": "long_text_speaker_gender_alignment_microagent",
                "next_step": "Route scope-retarget case to speaker-scope context guard before checkpoint.",
            },
        ),
        AgentSpec(
            agent_key="long_text_speaker_scope_retarget_guard",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_context_guard",
            scope_group="issue_long_text_mixed_structural_token_policy_blocker_subsplit",
            scope_sql=None,
            scope_description=(
                "Blocks speaker/listener token retargets when added and removed CK3 scopes differ and dialogue context is required."
            ),
            default_threshold=None,
            priority=22,
            dashboard_group="Issue Network",
            notes={
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "subsplit_units": 1,
                "subsplit_status": "needs_context_scope_review",
                "block_reason": "speaker_token_scope_retarget_requires_dialogue_context",
                "review_route": "speaker_scope_context_review",
                "partial_component_only": True,
                "source_microagent_key": "long_text_speaker_gender_alignment_microagent",
                "next_step": "Collect dialogue context evidence; do not checkpoint as mechanical token repair.",
            },
        ),
        AgentSpec(
            agent_key="long_text_concept_link_reference_guard",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_token_context_guard",
            scope_group="issue_long_text_mixed_structural_token_policy_shadow",
            scope_sql=None,
            scope_description=(
                "Guards long concept paragraphs when a proposed repair adds, removes or redirects a game "
                "concept link and therefore needs semantic context instead of mechanical token release."
            ),
            default_threshold=None,
            priority=23,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "token_policy_shadow_table": "ml_issue_long_text_mixed_structural_token_policy_shadow_runs",
                "latest_token_policy_shadow_run_id": 1,
                "split_units": 1,
                "token_policy_blocked": 1,
                "token_policy_blocked_reason": "concept_reference_added_requires_semantic_context",
                "token_policy_blocker_subsplit_table": "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs",
                "latest_token_policy_blocker_subsplit_run_id": 1,
                "token_policy_blocker_subsplit_semantic_review": 1,
                "subsplit_status": "needs_semantic_review",
                "review_route": "semantic_concept_reference_review",
                "partial_component_only": True,
                "source_microagent_key": "long_text_concept_link_reference_guard",
                "next_step": "Route concept-link changes to semantic review; do not release as token-only repair.",
            },
        ),
        AgentSpec(
            agent_key="long_text_concept_paragraph_semantic_microagent",
            agent_type="microagent",
            parent_agent_key="long_text_repair_router",
            model_kind=None,
            status="experimental",
            operational_state="shadow",
            decision_role="partial_semantic_voter",
            scope_group="issue_long_text_mixed_structural_split",
            scope_sql=None,
            scope_description=(
                "Votes on long concept paragraphs where the correction changes meaning, modality or concept references."
            ),
            default_threshold=None,
            priority=23,
            dashboard_group="Issue Network",
            notes={
                "split_table": "ml_issue_long_text_mixed_structural_split_runs",
                "latest_split_run_id": 1,
                "split_units": 1,
                "split_status": "needs_semantic_review",
                "source_microagent_key": "long_text_concept_paragraph_semantic_microagent",
                "next_step": "Route to semantic context review; do not treat as mechanical repair.",
            },
        ),
    ]
    for key, config in SPECIALISTS.items():
        parent = "coordinator_ensemble_v1"
        agent_type = "specialist"
        if key.startswith("title_") or key == "culture_title_labels":
            parent = "titles"
            agent_type = "subspecialist"
        elif key == "select_cstring_ep3_laamp_roles":
            parent = "select_cstring_scene_router"
            agent_type = "subspecialist"
        elif key == "select_cstring_ep3_laamp_spanish_role_boundary":
            parent = "select_cstring_ep3_laamp_roles"
            agent_type = "subspecialist"
        elif key == "select_cstring_ep3_laamp_role_correction_candidate":
            parent = "select_cstring_ep3_laamp_spanish_role_boundary"
            agent_type = "subspecialist"
        elif key in {"religion_divine_realm_contextual_boundary", "religion_dab_qhuas_terms"}:
            parent = "religion_preserved_terms"
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
                dashboard_group=(
                    "Titles"
                    if key.startswith("title") or key == "culture_title_labels"
                    else "Token Gate"
                    if key.startswith("select_cstring_")
                    else "Religion"
                ),
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


def scope_score_eligible_count(conn, scope_sql: str | None) -> int:
    if not scope_sql:
        qualified = "1 = 1"
    else:
        qualified = qualify_scope_sql(scope_sql, "s")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND coalesce(o.portuguese_text, sc.confirmed_text, s.old_text, s.spanish_text, '') <> ''
          AND NOT (
              sc.confirmation_level IN ('human_confirmed', 'human')
              AND sc.locked = 1
          )
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


def latest_specialist_policy_snapshot(conn, specialist_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            status,
            pending_real_count,
            specialist_new_safe_count,
            specialist_demoted_count,
            score_run_id,
            model_run_id,
            created_at
        FROM ml_specialist_policy_snapshots
        WHERE specialist_key = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (specialist_key,),
    ).fetchone()
    return dict(row) if row else None


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
        active_count = scope_active_count(conn, spec.scope_sql)
        eligible_count = scope_score_eligible_count(conn, spec.scope_sql)
        specialist_policy = latest_specialist_policy_snapshot(conn, spec.agent_key)
        if evidence["evidence_count"] > 0 and active_count == 0:
            status = "inactive_scope"
            reason = "Reviewed evidence exists, but the current source package has no active rows in this scope."
        elif evidence["evidence_count"] > 0 and eligible_count == 0:
            status = "covered_locked"
            reason = "All active scoped rows are locked human confirmations; keep as historical boundary, not a training target."
        elif (
            specialist_policy
            and specialist_policy.get("status") == "READY_BLOCKER_ONLY"
            and int(specialist_policy.get("pending_real_count") or 0) == 0
        ):
            status = "covered_blocker_only"
            reason = (
                "Latest specialist policy is clean but releases zero auto-safe rows; "
                "monitor as a trained boundary instead of retraining this scope."
            )
        elif (
            specialist_policy
            and str(specialist_policy.get("status") or "").startswith("READY")
            and int(specialist_policy.get("pending_real_count") or 0) == 0
            and int(specialist_policy.get("specialist_new_safe_count") or 0) == 0
        ):
            status = "covered_clean"
            reason = "Latest specialist policy has no pending real divergences; keep monitoring instead of retraining."
        elif evidence["evidence_count"] == 0:
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
        recommendation_specs = recommendation_subagents()
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
