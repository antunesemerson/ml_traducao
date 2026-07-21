from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import db
import local_quality_validator
import quality_pairwise_evidence as pairwise
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from quality_utf8_mojibake_shadow import (
    ELIGIBLE_LANE,
    ISSUE_CODE,
    RULE_VERSION as SOURCE_RULE_VERSION,
    repair_utf8_mojibake,
)


RULE_VERSION = "quality_utf8_mojibake_pairwise_evidence_v1"
EVIDENCE_TYPE = "deterministic_utf8_mojibake_repair"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_evidence(
    conn: sqlite3.Connection, shadow_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not shadow_rows:
        return []
    score_run_ids = {int(row["score_run_id"]) for row in shadow_rows}
    if len(score_run_ids) != 1:
        raise RuntimeError(f"Expected one score run, found {sorted(score_run_ids)}.")
    score_run_id = score_run_ids.pop()
    run = conn.execute(
        "SELECT model_run_id FROM ml_score_runs WHERE id = ?", (score_run_id,)
    ).fetchone()
    if not run or not int(run["model_run_id"] or 0):
        raise RuntimeError(f"Score run {score_run_id} has no model run.")
    model_run_id = int(run["model_run_id"])
    segment_ids = [int(row["segment_id"]) for row in shadow_rows]
    placeholders = ",".join("?" for _ in segment_ids)
    exact_rows = conn.execute(
        f"""
        SELECT segment_id, candidate_text, model_safe_probability
        FROM ml_score_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (score_run_id, *segment_ids),
    ).fetchall()
    exact = {int(row["segment_id"]): dict(row) for row in exact_rows}

    evidence: list[dict[str, Any]] = []
    for shadow in shadow_rows:
        segment_id = int(shadow["segment_id"])
        score = exact.get(segment_id)
        if not score:
            raise RuntimeError(f"Missing exact score text for segment {segment_id}.")
        if shadow.get("blockers") or not bool(shadow.get("token_integrity_ok")):
            raise RuntimeError(f"Blocked row leaked into evidence for segment {segment_id}.")
        baseline = str(score.get("candidate_text") or "")
        if sha256_text(baseline) != str(shadow.get("baseline_hash") or ""):
            raise RuntimeError(f"Shadow baseline is stale for segment {segment_id}.")
        candidate, repairs = repair_utf8_mojibake(baseline)
        if not repairs or candidate == baseline:
            raise RuntimeError(f"Evidence has no UTF-8 repair for segment {segment_id}.")
        if sha256_text(candidate) != str(shadow.get("candidate_hash") or ""):
            raise RuntimeError(f"Shadow candidate changed for segment {segment_id}.")
        if protected_tokens(baseline) != protected_tokens(candidate):
            raise RuntimeError(f"Token signature changed for segment {segment_id}.")
        post_codes = {
            str(item.get("code"))
            for item in local_quality_validator.validate_text(candidate).get("issues") or []
            if item.get("code")
        }
        if ISSUE_CODE in post_codes or post_codes:
            raise RuntimeError(f"Post-validation issue for segment {segment_id}: {sorted(post_codes)}")
        baseline_score = float(score.get("model_safe_probability") or 0.0)
        pairwise_score = float(shadow.get("calibrated_candidate_score") or 0.0)
        if pairwise_score <= baseline_score:
            raise RuntimeError(f"Pairwise score is not monotonic for segment {segment_id}.")
        baseline_hash = sha256_text(baseline)
        candidate_hash = sha256_text(candidate)
        metadata = {
            **shadow,
            "model_run_id": model_run_id,
            "score_run_id": score_run_id,
            "promotion_requires_human_unlock": bool(shadow.get("human_locked")),
        }
        evidence.append(
            {
                "evidence_key": sha256_text(
                    f"{EVIDENCE_TYPE}|{segment_id}|{baseline_hash}|{candidate_hash}"
                ),
                "segment_id": segment_id,
                "relative_path": str(shadow.get("relative_path") or ""),
                "source_key": str(shadow.get("source_key") or ""),
                "baseline_text": baseline,
                "candidate_text": candidate,
                "baseline_hash": baseline_hash,
                "candidate_hash": candidate_hash,
                "baseline_score_raw": baseline_score,
                "candidate_score_raw": float(shadow.get("raw_candidate_score") or 0.0),
                "pairwise_score": pairwise_score,
                "pairwise_delta": pairwise_score - baseline_score,
                "calibration_band": pairwise.calibration_band(baseline_score),
                "recommended_route": (
                    "human_unlock_review"
                    if bool(shadow.get("human_locked"))
                    else "monotonic_repair_diagnostic_gate"
                ),
                "source_metadata": metadata,
            }
        )
    return evidence


def reconcile_active_evidence(
    conn: sqlite3.Connection, current_run_id: int | None
) -> None:
    if current_run_id is None:
        conn.execute(
            """
            UPDATE ml_pairwise_quality_evidence
            SET training_eligible = 0, promotion_eligible = 0
            WHERE evidence_type = ?
            """,
            (EVIDENCE_TYPE,),
        )
    else:
        conn.execute(
            """
            UPDATE ml_pairwise_quality_evidence
            SET training_eligible = CASE WHEN last_run_id = ? THEN 1 ELSE 0 END,
                promotion_eligible = 0
            WHERE evidence_type = ?
            """,
            (current_run_id, EVIDENCE_TYPE),
        )
    conn.commit()


def summarize(
    source_path: Path | str,
    evidence: list[dict[str, Any]],
    *,
    apply: bool,
    run_id: int | None,
    inserted: int,
    reused: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "source_shadow": str(source_path),
        "source_shadow_kind": (
            "database_snapshot" if str(source_path).startswith("db:") else "legacy_jsonl"
        ),
        "evidence_type": EVIDENCE_TYPE,
        "database_write": apply,
        "pairwise_run_id": run_id,
        "evidence_count": len(evidence),
        "human_unlock_review_count": sum(
            bool(item["source_metadata"].get("promotion_requires_human_unlock"))
            for item in evidence
        ),
        "inserted_count": inserted,
        "reused_count": reused,
        "confirmation_write_count": 0,
        "segment_state_write_count": 0,
        "output_write_count": 0,
        "reports_required": False,
        "artifacts": {},
    }


def load_legacy_shadow(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    selected = [row for row in rows if row.get("lane") == ELIGIBLE_LANE]
    if any(row.get("source") != SOURCE_RULE_VERSION for row in selected):
        raise RuntimeError("Shadow JSONL contains an unexpected source rule version.")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist deterministic UTF-8 pairwise evidence.")
    parser.add_argument("--shadow-jsonl", type=Path)
    parser.add_argument("--shadow-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    run_id = None
    inserted = 0
    reused = 0
    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            if args.shadow_jsonl:
                source_path: Path | str = args.shadow_jsonl.resolve()
                shadow_rows = load_legacy_shadow(args.shadow_jsonl)
            else:
                shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                    conn,
                    source_rule_version=SOURCE_RULE_VERSION,
                    run_id=args.shadow_run_id,
                    eligible_lane=ELIGIBLE_LANE,
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows)
            if evidence:
                run_id, inserted, reused = pairwise.insert_run(
                    conn,
                    source_path,
                    evidence,
                    evidence_type=EVIDENCE_TYPE,
                    source_rule_version=SOURCE_RULE_VERSION,
                    absolute_quality_lane=ELIGIBLE_LANE,
                )
            reconcile_active_evidence(conn, run_id)
    else:
        database_path = db.get_database_path(settings)
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            if args.shadow_jsonl:
                source_path = args.shadow_jsonl.resolve()
                shadow_rows = load_legacy_shadow(args.shadow_jsonl)
            else:
                shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                    conn,
                    source_rule_version=SOURCE_RULE_VERSION,
                    run_id=args.shadow_run_id,
                    eligible_lane=ELIGIBLE_LANE,
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows)
    summary = summarize(
        source_path,
        evidence,
        apply=args.apply,
        run_id=run_id,
        inserted=inserted,
        reused=reused,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
