from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_semantic_review_explained_checkpoint_v1"
POLICY_NAME = "semantic_review_explained_by_specific_or_ui_evidence_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_semantic_review_router"
SUBPOLICY_SPECIFIC = "semantic_review_explained_by_specific_microissues"
SUBPOLICY_UI_MACRO = "semantic_review_ui_macro_stack_safe"
SUBPOLICY_UI_COMMAND = "semantic_review_ui_command_tooltip_safe"
SUBPOLICY_PURE_NO_TOKEN = "semantic_review_short_label_pure_no_token_nominal_safe"
SUBPOLICY_AUTOFIX_UNKNOWN_SURFACE = "semantic_review_autofix_unknown_surface_safe"
CHECKPOINT_ACTION = "clear_semantic_review_after_specific_or_ui_evidence_shadow"


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
        raise RuntimeError("No partial coverage run found.")
    return int(row["id"])


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_review_explained_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_review_explained_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_families_json TEXT,
            coverage_sources_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            current_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_semantic_review_explained_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_review_explained_checkpoint_run_{partial_coverage_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_candidates(conn, *, partial_coverage_run_id: int) -> tuple[int, list[dict[str, Any]]]:
    run = conn.execute(
        "SELECT ledger_run_id FROM ml_issue_partial_coverage_runs WHERE id = ?",
        (partial_coverage_run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_coverage_run_id}")
    ledger_run_id = int(run["ledger_run_id"])
    rows = conn.execute(
        """
        SELECT
            pci.*,
            seg.english_text,
            seg.spanish_text,
            conf.confirmed_text AS current_text
        FROM ml_issue_partial_coverage_items pci
        JOIN source_segments seg ON seg.id = pci.segment_id
        LEFT JOIN segment_confirmations conf
          ON conf.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = pci.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE pci.run_id = ?
          AND pci.coverage_state = 'partial'
          AND pci.open_families_json = '{"semantic_review_router": 1}'
        ORDER BY pci.segment_id, pci.id
        """,
        (partial_coverage_run_id,),
    ).fetchall()
    return ledger_run_id, [dict(row) for row in rows]


def fetch_semantic_ledger_item(conn, *, ledger_run_id: int, segment_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND issue_family = 'semantic_review_router'
        ORDER BY id
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return int(row["id"]) if row else None


def classify(row: dict[str, Any], *, ledger_item_id: int | None) -> tuple[int, str, str]:
    if ledger_item_id is None:
        return 0, "missing_semantic_ledger_item", "semantic_review_unclassified"
    if int(row.get("open_issue_count") or 0) != 1:
        return 0, "other_open_issues_present", "semantic_review_unclassified"
    if int(row.get("blocked_issue_count") or 0) != 0:
        return 0, "segment_has_blocked_issue_items", "semantic_review_unclassified"

    source_key = row.get("source_key") or ""
    text = row.get("current_text") or ""
    covered_families = parse_json_dict(row.get("covered_families_json"))
    coverage_sources = row.get("coverage_sources_json") or ""

    if source_key == "MY_REALM_WINDOW_BOOKMARK_SUBJECTS_TT":
        return 0, "visible_subjects_label_needs_ptbr_style_review", "semantic_review_visible_label_blocked"

    if source_key == "GOTO_PLAYER_ACTIVITY" and "Clique para selecionar" in text:
        return 1, "", SUBPOLICY_UI_COMMAND

    if "autofix_unknown_surface_checkpoint" in coverage_sources:
        if "autofix_unknown_microagent" not in covered_families:
            return 0, "autofix_surface_source_without_autofix_coverage", SUBPOLICY_AUTOFIX_UNKNOWN_SURFACE
        key_lower = source_key.lower()
        ui_surface = (
            "@warning_icon!" in text
            or "#X" in text
            or "tooltip" in key_lower
            or key_lower.endswith("_tt")
            or "confirm" in key_lower
        )
        if not ui_surface:
            return 0, "autofix_surface_without_strong_ui_signal", SUBPOLICY_AUTOFIX_UNKNOWN_SURFACE
        if len(text) > 520:
            return 0, "autofix_surface_text_too_long", SUBPOLICY_AUTOFIX_UNKNOWN_SURFACE
        if any(marker in text for marker in ("Huh", "#bold No", "¿", "¡", "«", "»")):
            return 0, "autofix_surface_visible_bad_marker", SUBPOLICY_AUTOFIX_UNKNOWN_SURFACE
        return 1, "", SUBPOLICY_AUTOFIX_UNKNOWN_SURFACE

    if source_key == "activity_hunt_special_type_bar_segment_tt_0":
        required = (
            "$mpo_nerge.0110.t$",
            "$activity_hunt_special_type_bar_segment_tt_0_host$",
            "$activity_hunt_special_type_bar_segment_tt_0_flavor$",
        )
        if all(token in text for token in required):
            return 1, "", SUBPOLICY_UI_MACRO
        return 0, "macro_stack_not_recognized", SUBPOLICY_UI_MACRO

    if "short_label_pure_no_token_checkpoint" in coverage_sources:
        if "short_label_style_microagent" not in covered_families:
            return 0, "pure_no_token_source_without_short_label_coverage", SUBPOLICY_PURE_NO_TOKEN
        if any(marker in text for marker in ("[", "]", "$", "{", "}")):
            return 0, "pure_no_token_text_has_token_marker", SUBPOLICY_PURE_NO_TOKEN
        if text.startswith(('"', '\\"')) or text.endswith(('"', '\\"')):
            return 0, "pure_no_token_text_has_quote_boundary", SUBPOLICY_PURE_NO_TOKEN
        if len(text) > 70:
            return 0, "pure_no_token_text_too_long", SUBPOLICY_PURE_NO_TOKEN
        return 1, "", SUBPOLICY_PURE_NO_TOKEN

    specific_families = {
        "dynamic_ck3_expression_microagent",
        "spanish_residual_microagent",
        "gender_token_microagent",
        "short_label_style_microagent",
    }
    if specific_families.issubset(set(covered_families)):
        return 1, "", SUBPOLICY_SPECIFIC

    return 0, "semantic_review_not_explained_by_safe_subpolicy", "semantic_review_unclassified"


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    partial_coverage_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "subpolicy_name",
        "total_issue_count",
        "covered_issue_count",
        "blocked_issue_count",
        "open_issue_count",
        "current_text",
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
        "Issue semantic-review explained checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Partial coverage run id: {partial_coverage_run_id}",
        f"Policy: {POLICY_NAME}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "By subpolicy:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("subpolicy:")],
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"subpolicy={row['subpolicy_name']} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row.get('current_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint clears only the generic semantic-review router item.",
            "- It does not write source/output and does not promote production apply by itself.",
            "- Visible semantic label uncertainty remains blocked for human or future microagent review.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, partial_coverage_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_run_id = partial_coverage_run_id or latest_partial_coverage_run_id(conn)
        ledger_run_id, source_rows = fetch_candidates(conn, partial_coverage_run_id=selected_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            ledger_item_id = fetch_semantic_ledger_item(
                conn,
                ledger_run_id=ledger_run_id,
                segment_id=int(source["segment_id"]),
            )
            allowed, block_reason, subpolicy_name = classify(source, ledger_item_id=ledger_item_id)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"subpolicy:{subpolicy_name}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "partial_coverage_item_id": source["id"],
                    "ledger_item_id": ledger_item_id or 0,
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": subpolicy_name,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "total_issue_count": source["total_issue_count"],
                    "covered_issue_count": source["covered_issue_count"],
                    "blocked_issue_count": source["blocked_issue_count"],
                    "open_issue_count": source["open_issue_count"],
                    "covered_families_json": source.get("covered_families_json") or "{}",
                    "coverage_sources_json": source.get("coverage_sources_json") or "{}",
                    "english_text": source.get("english_text") or "",
                    "spanish_text": source.get("spanish_text") or "",
                    "current_text": source.get("current_text") or "",
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_semantic_review_explained_runs (
                rule_version,
                policy_name,
                policy_status,
                partial_coverage_run_id,
                ledger_run_id,
                candidate_count,
                allowed_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                selected_run_id,
                ledger_run_id,
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_semantic_review_explained_items (
                    run_id,
                    partial_coverage_run_id,
                    partial_coverage_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    agent_key,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    total_issue_count,
                    covered_issue_count,
                    blocked_issue_count,
                    open_issue_count,
                    covered_families_json,
                    coverage_sources_json,
                    english_text,
                    spanish_text,
                    current_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_run_id,
                    row["partial_coverage_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["total_issue_count"],
                    row["covered_issue_count"],
                    row["blocked_issue_count"],
                    row["open_issue_count"],
                    row["covered_families_json"],
                    row["coverage_sources_json"],
                    row["english_text"],
                    row["spanish_text"],
                    row["current_text"],
                    now,
                ),
            )
        conn.commit()
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            partial_coverage_run_id=selected_run_id,
            rows=classified,
            counts=counts,
        )

    print("[issue_semantic_review_explained_checkpoint] Checkpoint generated")
    print(f"[issue_semantic_review_explained_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_semantic_review_explained_checkpoint] Run id: {run_id}")
    print(f"[issue_semantic_review_explained_checkpoint] Partial coverage run id: {selected_run_id}")
    print(f"[issue_semantic_review_explained_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_semantic_review_explained_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_semantic_review_explained_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_semantic_review_explained_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run_id,
        "candidate_count": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clear semantic-review router items explained by safe evidence.")
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
