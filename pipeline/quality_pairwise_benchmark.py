from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "quality_pairwise_benchmark_v1"


def preference_is_correct(row: dict[str, Any], candidate_score: float) -> bool:
    baseline_score = float(row.get("baseline_score_raw") or 0)
    preference = str(row.get("preference_label") or "candidate_preferred")
    if preference == "baseline_preferred":
        return candidate_score < baseline_score
    return candidate_score > baseline_score


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def load_rows(conn: sqlite3.Connection, evidence_type: str | None) -> list[dict[str, Any]]:
    where = "WHERE training_eligible = 1"
    params: tuple[Any, ...] = ()
    if evidence_type:
        where += " AND evidence_type = ?"
        params = (evidence_type,)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_pairwise_quality_evidence
        {where}
        ORDER BY baseline_score_raw DESC, segment_id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def write_reports(rows: list[dict[str, Any]], evidence_type: str | None) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_quality_pairwise_benchmark"
    markdown_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = base.with_name(base.name + "_summary.json")

    raw_correct = [
        row
        for row in rows
        if preference_is_correct(row, float(row.get("candidate_score_raw") or 0))
    ]
    calibrated_correct = [
        row
        for row in rows
        if preference_is_correct(row, float(row.get("pairwise_score") or 0))
    ]
    raw_inversions = [row for row in rows if row not in raw_correct]
    bands = Counter(str(row.get("calibration_band") or "unknown") for row in rows)
    routes = Counter(str(row.get("recommended_route") or "unknown") for row in rows)
    auto_safe_count = sum(int(row.get("auto_safe_eligible") or 0) for row in rows)
    promotion_count = sum(int(row.get("promotion_eligible") or 0) for row in rows)
    total = len(rows)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "segment_id": row["segment_id"],
                        "evidence_type": row["evidence_type"],
                        "calibration_band": row["calibration_band"],
                        "recommended_route": row["recommended_route"],
                        "baseline_score_raw": row["baseline_score_raw"],
                        "candidate_score_raw": row["candidate_score_raw"],
                        "pairwise_score": row["pairwise_score"],
                        "raw_preference_correct": row in raw_correct,
                        "calibrated_preference_correct": row in calibrated_correct,
                        "promotion_eligible": bool(row["promotion_eligible"]),
                        "auto_safe_eligible": bool(row["auto_safe_eligible"]),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "evidence_type": evidence_type or "all",
        "evidence_count": total,
        "raw_pairwise_correct_count": len(raw_correct),
        "raw_pairwise_accuracy": round(len(raw_correct) / total, 6) if total else None,
        "raw_pairwise_inversion_count": len(raw_inversions),
        "calibrated_pairwise_correct_count": len(calibrated_correct),
        "calibrated_pairwise_accuracy": round(len(calibrated_correct) / total, 6) if total else None,
        "calibration_recovered_count": len(calibrated_correct) - len(raw_correct),
        "promotion_eligible_count": promotion_count,
        "auto_safe_eligible_count": auto_safe_count,
        "band_counts": dict(bands),
        "route_counts": dict(routes),
        "gate_passed": bool(
            total
            and len(calibrated_correct) == total
            and promotion_count == 0
            and auto_safe_count == 0
        ),
        "artifacts": {
            "markdown": str(markdown_path),
            "jsonl": str(jsonl_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Pairwise quality calibration benchmark",
        "",
        f"- Evidence: `{total}`",
        f"- Raw preference accuracy: `{summary['raw_pairwise_accuracy']}`",
        f"- Raw inversions: `{len(raw_inversions)}`",
        f"- Calibrated preference accuracy: `{summary['calibrated_pairwise_accuracy']}`",
        f"- Recovered preferences: `{summary['calibration_recovered_count']}`",
        f"- Promotion eligible: `{promotion_count}`",
        f"- Auto-safe eligible: `{auto_safe_count}`",
        f"- Safety gate: `{'passed' if summary['gate_passed'] else 'failed'}`",
        "",
        "## Quality bands",
        "",
    ]
    lines.extend(f"- `{name}`: `{count}`" for name, count in bands.most_common())
    lines.extend(["", "## Next routes", ""])
    lines.extend(f"- `{name}`: `{count}`" for name, count in routes.most_common())
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "The benchmark measures local pairwise preference, not whole-segment safety.",
            "A recovered preference cannot promote, close or apply a segment by itself.",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark durable pairwise calibration evidence.")
    parser.add_argument("--evidence-type")
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_rows(conn, args.evidence_type)
    if not rows:
        raise RuntimeError("No pairwise quality evidence matched the requested scope.")
    summary = write_reports(rows, args.evidence_type)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
