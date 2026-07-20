from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "v3_stable_output_no_reopen_shadow_policy_v1"
TARGET_FAMILIES = ("autofix_unknown_microagent", "short_label_style_microagent")
SCORE_FLOOR = 0.75
MIN_ACTIVATION_PRECISION = 0.95


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_review() -> Path:
    reports = db.project_path(db.load_settings()["reports_dir"])
    matches = sorted(reports.glob("*_v3_improvement_shadow_review_readonly.jsonl"))
    if not matches:
        raise RuntimeError("No V3 shadow review JSONL was found.")
    return matches[-1]


def report_paths() -> dict[str, Path]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    base = reports / f"{stamp()}_v3_stable_output_no_reopen_shadow_policy"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
        "spec": base.with_name(base.name + "_spec.json"),
    }


def load_review(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 40:
        raise RuntimeError(f"Expected 40 reviewed records, got {len(rows)}.")
    return rows


def collect_scope(conn: sqlite3.Connection, backlog_run_id: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_FAMILIES)
    rows = conn.execute(
        f"""
        SELECT
            backlog.segment_id,
            backlog.relative_path,
            backlog.source_key,
            backlog.primary_family,
            backlog.candidate_score,
            backlog.old_score,
            backlog.confirmation_locked,
            backlog.confirmed_matches_output,
            backlog.output_equals_baseline,
            backlog.needs_output_apply,
            backlog.source_delta_status,
            state.final_state,
            state.state_group,
            state.is_closed,
            state.lifecycle_policy_action,
            source.english_text,
            source.spanish_text,
            output.portuguese_text AS output_text
        FROM v3_improvement_backlog_items backlog
        JOIN source_segments source ON source.id = backlog.segment_id
        JOIN output_segments output ON output.segment_id = backlog.segment_id
        JOIN segment_state_items state
          ON state.segment_id = backlog.segment_id
         AND state.run_id = 663
        WHERE backlog.run_id = ?
          AND backlog.primary_family IN ({placeholders})
          AND backlog.candidate_score >= ?
          AND backlog.confirmation_locked = 0
          AND backlog.confirmed_matches_output = 1
          AND backlog.output_equals_baseline = 1
          AND backlog.needs_output_apply = 0
        ORDER BY backlog.candidate_score DESC, backlog.segment_id ASC
        """,
        (backlog_run_id, *TARGET_FAMILIES, SCORE_FLOOR),
    ).fetchall()
    return [dict(row) for row in rows]


def write_reports(
    paths: dict[str, Path],
    review_path: Path,
    reviewed: list[dict[str, Any]],
    scope: list[dict[str, Any]],
) -> dict[str, Any]:
    approved = [row for row in reviewed if row["decision"] == "approve_stable_output"]
    evidence_ids = {int(row["segment_id"]) for row in approved}
    reviewed_ids = {int(row["segment_id"]) for row in reviewed}
    family_reviewed = Counter(row["primary_family"] for row in reviewed)
    family_approved = Counter(row["primary_family"] for row in approved)
    sample_precision = len(approved) / len(reviewed)
    family_precision = {
        family: family_approved[family] / family_reviewed[family]
        for family in sorted(family_reviewed)
    }
    for row in scope:
        segment_id = int(row["segment_id"])
        if segment_id in evidence_ids:
            row["shadow_decision"] = "positive_evidence_no_reopen"
        elif segment_id in reviewed_ids:
            row["shadow_decision"] = "reviewed_not_safe_for_suppression"
        else:
            row["shadow_decision"] = "unreviewed_shadow_candidate"
        row["automatic_suppression_allowed"] = False

    activation_allowed = sample_precision >= MIN_ACTIVATION_PRECISION
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "source_review": str(review_path),
        "backlog_run_id": 1,
        "source_segment_state_run_id": 663,
        "score_floor": SCORE_FLOOR,
        "target_families": list(TARGET_FAMILIES),
        "reviewed_sample_count": len(reviewed),
        "positive_evidence_count": len(approved),
        "sample_precision": round(sample_precision, 6),
        "family_precision": family_precision,
        "full_guard_shadow_scope_count": len(scope),
        "unreviewed_shadow_candidate_count": sum(row["shadow_decision"] == "unreviewed_shadow_candidate" for row in scope),
        "reviewed_not_safe_count": sum(row["shadow_decision"] == "reviewed_not_safe_for_suppression" for row in scope),
        "minimum_activation_precision": MIN_ACTIVATION_PRECISION,
        "automatic_activation_allowed": activation_allowed,
        "candidate_generation": 0,
        "apply": 0,
        "database_changed": False,
        "source_changed": False,
        "output_changed": False,
    }
    spec = {
        "policy_name": "stable_output_no_reopen_shadow",
        "rule_version": RULE_VERSION,
        "status": "shadow_only",
        "intent": "Prevent legacy issue families from reopening a frozen stable output without stronger evidence.",
        "required_guards": [
            "candidate_score >= 0.75",
            "old == output == confirmed (canonical)",
            "confirmation_locked == 0",
            "needs_output_apply == 0",
            "segment is operationally closed by stable-baseline inheritance",
            "no source delta requiring retranslation",
            "validated pattern precision >= 0.95",
        ],
        "effect_when_eventually_activated": "suppress legacy reopen only; retain improvement backlog and never write output",
        "current_blocker": "observed precision below activation threshold",
        "observed_precision": round(sample_precision, 6),
        "required_precision": MIN_ACTIVATION_PRECISION,
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["spec"].write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in scope:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# V3 stable output no-reopen shadow policy",
        "",
        f"- Reviewed evidence: `{len(reviewed)}`",
        f"- Positive evidence: `{len(approved)}`",
        f"- Observed precision: `{sample_precision:.2%}`",
        f"- Required activation precision: `{MIN_ACTIVATION_PRECISION:.2%}`",
        f"- Full-guard shadow scope: `{len(scope)}`",
        f"- Automatic activation allowed: `{str(activation_allowed).lower()}`",
        "- Apply: `0`",
        "",
        "The rule remains shadow-only because the reviewed sample does not support broad automatic suppression.",
        "Its safe role is to prioritize further calibration evidence without changing lifecycle or output.",
        "",
        "## Precision by family",
        "",
    ]
    lines.extend(f"- `{family}`: `{value:.2%}`" for family, value in family_precision.items())
    lines.extend(["", "## Shadow decisions", ""])
    counts = Counter(row["shadow_decision"] for row in scope)
    lines.extend(f"- `{name}`: `{count}`" for name, count in counts.items())
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Measure a no-reopen policy in shadow mode.")
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    review_path = args.review.resolve() if args.review else latest_review()
    reviewed = load_review(review_path)
    settings = db.load_settings()
    with sqlite3.connect(f"file:{db.get_database_path(settings)}?mode=ro", uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        scope = collect_scope(conn, 1)
    paths = report_paths()
    summary = write_reports(paths, review_path, reviewed, scope)
    print("[v3-no-reopen-shadow] Completed")
    print(f"[v3-no-reopen-shadow] Precision: {summary['sample_precision']:.2%}")
    print(f"[v3-no-reopen-shadow] Scope: {summary['full_guard_shadow_scope_count']}")
    print(f"[v3-no-reopen-shadow] Activation allowed: {summary['automatic_activation_allowed']}")
    print(f"[v3-no-reopen-shadow] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
