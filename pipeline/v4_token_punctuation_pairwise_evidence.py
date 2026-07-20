from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import quality_pairwise_evidence as pairwise
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from v4_token_punctuation_boundary_shadow import (
    RULE_VERSION as SOURCE_RULE_VERSION,
    repair_token_punctuation_boundaries,
)


RULE_VERSION = "quality_token_punctuation_pairwise_evidence_v1"
EVIDENCE_TYPE = "deterministic_token_punctuation_boundary_repair"
SOURCE_LANE = "ready_for_review"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_shadow_jsonl() -> Path:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    paths = sorted(reports_dir.glob("*_quality_token_punctuation_boundary_shadow.jsonl"))
    if not paths:
        raise RuntimeError("No token/punctuation shadow JSONL was found.")
    return paths[-1]


def load_shadow_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [row for row in rows if row.get("lane") == SOURCE_LANE]
    if any(row.get("source") != SOURCE_RULE_VERSION for row in selected):
        raise RuntimeError("Shadow JSONL contains an unexpected source rule version.")
    return selected


def prepare_evidence(
    conn: sqlite3.Connection,
    shadow_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    score_run_ids = {int(row["score_run_id"]) for row in shadow_rows}
    if len(score_run_ids) != 1:
        raise RuntimeError(f"Expected one score run, found {sorted(score_run_ids)}.")
    score_run_id = score_run_ids.pop()
    run = conn.execute(
        "SELECT model_run_id FROM ml_score_runs WHERE id = ?",
        (score_run_id,),
    ).fetchone()
    if not run or not int(run["model_run_id"] or 0):
        raise RuntimeError(f"Score run {score_run_id} has no model run.")
    model_run_id = int(run["model_run_id"])
    ids = [int(row["segment_id"]) for row in shadow_rows]
    placeholders = ",".join("?" for _ in ids)
    exact_rows = conn.execute(
        f"""
        SELECT segment_id, candidate_text, model_safe_probability
        FROM ml_score_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (score_run_id, *ids),
    ).fetchall()
    exact = {int(row["segment_id"]): dict(row) for row in exact_rows}
    evidence: list[dict[str, Any]] = []
    for shadow in shadow_rows:
        segment_id = int(shadow["segment_id"])
        score = exact.get(segment_id)
        if not score:
            raise RuntimeError(f"Missing exact score text for segment {segment_id}.")
        baseline = str(score.get("candidate_text") or "")
        candidate, repairs = repair_token_punctuation_boundaries(baseline)
        if not repairs or candidate == baseline:
            raise RuntimeError(f"Evidence has no repair for segment {segment_id}.")
        if shadow.get("blockers") or not bool(shadow.get("token_integrity_ok")):
            raise RuntimeError(f"Blocked row leaked into evidence for segment {segment_id}.")
        if protected_tokens(baseline) != protected_tokens(candidate):
            raise RuntimeError(f"Token signature changed for segment {segment_id}.")
        baseline_hash = sha256_text(baseline)
        candidate_hash = sha256_text(candidate)
        baseline_score = float(score.get("model_safe_probability") or 0.0)
        pairwise_score = min(1.0, baseline_score + 0.02)
        metadata = {
            **shadow,
            "model_run_id": model_run_id,
            "score_run_id": score_run_id,
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
                "candidate_score_raw": baseline_score,
                "pairwise_score": pairwise_score,
                "pairwise_delta": pairwise_score - baseline_score,
                "calibration_band": pairwise.calibration_band(baseline_score),
                "recommended_route": "monotonic_repair_diagnostic_gate",
                "source_metadata": metadata,
            }
        )
    return evidence


def mark_offline_queue_consumed(
    conn: sqlite3.Connection,
    evidence: list[dict[str, Any]],
) -> tuple[int, int]:
    now = db.utc_now()
    consumed = 0
    for item in evidence:
        cursor = conn.execute(
            """
            UPDATE offline_proposals
            SET apply_result = 'superseded_by_pairwise_diagnostic', updated_at = ?
            WHERE segment_id = ?
              AND proposal_source = 'remove_space_before_punctuation'
              AND proposed_text = ?
              AND status = 'auto_ready'
              AND apply_result IS NULL
            """,
            (now, int(item["segment_id"]), item["candidate_text"]),
        )
        consumed += int(cursor.rowcount or 0)
    rejected = conn.execute(
        """
        UPDATE offline_proposals
        SET apply_result = 'rejected_by_quality_diagnostic_gate', updated_at = ?
        WHERE candidate_bucket IN ('v4_token_punctuation_boundary', 'quality_token_punctuation_boundary')
          AND proposal_source = 'remove_space_before_punctuation'
          AND status = 'auto_ready'
          AND apply_result IS NULL
        """,
        (now,),
    ).rowcount
    conn.commit()
    return consumed, int(rejected or 0)


def write_report(
    source_path: Path | str,
    evidence: list[dict[str, Any]],
    *,
    apply: bool,
    run_id: int | None,
    inserted: int,
    reused: int,
    consumed: int,
    rejected: int,
) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_quality_token_punctuation_pairwise_evidence"
    summary_path = base.with_name(base.name + "_summary.json")
    markdown_path = base.with_suffix(".md")
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "source_shadow": str(source_path),
        "source_shadow_kind": "database_snapshot" if str(source_path).startswith("db:") else "legacy_jsonl",
        "source_shadow_jsonl": None if str(source_path).startswith("db:") else str(source_path),
        "evidence_type": EVIDENCE_TYPE,
        "database_write": apply,
        "pairwise_run_id": run_id,
        "evidence_count": len(evidence),
        "inserted_count": inserted,
        "reused_count": reused,
        "offline_proposals_consumed_count": consumed,
        "offline_proposals_rejected_count": rejected,
        "confirmation_write_count": 0,
        "segment_state_write_count": 0,
        "output_write_count": 0,
        "artifacts": {
            "markdown": str(markdown_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(
        "\n".join(
            [
                "# Token/punctuation pairwise evidence",
                "",
                f"- Evidence: `{len(evidence)}`",
                f"- Pairwise run: `{run_id if run_id is not None else 'dry-run'}`",
                f"- Inserted/reused: `{inserted}/{reused}`",
                f"- Legacy offline proposals consumed: `{consumed}`",
                f"- Legacy offline proposals rejected by strict gate: `{rejected}`",
                "- Confirmations/output writes: `0`",
                "",
                "The diagnostic owns discovery and promotion eligibility; Evaluation owns confirmation.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist version-independent token/punctuation evidence.")
    parser.add_argument("--shadow-jsonl", type=Path)
    parser.add_argument("--shadow-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    run_id = None
    inserted = 0
    reused = 0
    consumed = 0
    rejected = 0
    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            if args.shadow_jsonl:
                source_path: Path | str = args.shadow_jsonl.resolve()
                shadow_rows = load_shadow_rows(args.shadow_jsonl)
            else:
                shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                    conn,
                    source_rule_version=SOURCE_RULE_VERSION,
                    run_id=args.shadow_run_id,
                    eligible_lane=SOURCE_LANE,
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows) if shadow_rows else []
            if evidence:
                run_id, inserted, reused = pairwise.insert_run(
                    conn,
                    source_path,
                    evidence,
                    evidence_type=EVIDENCE_TYPE,
                    source_rule_version=SOURCE_RULE_VERSION,
                    absolute_quality_lane=SOURCE_LANE,
                )
                consumed, rejected = mark_offline_queue_consumed(conn, evidence)
    else:
        database_path = db.get_database_path(settings)
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            if args.shadow_jsonl:
                source_path = args.shadow_jsonl.resolve()
                shadow_rows = load_shadow_rows(args.shadow_jsonl)
            else:
                shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                    conn,
                    source_rule_version=SOURCE_RULE_VERSION,
                    run_id=args.shadow_run_id,
                    eligible_lane=SOURCE_LANE,
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows) if shadow_rows else []
    summary = write_report(
        source_path,
        evidence,
        apply=args.apply,
        run_id=run_id,
        inserted=inserted,
        reused=reused,
        consumed=consumed,
        rejected=rejected,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
