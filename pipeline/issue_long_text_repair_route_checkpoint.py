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
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_long_text_repair_route_checkpoint_v1"
POLICY_NAME = "long_text_repair_route_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "long_text_repair_router"
CHECKPOINT_ACTION = "stage_long_text_repair_route_shadow"

SELECT_CSTRING_RE = re.compile(
    r"Select_CString\(\s*([^,]+?)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
GLOSSARY_RE = re.compile(r"Glossary\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", re.IGNORECASE)
CONCEPT_RE = re.compile(r"Concept\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", re.IGNORECASE)

SPANISH_MARKERS = (
    " quiera",
    " de la buena",
    " del buen",
    " otra gorrona",
    " otro gorr",
    " la guerrera",
    " el guerrero",
    " esa mujer",
    " ese hombre",
    " rebelion heiji",
    " rebelión heiji",
    " los efectos",
    " cuanto mas",
    " cuanto más",
    " auténtico héroe",
    " auténtica heroína",
    " autentico heroe",
    " autentica heroina",
)

MOJIBAKE_MARKERS = ("Ã", "�", "ï¿½")


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        WHERE run.finished_at IS NOT NULL
          AND decision.queue_bucket = 'long_text_composer_blocker'
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY run.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No long_text_composer_blocker decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_repair_route_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            composition_ready_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_repair_route_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            normalized_decision TEXT NOT NULL,
            reasons_json TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_repair_route_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_repair_route_checkpoint_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.id AS decision_id,
            decision.queue_item_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.agent_key,
            decision.issue_family,
            decision.issue_kind,
            decision.queue_bucket,
            decision.normalized_decision,
            decision.corrected_text,
            decision.notes,
            queue.confirmed_text,
            queue.evidence_text,
            queue.english_text,
            queue.spanish_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND decision.queue_bucket = 'long_text_composer_blocker'
        ORDER BY decision.id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def literal_payload_skeleton(text: str) -> str:
    def select_repl(match: re.Match[str]) -> str:
        condition = " ".join(match.group(1).split())
        return f"Select_CString({condition},'<lit>','<lit>')"

    def glossary_repl(match: re.Match[str]) -> str:
        key = match.group(2)
        return f"Glossary('<lit>','{key}')"

    def concept_repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return f"Concept('{key}','<lit>')"

    text = SELECT_CSTRING_RE.sub(select_repl, text)
    text = GLOSSARY_RE.sub(glossary_repl, text)
    text = CONCEPT_RE.sub(concept_repl, text)
    return text


def token_status(current: str, corrected: str) -> str:
    if structural_tokens(current) == structural_tokens(corrected):
        return "same_structural_tokens"
    if structural_tokens(literal_payload_skeleton(current)) == structural_tokens(literal_payload_skeleton(corrected)):
        return "dynamic_literal_payload_only"
    return "structural_token_change_review_required"


def has_residual(text: str) -> list[str]:
    lowered = f" {text.casefold()} "
    return [marker.strip() for marker in SPANISH_MARKERS if marker in lowered]


def has_mojibake(text: str) -> bool:
    return any(marker in text for marker in MOJIBAKE_MARKERS)


def classify_route(row: dict[str, Any], current: str, corrected: str) -> str:
    notes = (row.get("notes") or "").casefold()
    current_low = current.casefold()
    corrected_low = corrected.casefold()

    if "guillemet" in notes or "quote" in notes or "«" in current or "»" in current:
        return "quote_surface_normalization"
    if "select_cstring" in notes or (
        "select_cstring" in current_low and "select_cstring" in corrected_low and current != corrected
    ):
        return "spanish_select_cstring_literal"
    if "getherhim" in notes or "getherhim" in corrected_low or "ele/ela" in current_low:
        return "object_pronoun_token_repair"
    if "lexical gender" in notes or "campon" in current_low or "aventureir" in current_low:
        return "lexical_gender_split"
    if "glossary" in notes or "glossary(" in current_low:
        return "glossary_visible_label_translation"
    if "final paragraph" in notes or "los efectos" in current_low:
        return "concept_paragraph_spanish_residual"
    if "certez" in current_low:
        return "gender_suffix_invariant_word_repair"
    if "] ," in current or "space before" in notes:
        return "surface_spacing_normalization"
    return "long_text_repair_unclassified"


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("confirmed_text") or row.get("evidence_text") or ""
    corrected = (row.get("corrected_text") or "").strip()
    normalized_decision = row.get("normalized_decision") or ""
    route = "composition_ready" if normalized_decision == "composition_ready" else "long_text_repair_unclassified"
    reasons: list[str] = []

    if normalized_decision == "composition_ready":
        return {
            "checkpoint_allowed": 0,
            "block_reason": "composition_ready_observation_not_repair_candidate",
            "repair_route": route,
            "token_status": "not_applicable",
            "corrected_text": "",
            "reasons": ["composition_ready"],
        }

    if not current.strip():
        return {
            "checkpoint_allowed": 0,
            "block_reason": "missing_current_text",
            "repair_route": route,
            "token_status": "missing_text",
            "corrected_text": corrected,
            "reasons": ["missing_current_text"],
        }
    if not corrected or corrected == current:
        return {
            "checkpoint_allowed": 0,
            "block_reason": "no_corrected_text_delta",
            "repair_route": route,
            "token_status": "no_text_delta",
            "corrected_text": corrected,
            "reasons": ["no_corrected_text_delta"],
        }

    route = classify_route(row, current, corrected)
    status = token_status(current, corrected)
    residual = has_residual(corrected)
    if residual:
        reasons.append("residual_after_repair:" + ",".join(residual[:6]))
    if has_mojibake(corrected):
        reasons.append("mojibake_after_repair")

    if reasons:
        return {
            "checkpoint_allowed": 0,
            "block_reason": ";".join(reasons),
            "repair_route": route,
            "token_status": status,
            "corrected_text": corrected,
            "reasons": reasons,
        }

    if status in {"same_structural_tokens", "dynamic_literal_payload_only"}:
        return {
            "checkpoint_allowed": 1,
            "block_reason": "",
            "repair_route": route,
            "token_status": status,
            "corrected_text": corrected,
            "reasons": [route, status],
        }

    return {
        "checkpoint_allowed": 0,
        "block_reason": "structural_token_change_requires_token_policy_review",
        "repair_route": route,
        "token_status": status,
        "corrected_text": corrected,
        "reasons": [route, status],
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "repair_route",
        "token_status",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "normalized_decision",
        "current_text",
        "corrected_text",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["reasons"] = "; ".join(row.get("reasons") or [])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue long-text repair route checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Decision run id: {decision_run_id}",
        f"Policy: {POLICY_NAME}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed shadow repairs: {counts['allowed']:,}",
        f"- Blocked repairs: {counts['blocked']:,}",
        f"- Composition-ready observations: {counts['composition_ready']:,}",
        "",
        "By repair route:",
        *[f"- {key.removeprefix('route:')}: {value:,}" for key, value in counts.items() if key.startswith("route:")],
        "",
        "By token status:",
        *[f"- {key.removeprefix('token:')}: {value:,}" for key, value in counts.items() if key.startswith("token:")],
        "",
        "Blocks:",
        *[f"- {key.removeprefix('block:')}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} route={row['repair_route']} "
                    f"token={row['token_status']} block={row['block_reason'] or 'none'} "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row.get('current_text'), 220)}",
                f"  corrected: {short(row.get('corrected_text'), 220) if row.get('corrected_text') else '<none>'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records long-text repair route evidence only.",
            "- It does not write source/output and does not promote production apply by itself.",
            "- Structural token changes remain blocked for token policy or a narrower subpolicy.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    started_at_db = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn)
        source_rows = fetch_rows(conn, decision_run_id=selected_decision_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_repair_route_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                decision_run_id,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (RULE_VERSION, POLICY_NAME, POLICY_STATUS, selected_decision_run_id, started_at_db, started_at_db),
        )
        run_id = int(cur.lastrowid)
        counts: Counter[str] = Counter()
        output_rows: list[dict[str, Any]] = []
        now = db.utc_now()
        for row in source_rows:
            classified = classify(row)
            current_text = row.get("confirmed_text") or row.get("evidence_text") or ""
            output = {
                **row,
                **classified,
                "run_id": run_id,
                "decision_run_id": selected_decision_run_id,
                "current_text": current_text,
            }
            output_rows.append(output)
            allowed = int(classified["checkpoint_allowed"])
            if row.get("normalized_decision") == "composition_ready":
                counts["composition_ready"] += 1
            elif allowed:
                counts["allowed"] += 1
            else:
                counts["blocked"] += 1
            counts[f"route:{classified['repair_route']}"] += 1
            counts[f"token:{classified['token_status']}"] += 1
            if classified["block_reason"]:
                counts[f"block:{classified['block_reason']}"] += 1
            conn.execute(
                """
                INSERT INTO ml_issue_long_text_repair_route_checkpoint_items (
                    run_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    repair_route,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    normalized_decision,
                    reasons_json,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_decision_run_id,
                    int(row["decision_id"]),
                    int(row["queue_item_id"]),
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    AGENT_KEY,
                    classified["repair_route"],
                    allowed,
                    CHECKPOINT_ACTION,
                    classified["block_reason"],
                    classified["token_status"],
                    current_text,
                    classified["corrected_text"],
                    row["normalized_decision"],
                    json.dumps(classified["reasons"], ensure_ascii=False, sort_keys=True),
                    row.get("notes") or "",
                    now,
                ),
            )
        finished_at = datetime.now().isoformat(timespec="seconds")
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            decision_run_id=selected_decision_run_id,
            rows=output_rows,
            counts=counts,
        )
        conn.execute(
            """
            UPDATE ml_issue_long_text_repair_route_checkpoint_runs
            SET
                candidate_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                composition_ready_count = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(output_rows),
                counts["allowed"],
                counts["blocked"],
                counts["composition_ready"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    print("[issue_long_text_repair_route_checkpoint] Checkpoint generated")
    print(f"[issue_long_text_repair_route_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_repair_route_checkpoint] Run id: {run_id}")
    print(f"[issue_long_text_repair_route_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[issue_long_text_repair_route_checkpoint] Candidates: {len(output_rows):,}")
    print(f"[issue_long_text_repair_route_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_long_text_repair_route_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_long_text_repair_route_checkpoint] Composition ready: {counts['composition_ready']:,}")
    print(f"[issue_long_text_repair_route_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(output_rows),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "composition_ready_count": counts["composition_ready"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint long-text repair routes from reviewed issue decisions.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
