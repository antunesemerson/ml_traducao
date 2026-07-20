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


RULE_VERSION = "quality_provider_proposal_generator_v1"
ACTIONABLE_STATUSES = ("new_candidate", "recurring_candidate")
MAX_POSITIVE_CASES = 3
MAX_NEGATIVE_CASES = 3


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
    families: list[dict[str, Any]] = []
    for row in rows:
        family = dict(row)
        family["samples"] = _json_list(family.pop("samples_json", "[]"))
        family["metrics"] = _json_object(family.pop("metrics_json", "{}"))
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
        }
        contract = {
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
