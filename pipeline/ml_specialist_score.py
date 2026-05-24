from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from typing import Any

import db
import ml_score_segments
from ml_specialist_models import SPECIALIST_GROUPS, SPECIALISTS, SpecialistConfig


RULE_VERSION = "ml_specialist_score_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def latest_model_run_id(conn, model_kind: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_model_runs
        WHERE model_kind = ?
          AND model_path IS NOT NULL
          AND finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (model_kind,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No finished model found for {model_kind}.")
    return int(row["id"])


def score_counts(conn, score_run_id: int) -> Counter[str]:
    rows = conn.execute(
        """
        SELECT final_action, COUNT(*) AS total
        FROM ml_score_items
        WHERE run_id = ?
        GROUP BY final_action
        """,
        (score_run_id,),
    ).fetchall()
    return Counter({row["final_action"]: int(row["total"] or 0) for row in rows})


def score_specialist(
    config: SpecialistConfig,
    model_run_id: int | None,
    limit: int | None,
    batch_size: int,
    include_locked: bool,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        resolved_model_run_id = model_run_id or latest_model_run_id(conn, config.name)

    print(f"[ml_specialist_score] Scoring {config.name} with model_run_id={resolved_model_run_id}")
    score_run_id = ml_score_segments.main(
        limit=limit,
        path_like=None,
        safe_threshold=None,
        include_locked=include_locked,
        batch_size=batch_size,
        model_run_id=resolved_model_run_id,
        scope_sql=config.scope_sql,
    )
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        counts = score_counts(conn, score_run_id)
    total = sum(counts.values())
    return {
        "specialist": config.name,
        "model_run_id": resolved_model_run_id,
        "score_run_id": score_run_id,
        "total": total,
        "auto_safe": counts["auto_safe"],
        "needs_human": counts["needs_human"],
        "needs_autofix": counts["needs_autofix"],
        "blocked_structure": counts["blocked_structure"],
    }


def resolve_specialists(specialist: str) -> list[SpecialistConfig]:
    keys = SPECIALIST_GROUPS.get(specialist, [specialist])
    return [SPECIALISTS[key] for key in keys]


def main(
    specialist: str = "title_promising_subspecialists",
    model_run_id: int | None = None,
    limit: int | None = None,
    batch_size: int = 5000,
    include_locked: bool = False,
) -> None:
    if model_run_id is not None and specialist in SPECIALIST_GROUPS:
        raise ValueError("--model-run-id can only be used with a single specialist.")

    settings = db.load_settings()
    started_at = datetime.now()
    results = [
        score_specialist(
            config,
            model_run_id=model_run_id,
            limit=limit,
            batch_size=batch_size,
            include_locked=include_locked,
        )
        for config in resolve_specialists(specialist)
    ]
    elapsed = datetime.now() - started_at
    report_lines = [
        "ML specialist score summary",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Requested specialist: {specialist}",
        f"Include locked: {include_locked}",
        "",
        "Results:",
    ]
    for result in results:
        total = result["total"]
        auto_rate = result["auto_safe"] / total if total else 0
        report_lines.append(
            f"- {result['specialist']}: model_run_id={result['model_run_id']}, "
            f"score_run_id={result['score_run_id']}, scored={total}, "
            f"auto_safe={result['auto_safe']} ({auto_rate:.2%}), "
            f"needs_human={result['needs_human']}, needs_autofix={result['needs_autofix']}, "
            f"blocked_structure={result['blocked_structure']}"
        )
    report_lines.extend(
        [
            "",
            "Interpretation:",
            "- This command only scores trusted specialist scopes; it does not apply output or promote models.",
            "- Use an auditor/review queue before converting specialist auto_safe into policy.",
        ]
    )
    report_path = db.write_report(settings, "ml_specialist_score", report_lines)
    print(f"[ml_specialist_score] Summary report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score segments with trusted specialist model scopes.")
    parser.add_argument("--specialist", choices=sorted({*SPECIALIST_GROUPS, *SPECIALISTS}), default="title_promising_subspecialists")
    parser.add_argument("--model-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--include-locked", action="store_true")
    args = parser.parse_args()
    main(
        specialist=args.specialist,
        model_run_id=args.model_run_id,
        limit=args.limit,
        batch_size=args.batch_size,
        include_locked=args.include_locked,
    )
