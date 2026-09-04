from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from typing import Any

import db


RULE_VERSION = "quality_provider_proposal_generator_v3_punctuation_routing"
ACTIONABLE_STATUSES = ("new_candidate", "recurring_candidate")
MAX_POSITIVE_CASES = 3
MAX_NEGATIVE_CASES = 3


def _correction_lane(issue_type: str, token_context: str) -> dict[str, str]:
    signal = f"{issue_type} {token_context}".casefold()
    if any(token in signal for token in ("punctuation", "angular_quote", "guillemet", "aspas")):
        return {
            "id": "ptbr_punctuation",
            "label": "Aspas e pontuação PT-BR",
            "objective": "Normalizar aspas e pontuação visível para o padrão PT-BR sem alterar tokens CK3.",
        }
    if any(token in signal for token in ("spanish", "espanhol", "untranslated", "residual")):
        return {
            "id": "spanish_residual",
            "label": "Espanhol residual",
            "objective": "Remover espanhol residual sem alterar tokens ou estruturas dinâmicas.",
        }
    if any(token in signal for token in ("mojibake", "unicode", "encoding", "garbled")):
        return {
            "id": "mojibake_unicode",
            "label": "Unicode / mojibake",
            "objective": "Restaurar caracteres corrompidos preservando a intenção e a pontuação legítima.",
        }
    if any(token in signal for token in ("gender", "genero", "article", "pronoun", "meu", "minha", "seu", "sua")):
        return {
            "id": "dynamic_gender",
            "label": "Gênero e concordância dinâmica",
            "objective": "Aplicar gênero, artigo e posse conforme o escopo dinâmico do segmento.",
        }
    if any(token in signal for token in ("token", "structure", "syntax", "boundary", "bracket", "quote", "escape")):
        return {
            "id": "structure_tokens",
            "label": "Estrutura e tokens",
            "objective": "Corrigir fronteiras e sintaxe mantendo o multiconjunto de tokens protegido.",
        }
    if "glossary" in signal:
        return {
            "id": "glossary_terms",
            "label": "Glossário e termos",
            "objective": "Aplicar o termo aprovado sem modificar a estrutura que o contém.",
        }
    return {
        "id": "other_patterns",
        "label": "Outros padrões confirmados",
        "objective": "Materializar um reparo determinístico com controles positivos, negativos e de fronteira.",
    }


def _discovery_review_by_family(
    conn: sqlite3.Connection,
    discovery_run_id: int,
) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "ml_regenerative_review_decisions"):
        return {}
    rows = conn.execute(
        """
        SELECT id, segment_id, decision, evidence_json
        FROM ml_regenerative_review_decisions
        WHERE queue_type = 'discovery'
          AND is_active = 1
          AND snapshot_id <= ?
        ORDER BY id DESC
        """,
        (discovery_run_id,),
    ).fetchall()
    summaries: dict[str, dict[str, Any]] = {}
    seen_examples: set[tuple[str, int]] = set()
    for row in rows:
        evidence = _json_object(row["evidence_json"])
        family_key = str(evidence.get("family_key") or "").strip()
        if not family_key:
            continue
        example_key = (
            family_key,
            int(row["segment_id"] or evidence.get("segment_id") or 0),
        )
        if example_key in seen_examples:
            continue
        seen_examples.add(example_key)
        summary = summaries.setdefault(
            family_key,
            {
                "reviewed_count": 0,
                "supports_pattern": 0,
                "contradicts_pattern": 0,
                "boundary_case": 0,
            },
        )
        decision = str(row["decision"] or "")
        summary["reviewed_count"] += 1
        if decision in summary:
            summary[decision] += 1
    for summary in summaries.values():
        supports = int(summary["supports_pattern"])
        contradicts = int(summary["contradicts_pattern"])
        boundaries = int(summary["boundary_case"])
        if supports > contradicts:
            summary["routing_status"] = (
                "ready_with_boundary_guard" if boundaries else "ready_for_correction"
            )
            summary["route_to_correction"] = True
        elif contradicts >= supports and contradicts > 0:
            summary["routing_status"] = "rejected_by_human_evidence"
            summary["route_to_correction"] = False
        elif boundaries:
            summary["routing_status"] = "boundary_only"
            summary["route_to_correction"] = False
        else:
            summary["routing_status"] = "awaiting_review"
            summary["route_to_correction"] = False
    return summaries


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        payload = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_") or "quality_pattern"


def _stable_key(prefix: str, *parts: Any) -> str:
    canonical = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"{prefix}_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _path_family(relative_path: str) -> str:
    parts = [part for part in str(relative_path or "").replace("\\", "/").split("/") if part]
    if len(parts) <= 1:
        return "_root"
    if parts[0] in {"dlc", "event_localization", "activities", "culture"} and len(parts) >= 3:
        return "/".join(parts[:2])
    return parts[0]


def _discovery_context(
    conn: sqlite3.Connection,
    discovery_run_id: int | None,
) -> dict[str, Any]:
    if not _table_exists(conn, "ml_quality_pattern_discovery_runs"):
        raise RuntimeError("Pattern discovery is not instrumented in this database.")
    row = conn.execute(
        """
        SELECT id, quality_epoch_id, score_run_id, actionable_family_count,
               status, finished_at
        FROM ml_quality_pattern_discovery_runs
        WHERE status = 'completed'
          AND (? IS NULL OR id = ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (discovery_run_id, discovery_run_id),
    ).fetchone()
    if not row:
        raise RuntimeError("No completed pattern discovery run is available.")
    return dict(row)


def _actionable_families(
    conn: sqlite3.Connection,
    discovery_run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT family.id AS family_id, family.family_key, family.issue_type,
               family.token_context, family.file_family, family.text_relation,
               family.evidence_kind, family.provider_id, family.evidence_type,
               observation.status, observation.segment_count,
               observation.occurrence_count, observation.priority,
               observation.confidence, observation.severity,
               observation.samples_json, observation.metrics_json
        FROM ml_quality_pattern_observations observation
        JOIN ml_quality_pattern_families family ON family.id = observation.family_id
        WHERE observation.run_id = ?
          AND observation.status IN ('new_candidate', 'recurring_candidate')
          AND COALESCE(family.provider_id, '') = ''
        ORDER BY observation.priority DESC, observation.segment_count DESC, family.id
        """,
        (discovery_run_id,),
    ).fetchall()
    reviews_by_family = _discovery_review_by_family(conn, discovery_run_id)
    supervised_contract_available = _table_exists(
        conn, "ml_regenerative_review_decisions"
    )
    families: list[dict[str, Any]] = []
    for row in rows:
        family = dict(row)
        family["samples"] = _json_list(family.pop("samples_json", "[]"))
        family["metrics"] = _json_object(family.pop("metrics_json", "{}"))
        review = reviews_by_family.get(str(family.get("family_key") or ""), {})
        family["human_review"] = review or {
            "reviewed_count": 0,
            "routing_status": "awaiting_review",
            "route_to_correction": not supervised_contract_available,
        }
        if supervised_contract_available and not family["human_review"].get(
            "route_to_correction"
        ):
            continue
        family["correction_lane"] = _correction_lane(
            str(family.get("issue_type") or ""),
            str(family.get("token_context") or ""),
        )
        families.append(family)
    return families


def _negative_controls(
    conn: sqlite3.Connection,
    *,
    score_run_id: int,
    file_families: set[str],
    excluded_segment_ids: set[int],
    limit: int = MAX_NEGATIVE_CASES,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "ml_score_items") or limit <= 0:
        return []
    path_clauses: list[str] = []
    path_parameters: list[Any] = []
    for family in sorted(file_families):
        if family == "_root":
            path_clauses.append("instr(item.relative_path, '/') = 0")
        else:
            path_clauses.append("(item.relative_path = ? OR item.relative_path LIKE ?)")
            path_parameters.extend((family, f"{family}/%"))
    path_filter = " AND (" + " OR ".join(path_clauses) + ")" if path_clauses else ""
    rows = conn.execute(
        f"""
        SELECT item.segment_id, item.relative_path, item.source_key,
               SUBSTR(COALESCE(item.candidate_text, ''), 1, 500) AS candidate_text,
               item.model_safe_probability, item.final_action, item.token_status
        FROM ml_score_items item
        WHERE item.run_id = ?
          AND item.issue_count = 0
          AND item.token_status <> 'mismatch'
          AND item.final_action <> 'blocked_structure'
          AND COALESCE(item.candidate_text, '') <> ''
          {path_filter}
        ORDER BY item.model_safe_probability DESC, item.segment_id
        LIMIT ?
        """,
        (score_run_id, *path_parameters, max(limit * 6, limit)),
    ).fetchall()
    controls: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        segment_id = int(item.get("segment_id") or 0)
        if segment_id in excluded_segment_ids:
            continue
        controls.append(item)
        if len(controls) >= limit:
            break
    return controls


def _case_key(proposal_key: str, case_kind: str, segment_id: int | None, source_key: str) -> str:
    return _stable_key("qpc", proposal_key, case_kind, segment_id or 0, source_key)


def _proposal_cases(
    conn: sqlite3.Connection,
    *,
    proposal_key: str,
    issue_type: str,
    score_run_id: int,
    families: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    seen_segment_ids: set[int] = set()
    for family in families:
        for raw_sample in family.get("samples") or []:
            if not isinstance(raw_sample, dict):
                continue
            segment_id = int(raw_sample.get("segment_id") or 0)
            if not segment_id or segment_id in seen_segment_ids:
                continue
            seen_segment_ids.add(segment_id)
            samples.append({**raw_sample, "family_key": family["family_key"]})
            if len(samples) >= MAX_POSITIVE_CASES:
                break
        if len(samples) >= MAX_POSITIVE_CASES:
            break

    cases: list[dict[str, Any]] = []
    for sample in samples:
        segment_id = int(sample["segment_id"])
        source_key = str(sample.get("source_key") or "")
        common = {
            "segment_id": segment_id,
            "relative_path": sample.get("relative_path"),
            "source_key": source_key,
            "input_text": str(sample.get("candidate_text") or ""),
        }
        cases.append(
            {
                **common,
                "case_key": _case_key(proposal_key, "positive", segment_id, source_key),
                "case_kind": "positive",
                "expected_behavior": "resolve_issue_or_reject_with_explicit_reason",
                "assertions": [
                    "candidate_differs_when_accepted",
                    "target_issue_is_absent_after_transform",
                    "protected_token_multiset_is_unchanged",
                    "post_validation_is_clean",
                ],
                "metadata": {
                    "issue_type": issue_type,
                    "family_key": sample.get("family_key"),
                    "score": sample.get("score"),
                    "source": "pattern_discovery_sample",
                },
            }
        )
        cases.append(
            {
                **common,
                "case_key": _case_key(proposal_key, "boundary", segment_id, source_key),
                "case_kind": "boundary",
                "expected_behavior": "deterministic_and_token_safe_or_explicitly_rejected",
                "assertions": [
                    "repeat_application_is_idempotent",
                    "protected_token_multiset_is_unchanged",
                    "source_key_is_unchanged",
                    "shadow_does_not_write_output",
                ],
                "metadata": {
                    "issue_type": issue_type,
                    "family_key": sample.get("family_key"),
                    "source": "positive_sample_boundary_contract",
                },
            }
        )

    controls = _negative_controls(
        conn,
        score_run_id=score_run_id,
        file_families={str(item.get("file_family") or "") for item in families},
        excluded_segment_ids=seen_segment_ids,
    )
    for control in controls:
        segment_id = int(control.get("segment_id") or 0)
        source_key = str(control.get("source_key") or "")
        cases.append(
            {
                "case_key": _case_key(proposal_key, "negative", segment_id, source_key),
                "case_kind": "negative",
                "segment_id": segment_id,
                "relative_path": control.get("relative_path"),
                "source_key": source_key,
                "input_text": str(control.get("candidate_text") or ""),
                "expected_behavior": "leave_unchanged",
                "assertions": [
                    "candidate_equals_input",
                    "no_target_issue_is_introduced",
                    "protected_token_multiset_is_unchanged",
                ],
                "metadata": {
                    "issue_type": issue_type,
                    "score": control.get("model_safe_probability"),
                    "source": "clean_same_family_control",
                },
            }
        )
    return cases


def generate_provider_proposals(
    conn: sqlite3.Connection,
    *,
    discovery_run_id: int | None = None,
) -> dict[str, Any]:
    context = _discovery_context(conn, discovery_run_id)
    run_id = int(context["id"])
    score_run_id = int(context["score_run_id"])
    families = _actionable_families(conn, run_id)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for family in families:
        grouped[(str(family["issue_type"]), str(family["token_context"]))].append(family)

    proposals: list[dict[str, Any]] = []
    for (issue_type, token_context), proposal_families in grouped.items():
        slug = _slug(issue_type)
        proposal_key = _stable_key("qpp", issue_type, token_context)
        provider_id = slug
        evidence_type = f"deterministic_{slug}_repair"
        priority = max(float(item.get("priority") or 0) for item in proposal_families)
        confidence = max(float(item.get("confidence") or 0) for item in proposal_families)
        segment_count = sum(
            int((item.get("metrics") or {}).get("operational_segment_count") or item.get("segment_count") or 0)
            for item in proposal_families
        )
        file_families = sorted({str(item.get("file_family") or "") for item in proposal_families})
        text_relations = sorted({str(item.get("text_relation") or "") for item in proposal_families})
        correction_lane = _correction_lane(issue_type, token_context)
        human_review = {
            "reviewed_count": sum(
                int((item.get("human_review") or {}).get("reviewed_count") or 0)
                for item in proposal_families
            ),
            "supports_pattern": sum(
                int((item.get("human_review") or {}).get("supports_pattern") or 0)
                for item in proposal_families
            ),
            "contradicts_pattern": sum(
                int((item.get("human_review") or {}).get("contradicts_pattern") or 0)
                for item in proposal_families
            ),
            "boundary_case": sum(
                int((item.get("human_review") or {}).get("boundary_case") or 0)
                for item in proposal_families
            ),
        }
        manifest_draft = {
            "schema_version": 1,
            "provider_id": provider_id,
            "label": f"Proposta: {issue_type.replace('_', ' ')}",
            "enabled": False,
            "priority": max(10, 1000 - int(round(priority * 10))),
            "evidence_type": evidence_type,
            "discovery": {"issue_types": [issue_type]},
            "shadow_script": f"pipeline/quality_{slug}_shadow.py",
            "shadow_args": [],
            "evidence_script": f"pipeline/quality_{slug}_pairwise_evidence.py",
            "evidence_args": [],
            "proposal_status": "draft_review_required",
            "correction_lane": correction_lane,
        }
        contract = {
            "correction_lane": correction_lane,
            "human_evidence": human_review,
            "selector": {
                "issue_types": [issue_type],
                "token_context": token_context,
                "file_families": file_families,
                "text_relations": text_relations,
            },
            "transformation": {
                "kind": "deterministic_candidate_required",
                "implementation_status": "not_implemented",
                "must_be_idempotent": True,
                "ambiguity_policy": "reject_with_reason",
            },
            "invariants": [
                "protected_tokens_unchanged",
                "source_key_unchanged",
                "yaml_structure_valid",
                "no_output_write_in_shadow",
            ],
            "validators": [
                "target_issue_removed",
                "post_validation_clean",
                "pairwise_evidence_required",
                "monotonic_gate_required",
            ],
            "activation": {
                "enabled": False,
                "requires_human_review": True,
                "requires_boundary_tests": True,
                "requires_full_package_shadow": True,
            },
        }
        cases = _proposal_cases(
            conn,
            proposal_key=proposal_key,
            issue_type=issue_type,
            score_run_id=score_run_id,
            families=proposal_families,
        )
        proposals.append(
            {
                "proposal_key": proposal_key,
                "provider_id": provider_id,
                "evidence_type": evidence_type,
                "label": manifest_draft["label"],
                "issue_type": issue_type,
                "token_context": token_context,
                "status": "draft_review_required",
                "correction_lane": correction_lane,
                "human_review": human_review,
                "priority": round(priority, 3),
                "confidence": round(confidence, 6),
                "family_count": len(proposal_families),
                "segment_count": segment_count,
                "families": [
                    {
                        "family_id": int(item["family_id"]),
                        "family_key": item["family_key"],
                        "priority": float(item.get("priority") or 0),
                        "segment_count": int(item.get("segment_count") or 0),
                    }
                    for item in proposal_families
                ],
                "manifest_draft": manifest_draft,
                "contract": contract,
                "cases": cases,
            }
        )
    proposals.sort(key=lambda item: (-float(item["priority"]), item["proposal_key"]))
    case_counts = defaultdict(int)
    for proposal in proposals:
        for case in proposal["cases"]:
            case_counts[str(case["case_kind"])] += 1
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "run_key": _stable_key("qppr", RULE_VERSION, run_id),
        "quality_epoch_id": context.get("quality_epoch_id"),
        "discovery_run_id": run_id,
        "score_run_id": score_run_id,
        "actionable_family_count": len(families),
        "supervised_routing": _table_exists(
            conn, "ml_regenerative_review_decisions"
        ),
        "correction_lane_count": len(
            {
                str(item.get("correction_lane", {}).get("id") or "other_patterns")
                for item in proposals
            }
        ),
        "proposal_count": len(proposals),
        "positive_case_count": case_counts["positive"],
        "negative_case_count": case_counts["negative"],
        "boundary_case_count": case_counts["boundary"],
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "score_write_count": 0,
        "proposals": proposals,
    }


def persist_provider_proposals(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    db.ensure_database(conn)
    now = db.utc_now()
    summary = {
        key: result.get(key)
        for key in (
            "actionable_family_count",
            "proposal_count",
            "positive_case_count",
            "negative_case_count",
            "boundary_case_count",
        )
    }
    summary["top_proposals"] = [
        {
            "proposal_key": item["proposal_key"],
            "provider_id": item["provider_id"],
            "issue_type": item["issue_type"],
            "priority": item["priority"],
            "segment_count": item["segment_count"],
        }
        for item in result["proposals"][:20]
    ]
    conn.execute(
        """
        INSERT INTO ml_quality_provider_proposal_runs (
            run_key, rule_version, quality_epoch_id, discovery_run_id, score_run_id,
            actionable_family_count, proposal_count, positive_case_count,
            negative_case_count, boundary_case_count, status, summary_json,
            started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        ON CONFLICT(run_key) DO UPDATE SET
            rule_version = excluded.rule_version,
            quality_epoch_id = excluded.quality_epoch_id,
            discovery_run_id = excluded.discovery_run_id,
            score_run_id = excluded.score_run_id,
            actionable_family_count = excluded.actionable_family_count,
            proposal_count = excluded.proposal_count,
            positive_case_count = excluded.positive_case_count,
            negative_case_count = excluded.negative_case_count,
            boundary_case_count = excluded.boundary_case_count,
            status = excluded.status,
            summary_json = excluded.summary_json,
            finished_at = excluded.finished_at,
            updated_at = excluded.updated_at
        """,
        (
            result["run_key"], RULE_VERSION, result.get("quality_epoch_id"),
            result["discovery_run_id"], result["score_run_id"],
            result["actionable_family_count"], result["proposal_count"],
            result["positive_case_count"], result["negative_case_count"],
            result["boundary_case_count"],
            json.dumps(summary, ensure_ascii=False, sort_keys=True), now, now, now,
        ),
    )
    run_id = int(
        conn.execute(
            "SELECT id FROM ml_quality_provider_proposal_runs WHERE run_key = ?",
            (result["run_key"],),
        ).fetchone()["id"]
    )
    conn.execute("DELETE FROM ml_quality_provider_proposals WHERE run_id = ?", (run_id,))
    for proposal in result["proposals"]:
        cursor = conn.execute(
            """
            INSERT INTO ml_quality_provider_proposals (
                run_id, proposal_key, provider_id, evidence_type, label,
                issue_type, token_context, status, priority, confidence,
                family_count, segment_count, manifest_draft_json, contract_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, proposal["proposal_key"], proposal["provider_id"],
                proposal["evidence_type"], proposal["label"], proposal["issue_type"],
                proposal["token_context"], proposal["status"], proposal["priority"],
                proposal["confidence"], proposal["family_count"], proposal["segment_count"],
                json.dumps(proposal["manifest_draft"], ensure_ascii=False, sort_keys=True),
                json.dumps(proposal["contract"], ensure_ascii=False, sort_keys=True),
                now, now,
            ),
        )
        proposal_id = int(cursor.lastrowid)
        for family in proposal["families"]:
            conn.execute(
                """
                INSERT INTO ml_quality_provider_proposal_families (
                    proposal_id, family_id, priority, segment_count
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    proposal_id, family["family_id"], family["priority"],
                    family["segment_count"],
                ),
            )
        for case in proposal["cases"]:
            conn.execute(
                """
                INSERT INTO ml_quality_provider_proposal_cases (
                    proposal_id, case_key, case_kind, segment_id, relative_path,
                    source_key, input_text, expected_behavior, assertions_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id, case["case_key"], case["case_kind"],
                    case.get("segment_id"), case.get("relative_path"),
                    case.get("source_key"), case["input_text"],
                    case["expected_behavior"],
                    json.dumps(case["assertions"], ensure_ascii=False, sort_keys=True),
                    json.dumps(case["metadata"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
    conn.commit()
    return run_id


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=(
            "Draft disabled quality providers and boundary-test contracts from "
            "uncovered actionable pattern families."
        )
    )
    parser.add_argument("--discovery-run-id", type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist proposal metadata only; never writes confirmations, scores, or output.",
    )
    args = parser.parse_args()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        result = generate_provider_proposals(conn, discovery_run_id=args.discovery_run_id)
        run_id = persist_provider_proposals(conn, result) if args.apply else None
    payload = {key: value for key, value in result.items() if key != "proposals"}
    payload.update(
        {
            "apply": args.apply,
            "run_id": run_id,
            "proposals": [
                {
                    key: value
                    for key, value in proposal.items()
                    if key not in {"cases", "contract", "manifest_draft", "families"}
                }
                | {
                    "case_counts": {
                        kind: sum(1 for case in proposal["cases"] if case["case_kind"] == kind)
                        for kind in ("positive", "negative", "boundary")
                    },
                    "manifest_draft": proposal["manifest_draft"],
                }
                for proposal in result["proposals"][:20]
            ],
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
