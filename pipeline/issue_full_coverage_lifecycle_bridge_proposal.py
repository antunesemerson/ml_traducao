from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_review_assisted_draft import english_hits, has_actual_mojibake, spanish_hits
from segment_state_snapshot import canonical_localization_text, protected_tokens_signature


RULE_VERSION = "issue_full_coverage_lifecycle_bridge_proposal_v1"
BRIDGE_NAME = "full_issue_coverage_lifecycle_bridge_v1"
BRIDGE_ACTION = "close_reopen_full_issue_coverage_lifecycle"

NON_CLOSURE_SOURCES = {
    "decision_pattern_checkpoint:carry_forward",
    "dynamic_concept_expression_guarded_checkpoint:exact",
    "short_label_release_checkpoint:carry_forward",
    "short_label_route_checkpoint:carry_forward",
    "semantic_short_label_pair_checkpoint:carry_forward",
    "high_issue_explained_checkpoint:carry_forward",
    "trigger_gender_role_lifecycle:carry_forward",
}

VISIBLE_MOJIBAKE_MARKERS = ("Ã", "Â", "Ð", "ð", "�")
VISIBLE_ENGLISH_TERMS = (
    "Victory",
    "Increase",
    "County",
    "Baron",
    "Opinion",
    "Force",
    "Partition",
    "Lowered",
    "Focus",
    "Unlocks",
    "Odds",
    "odds",
)
VISIBLE_BAD_PTBR_FRAGMENTS = (
    "Regal",
    "Enthusi",
    "Entusi",
    "não se #EMP me#! pode",
    "É sobrinh",
    "É net ",
    "É av ",
    "É bisav ",
    "herdeiro/a",
    "primo/prima",
)
NEGATIVE_REVIEW_DECISIONS = {
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
}


def latest_partial_coverage_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished partial coverage run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_full_coverage_lifecycle_bridge_proposal"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_full_coverage_lifecycle_bridge_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            bridge_status TEXT NOT NULL,
            source_partial_coverage_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            issue_count_counts_json TEXT,
            family_counts_json TEXT,
            source_counts_json TEXT,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_full_coverage_lifecycle_bridge_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            bridge_ready INTEGER NOT NULL DEFAULT 0,
            bridge_action TEXT NOT NULL,
            block_reason TEXT,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            issue_families_json TEXT,
            covered_families_json TEXT,
            coverage_sources_json TEXT,
            confirmation_label TEXT,
            final_state TEXT,
            evidence_preview TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_full_coverage_lifecycle_bridge_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_full_coverage_bridge_items_run
        ON ml_issue_full_coverage_lifecycle_bridge_items(run_id, bridge_ready, block_reason);

        CREATE INDEX IF NOT EXISTS idx_full_coverage_bridge_items_segment
        ON ml_issue_full_coverage_lifecycle_bridge_items(segment_id);
        """
    )


def fetch_run(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial coverage run not found: {run_id}")
    return dict(row)


def fetch_candidates(conn, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            state.review_state AS current_review_state,
            state.apply_state AS current_apply_state,
            state.needs_output_apply AS current_needs_output_apply,
            state.needs_reopen AS current_needs_reopen,
            state.confirmed_matches_output AS current_confirmed_matches_output,
            state.locked AS current_state_locked,
            confirmation.confirmed_text,
            confirmation.confirmation_label,
            confirmation.confirmation_source,
            confirmation.locked AS confirmation_locked,
            output.portuguese_text
        FROM ml_issue_partial_coverage_items item
        JOIN segment_state_items state
          ON state.run_id = item.segment_state_run_id
         AND state.segment_id = item.segment_id
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.coverage_state = 'full'
          AND item.total_issue_count > 0
        ORDER BY item.total_issue_count DESC, item.relative_path, item.source_line_number, item.source_key
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[int, str]:
    if row.get("state_group") != "pending":
        return 0, "state_not_pending"
    if row.get("final_state") != "reopen_auto_confirmed_autofix":
        return 0, "state_not_reopen_auto_confirmed_autofix"
    if row.get("current_review_state") != "auto_confirmed":
        return 0, "review_state_not_auto_confirmed"
    if int(row.get("current_needs_output_apply") or 0) != 0:
        return 0, "needs_output_apply"
    if int(row.get("current_needs_reopen") or 0) != 1:
        return 0, "needs_reopen_not_set"
    if int(row.get("confirmation_locked") or 0) != 0:
        return 0, "confirmation_locked"
    if int(row.get("blocked_issue_count") or 0) != 0:
        return 0, "blocked_issue_count_not_zero"
    if int(row.get("open_issue_count") or 0) != 0:
        return 0, "open_issue_count_not_zero"
    if int(row.get("covered_issue_count") or 0) != int(row.get("total_issue_count") or 0):
        return 0, "coverage_not_complete"
    reviewed_decisions = parse_json(row.get("reviewed_decisions_json"))
    negative_decisions = [
        decision
        for decision in sorted(NEGATIVE_REVIEW_DECISIONS)
        if int(reviewed_decisions.get(decision) or 0) > 0
    ]
    if negative_decisions:
        return 0, "negative_review_decision:" + ",".join(negative_decisions)

    confirmed = row.get("confirmed_text") or ""
    output = row.get("portuguese_text") or ""
    if not confirmed.strip() or not output.strip():
        return 0, "missing_confirmed_or_output_text"
    if canonical_localization_text(confirmed) != canonical_localization_text(output):
        return 0, "confirmed_output_canonical_mismatch"
    if protected_tokens_signature(confirmed) != protected_tokens_signature(output):
        return 0, "token_signature_mismatch"
    if has_actual_mojibake(output):
        return 0, "visible_mojibake_in_output"
    if any(marker in output for marker in VISIBLE_MOJIBAKE_MARKERS):
        return 0, "visible_mojibake_marker"
    spanish = spanish_hits(output)
    if spanish:
        return 0, "visible_spanish_residual:" + ",".join(spanish[:3])
    english = english_hits(output)
    if english:
        return 0, "visible_english_residual:" + ",".join(english[:3])
    english_terms = [term for term in VISIBLE_ENGLISH_TERMS if term in output]
    if english_terms:
        return 0, "visible_english_ui_term:" + ",".join(english_terms[:3])
    bad_ptbr = [fragment for fragment in VISIBLE_BAD_PTBR_FRAGMENTS if fragment in output]
    if bad_ptbr:
        return 0, "visible_bad_ptbr_fragment:" + ",".join(bad_ptbr[:3])

    sources = parse_json(row.get("coverage_sources_json"))
    if not sources:
        return 0, "missing_coverage_sources"
    unsafe_sources = sorted(source for source in sources if source in NON_CLOSURE_SOURCES)
    if unsafe_sources:
        return 0, "non_closure_grade_sources:" + ",".join(unsafe_sources[:3])
    families = parse_json(row.get("issue_families_json"))
    covered_families = parse_json(row.get("covered_families_json"))
    if families != covered_families:
        return 0, "family_coverage_mismatch"
    return 1, ""


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    partial_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "bridge_ready",
        "block_reason",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "total_issue_count",
        "covered_issue_count",
        "issue_families_json",
        "covered_families_json",
        "coverage_sources_json",
        "confirmation_label",
        "final_state",
        "evidence_preview",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Full Issue Coverage Lifecycle Bridge Proposal",
        f"Rule version: {RULE_VERSION}",
        f"Bridge: {BRIDGE_NAME}",
        f"Run id: {run_id}",
        f"Partial coverage run id: {partial_run['id']}",
        f"Ledger run id: {partial_run['ledger_run_id']}",
        f"Segment-state run id: {partial_run['segment_state_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready: {counts['ready']:,}",
        f"- Blocked: {counts['blocked']:,}",
        f"- Estimated closed gain: {counts['ready']:,}",
        "",
        "Issue count distribution:",
    ]
    for key, value in sorted(((key, value) for key, value in counts.items() if key.startswith("issues:")), key=lambda item: item[0]):
        lines.append(f"- {key.replace('issues:', '')}: {value:,}")
    lines.extend(["", "Families:"])
    for key, value in sorted(((key, value) for key, value in counts.items() if key.startswith("family:")), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key.replace('family:', '')}: {value:,}")
    lines.extend(["", "Coverage sources:"])
    for key, value in sorted(((key, value) for key, value in counts.items() if key.startswith("source:")), key=lambda item: (-item[1], item[0]))[:40]:
        lines.append(f"- {key.replace('source:', '')}: {value:,}")
    lines.extend(["", "Blocks:"])
    for key, value in sorted(((key, value) for key, value in counts.items() if key.startswith("block:")), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key.replace('block:', '')}: {value:,}")
    lines.extend(["", "Ready samples:"])
    for row in [item for item in rows if item["bridge_ready"]][:30]:
        lines.append(
            "- segment={segment_id} | {relative_path}:{source_line_number} | {source_key} | issues={total_issue_count} | {evidence_preview}".format(
                **row
            )
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, partial_coverage_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_run = partial_coverage_run_id or latest_partial_coverage_run_id(conn)
        partial_run = fetch_run(conn, selected_run)
        candidates = fetch_candidates(conn, selected_run)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = db.utc_now()
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_full_coverage_lifecycle_bridge_runs (
                rule_version, bridge_name, bridge_status,
                source_partial_coverage_run_id, source_ledger_run_id, source_segment_state_run_id,
                candidate_count, ready_count, blocked_count, estimated_closed_gain,
                production_release_allowed, started_at, updated_at
            )
            VALUES (?, ?, 'shadow', ?, ?, ?, 0, 0, 0, 0, 0, ?, ?)
            """,
            (
                RULE_VERSION,
                BRIDGE_NAME,
                int(partial_run["id"]),
                int(partial_run["ledger_run_id"]),
                int(partial_run["segment_state_run_id"]),
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        counts: Counter[str] = Counter()
        output_rows: list[dict[str, Any]] = []
        for row in candidates:
            ready, block_reason = classify(row)
            counts["ready" if ready else "blocked"] += 1
            counts[f"issues:{int(row.get('total_issue_count') or 0)}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            for family, amount in parse_json(row.get("issue_families_json")).items():
                counts[f"family:{family}"] += int(amount or 0)
            for source, amount in parse_json(row.get("coverage_sources_json")).items():
                counts[f"source:{source}"] += int(amount or 0)
            out = {
                "bridge_ready": ready,
                "block_reason": block_reason,
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "total_issue_count": int(row.get("total_issue_count") or 0),
                "covered_issue_count": int(row.get("covered_issue_count") or 0),
                "issue_families_json": row.get("issue_families_json") or "{}",
                "covered_families_json": row.get("covered_families_json") or "{}",
                "coverage_sources_json": row.get("coverage_sources_json") or "{}",
                "confirmation_label": row.get("confirmation_label") or "",
                "final_state": row.get("final_state") or "",
                "evidence_preview": (row.get("portuguese_text") or "")[:180].replace("\n", "\\n"),
            }
            output_rows.append(out)
            conn.execute(
                """
                INSERT INTO ml_issue_full_coverage_lifecycle_bridge_items (
                    run_id, partial_coverage_run_id, partial_coverage_item_id, ledger_run_id,
                    segment_state_run_id, segment_id, relative_path, source_key, source_line_number,
                    bridge_ready, bridge_action, block_reason, total_issue_count, covered_issue_count,
                    issue_families_json, covered_families_json, coverage_sources_json,
                    confirmation_label, final_state, evidence_preview, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(row["run_id"]),
                    int(row["id"]),
                    int(row["ledger_run_id"]),
                    int(row["segment_state_run_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    ready,
                    BRIDGE_ACTION if ready else "hold_full_coverage_bridge_candidate",
                    block_reason,
                    int(row.get("total_issue_count") or 0),
                    int(row.get("covered_issue_count") or 0),
                    row.get("issue_families_json") or "{}",
                    row.get("covered_families_json") or "{}",
                    row.get("coverage_sources_json") or "{}",
                    row.get("confirmation_label") or "",
                    row.get("final_state") or "",
                    (row.get("portuguese_text") or "")[:180].replace("\n", "\\n"),
                    now,
                ),
            )

        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            partial_run=partial_run,
            rows=output_rows,
            counts=counts,
        )
        conn.execute(
            """
            UPDATE ml_issue_full_coverage_lifecycle_bridge_runs
            SET candidate_count = ?,
                ready_count = ?,
                blocked_count = ?,
                estimated_closed_gain = ?,
                issue_count_counts_json = ?,
                family_counts_json = ?,
                source_counts_json = ?,
                block_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(output_rows),
                counts["ready"],
                counts["blocked"],
                counts["ready"],
                json.dumps({k.removeprefix("issues:"): v for k, v in counts.items() if k.startswith("issues:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k.removeprefix("family:"): v for k, v in counts.items() if k.startswith("family:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k.removeprefix("source:"): v for k, v in counts.items() if k.startswith("source:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k.removeprefix("block:"): v for k, v in counts.items() if k.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                db.utc_now(),
                db.utc_now(),
                run_id,
            ),
        )
        conn.commit()

    print("[issue_full_coverage_lifecycle_bridge_proposal] Proposal generated")
    print(f"[issue_full_coverage_lifecycle_bridge_proposal] Run id: {run_id}")
    print(f"[issue_full_coverage_lifecycle_bridge_proposal] Partial coverage run id: {selected_run}")
    print(f"[issue_full_coverage_lifecycle_bridge_proposal] Candidates: {len(output_rows):,}")
    print(f"[issue_full_coverage_lifecycle_bridge_proposal] Ready: {counts['ready']:,}")
    print(f"[issue_full_coverage_lifecycle_bridge_proposal] Blocked: {counts['blocked']:,}")
    print(f"[issue_full_coverage_lifecycle_bridge_proposal] Report: {txt_path}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run,
        "candidates": len(output_rows),
        "ready": counts["ready"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--partial-coverage-run-id", type=int)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
