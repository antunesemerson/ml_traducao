from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
import quality_pairwise_evidence as pairwise
import quality_shadow_store
from apply_safe_output_updates import protected_tokens
from quality_dynamic_name_de_prefix_shadow import (
    ELIGIBLE_LANE,
    ISSUE_CODE,
    RULE_VERSION as SOURCE_RULE_VERSION,
    repair_dynamic_name_de_prefix,
)


RULE_VERSION = "quality_dynamic_name_de_prefix_pairwise_evidence_v1"
EVIDENCE_TYPE = "deterministic_dynamic_name_de_prefix_repair"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_shadow_jsonl() -> Path:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    paths = sorted(reports_dir.glob("*_quality_dynamic_name_de_prefix_shadow.jsonl"))
    if not paths:
        raise RuntimeError("No dynamic-name prefix shadow JSONL was found.")
    return paths[-1]


def load_shadow_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [row for row in rows if row.get("lane") == ELIGIBLE_LANE]
    if any(row.get("source") != SOURCE_RULE_VERSION for row in selected):
        raise RuntimeError("Shadow JSONL contains an unexpected source rule version.")
    return selected


def prepare_evidence(conn: sqlite3.Connection, shadow_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not shadow_rows:
        return []
    score_run_ids = {int(row["score_run_id"]) for row in shadow_rows}
    if len(score_run_ids) != 1:
        raise RuntimeError(f"Expected one score run, found {sorted(score_run_ids)}.")
    score_run_id = score_run_ids.pop()
    run = conn.execute("SELECT model_run_id FROM ml_score_runs WHERE id = ?", (score_run_id,)).fetchone()
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
        baseline = str(score.get("candidate_text") or "")
        candidate, repairs = repair_dynamic_name_de_prefix(baseline)
        if not repairs or candidate == baseline:
            raise RuntimeError(f"Evidence has no repair for segment {segment_id}.")
        if shadow.get("blockers") or not bool(shadow.get("token_integrity_ok")):
            raise RuntimeError(f"Blocked row leaked into evidence for segment {segment_id}.")
        if sha256_text(baseline) != str(shadow.get("baseline_hash") or ""):
            raise RuntimeError(f"Shadow baseline is stale for segment {segment_id}.")
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
        metadata = {**shadow, "model_run_id": model_run_id, "score_run_id": score_run_id}
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
                "recommended_route": "monotonic_repair_diagnostic_gate",
                "source_metadata": metadata,
            }
        )
    return evidence


def reconcile_active_evidence(conn: sqlite3.Connection, current_run_id: int | None) -> None:
    if current_run_id is None:
        conn.execute(
            "UPDATE ml_pairwise_quality_evidence SET training_eligible = 0, promotion_eligible = 0 WHERE evidence_type = ?",
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


def write_report(
    source_path: Path | str,
    evidence: list[dict[str, Any]],
    *,
    apply: bool,
    run_id: int | None,
    inserted: int,
    reused: int,
) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_quality_dynamic_name_de_prefix_pairwise_evidence"
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
        "confirmation_write_count": 0,
        "segment_state_write_count": 0,
        "output_write_count": 0,
        "artifacts": {"markdown": str(markdown_path), "summary": str(summary_path)},
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(
        "\n".join(
            [
                "# Evidencia pairwise: preposicao antes de nome dinamico",
                "",
                f"- Evidencias: `{len(evidence)}`",
                f"- Run pairwise: `{run_id if run_id is not None else 'dry-run'}`",
                f"- Inseridas/reutilizadas: `{inserted}/{reused}`",
                "- Confirmacoes/output escritos: `0`",
                "",
                "O Diagnostico registra somente a preferencia local d [nome] -> de [nome].",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist dynamic-name prefix pairwise evidence.")
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
                shadow_rows = load_shadow_rows(args.shadow_jsonl)
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
                shadow_rows = load_shadow_rows(args.shadow_jsonl)
            else:
                shadow_run, shadow_rows = quality_shadow_store.load_snapshot(
                    conn,
                    source_rule_version=SOURCE_RULE_VERSION,
                    run_id=args.shadow_run_id,
                    eligible_lane=ELIGIBLE_LANE,
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows)
    summary = write_report(
        source_path, evidence, apply=args.apply, run_id=run_id, inserted=inserted, reused=reused
    )
    if args.apply and run_id is not None:
        with db.connect(settings) as conn:
            conn.execute(
                """
                UPDATE ml_pairwise_quality_runs
                SET report_path = ?, dataset_path = ?, summary_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    summary["artifacts"]["markdown"],
                    str(source_path),
                    summary["artifacts"]["summary"],
                    db.utc_now(),
                    run_id,
                ),
            )
            conn.commit()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
