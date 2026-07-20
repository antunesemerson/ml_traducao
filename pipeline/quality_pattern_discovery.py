from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "quality_pattern_discovery_v2"
DEFAULT_LOW_SCORE_THRESHOLD = 0.5
MIN_ACTIONABLE_SEGMENTS = 3
MAX_SAMPLES = 3
PROVIDERS_DIR = Path(__file__).resolve().with_name("quality_promotion_providers")

SEVERITY_WEIGHT = {
    "critical": 1.0,
    "high": 1.0,
    "medium": 0.65,
    "low": 0.35,
    "info": 0.2,
    "unknown": 0.5,
}


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


def _path_family(relative_path: str) -> str:
    parts = [part for part in str(relative_path or "").replace("\\", "/").split("/") if part]
    if len(parts) <= 1:
        return "_root"
    if parts[0] in {"dlc", "event_localization", "activities", "culture"} and len(parts) >= 3:
        return "/".join(parts[:2])
    return parts[0]


def _token_context(issue_type: str, candidate_text: str) -> str:
    issue = str(issue_type or "").casefold()
    text = str(candidate_text or "")
    folded = text.casefold()
    if "gender_token" in issue or "custom('es_" in folded or 'custom("es_' in folded:
        return "gender_helper"
    if "literal" in issue or "select_cstring(" in folded:
        return "select_cstring"
    if "space_" in issue and "[" in text:
        return "token_boundary"
    if "concept(" in folded:
        return "concept"
    if "[" in text and "]" in text:
        return "protected_token"
    return "plain_text"


def _family_key(
    evidence_kind: str,
    issue_type: str,
    token_context: str,
    file_family: str,
    text_relation: str,
) -> str:
    canonical = json.dumps(
        [evidence_kind, issue_type, token_context, file_family, text_relation],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return "qpf_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _run_key(epoch_id: int | None, score_run_id: int, threshold: float) -> str:
    canonical = f"{RULE_VERSION}|{epoch_id or 0}|{score_run_id}|{threshold:.6f}"
    return "qpd_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def load_provider_coverage(directory: Path = PROVIDERS_DIR) -> dict[str, dict[str, str]]:
    coverage: dict[str, dict[str, str]] = {}
    for manifest_path in sorted(directory.glob("*.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not payload.get("enabled", True):
            continue
        discovery = payload.get("discovery")
        if not isinstance(discovery, dict):
            continue
        provider_id = str(payload.get("provider_id") or "").strip()
        evidence_type = str(payload.get("evidence_type") or "").strip()
        issue_types = discovery.get("issue_types") or []
        if not isinstance(issue_types, list) or not all(isinstance(item, str) for item in issue_types):
            raise RuntimeError(f"Invalid discovery issue types: {manifest_path}")
        for raw_issue_type in issue_types:
            issue_type = raw_issue_type.strip()
            if not issue_type:
                continue
            previous = coverage.get(issue_type)
            if previous and previous["provider_id"] != provider_id:
                raise RuntimeError(f"Issue type mapped by multiple providers: {issue_type}")
            coverage[issue_type] = {
                "provider_id": provider_id,
                "evidence_type": evidence_type,
            }
    return coverage


def _score_context(
    conn: sqlite3.Connection,
    score_run_id: int | None,
) -> dict[str, Any]:
    epoch: dict[str, Any] = {}
    if _table_exists(conn, "quality_epochs"):
        row = conn.execute(
            """
            SELECT id, epoch_key, scoring_contract_hash, output_score_run_id, status
            FROM quality_epochs
            WHERE (? IS NULL OR output_score_run_id = ?)
            ORDER BY CASE WHEN output_score_run_id = ? THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (score_run_id, score_run_id, score_run_id),
        ).fetchone()
        epoch = dict(row) if row else {}
    if score_run_id is None:
        score_run_id = int(epoch.get("output_score_run_id") or 0)
    if not score_run_id:
        row = conn.execute(
            """
            SELECT id
            FROM ml_score_runs
            WHERE candidate_text_source = 'output'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        score_run_id = int(row["id"]) if row else 0
    if not score_run_id:
        raise RuntimeError("No output score run is available for pattern discovery.")
    if not epoch or int(epoch.get("output_score_run_id") or 0) != score_run_id:
        row = conn.execute(
            """
            SELECT id, epoch_key, scoring_contract_hash, output_score_run_id, status
            FROM quality_epochs
            WHERE output_score_run_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (score_run_id,),
        ).fetchone() if _table_exists(conn, "quality_epochs") else None
        epoch = dict(row) if row else {}
    active_segment_count = int(
        conn.execute("SELECT COUNT(*) FROM source_segments WHERE is_active = 1").fetchone()[0]
    )
    return {
        "quality_epoch_id": int(epoch.get("id") or 0) or None,
        "quality_epoch_key": epoch.get("epoch_key"),
        "scoring_contract_hash": epoch.get("scoring_contract_hash"),
        "score_run_id": score_run_id,
        "active_segment_count": active_segment_count,
    }


def _existing_run_id(conn: sqlite3.Connection, run_key: str) -> int | None:
    if not _table_exists(conn, "ml_quality_pattern_discovery_runs"):
        return None
    row = conn.execute(
        "SELECT id FROM ml_quality_pattern_discovery_runs WHERE run_key = ?",
        (run_key,),
    ).fetchone()
    return int(row["id"]) if row else None


def _historical_families(
    conn: sqlite3.Connection,
    current_run_id: int | None,
) -> dict[str, dict[str, Any]]:
    if not (
        _table_exists(conn, "ml_quality_pattern_families")
        and _table_exists(conn, "ml_quality_pattern_observations")
    ):
        return {}
    rows = conn.execute(
        """
        SELECT family.family_key, family.status, family.provider_id,
               family.evidence_type, family.observation_count,
               EXISTS (
                   SELECT 1
                   FROM ml_quality_pattern_observations observation
                   WHERE observation.family_id = family.id
                     AND (? IS NULL OR observation.run_id <> ?)
               ) AS observed_before
        FROM ml_quality_pattern_families family
        """,
        (current_run_id, current_run_id),
    )
    return {
        str(row["family_key"]): dict(row)
        for row in rows
        if int(row["observed_before"] or 0)
    }


def _evidence_rows(conn: sqlite3.Connection, score_run_id: int) -> list[dict[str, Any]]:
    state_run_id = 0
    if _table_exists(conn, "segment_state_runs") and _table_exists(conn, "segment_state_items"):
        state_run = conn.execute(
            """
            SELECT id
            FROM segment_state_runs
            WHERE finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        state_run_id = int(state_run["id"] or 0) if state_run else 0
    lifecycle_select = (
        "lifecycle.state_group, lifecycle.locked AS lifecycle_locked, "
        "lifecycle.confirmed_matches_output, lifecycle.needs_output_apply, lifecycle.is_closed"
        if state_run_id
        else (
            "NULL AS state_group, NULL AS lifecycle_locked, "
            "NULL AS confirmed_matches_output, NULL AS needs_output_apply, NULL AS is_closed"
        )
    )
    lifecycle_join = (
        "LEFT JOIN segment_state_items lifecycle "
        "ON lifecycle.segment_id = item.segment_id AND lifecycle.run_id = ?"
        if state_run_id
        else ""
    )
    query_parameters = (state_run_id, score_run_id) if state_run_id else (score_run_id,)
    relation_sql = """
        CASE
          WHEN item.candidate_text = source.old_text
           AND item.candidate_text = source.spanish_text THEN 'equals_old_and_spanish'
          WHEN item.candidate_text = source.old_text THEN 'equals_old'
          WHEN item.candidate_text = source.spanish_text THEN 'equals_spanish'
          WHEN item.candidate_text = source.english_text THEN 'equals_english'
          ELSE 'distinct_candidate'
        END
    """
    explicit = conn.execute(
        f"""
        SELECT item.segment_id, item.relative_path, item.source_key,
               SUBSTR(COALESCE(item.candidate_text, ''), 1, 2000) AS candidate_text,
               item.model_safe_probability, item.final_action, item.risk_class,
               item.token_status, {relation_sql} AS text_relation,
               {lifecycle_select},
               issue.value AS issue_json, 'explicit_issue' AS evidence_kind
        FROM ml_score_items item
        JOIN source_segments source ON source.id = item.segment_id AND source.is_active = 1
        {lifecycle_join}
        JOIN json_each(
            CASE WHEN json_valid(COALESCE(item.issues_json, '[]'))
                 THEN COALESCE(item.issues_json, '[]') ELSE '[]' END
        ) issue
        WHERE item.run_id = ?
        ORDER BY item.model_safe_probability IS NULL,
                 item.model_safe_probability, item.segment_id
        """,
        query_parameters,
    ).fetchall()
    structural = conn.execute(
        f"""
        SELECT item.segment_id, item.relative_path, item.source_key,
               SUBSTR(COALESCE(item.candidate_text, ''), 1, 2000) AS candidate_text,
               item.model_safe_probability, item.final_action, item.risk_class,
               item.token_status, {relation_sql} AS text_relation,
               {lifecycle_select},
               NULL AS issue_json, 'structural_block' AS evidence_kind
        FROM ml_score_items item
        JOIN source_segments source ON source.id = item.segment_id AND source.is_active = 1
        {lifecycle_join}
        WHERE item.run_id = ?
          AND item.issue_count = 0
          AND (item.token_status = 'mismatch' OR item.final_action = 'blocked_structure')
        ORDER BY item.model_safe_probability IS NULL,
                 item.model_safe_probability, item.segment_id
        """,
        query_parameters,
    ).fetchall()
    return [dict(row) for row in [*explicit, *structural]]


def _score_only_count(
    conn: sqlite3.Connection,
    score_run_id: int,
    threshold: float,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM ml_score_items item
        JOIN source_segments source ON source.id = item.segment_id AND source.is_active = 1
        WHERE item.run_id = ?
          AND item.model_safe_probability < ?
          AND item.issue_count = 0
          AND item.token_status <> 'mismatch'
          AND item.final_action <> 'blocked_structure'
        """,
        (score_run_id, threshold),
    ).fetchone()
    return int(row[0] or 0)


def _severity_value(raw: Any, evidence_kind: str) -> float:
    if evidence_kind == "structural_block":
        return 0.8
    return SEVERITY_WEIGHT.get(str(raw or "unknown").casefold(), 0.5)


def _score_family(
    family: dict[str, Any],
    active_segment_count: int,
    historical: dict[str, Any] | None,
    provider: dict[str, str] | None,
) -> None:
    segment_count = len(family["segment_ids"])
    operational_segment_count = len(family["operational_segment_ids"])
    closed_segment_count = len(family["closed_segment_ids"])
    lifecycle_observed_count = len(family["lifecycle_observed_segment_ids"])
    low_score_count = len(family["low_score_segment_ids"])
    operational_low_score_count = len(family["operational_low_score_segment_ids"])
    severity = float(family["severity"])
    scored_reach_count = operational_segment_count if lifecycle_observed_count else segment_count
    support = min(0.18, math.log10(1 + scored_reach_count) * 0.08)
    confidence = (0.72 if family["evidence_kind"] == "explicit_issue" else 0.58) + support
    if severity >= 1.0:
        confidence += 0.04
    if historical:
        confidence += 0.05
    confidence = min(0.98, confidence)
    reach_denominator = max(25, round(max(1, active_segment_count) * 0.01))
    reach = min(1.0, math.log1p(scored_reach_count) / math.log1p(reach_denominator))
    low_score_share = low_score_count / segment_count if segment_count else 0.0
    operational_low_score_share = (
        operational_low_score_count / operational_segment_count
        if operational_segment_count
        else 0.0
    )
    novelty = 0.0 if provider else (0.4 if historical else 1.0)
    priority = 100.0 * (
        0.35 * severity
        + 0.25 * reach
        + 0.20 * confidence
        + 0.10 * novelty
        + 0.10 * low_score_share
    )
    fully_closed = bool(
        segment_count
        and lifecycle_observed_count == segment_count
        and operational_segment_count == 0
    )
    if fully_closed:
        status = "closed_observation"
        priority *= 0.20
    elif provider:
        status = "covered_by_provider"
        priority *= 0.55
    elif scored_reach_count >= MIN_ACTIONABLE_SEGMENTS and confidence >= 0.55:
        status = "recurring_candidate" if historical else "new_candidate"
    else:
        status = "monitoring"
        priority *= 0.70
    family.update(
        {
            "segment_count": segment_count,
            "operational_segment_count": operational_segment_count,
            "closed_segment_count": closed_segment_count,
            "lifecycle_observed_count": lifecycle_observed_count,
            "low_score_count": low_score_count,
            "low_score_share": round(low_score_share, 6),
            "operational_low_score_count": operational_low_score_count,
            "operational_low_score_share": round(operational_low_score_share, 6),
            "active_package_share": round(segment_count / max(1, active_segment_count), 8),
            "severity": round(severity, 6),
            "confidence": round(confidence, 6),
            "novelty": round(novelty, 6),
            "reach": round(reach, 6),
            "priority": round(priority, 3),
            "status": status,
            "provider_id": provider.get("provider_id") if provider else None,
            "evidence_type": provider.get("evidence_type") if provider else None,
            "historical_observation_count": int((historical or {}).get("observation_count") or 0),
        }
    )


def discover_patterns(
    conn: sqlite3.Connection,
    *,
    score_run_id: int | None = None,
    low_score_threshold: float = DEFAULT_LOW_SCORE_THRESHOLD,
    provider_coverage: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    context = _score_context(conn, score_run_id)
    run_key = _run_key(
        context["quality_epoch_id"],
        context["score_run_id"],
        low_score_threshold,
    )
    current_run_id = _existing_run_id(conn, run_key)
    history = _historical_families(conn, current_run_id)
    provider_coverage = provider_coverage if provider_coverage is not None else load_provider_coverage()
    groups: dict[str, dict[str, Any]] = {}
    evidence_segment_ids: set[int] = set()
    for row in _evidence_rows(conn, context["score_run_id"]):
        evidence_kind = str(row["evidence_kind"])
        issue = _json_object(row.get("issue_json"))
        if evidence_kind == "explicit_issue":
            issue_type = str(issue.get("code") or issue.get("type") or "unknown_explicit_issue")
            severity = _severity_value(issue.get("severity"), evidence_kind)
        else:
            risk = str(row.get("risk_class") or "unknown").casefold().replace(" ", "_")
            issue_type = f"structural_{row.get('token_status')}_{row.get('final_action')}_{risk}"
            severity = _severity_value(None, evidence_kind)
        token_context = _token_context(issue_type, str(row.get("candidate_text") or ""))
        file_family = _path_family(str(row.get("relative_path") or ""))
        text_relation = str(row.get("text_relation") or "unknown")
        family_key = _family_key(
            evidence_kind,
            issue_type,
            token_context,
            file_family,
            text_relation,
        )
        family = groups.setdefault(
            family_key,
            {
                "family_key": family_key,
                "evidence_kind": evidence_kind,
                "issue_type": issue_type,
                "token_context": token_context,
                "file_family": file_family,
                "text_relation": text_relation,
                "segment_ids": set(),
                "operational_segment_ids": set(),
                "closed_segment_ids": set(),
                "lifecycle_observed_segment_ids": set(),
                "low_score_segment_ids": set(),
                "operational_low_score_segment_ids": set(),
                "occurrence_count": 0,
                "scores": [],
                "severity": 0.0,
                "samples": [],
            },
        )
        segment_id = int(row["segment_id"])
        score = row.get("model_safe_probability")
        family["segment_ids"].add(segment_id)
        lifecycle_observed = row.get("state_group") is not None
        lifecycle_closed = bool(
            lifecycle_observed
            and (
                str(row.get("state_group") or "").casefold() == "closed"
                or int(row.get("is_closed") or 0) == 1
            )
        )
        needs_output_apply = bool(int(row.get("needs_output_apply") or 0))
        operational = not lifecycle_observed or not lifecycle_closed or needs_output_apply
        if lifecycle_observed:
            family["lifecycle_observed_segment_ids"].add(segment_id)
        if lifecycle_closed and not needs_output_apply:
            family["closed_segment_ids"].add(segment_id)
        if operational:
            family["operational_segment_ids"].add(segment_id)
        family["occurrence_count"] += 1
        family["severity"] = max(float(family["severity"]), severity)
        evidence_segment_ids.add(segment_id)
        if score is not None:
            numeric_score = float(score)
            family["scores"].append(numeric_score)
            if numeric_score < low_score_threshold:
                family["low_score_segment_ids"].add(segment_id)
                if operational:
                    family["operational_low_score_segment_ids"].add(segment_id)
        if len(family["samples"]) < MAX_SAMPLES:
            family["samples"].append(
                {
                    "segment_id": segment_id,
                    "relative_path": row.get("relative_path"),
                    "source_key": row.get("source_key"),
                    "candidate_text": str(row.get("candidate_text") or "")[:500],
                    "score": round(float(score), 6) if score is not None else None,
                    "final_action": row.get("final_action"),
                    "token_status": row.get("token_status"),
                    "state_group": row.get("state_group"),
                    "lifecycle_locked": bool(row.get("lifecycle_locked")) if lifecycle_observed else None,
                    "confirmed_matches_output": bool(row.get("confirmed_matches_output")) if lifecycle_observed else None,
                    "needs_output_apply": needs_output_apply if lifecycle_observed else None,
                }
            )
    families: list[dict[str, Any]] = []
    for family_key, family in groups.items():
        scores = family.pop("scores")
        family["average_score"] = round(sum(scores) / len(scores), 6) if scores else None
        family["minimum_score"] = round(min(scores), 6) if scores else None
        family["maximum_score"] = round(max(scores), 6) if scores else None
        historical = history.get(family_key)
        provider = provider_coverage.get(str(family["issue_type"]))
        _score_family(
            family,
            context["active_segment_count"],
            historical,
            provider,
        )
        family.pop("segment_ids")
        family.pop("operational_segment_ids")
        family.pop("closed_segment_ids")
        family.pop("lifecycle_observed_segment_ids")
        family.pop("low_score_segment_ids")
        family.pop("operational_low_score_segment_ids")
        families.append(family)
    families.sort(key=lambda item: (-float(item["priority"]), -int(item["segment_count"]), item["family_key"]))
    counts = defaultdict(int)
    for family in families:
        counts[str(family["status"])] += 1
    ignored_score_only_count = _score_only_count(
        conn,
        context["score_run_id"],
        low_score_threshold,
    )
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "run_key": run_key,
        **context,
        "low_score_threshold": low_score_threshold,
        "evidence_segment_count": len(evidence_segment_ids),
        "family_count": len(families),
        "new_family_count": counts["new_candidate"],
        "recurring_family_count": counts["recurring_candidate"],
        "covered_family_count": counts["covered_by_provider"],
        "monitoring_family_count": counts["monitoring"],
        "closed_family_count": counts["closed_observation"],
        "actionable_family_count": counts["new_candidate"] + counts["recurring_candidate"],
        "ignored_score_only_count": ignored_score_only_count,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "score_write_count": 0,
        "families": families,
    }


def persist_discovery(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    db.ensure_database(conn)
    now = db.utc_now()
    summary = {
        key: result.get(key)
        for key in (
            "evidence_segment_count",
            "family_count",
            "new_family_count",
            "recurring_family_count",
            "covered_family_count",
            "monitoring_family_count",
            "closed_family_count",
            "actionable_family_count",
            "ignored_score_only_count",
        )
    }
    summary["top_families"] = [
        {
            "family_key": item["family_key"],
            "issue_type": item["issue_type"],
            "status": item["status"],
            "segment_count": item["segment_count"],
            "priority": item["priority"],
        }
        for item in result["families"][:20]
    ]
    conn.execute(
        """
        INSERT INTO ml_quality_pattern_discovery_runs (
            run_key, rule_version, quality_epoch_id, score_run_id,
            scoring_contract_hash, low_score_threshold, active_segment_count,
            evidence_segment_count, family_count, new_family_count,
            recurring_family_count, covered_family_count, actionable_family_count,
            ignored_score_only_count, status, summary_json, started_at,
            finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        ON CONFLICT(run_key) DO UPDATE SET
            rule_version = excluded.rule_version,
            quality_epoch_id = excluded.quality_epoch_id,
            score_run_id = excluded.score_run_id,
            scoring_contract_hash = excluded.scoring_contract_hash,
            low_score_threshold = excluded.low_score_threshold,
            active_segment_count = excluded.active_segment_count,
            evidence_segment_count = excluded.evidence_segment_count,
            family_count = excluded.family_count,
            new_family_count = excluded.new_family_count,
            recurring_family_count = excluded.recurring_family_count,
            covered_family_count = excluded.covered_family_count,
            actionable_family_count = excluded.actionable_family_count,
            ignored_score_only_count = excluded.ignored_score_only_count,
            status = excluded.status,
            summary_json = excluded.summary_json,
            finished_at = excluded.finished_at,
            updated_at = excluded.updated_at
        """,
        (
            result["run_key"],
            RULE_VERSION,
            result.get("quality_epoch_id"),
            result["score_run_id"],
            result.get("scoring_contract_hash"),
            result["low_score_threshold"],
            result["active_segment_count"],
            result["evidence_segment_count"],
            result["family_count"],
            result["new_family_count"],
            result["recurring_family_count"],
            result["covered_family_count"],
            result["actionable_family_count"],
            result["ignored_score_only_count"],
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    run_id = int(
        conn.execute(
            "SELECT id FROM ml_quality_pattern_discovery_runs WHERE run_key = ?",
            (result["run_key"],),
        ).fetchone()["id"]
    )
    previous_family_ids = {
        int(row["family_id"])
        for row in conn.execute(
            "SELECT family_id FROM ml_quality_pattern_observations WHERE run_id = ?",
            (run_id,),
        )
    }
    conn.execute("DELETE FROM ml_quality_pattern_observations WHERE run_id = ?", (run_id,))
    for family in result["families"]:
        existing = conn.execute(
            "SELECT id FROM ml_quality_pattern_families WHERE family_key = ?",
            (family["family_key"],),
        ).fetchone()
        samples_json = json.dumps(family["samples"], ensure_ascii=False, sort_keys=True)
        metadata_json = json.dumps(
            {
                "low_score_share": family["low_score_share"],
                "active_package_share": family["active_package_share"],
                "historical_observation_count": family["historical_observation_count"],
                "operational_segment_count": family["operational_segment_count"],
                "closed_segment_count": family["closed_segment_count"],
                "lifecycle_observed_count": family["lifecycle_observed_count"],
                "operational_low_score_count": family["operational_low_score_count"],
                "operational_low_score_share": family["operational_low_score_share"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if existing:
            family_id = int(existing["id"])
            increment = 0 if family_id in previous_family_ids else 1
            conn.execute(
                """
                UPDATE ml_quality_pattern_families
                SET evidence_kind = ?, issue_type = ?, token_context = ?,
                    file_family = ?, text_relation = ?, provider_id = ?,
                    evidence_type = ?, status = ?, last_run_id = ?,
                    last_score_run_id = ?, observation_count = observation_count + ?,
                    latest_priority = ?, latest_confidence = ?, latest_reach = ?,
                    latest_severity = ?, latest_segment_count = ?, samples_json = ?,
                    metadata_json = ?, last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    family["evidence_kind"], family["issue_type"], family["token_context"],
                    family["file_family"], family["text_relation"], family.get("provider_id"),
                    family.get("evidence_type"), family["status"], run_id,
                    result["score_run_id"], increment, family["priority"], family["confidence"],
                    family["reach"], family["severity"], family["segment_count"], samples_json,
                    metadata_json, now, now, family_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO ml_quality_pattern_families (
                    family_key, evidence_kind, issue_type, token_context, file_family,
                    text_relation, provider_id, evidence_type, status, first_run_id,
                    last_run_id, first_score_run_id, last_score_run_id,
                    observation_count, latest_priority, latest_confidence, latest_reach,
                    latest_severity, latest_segment_count, samples_json, metadata_json,
                    first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    family["family_key"], family["evidence_kind"], family["issue_type"],
                    family["token_context"], family["file_family"], family["text_relation"],
                    family.get("provider_id"), family.get("evidence_type"), family["status"],
                    run_id, run_id, result["score_run_id"], result["score_run_id"],
                    family["priority"], family["confidence"], family["reach"], family["severity"],
                    family["segment_count"], samples_json, metadata_json, now, now, now,
                ),
            )
            family_id = int(cursor.lastrowid)
        metrics_json = json.dumps(
            {
                "issue_type": family["issue_type"],
                "token_context": family["token_context"],
                "file_family": family["file_family"],
                "text_relation": family["text_relation"],
                "provider_id": family.get("provider_id"),
                "evidence_type": family.get("evidence_type"),
                "operational_segment_count": family["operational_segment_count"],
                "closed_segment_count": family["closed_segment_count"],
                "lifecycle_observed_count": family["lifecycle_observed_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO ml_quality_pattern_observations (
                run_id, family_id, segment_count, occurrence_count, low_score_count,
                low_score_share, active_package_share, average_score, minimum_score,
                maximum_score, severity, confidence, novelty, reach, priority, status,
                samples_json, metrics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, family_id, family["segment_count"], family["occurrence_count"],
                family["low_score_count"], family["low_score_share"],
                family["active_package_share"], family["average_score"],
                family["minimum_score"], family["maximum_score"], family["severity"],
                family["confidence"], family["novelty"], family["reach"], family["priority"],
                family["status"], samples_json, metrics_json, now,
            ),
        )
    conn.commit()
    return run_id


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Discover package-wide quality issue families without changing output or scores."
    )
    parser.add_argument("--score-run-id", type=int)
    parser.add_argument("--low-score-threshold", type=float, default=DEFAULT_LOW_SCORE_THRESHOLD)
    parser.add_argument("--apply", action="store_true", help="Persist metadata-only discovery results.")
    args = parser.parse_args()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        if args.apply:
            db.ensure_database(conn)
        result = discover_patterns(
            conn,
            score_run_id=args.score_run_id,
            low_score_threshold=args.low_score_threshold,
        )
        run_id = persist_discovery(conn, result) if args.apply else None
    payload = {
        key: value
        for key, value in result.items()
        if key != "families"
    }
    payload.update(
        {
            "apply": args.apply,
            "run_id": run_id,
            "top_families": result["families"][:20],
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
