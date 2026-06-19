from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from local_quality_validator import validate_text


RULE_VERSION = "issue_gender_longform_context_route_diagnostic_v1"
DIAGNOSTIC_RULE_VERSION = "issue_gender_dynamic_token_delegate_diagnostic_v1"
SOURCE_SUBPATTERN = "longform_gender_dynamic_context"

KINSHIP_ESOA_RE = re.compile(r"\b(?:av[oô]|net|irm[aã]o|tio|tia)\s*\[[^\]]*Custom\(\s*['\"]ES_OA", re.IGNORECASE)
SELECT_CSTRING_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
GENDER_TOKEN_RE = re.compile(r"\[[^\]]*Custom\(\s*['\"](ES_[A-Za-z]+)['\"]")
LOCAL_PLAYER_RE = re.compile(r"LocalPlayerString|IsLocalPlayer", re.IGNORECASE)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_longform_context_route_diagnostic_diag_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_dynamic_delegate_diagnostic_runs
        WHERE rule_version = ?
          AND candidate_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (DIAGNOSTIC_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No gender dynamic delegate diagnostic run found.")
    return int(row["id"])


def latest_segment_state_run_id(conn) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs").fetchone()
    return int(row["id"]) if row and row["id"] is not None else None


def fetch_candidates(conn, diagnostic_run_id: int, segment_state_run_id: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            d.id AS diagnostic_item_id,
            d.run_id AS diagnostic_run_id,
            d.segment_id,
            d.relative_path,
            d.source_key,
            d.source_line_number,
            d.queue_run_id,
            d.ledger_run_id,
            d.ledger_item_id,
            d.text_length,
            d.token_count,
            d.word_count,
            d.gender_methods,
            d.evidence_count,
            c.id AS confirmation_id,
            c.confirmed_text,
            c.locked AS confirmation_locked,
            o.portuguese_text AS output_text,
            st.run_id AS segment_state_run_id,
            st.final_state,
            st.confirmed_matches_output,
            st.needs_output_apply,
            st.reasons_json AS segment_state_reasons_json
        FROM ml_issue_gender_dynamic_delegate_diagnostic_items d
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = d.segment_id
              ORDER BY c2.updated_at DESC, c2.confirmed_at DESC, c2.id DESC
              LIMIT 1
          )
        LEFT JOIN output_segments o
          ON o.segment_id = d.segment_id
        LEFT JOIN segment_state_items st
          ON st.segment_id = d.segment_id
         AND st.run_id = ?
        WHERE d.run_id = ?
          AND d.subpattern = ?
        ORDER BY d.relative_path, d.source_line_number, d.source_key, d.segment_id
        """,
        (segment_state_run_id, diagnostic_run_id, SOURCE_SUBPATTERN),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_route_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            potential_false_reopen_count INTEGER NOT NULL DEFAULT 0,
            visible_residual_count INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            next_action_counts_json TEXT,
            validation_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_route_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            route_key TEXT NOT NULL,
            next_action TEXT NOT NULL,
            leverage TEXT NOT NULL,
            candidate_close_ready INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            validation_issue_codes TEXT,
            validation_issue_matches_json TEXT,
            final_state TEXT,
            confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            needs_output_apply INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            gender_methods TEXT,
            has_select_cstring INTEGER NOT NULL DEFAULT 0,
            has_local_player INTEGER NOT NULL DEFAULT 0,
            has_kinship_es_oa INTEGER NOT NULL DEFAULT 0,
            current_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_longform_context_route_runs(id) ON DELETE CASCADE
        )
        """
    )


def validation_summary(text: str) -> tuple[list[str], dict[str, list[str]]]:
    result = validate_text(text)
    issues = result.get("issues") if isinstance(result, dict) else []
    codes: list[str] = []
    matches: dict[str, list[str]] = {}
    if not isinstance(issues, list):
        return codes, matches
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "")
        severity = str(issue.get("severity") or "")
        if not code:
            continue
        codes.append(f"{severity}:{code}" if severity else code)
        raw_matches = issue.get("matches")
        if isinstance(raw_matches, list):
            matches[code] = [str(value) for value in raw_matches[:12]]
    return sorted(set(codes)), matches


def route_for(row: dict[str, Any], codes: list[str]) -> tuple[str, str, str, int, str]:
    relative_path = row.get("relative_path") or ""
    text = row.get("confirmed_text") or row.get("output_text") or ""
    final_state = row.get("final_state") or ""
    confirmed_matches_output = int(row.get("confirmed_matches_output") or 0)
    needs_output_apply = int(row.get("needs_output_apply") or 0)
    has_visible_residual = any(
        code.endswith(":spanish_residue")
        or code.endswith(":spanish_punctuation")
        or code.endswith(":spanish_residue_in_literal")
        or code.endswith(":mojibake")
        for code in codes
    )
    base_reasons: list[str] = []
    if not final_state.startswith("reopen_auto_confirmed"):
        base_reasons.append("not_auto_confirmed_reopen")
    if not confirmed_matches_output:
        base_reasons.append("confirmation_output_mismatch")
    if needs_output_apply:
        base_reasons.append("needs_output_apply")

    if has_visible_residual:
        return (
            "visible_residual_longform_repair",
            "route_spanish_residual_or_mojibake_microrepair",
            "medium",
            0,
            ";".join(base_reasons + ["visible_residual"]),
        )
    if KINSHIP_ESOA_RE.search(text):
        return (
            "kinship_gender_suffix_boundary_repair",
            "build_kinship_gender_suffix_boundary_microrepair",
            "medium",
            0,
            ";".join(base_reasons + ["kinship_es_oa_boundary"]),
        )
    if "single_combat_events" in relative_path:
        ready = 1 if not base_reasons else 0
        return (
            "single_combat_gender_surface_false_reopen_candidate",
            "build_single_combat_gender_surface_lifecycle_bridge",
            "high",
            ready,
            ";".join(base_reasons),
        )
    if "tutorial_objectives" in relative_path:
        ready = 1 if not base_reasons else 0
        return (
            "tutorial_help_gender_surface_false_reopen_candidate",
            "build_tutorial_help_gender_surface_lifecycle_bridge",
            "medium",
            ready,
            ";".join(base_reasons),
        )
    if "laamp_contract_schemes" in relative_path:
        return (
            "laamp_contract_longform_gender_context",
            "route_laamp_contract_context_composer",
            "low",
            0,
            ";".join(base_reasons + ["needs_context_composer"]),
        )
    if "ES_EA" in (row.get("gender_methods") or ""):
        return (
            "demonstrative_article_gender_boundary_context",
            "route_article_gender_boundary_context_microagent",
            "low",
            0,
            ";".join(base_reasons + ["demonstrative_article_context"]),
        )
    ready = 1 if not base_reasons else 0
    return (
        "generic_longform_gender_surface_false_reopen_candidate",
        "build_generic_longform_gender_surface_recheck",
        "medium" if ready else "low",
        ready,
        ";".join(base_reasons),
    )


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    text = row.get("confirmed_text") or row.get("output_text") or ""
    codes, matches = validation_summary(text)
    route_key, next_action, leverage, candidate_close_ready, block_reason = route_for(row, codes)
    methods = ",".join(sorted(set(GENDER_TOKEN_RE.findall(text))))
    return {
        "diagnostic_item_id": int(row["diagnostic_item_id"]),
        "diagnostic_run_id": int(row["diagnostic_run_id"]),
        "segment_state_run_id": row.get("segment_state_run_id"),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "source_line_number": int(row.get("source_line_number") or 0),
        "route_key": route_key,
        "next_action": next_action,
        "leverage": leverage,
        "candidate_close_ready": candidate_close_ready,
        "block_reason": block_reason,
        "validation_issue_codes": ",".join(codes),
        "validation_issue_matches_json": json.dumps(matches, ensure_ascii=False, sort_keys=True),
        "final_state": row.get("final_state") or "",
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "text_length": int(row.get("text_length") or len(text)),
        "token_count": int(row.get("token_count") or 0),
        "word_count": int(row.get("word_count") or 0),
        "gender_methods": methods or row.get("gender_methods") or "",
        "has_select_cstring": 1 if SELECT_CSTRING_RE.search(text) else 0,
        "has_local_player": 1 if LOCAL_PLAYER_RE.search(text) else 0,
        "has_kinship_es_oa": 1 if KINSHIP_ESOA_RE.search(text) else 0,
        "current_text": text,
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    diagnostic_run_id: int,
    segment_state_run_id: int | None,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "route_key",
        "next_action",
        "leverage",
        "candidate_close_ready",
        "block_reason",
        "validation_issue_codes",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "final_state",
        "confirmed_matches_output",
        "needs_output_apply",
        "text_length",
        "token_count",
        "word_count",
        "gender_methods",
        "has_select_cstring",
        "has_local_player",
        "has_kinship_es_oa",
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

    by_route = Counter(row["route_key"] for row in rows)
    by_action = Counter(row["next_action"] for row in rows)
    by_validation = Counter()
    for row in rows:
        for code in (row["validation_issue_codes"] or "").split(","):
            if code:
                by_validation[code] += 1
    ready_rows = [row for row in rows if row["candidate_close_ready"]]
    residual_rows = [row for row in rows if row["route_key"] == "visible_residual_longform_repair"]

    lines = [
        "Issue gender longform context route diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Segment-state run id: {segment_state_run_id or '<none>'}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Potential false-reopen close candidates: {len(ready_rows):,}",
        f"- Visible residual/context repair candidates: {len(residual_rows):,}",
        "",
        "By route:",
        *[f"- {key}: {value:,}" for key, value in by_route.most_common()],
        "",
        "By next action:",
        *[f"- {key}: {value:,}" for key, value in by_action.most_common()],
        "",
        "Validation issue codes:",
        *([f"- {key}: {value:,}" for key, value in by_validation.most_common()] or ["- none"]),
        "",
        "Potential close samples:",
    ]
    if ready_rows:
        for row in ready_rows[:25]:
            lines.extend(
                [
                    f"- {row['route_key']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                    f"  text: {short(row['current_text'], 220)}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "Repair/blocker samples:"])
    blocked = [row for row in rows if not row["candidate_close_ready"]]
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['route_key']} | {row['block_reason'] or 'context_review'} | "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                    f"  validation: {row['validation_issue_codes'] or 'none'}",
                    f"  text: {short(row['current_text'], 220)}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Diagnostic only: no production run, no confirmations, no source/output writes.",
            "- Candidate close ready means route-level evidence, not automatic lifecycle closure.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None, segment_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        selected_segment_state_run_id = segment_state_run_id if segment_state_run_id is not None else latest_segment_state_run_id(conn)
        source_rows = fetch_candidates(conn, selected_diagnostic_run_id, selected_segment_state_run_id)
        rows = [evaluate(row) for row in source_rows]
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_diagnostic_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        route_counts = Counter(row["route_key"] for row in rows)
        action_counts = Counter(row["next_action"] for row in rows)
        validation_counts = Counter()
        for row in rows:
            for code in (row["validation_issue_codes"] or "").split(","):
                if code:
                    validation_counts[code] += 1
        potential_false_reopen_count = sum(1 for row in rows if row["candidate_close_ready"])
        visible_residual_count = int(route_counts["visible_residual_longform_repair"])
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_longform_context_route_runs (
                rule_version,
                diagnostic_run_id,
                segment_state_run_id,
                candidate_count,
                potential_false_reopen_count,
                visible_residual_count,
                route_counts_json,
                next_action_counts_json,
                validation_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_diagnostic_run_id,
                selected_segment_state_run_id,
                len(rows),
                potential_false_reopen_count,
                visible_residual_count,
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(validation_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_gender_longform_context_route_items (
                    run_id,
                    diagnostic_item_id,
                    diagnostic_run_id,
                    segment_state_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    route_key,
                    next_action,
                    leverage,
                    candidate_close_ready,
                    block_reason,
                    validation_issue_codes,
                    validation_issue_matches_json,
                    final_state,
                    confirmed_matches_output,
                    needs_output_apply,
                    text_length,
                    token_count,
                    word_count,
                    gender_methods,
                    has_select_cstring,
                    has_local_player,
                    has_kinship_es_oa,
                    current_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["diagnostic_item_id"],
                    row["diagnostic_run_id"],
                    row["segment_state_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["route_key"],
                    row["next_action"],
                    row["leverage"],
                    row["candidate_close_ready"],
                    row["block_reason"],
                    row["validation_issue_codes"],
                    row["validation_issue_matches_json"],
                    row["final_state"],
                    row["confirmed_matches_output"],
                    row["needs_output_apply"],
                    row["text_length"],
                    row["token_count"],
                    row["word_count"],
                    row["gender_methods"],
                    row["has_select_cstring"],
                    row["has_local_player"],
                    row["has_kinship_es_oa"],
                    row["current_text"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            diagnostic_run_id=selected_diagnostic_run_id,
            segment_state_run_id=selected_segment_state_run_id,
            rows=rows,
        )
        conn.commit()

    print(f"Gender longform context route diagnostic: {run_id}")
    print(f"Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"Segment-state run id: {selected_segment_state_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Potential false-reopen close candidates: {potential_false_reopen_count}")
    print(f"Visible residual/context repair candidates: {visible_residual_count}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "segment_state_run_id": selected_segment_state_run_id,
        "candidate_count": len(rows),
        "potential_false_reopen_count": potential_false_reopen_count,
        "visible_residual_count": visible_residual_count,
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route longform gender dynamic context candidates into reusable microagent lanes.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--segment-state-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id, segment_state_run_id=args.segment_state_run_id)
