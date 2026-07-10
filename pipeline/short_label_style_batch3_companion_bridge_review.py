from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


READY_DECISIONS = {
    "companion_ready_semantic_short_label",
    "companion_ready_semantic_context_confirmed",
    "companion_ready_false_reopen_pair",
}
LIFECYCLE_BATCH_DECISIONS = {
    "lifecycle_ready_plain_noop",
    "lifecycle_ready_compact_ui_label",
    "lifecycle_ready_short_phrase",
}
DYNAMIC_RE = re.compile(
    r"Select_CString|Custom\(|(?:^|[.\[])\s*Get[A-Za-z_]*\b|Concept\(|ScriptValue\(",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(r"culture|religion|title|nicknames?|traits?|laws?|accolades?", re.IGNORECASE)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def classify(
    *,
    diagnostic: dict[str, Any],
    batch: dict[str, Any],
    state: dict[str, Any] | None,
    issue_rows: list[dict[str, Any]],
) -> tuple[str, str, bool]:
    families = set(diagnostic["open_issue_families"])
    kinds = set(diagnostic.get("open_issue_kinds", []))
    current = str(batch.get("current_text") or "")
    english = str(batch.get("english_text") or "")
    relative_path = str(batch.get("relative_path") or "")
    issue_text = " ".join(sorted(families | kinds)).lower()

    if not state or state["state_group"] != "pending":
        return "blocked_uncertain", "segment is not pending in the requested segment-state run", False
    if int(state["needs_output_apply"] or 0) != 0:
        return "blocked_uncertain", "segment still needs output apply", False
    if int(state["confirmed_matches_output"] or 0) != 1:
        return "blocked_uncertain", "confirmation/output mismatch blocks companion closure", False
    if "spanish_residual_microagent" in families or "spanish_residue" in issue_text or "spanish_literal" in issue_text:
        return "needs_residual_repair", "open Spanish residual issue indicates real repair risk", False
    if "english_residual_microagent" in families or "english" in issue_text:
        return "needs_residual_repair", "open English residual issue indicates real repair risk", False
    if DYNAMIC_RE.search(current) or DYNAMIC_RE.search(english) or "dynamic" in issue_text or "expression" in issue_text:
        return "needs_dynamic_expression_agent", "dynamic CK3 expression markers require expression review", False
    if DOMAIN_RE.search(relative_path):
        return "needs_domain_context", "domain-sensitive path needs policy/context before closure", False
    if "gender_token_microagent" in families or "gender_token" in issue_text:
        return "needs_new_microagent", "gender-token companion pattern needs a governed gender-aware microagent", False
    if len(families - {"short_label_style_microagent", "semantic_review_router", "autofix_unknown_microagent"}) > 0:
        return "blocked_uncertain", "additional companion families remain outside this bridge", False
    if "semantic_review_router" in families and families <= {"short_label_style_microagent", "semantic_review_router"}:
        return (
            "companion_ready_semantic_short_label",
            "semantic router and short-label issue form a clean false-reopen pair",
            True,
        )
    if "autofix_unknown_microagent" in families and families <= {"short_label_style_microagent", "autofix_unknown_microagent"}:
        return (
            "companion_ready_false_reopen_pair",
            "autofix/short-label pair appears governed as false reopen",
            True,
        )
    return "blocked_uncertain", "companion evidence is not sufficient for governed closure", False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostic-jsonl", required=True, type=Path)
    parser.add_argument("--batch3-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    args = parser.parse_args()

    diagnostic_rows = read_jsonl(db.project_path(args.diagnostic_jsonl))
    batch_rows = read_jsonl(db.project_path(args.batch3_jsonl))
    companion_rows = [row for row in diagnostic_rows if row.get("recommended_route") == "companion_bridge"]
    batch_by_id = {int(row["segment_id"]): row for row in batch_rows}
    segment_ids = [int(row["segment_id"]) for row in companion_rows]
    if not segment_ids:
        raise RuntimeError("No companion_bridge rows found")
    placeholders = ",".join("?" for _ in segment_ids)

    conn = db.connect()
    state_by_segment = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, final_state, state_group, needs_output_apply, confirmed_matches_output, needs_reopen
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            """,
            (args.segment_state_run_id, *segment_ids),
        ).fetchall()
    }
    issues_by_segment: dict[int, list[dict[str, Any]]] = {segment_id: [] for segment_id in segment_ids}
    for row in conn.execute(
        f"""
        SELECT segment_id, issue_family, issue_kind, issue_severity, evidence_text
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        ORDER BY segment_id, issue_family, issue_kind
        """,
        (args.ledger_run_id, *segment_ids),
    ).fetchall():
        issues_by_segment[int(row["segment_id"])].append(dict(row))

    reviewed = []
    for diagnostic in companion_rows:
        segment_id = int(diagnostic["segment_id"])
        batch = batch_by_id[segment_id]
        decision, notes, lifecycle_later = classify(
            diagnostic=diagnostic,
            batch=batch,
            state=state_by_segment.get(segment_id),
            issue_rows=issues_by_segment.get(segment_id, []),
        )
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": diagnostic["key"],
                "relative_path": diagnostic["relative_path"],
                "batch3_decision": diagnostic["batch3_decision"],
                "open_issue_families": diagnostic["open_issue_families"],
                "open_issue_kinds": diagnostic.get("open_issue_kinds", []),
                "companion_decision": decision,
                "companion_subpolicy": decision.replace("companion_ready_", "").replace("needs_", ""),
                "requires_lifecycle_later": lifecycle_later,
                "requires_apply_later": False,
                "corrected_text": "",
                "current_text": batch.get("current_text", ""),
                "english_text": batch.get("english_text", ""),
                "notes": notes,
            }
        )
    conn.close()

    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    jsonl_path = reports_dir / f"{stamp}_short_label_style_batch3_companion_bridge_review.jsonl"
    txt_path = reports_dir / f"{stamp}_short_label_style_batch3_companion_bridge_review.txt"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )

    decision_counts = Counter(row["companion_decision"] for row in reviewed)
    ready_count = sum(count for decision, count in decision_counts.items() if decision in READY_DECISIONS)
    combo_counts = Counter(" + ".join(row["open_issue_families"]) for row in reviewed)
    lines = [
        "Short-label style batch3 companion bridge review",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"ledger_run_id: {args.ledger_run_id}",
        f"diagnostic_jsonl: {args.diagnostic_jsonl}",
        f"batch3_jsonl: {args.batch3_jsonl}",
        f"reviewed: {len(reviewed)}",
        f"companion_ready_total: {ready_count}",
        "",
        "Counts by companion_decision:",
    ]
    for decision, count in decision_counts.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(["", "Top family combinations:"])
    for combo, count in combo_counts.most_common():
        lines.append(f"- {combo}: {count}")
    lines.extend(
        [
            "",
            "Recommendation:",
            "- Do not prepare a companion lifecycle bridge from this review: companion_ready_* is below 30.",
            "- Route Spanish residual combinations to residual repair first, and route gender-token combinations to a gender-aware companion/microagent policy before any lifecycle closure.",
            "",
            "Safety confirmation: read-only review only; no production, no apply, no lifecycle, no segment-state, no confirmations, no reindex, no training/model promotion, no source/output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"reviewed={len(reviewed)}")
    print(f"companion_ready_total={ready_count}")
    print(f"decision_counts={dict(decision_counts)}")
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
