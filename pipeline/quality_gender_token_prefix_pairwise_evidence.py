from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import quality_shadow_store
from quality_gender_token_prefix_shadow import BROKEN_PREFIX_RE, RULE_VERSION as SOURCE_RULE_VERSION


RULE_VERSION = "quality_gender_token_prefix_pairwise_evidence_v1"
EVIDENCE_TYPE = "deterministic_gender_token_extra_prefix_repair"
ABSOLUTE_QUALITY_LANE = "local_deterministic_repair_only"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_shadow_jsonl() -> Path:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    paths = sorted(reports_dir.glob("*_quality_gender_token_prefix_shadow.jsonl"))
    if not paths:
        raise RuntimeError("No gender-token prefix shadow JSONL was found.")
    return paths[-1]


def load_shadow_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [row for row in rows if row.get("lane") == "pairwise_evidence_eligible"]
    if any(row.get("source") != SOURCE_RULE_VERSION for row in selected):
        raise RuntimeError("Shadow JSONL contains an unexpected source rule version.")
    return selected


def calibration_band(score: float) -> str:
    if score < 0.20:
        return "critical_lt_20"
    if score < 0.50:
        return "low_20_50"
    if score < 0.75:
        return "moderate_50_75"
    return "near_promotion_75_90"


def recommended_route(score: float) -> str:
    if score >= 0.75:
        return "context_review_for_promotion"
    if score >= 0.50:
        return "specialist_rule_followup"
    if score >= 0.20:
        return "pairwise_training_only"
    return "architecture_or_semantic_backlog"


def load_exact_texts(
    conn: sqlite3.Connection, score_run_id: int, segment_ids: list[int]
) -> dict[int, str]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, candidate_text
        FROM ml_score_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (score_run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): str(row["candidate_text"] or "") for row in rows}


def repair(text: str) -> str:
    return BROKEN_PREFIX_RE.sub(lambda match: match.group("stem") + match.group("token"), text)


def prepare_evidence(
    conn: sqlite3.Connection, shadow_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    score_run_ids = {int(row["score_run_id"]) for row in shadow_rows}
    if len(score_run_ids) != 1:
        raise RuntimeError(f"Expected one score run, found {sorted(score_run_ids)}.")
    score_run_id = score_run_ids.pop()
    exact_texts = load_exact_texts(
        conn, score_run_id, [int(row["segment_id"]) for row in shadow_rows]
    )
    evidence: list[dict[str, Any]] = []
    for row in shadow_rows:
        segment_id = int(row["segment_id"])
        baseline = exact_texts.get(segment_id)
        if baseline is None:
            raise RuntimeError(f"Missing exact score text for segment {segment_id}.")
        candidate = repair(baseline)
        if candidate == baseline:
            raise RuntimeError(f"Pairwise evidence has no text change for segment {segment_id}.")
        if row.get("blockers"):
            raise RuntimeError(f"Blocked row leaked into pairwise evidence for segment {segment_id}.")
        if not bool(row.get("token_integrity_ok")) or row.get("post_issue_codes"):
            raise RuntimeError(f"Unsafe pairwise row for segment {segment_id}.")
        baseline_hash = sha256_text(baseline)
        candidate_hash = sha256_text(candidate)
        baseline_score = float(row.get("raw_current_score") or 0.0)
        evidence.append(
            {
                "evidence_key": sha256_text(
                    f"{EVIDENCE_TYPE}|{segment_id}|{baseline_hash}|{candidate_hash}"
                ),
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "baseline_text": baseline,
                "candidate_text": candidate,
                "baseline_hash": baseline_hash,
                "candidate_hash": candidate_hash,
                "baseline_score_raw": baseline_score,
                "candidate_score_raw": float(row.get("raw_candidate_score") or 0.0),
                "pairwise_score": float(row.get("calibrated_candidate_score") or baseline_score),
                "pairwise_delta": float(row.get("calibrated_score_delta") or 0.0),
                "calibration_band": calibration_band(baseline_score),
                "recommended_route": recommended_route(baseline_score),
                "source_metadata": row,
            }
        )
    return evidence


def insert_run(
    conn: sqlite3.Connection, source_path: Path, evidence: list[dict[str, Any]]
) -> tuple[int, int, int]:
    now = db.utc_now()
    metadata = evidence[0]["source_metadata"]
    cursor = conn.execute(
        """
        INSERT INTO ml_pairwise_quality_runs (
          rule_version, source_rule_version, source_score_run_id, source_model_run_id,
          evidence_type, source_jsonl_path, candidate_count,
          training_eligible_count, promotion_eligible_count, auto_safe_eligible_count,
          notes, started_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            SOURCE_RULE_VERSION,
            int(metadata["score_run_id"]),
            int(metadata.get("model_run_id") or 0),
            EVIDENCE_TYPE,
            str(source_path),
            len(evidence),
            len(evidence),
            (
                "Trusted-stem gender-token repair as pairwise preference only. "
                "It cannot promote, close or apply a segment by itself."
            ),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    inserted = 0
    reused = 0
    for item in evidence:
        existing = conn.execute(
            "SELECT id FROM ml_pairwise_quality_evidence WHERE evidence_key = ?",
            (item["evidence_key"],),
        ).fetchone()
        metadata_json = json.dumps(item["source_metadata"], ensure_ascii=False, sort_keys=True)
        if existing:
            conn.execute(
                """
                UPDATE ml_pairwise_quality_evidence
                SET last_run_id = ?, occurrence_count = occurrence_count + 1,
                    last_seen_at = ?, source_metadata_json = ?
                WHERE evidence_key = ?
                """,
                (run_id, now, metadata_json, item["evidence_key"]),
            )
            reused += 1
            continue
        conn.execute(
            """
            INSERT INTO ml_pairwise_quality_evidence (
              evidence_key, first_run_id, last_run_id, segment_id, relative_path, source_key,
              baseline_text, candidate_text, baseline_hash, candidate_hash,
              evidence_type, preference_label, training_target, absolute_quality_lane,
              calibration_band, recommended_route,
              baseline_score_raw, candidate_score_raw, pairwise_score, pairwise_delta,
              evidence_weight, token_integrity_ok, post_validation_clean,
              training_eligible, promotion_eligible, auto_safe_eligible,
              blockers_json, source_metadata_json, first_seen_at, last_seen_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, 'candidate_preferred', 'pairwise_preference_only', ?, ?, ?,
              ?, ?, ?, ?, 1.0, 1, 1, 1, 0, 0, '[]', ?, ?, ?
            )
            """,
            (
                item["evidence_key"], run_id, run_id, item["segment_id"],
                item["relative_path"], item["source_key"], item["baseline_text"],
                item["candidate_text"], item["baseline_hash"], item["candidate_hash"],
                EVIDENCE_TYPE, ABSOLUTE_QUALITY_LANE, item["calibration_band"],
                item["recommended_route"], item["baseline_score_raw"],
                item["candidate_score_raw"], item["pairwise_score"], item["pairwise_delta"],
                metadata_json, now, now,
            ),
        )
        inserted += 1
    conn.execute(
        """
        UPDATE ml_pairwise_quality_runs
        SET inserted_count = ?, reused_count = ?, finished_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (inserted, reused, now, now, run_id),
    )
    conn.commit()
    return run_id, inserted, reused


def write_reports(
    source_path: Path | str,
    evidence: list[dict[str, Any]],
    apply: bool,
    run_id: int | None,
    inserted: int,
    reused: int,
) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_quality_gender_token_prefix_pairwise_evidence"
    dataset_path = base.with_suffix(".jsonl")
    markdown_path = base.with_suffix(".md")
    summary_path = base.with_name(base.name + "_summary.json")
    bands = Counter(item["calibration_band"] for item in evidence)
    routes = Counter(item["recommended_route"] for item in evidence)
    with dataset_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in evidence:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "source_shadow": str(source_path),
        "source_shadow_kind": "database_snapshot" if str(source_path).startswith("db:") else "legacy_jsonl",
        "source_shadow_jsonl": None if str(source_path).startswith("db:") else str(source_path),
        "database_write": apply,
        "pairwise_run_id": run_id,
        "evidence_type": EVIDENCE_TYPE,
        "evidence_count": len(evidence),
        "inserted_count": inserted,
        "reused_count": reused,
        "training_eligible_count": len(evidence),
        "promotion_eligible_count": 0,
        "auto_safe_eligible_count": 0,
        "band_counts": dict(bands),
        "route_counts": dict(routes),
        "guards": {
            "pairwise_preference_only": True,
            "confirmation_write_count": 0,
            "segment_state_write_count": 0,
            "promotion_write_count": 0,
            "output_write_count": 0,
        },
        "artifacts": {
            "markdown": str(markdown_path),
            "dataset_jsonl": str(dataset_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Gender-token prefix pairwise evidence", "",
        f"- Evidence: `{len(evidence)}`", f"- Persisted: `{apply}`",
        f"- Run: `{run_id if run_id is not None else 'dry-run'}`",
        f"- Inserted/reused: `{inserted}/{reused}`",
        "- Promotion eligible: `0`", "- Auto-safe eligible: `0`", "",
        "## Safety contract", "",
        "This dataset teaches only that removing the duplicated a/o prefix before ES_OA/ES_AO is preferred.",
        "Whole-segment quality, lifecycle and publication gates remain unchanged.",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist trusted gender-token pairwise evidence.")
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
                    eligible_lane="pairwise_evidence_eligible",
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows) if shadow_rows else []
            if evidence:
                run_id, inserted, reused = insert_run(conn, source_path, evidence)
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
                    eligible_lane="pairwise_evidence_eligible",
                )
                source_path = f"db:ml_quality_shadow_runs/{shadow_run['id']}"
            evidence = prepare_evidence(conn, shadow_rows) if shadow_rows else []
    summary = write_reports(source_path, evidence, args.apply, run_id, inserted, reused)
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
                    summary["artifacts"]["dataset_jsonl"],
                    summary["artifacts"]["summary"], db.utc_now(), run_id,
                ),
            )
            conn.commit()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
