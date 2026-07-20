from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from typing import Any

import db


RULE_VERSION = "quality_closed_observation_audit_v1"
MAX_SAMPLES_PER_FAMILY = 4


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        payload = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _run_key(discovery_run_id: int, score_run_id: int, state_run_id: int | None) -> str:
    raw = f"{RULE_VERSION}:{discovery_run_id}:{score_run_id}:{state_run_id or 0}"
    return "qcoa_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _context(conn: sqlite3.Connection, discovery_run_id: int | None) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT discovery.id, discovery.quality_epoch_id, discovery.score_run_id,
               discovery.status, discovery.finished_at,
               epoch.segment_state_run_id AS epoch_segment_state_run_id
        FROM ml_quality_pattern_discovery_runs discovery
        LEFT JOIN quality_epochs epoch ON epoch.id = discovery.quality_epoch_id
        WHERE discovery.status = 'completed'
          AND (? IS NULL OR discovery.id = ?)
        ORDER BY discovery.id DESC
        LIMIT 1
        """,
        (discovery_run_id, discovery_run_id),
    ).fetchone()
    if not row:
        raise RuntimeError("No completed pattern discovery run is available for audit.")
    context = dict(row)
    state_run_id = int(context.get("epoch_segment_state_run_id") or 0)
    if not state_run_id:
        state = conn.execute(
            """
            SELECT id
            FROM segment_state_runs
            WHERE finished_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        state_run_id = int(state["id"] or 0) if state else 0
    context["segment_state_run_id"] = state_run_id or None
    return context


def _observation_quality(conn: sqlite3.Connection, discovery_run_id: int) -> dict[str, int]:
    duplicate_count = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(item_count - 1), 0)
            FROM (
                SELECT COUNT(*) AS item_count
                FROM ml_quality_pattern_observations
                WHERE run_id = ?
                GROUP BY family_id
                HAVING COUNT(*) > 1
            )
            """,
            (discovery_run_id,),
        ).fetchone()[0]
        or 0
    )
    orphan_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ml_quality_pattern_observations observation
            LEFT JOIN ml_quality_pattern_families family ON family.id = observation.family_id
            WHERE observation.run_id = ? AND family.id IS NULL
            """,
            (discovery_run_id,),
        ).fetchone()[0]
        or 0
    )
    invalid_status_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ml_quality_pattern_observations
            WHERE run_id = ?
              AND status NOT IN (
                  'new_candidate', 'recurring_candidate', 'covered_by_provider',
                  'monitoring', 'closed_observation'
              )
            """,
            (discovery_run_id,),
        ).fetchone()[0]
        or 0
    )
    return {
        "duplicate_count": duplicate_count,
        "orphan_count": orphan_count,
        "invalid_status_count": invalid_status_count,
    }


def _closed_observations(conn: sqlite3.Connection, discovery_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT observation.id AS observation_id, observation.family_id,
                   observation.segment_count, observation.low_score_count,
                   observation.priority, observation.samples_json,
                   family.family_key, family.evidence_kind, family.issue_type,
                   family.token_context, family.file_family, family.text_relation
            FROM ml_quality_pattern_observations observation
            JOIN ml_quality_pattern_families family ON family.id = observation.family_id
            WHERE observation.run_id = ?
              AND observation.status = 'closed_observation'
            ORDER BY observation.priority DESC, observation.segment_count DESC,
                     observation.family_id
            """,
            (discovery_run_id,),
        )
    ]


def _segment_evidence(
    conn: sqlite3.Connection,
    *,
    segment_ids: set[int],
    score_run_id: int,
    state_run_id: int | None,
) -> dict[int, dict[str, Any]]:
    evidence: dict[int, dict[str, Any]] = {}
    ordered_ids = sorted(segment_ids)
    for start in range(0, len(ordered_ids), 400):
        chunk = ordered_ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT source.id AS segment_id, source.relative_path, source.source_key,
                   source.spanish_text, source.old_text,
                   output.portuguese_text,
                   score.model_safe_probability, score.candidate_text,
                   state.state_group, state.final_state, state.locked,
                   state.confirmed_matches_output, state.needs_output_apply,
                   state.is_closed,
                   confirmation.confirmation_level,
                   confirmation.confirmation_source,
                   confirmation.confirmation_label,
                   confirmation.confirmed_text,
                   confirmation.locked AS confirmation_locked
            FROM source_segments source
            LEFT JOIN output_segments output ON output.segment_id = source.id
            LEFT JOIN ml_score_items score
              ON score.segment_id = source.id AND score.run_id = ?
            LEFT JOIN segment_state_items state
              ON state.segment_id = source.id AND state.run_id = ?
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.segment_id = source.id
            WHERE source.id IN ({placeholders})
            """,
            (score_run_id, state_run_id or -1, *chunk),
        ).fetchall()
        evidence.update({int(row["segment_id"]): dict(row) for row in rows})
    return evidence


def _classify(family: dict[str, Any], evidence: dict[str, Any] | None) -> tuple[str, str, dict[str, Any]]:
    if not evidence:
        return "inconsistent_closure", "sample_segment_missing", {"source_present": False}
    state_group = str(evidence.get("state_group") or "")
    is_closed = bool(int(evidence.get("is_closed") or 0)) or state_group.casefold() == "closed"
    needs_apply = bool(int(evidence.get("needs_output_apply") or 0))
    output_text = evidence.get("portuguese_text")
    confirmed_text = evidence.get("confirmed_text")
    confirmation_matches_output = bool(
        output_text is not None
        and confirmed_text is not None
        and str(output_text) == str(confirmed_text)
    )
    locked = bool(int(evidence.get("locked") or 0))
    confirmed_matches_output = bool(int(evidence.get("confirmed_matches_output") or 0))
    text_relation = str(family.get("text_relation") or "")
    baseline_aligned = bool(
        text_relation.startswith("equals_old")
        or text_relation == "equals_spanish"
        or (output_text is not None and str(output_text) == str(evidence.get("old_text") or ""))
        or (output_text is not None and str(output_text) == str(evidence.get("spanish_text") or ""))
    )
    facts = {
        "source_present": True,
        "score_present": evidence.get("model_safe_probability") is not None,
        "state_present": bool(state_group),
        "output_present": output_text is not None,
        "is_closed": is_closed,
        "needs_output_apply": needs_apply,
        "locked": locked,
        "confirmed_matches_output": confirmed_matches_output,
        "confirmation_matches_output": confirmation_matches_output,
        "baseline_aligned": baseline_aligned,
    }
    if not state_group or not is_closed or needs_apply:
        return "inconsistent_closure", "lifecycle_not_closed_or_apply_pending", facts
    if locked and confirmed_matches_output and (
        confirmation_matches_output or evidence.get("confirmation_level") is None
    ):
        return "accepted_closed", "locked_confirmation_aligned_with_output", facts
    if baseline_aligned:
        return "baseline_sensitive_watch", "closed_by_baseline_alignment_without_locked_confirmation", facts
    if str(family.get("evidence_kind") or "") == "explicit_issue":
        return "review_required", "explicit_issue_closed_without_governed_confirmation", facts
    return "accepted_closed", "closed_structural_observation_without_active_lifecycle", facts


def audit_closed_observations(
    conn: sqlite3.Connection,
    *,
    discovery_run_id: int | None = None,
) -> dict[str, Any]:
    context = _context(conn, discovery_run_id)
    run_id = int(context["id"])
    score_run_id = int(context["score_run_id"])
    state_run_id = context.get("segment_state_run_id")
    quality = _observation_quality(conn, run_id)
    total_observations = int(
        conn.execute(
            "SELECT COUNT(*) FROM ml_quality_pattern_observations WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    )
    observations = _closed_observations(conn, run_id)
    invalid_sample_count = 0
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[int, int]] = set()
    for observation in observations:
        samples = _json_list(observation.get("samples_json"))
        if not samples and int(observation.get("segment_count") or 0):
            invalid_sample_count += 1
        for sample in samples[:MAX_SAMPLES_PER_FAMILY]:
            if not isinstance(sample, dict):
                invalid_sample_count += 1
                continue
            segment_id = int(sample.get("segment_id") or 0)
            key = (int(observation["observation_id"]), segment_id)
            if not segment_id or key in seen:
                invalid_sample_count += 1
                continue
            seen.add(key)
            selected.append((observation, sample))
    evidence_by_segment = _segment_evidence(
        conn,
        segment_ids={int(sample["segment_id"]) for _, sample in selected},
        score_run_id=score_run_id,
        state_run_id=int(state_run_id) if state_run_id else None,
    )
    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for observation, sample in selected:
        segment_id = int(sample["segment_id"])
        evidence = evidence_by_segment.get(segment_id)
        closure_class, reason, facts = _classify(observation, evidence)
        counts[closure_class] += 1
        row = evidence or {}
        items.append(
            {
                "observation_id": int(observation["observation_id"]),
                "family_id": int(observation["family_id"]),
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or sample.get("relative_path"),
                "source_key": row.get("source_key") or sample.get("source_key"),
                "evidence_kind": observation["evidence_kind"],
                "issue_type": observation["issue_type"],
                "token_context": observation["token_context"],
                "file_family": observation["file_family"],
                "text_relation": observation["text_relation"],
                "score": row.get("model_safe_probability", sample.get("score")),
                "state_group": row.get("state_group"),
                "final_state": row.get("final_state"),
                "locked": int(row.get("locked") or 0),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "closure_class": closure_class,
                "review_reason": reason,
                "evidence": facts,
            }
        )
    sampled_families = {item["family_id"] for item in items}
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "run_key": _run_key(run_id, score_run_id, int(state_run_id) if state_run_id else None),
        "discovery_run_id": run_id,
        "quality_epoch_id": context.get("quality_epoch_id"),
        "score_run_id": score_run_id,
        "segment_state_run_id": state_run_id,
        "observation_count": total_observations,
        "closed_observation_count": len(observations),
        "sampled_family_count": len(sampled_families),
        "sampled_segment_count": len({item["segment_id"] for item in items}),
        "sampled_item_count": len(items),
        "accepted_count": counts["accepted_closed"],
        "baseline_watch_count": counts["baseline_sensitive_watch"],
        "review_required_count": counts["review_required"],
        "inconsistent_count": counts["inconsistent_closure"],
        "duplicate_count": quality["duplicate_count"],
        "orphan_count": quality["orphan_count"],
        "invalid_status_count": quality["invalid_status_count"],
        "invalid_sample_count": invalid_sample_count,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "score_write_count": 0,
        "items": items,
    }


def persist_audit(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    db.ensure_database(conn)
    now = db.utc_now()
    summary_keys = (
        "observation_count", "closed_observation_count", "sampled_family_count",
        "sampled_segment_count", "sampled_item_count", "accepted_count", "baseline_watch_count",
        "review_required_count", "inconsistent_count", "duplicate_count",
        "orphan_count", "invalid_status_count", "invalid_sample_count",
    )
    summary = {key: result.get(key) for key in summary_keys}
    conn.execute(
        """
        INSERT INTO ml_quality_closed_observation_audit_runs (
            run_key, rule_version, discovery_run_id, quality_epoch_id, score_run_id,
            segment_state_run_id, observation_count, closed_observation_count,
            sampled_family_count, sampled_segment_count, sampled_item_count, accepted_count,
            baseline_watch_count, review_required_count, inconsistent_count,
            duplicate_count, orphan_count, invalid_sample_count, status,
            summary_json, started_at, finished_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        ON CONFLICT(run_key) DO UPDATE SET
            observation_count = excluded.observation_count,
            closed_observation_count = excluded.closed_observation_count,
            sampled_family_count = excluded.sampled_family_count,
            sampled_segment_count = excluded.sampled_segment_count,
            sampled_item_count = excluded.sampled_item_count,
            accepted_count = excluded.accepted_count,
            baseline_watch_count = excluded.baseline_watch_count,
            review_required_count = excluded.review_required_count,
            inconsistent_count = excluded.inconsistent_count,
            duplicate_count = excluded.duplicate_count,
            orphan_count = excluded.orphan_count,
            invalid_sample_count = excluded.invalid_sample_count,
            status = excluded.status,
            summary_json = excluded.summary_json,
            finished_at = excluded.finished_at,
            updated_at = excluded.updated_at
        """,
        (
            result["run_key"], RULE_VERSION, result["discovery_run_id"],
            result.get("quality_epoch_id"), result["score_run_id"],
            result.get("segment_state_run_id"), result["observation_count"],
            result["closed_observation_count"], result["sampled_family_count"],
            result["sampled_segment_count"], result["sampled_item_count"], result["accepted_count"],
            result["baseline_watch_count"], result["review_required_count"],
            result["inconsistent_count"], result["duplicate_count"],
            result["orphan_count"], result["invalid_sample_count"],
            json.dumps(summary, ensure_ascii=False, sort_keys=True), now, now, now,
        ),
    )
    audit_run_id = int(
        conn.execute(
            "SELECT id FROM ml_quality_closed_observation_audit_runs WHERE run_key = ?",
            (result["run_key"],),
        ).fetchone()["id"]
    )
    conn.execute(
        "DELETE FROM ml_quality_closed_observation_audit_items WHERE run_id = ?",
        (audit_run_id,),
    )
    for item in result["items"]:
        conn.execute(
            """
            INSERT INTO ml_quality_closed_observation_audit_items (
                run_id, observation_id, family_id, segment_id, relative_path,
                source_key, evidence_kind, issue_type, token_context, file_family,
                text_relation, score, state_group, final_state, locked,
                confirmed_matches_output, needs_output_apply, confirmation_level,
                confirmation_source, confirmation_label, closure_class,
                review_reason, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_run_id, item["observation_id"], item["family_id"],
                item["segment_id"], item.get("relative_path"), item.get("source_key"),
                item["evidence_kind"], item["issue_type"], item["token_context"],
                item["file_family"], item["text_relation"], item.get("score"),
                item.get("state_group"), item.get("final_state"), item["locked"],
                item["confirmed_matches_output"], item["needs_output_apply"],
                item.get("confirmation_level"), item.get("confirmation_source"),
                item.get("confirmation_label"), item["closure_class"],
                item["review_reason"],
                json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True), now,
            ),
        )
    conn.commit()
    return audit_run_id


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Audit a stratified sample of closed quality observations without reopening lifecycle."
    )
    parser.add_argument("--discovery-run-id", type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist audit metadata only; never writes confirmations, scores, or output.",
    )
    args = parser.parse_args()
    settings = db.load_settings()
    with db.connect(settings) as conn:
        if args.apply:
            db.ensure_database(conn)
        result = audit_closed_observations(conn, discovery_run_id=args.discovery_run_id)
        run_id = persist_audit(conn, result) if args.apply else None
    payload = {key: value for key, value in result.items() if key != "items"}
    payload.update(
        {
            "apply": args.apply,
            "run_id": run_id,
            "review_samples": [
                item for item in result["items"]
                if item["closure_class"] in {"review_required", "inconsistent_closure"}
            ][:20],
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
