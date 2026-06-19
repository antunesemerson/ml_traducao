from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_title_landed_adjective_spanish_es_base_same_semantic_decisions import (
    AGENT_KEY,
    QUEUE_STRATEGY,
    base_key,
    base_relation,
    english_family,
    fetch_base_titles,
    is_strict_final_accent_repair,
    latest_queue_run_id,
    parse_json,
    stem_relation,
)
from issue_title_landed_adjective_spanish_es_hint_audit import classify_hint


RULE_VERSION = "issue_title_landed_adjective_spanish_es_high_tier_strict_decisions_v1"
ALLOWED_TIERS = ("e_", "k_")


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_high_tier_strict_decisions_queue_{queue_run_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def fetch_rows(conn, *, queue_run_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue_rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
          AND agent_key = ?
          AND review_status != 'reviewed'
        ORDER BY queue_bucket, source_key, id
        """,
        (queue_run_id, AGENT_KEY),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for row in queue_rows:
        item = dict(row)
        evidence = parse_json(item.get("evidence_json"), {})
        item["repair_hint"] = str(evidence.get("repair_hint") or "")
        item["hint_class"], item["hint_reason"] = classify_hint(item)
        rows.append(item)

    base_titles = fetch_base_titles(conn, rows)
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        source_key = str(row.get("source_key") or "")
        base = base_titles.get(base_key(source_key))
        row["base_title"] = base
        row["base_relation"] = base_relation(base)
        row["stem_relation"] = stem_relation(row, base)
        row["english_family"] = english_family(str(row.get("english_text") or ""))
        row["block_reasons"] = []

        if not source_key.startswith(ALLOWED_TIERS):
            row["block_reasons"].append("tier_not_allowed")
        if not base:
            row["block_reasons"].append("missing_base")
        if row.get("hint_reason") != "high_tier_title_requires_context":
            row["block_reasons"].append(f"hint_reason:{row.get('hint_reason')}")
        if not is_strict_final_accent_repair(row.get("evidence_text"), row.get("repair_hint")):
            row["block_reasons"].append("not_strict_final_es_to_es_circumflex_repair")

        if row["block_reasons"]:
            blocked.append(row)
        else:
            selected.append(row)
    return selected, blocked


def write_outputs(
    *,
    decisions_path: Path,
    report_path: Path,
    queue_run_id: int,
    selected: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            base = row["base_title"] or {}
            handle.write(
                json.dumps(
                    {
                        "queue_run_id": queue_run_id,
                        "queue_item_id": row["id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "decision": "needs_repair",
                        "corrected_text": row["repair_hint"],
                        "notes": (
                            f"{RULE_VERSION}; base_key={base_key(str(row.get('source_key') or ''))}; "
                            f"base_relation={row.get('base_relation')}; stem_relation={row.get('stem_relation')}; "
                            f"english_family={row.get('english_family')}; base_old={base.get('old_text')}; "
                            f"base_en={base.get('english_text')}; base_es={base.get('spanish_text')}; "
                            f"current={row.get('evidence_text')}; english={row.get('english_text')}"
                        ),
                        "reviewer": "codex_landed_title_spanish_es_high_tier_strict",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    selected_families = Counter(str(row.get("english_family")) for row in selected)
    selected_relations = Counter(str(row.get("base_relation")) for row in selected)
    selected_stems = Counter(str(row.get("stem_relation")) for row in selected)
    selected_tiers = Counter(str(row.get("source_key") or "")[:2] for row in selected)
    blocked_reasons = Counter(";".join(row["block_reasons"]) for row in blocked)
    lines = [
        "Spanish -es high-tier strict final-accent title adjective decisions",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Selected rows: {len(selected):,}",
        f"Blocked rows: {len(blocked):,}",
        f"Decisions path: {decisions_path}",
        "",
        "Selected by tier:",
        *[f"- {key}: {value:,}" for key, value in selected_tiers.most_common()],
        "",
        "Selected by English suffix family:",
        *[f"- {key}: {value:,}" for key, value in selected_families.most_common()],
        "",
        "Selected by base relation:",
        *[f"- {key}: {value:,}" for key, value in selected_relations.most_common()],
        "",
        "Selected by stem relation:",
        *[f"- {key}: {value:,}" for key, value in selected_stems.most_common()],
        "",
        "Blocked by guard:",
        *[f"- {key}: {value:,}" for key, value in blocked_reasons.most_common()],
        "",
        "Selected rows:",
    ]
    for row in selected:
        base = row["base_title"] or {}
        lines.append(
            f"- item={row['id']} segment={row['segment_id']} {row.get('source_key')} "
            f"{row.get('evidence_text')} -> {row.get('repair_hint')} "
            f"({row.get('english_text')}; relation={row.get('base_relation')}; "
            f"stem={row.get('stem_relation')}; base_old={base.get('old_text')}; base_en={base.get('english_text')})"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked[:80]:
        base = row.get("base_title") or {}
        lines.append(
            f"- item={row['id']} {row.get('source_key')} blocked={';'.join(row['block_reasons'])} "
            f"{row.get('evidence_text')} -> {row.get('repair_hint')} "
            f"({row.get('english_text')}; base_old={base.get('old_text')}; base_en={base.get('english_text')})"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This records learning evidence only.",
            "- It does not write source/output, confirmations, lifecycle policies, or production artifacts.",
            "- The policy is limited to high-tier e/k title adjectives with strict final -és -> -ês repair.",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        selected, blocked = fetch_rows(conn, queue_run_id=selected_queue_run_id)

    decisions_path, report_path = report_paths(settings, selected_queue_run_id)
    write_outputs(
        decisions_path=decisions_path,
        report_path=report_path,
        queue_run_id=selected_queue_run_id,
        selected=selected,
        blocked=blocked,
    )

    print("[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Decisions generated")
    print(f"[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Queue run id: {selected_queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Selected rows: {len(selected):,}")
    print(f"[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Blocked rows: {len(blocked):,}")
    print(f"[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Decisions: {decisions_path}")
    print(f"[issue_title_landed_adjective_spanish_es_high_tier_strict_decisions] Report: {report_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "selected": len(selected),
        "blocked": len(blocked),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate high-tier strict final-accent Spanish -es title adjective repair decisions.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
