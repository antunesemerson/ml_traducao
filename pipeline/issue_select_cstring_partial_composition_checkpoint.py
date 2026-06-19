from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_partial_composition_checkpoint_v1"
COMPOSITION_NAME = "select_cstring_partial_composition_checkpoint_v1"
COMPOSITION_STATUS = "shadow_learning_only"
COMPOSITION_ACTION = "measure_partial_select_cstring_microagent_coverage"
PRODUCTION_RELEASE_ALLOWED = 0

PRETERITE_AGENT = "select_cstring_local_player_preterite_verb_rewrite"
REFLEXIVE_AGENT = "select_cstring_local_player_reflexive_phrase_rewrite"
POSSESSIVE_AGENT = "select_cstring_local_player_possessive_pronoun_rewrite"
LITERAL_CONTEXT_AGENT = "select_cstring_literal_payload_context_review"
FUTURE_TENSE_AGENT = "select_cstring_local_player_future_tense_review"
SUPPORTED_AGENTS = {PRETERITE_AGENT, REFLEXIVE_AGENT, POSSESSIVE_AGENT, LITERAL_CONTEXT_AGENT, FUTURE_TENSE_AGENT}
ANCHOR_AGENTS = {PRETERITE_AGENT, REFLEXIVE_AGENT, POSSESSIVE_AGENT}
DEFAULT_AUDIT_GLOB = "*_issue_dynamic_select_cstring_literal_subtype_audit.csv"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def norm(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def component_key(row: dict[str, Any], agent_key: str | None = None) -> tuple[int, str, str, str]:
    return (
        int(row["segment_id"]),
        agent_key or str(row.get("suggested_microagent") or row.get("target_agent") or ""),
        norm(str(row.get("left_literal") or "")),
        norm(str(row.get("right_literal") or "")),
    )


def latest_audit_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    matches = sorted(reports_dir.glob(DEFAULT_AUDIT_GLOB))
    if not matches:
        raise SystemExit("No dynamic Select_CString literal subtype audit CSV found.")
    return matches[-1]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_select_cstring_partial_composition_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_partial_composition_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            composition_name TEXT NOT NULL,
            composition_status TEXT NOT NULL,
            composition_action TEXT NOT NULL,
            source_audit_csv TEXT NOT NULL,
            candidate_segment_count INTEGER NOT NULL DEFAULT 0,
            audit_piece_count INTEGER NOT NULL DEFAULT 0,
            ready_piece_count INTEGER NOT NULL DEFAULT 0,
            blocked_piece_count INTEGER NOT NULL DEFAULT 0,
            uncovered_piece_count INTEGER NOT NULL DEFAULT 0,
            segment_ready_count INTEGER NOT NULL DEFAULT 0,
            segment_partial_count INTEGER NOT NULL DEFAULT 0,
            segment_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            source_counts_json TEXT NOT NULL,
            status_counts_json TEXT NOT NULL,
            missing_agent_counts_json TEXT NOT NULL,
            block_counts_json TEXT NOT NULL,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_partial_composition_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            audit_piece_count INTEGER NOT NULL DEFAULT 0,
            ready_piece_count INTEGER NOT NULL DEFAULT 0,
            blocked_piece_count INTEGER NOT NULL DEFAULT 0,
            uncovered_piece_count INTEGER NOT NULL DEFAULT 0,
            composition_status TEXT NOT NULL,
            segment_closure_candidate INTEGER NOT NULL DEFAULT 0,
            ready_sources_json TEXT NOT NULL,
            missing_agents_json TEXT NOT NULL,
            block_reasons_json TEXT NOT NULL,
            english_text TEXT,
            spanish_text TEXT,
            current_text TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_partial_composition_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_partial_composition_checkpoint_pieces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            queue_item_id INTEGER,
            ledger_item_id INTEGER,
            literal_subtype TEXT,
            suggested_microagent TEXT,
            left_literal TEXT,
            right_literal TEXT,
            piece_status TEXT NOT NULL,
            component_source TEXT,
            component_item_id INTEGER,
            proposed_left_literal TEXT,
            proposed_right_literal TEXT,
            block_reason TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_partial_composition_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_partial_composition_items_run
        ON ml_issue_select_cstring_partial_composition_checkpoint_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_partial_composition_pieces_run
        ON ml_issue_select_cstring_partial_composition_checkpoint_pieces(run_id)
        """
    )
    conn.commit()


def load_audit_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def int_or_none(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(float(str(value).strip()))


def latest_context_by_segment(conn, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS current_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        segment_ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def add_component(
    components: dict[tuple[int, str, str, str], dict[str, Any]],
    *,
    key: tuple[int, str, str, str],
    ready: bool,
    source: str,
    item_id: int | None,
    proposed_left: str | None = None,
    proposed_right: str | None = None,
    block_reason: str | None = None,
) -> None:
    payload = {
        "ready": bool(ready),
        "source": source,
        "item_id": item_id,
        "proposed_left": proposed_left or "",
        "proposed_right": proposed_right or "",
        "block_reason": block_reason or "",
    }
    existing = components.get(key)
    if (
        existing is None
        or (payload["ready"] and not existing["ready"])
        or (
            not payload["ready"]
            and not existing["ready"]
            and payload["source"] == "reflexive_possessive_ptbr_checkpoint"
        )
    ):
        components[key] = payload


def fetch_components(conn) -> dict[tuple[int, str, str, str], dict[str, Any]]:
    components: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    if table_exists(conn, "ml_issue_select_cstring_preterite_ptbr_checkpoint_items"):
        rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_preterite_ptbr_checkpoint_items
            WHERE production_release_allowed = 0
            ORDER BY run_id, id
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            add_component(
                components,
                key=component_key(payload, PRETERITE_AGENT),
                ready=int(payload.get("checkpoint_allowed") or 0) == 1 and not (payload.get("block_reason") or ""),
                source="preterite_ptbr_checkpoint",
                item_id=int(payload["id"]),
                proposed_left=payload.get("proposed_left_literal"),
                proposed_right=payload.get("proposed_right_literal"),
                block_reason=payload.get("block_reason"),
            )
    if table_exists(conn, "ml_issue_select_cstring_possessive_context_checkpoint_items"):
        rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_possessive_context_checkpoint_items
            ORDER BY run_id, id
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            add_component(
                components,
                key=component_key(payload, POSSESSIVE_AGENT),
                ready=int(payload.get("checkpoint_allowed") or 0) == 1 and not (payload.get("block_reason") or ""),
                source="possessive_context_checkpoint",
                item_id=int(payload["id"]),
                proposed_left=payload.get("possessive_repair"),
                proposed_right=payload.get("possessive_repair"),
                block_reason=payload.get("block_reason"),
            )
    if table_exists(conn, "ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_items"):
        rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_reflexive_possessive_ptbr_checkpoint_items
            WHERE production_release_allowed = 0
            ORDER BY run_id, id
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            add_component(
                components,
                key=component_key(payload),
                ready=int(payload.get("microagent_ready") or 0) == 1,
                source="reflexive_possessive_ptbr_checkpoint",
                item_id=int(payload["id"]),
                proposed_left=payload.get("proposed_left_literal"),
                proposed_right=payload.get("proposed_right_literal"),
                block_reason=payload.get("block_reason"),
            )
    if table_exists(conn, "ml_issue_select_cstring_literal_payload_context_checkpoint_items"):
        rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_literal_payload_context_checkpoint_items
            WHERE production_release_allowed = 0
            ORDER BY run_id, id
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            add_component(
                components,
                key=component_key(payload, LITERAL_CONTEXT_AGENT),
                ready=int(payload.get("checkpoint_allowed") or 0) == 1 and not (payload.get("block_reason") or "").startswith("hard_block:"),
                source="literal_payload_context_checkpoint",
                item_id=int(payload["id"]),
                proposed_left=payload.get("proposed_left_literal"),
                proposed_right=payload.get("proposed_right_literal"),
                block_reason=payload.get("block_reason"),
            )
    if table_exists(conn, "ml_issue_select_cstring_future_tense_checkpoint_items"):
        rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_future_tense_checkpoint_items
            WHERE production_release_allowed = 0
            ORDER BY run_id, id
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            add_component(
                components,
                key=component_key(payload, FUTURE_TENSE_AGENT),
                ready=int(payload.get("checkpoint_allowed") or 0) == 1 and not (payload.get("block_reason") or ""),
                source="future_tense_checkpoint",
                item_id=int(payload["id"]),
                proposed_left=payload.get("proposed_left_literal"),
                proposed_right=payload.get("proposed_right_literal"),
                block_reason=payload.get("block_reason"),
            )
    return components


def fetch_segment_composers(conn) -> dict[int, dict[str, Any]]:
    composers: dict[int, dict[str, Any]] = {}
    if table_exists(conn, "ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items"):
        rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items
            WHERE production_release_allowed = 0
            ORDER BY run_id, id
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            if int(payload.get("checkpoint_allowed") or 0) != 1:
                continue
            if int(payload.get("segment_closure_candidate") or 0) != 1:
                continue
            segment_id = int(payload["segment_id"])
            composers[segment_id] = {
                "source": "passive_confiscation_sentence_composer",
                "item_id": int(payload["id"]),
                "corrected_text": payload.get("corrected_text") or "",
                "decision": payload.get("decision") or "",
            }
    return composers


def candidate_rows_from_audit(
    rows: list[dict[str, str]],
    components: dict[tuple[int, str, str, str], dict[str, Any]],
) -> list[dict[str, str]]:
    anchor_segments = {
        int(row["segment_id"])
        for row in rows
        if row.get("suggested_microagent") in ANCHOR_AGENTS
    }
    return [
        row
        for row in rows
        if int(row["segment_id"]) in anchor_segments
        or component_key(row) in components
        and (
            row.get("suggested_microagent")
            or row.get("literal_subtype")
            or row.get("left_literal")
            or row.get("right_literal")
        )
    ]


def classify_piece(row: dict[str, str], components: dict[tuple[int, str, str, str], dict[str, Any]]) -> dict[str, Any]:
    agent = row.get("suggested_microagent") or ""
    key = component_key(row)
    component = components.get(key)
    piece_status = "uncovered"
    block_reason = ""
    component_source = ""
    component_item_id = None
    proposed_left = ""
    proposed_right = ""
    if agent not in SUPPORTED_AGENTS:
        block_reason = f"unsupported_microagent:{agent or 'missing'}"
    elif component is None:
        block_reason = "supported_microagent_missing_checkpoint"
    elif component["ready"]:
        piece_status = "ready"
        component_source = component["source"]
        component_item_id = component["item_id"]
        proposed_left = component["proposed_left"]
        proposed_right = component["proposed_right"]
    else:
        piece_status = "blocked"
        component_source = component["source"]
        component_item_id = component["item_id"]
        block_reason = component["block_reason"] or "component_not_ready"
        proposed_left = component["proposed_left"]
        proposed_right = component["proposed_right"]

    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "queue_item_id": int_or_none(row.get("queue_item_id")),
        "ledger_item_id": int_or_none(row.get("ledger_item_id")),
        "literal_subtype": row.get("literal_subtype") or "",
        "suggested_microagent": agent,
        "left_literal": row.get("left_literal") or "",
        "right_literal": row.get("right_literal") or "",
        "piece_status": piece_status,
        "component_source": component_source,
        "component_item_id": component_item_id,
        "proposed_left_literal": proposed_left,
        "proposed_right_literal": proposed_right,
        "block_reason": block_reason,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def summarize_segments(
    pieces: list[dict[str, Any]],
    context: dict[int, dict[str, Any]],
    segment_composers: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for piece in pieces:
        grouped[int(piece["segment_id"])].append(piece)

    segment_rows: list[dict[str, Any]] = []
    for segment_id, rows in sorted(grouped.items()):
        ready = [row for row in rows if row["piece_status"] == "ready"]
        blocked = [row for row in rows if row["piece_status"] == "blocked"]
        uncovered = [row for row in rows if row["piece_status"] == "uncovered"]
        ready_sources = Counter(row["component_source"] for row in ready)
        missing_agents = Counter(row["suggested_microagent"] or "missing" for row in uncovered)
        block_reasons = Counter(row["block_reason"] or "blocked" for row in blocked + uncovered)
        segment_composer = segment_composers.get(segment_id)
        if segment_composer is not None:
            ready_sources[segment_composer["source"]] += 1
            status = "segment_composition_ready"
            closure_candidate = 1
            ready_count = len(rows)
            blocked_count = 0
            uncovered_count = 0
            missing_agents = Counter()
            block_reasons = Counter()
        elif ready and not blocked and not uncovered:
            status = "segment_composition_ready"
            closure_candidate = 1
            ready_count = len(ready)
            blocked_count = len(blocked)
            uncovered_count = len(uncovered)
        elif ready:
            status = "segment_partial_ready"
            closure_candidate = 0
            ready_count = len(ready)
            blocked_count = len(blocked)
            uncovered_count = len(uncovered)
        else:
            status = "segment_blocked_no_ready"
            closure_candidate = 0
            ready_count = len(ready)
            blocked_count = len(blocked)
            uncovered_count = len(uncovered)
        first = rows[0]
        ctx = context.get(segment_id, {})
        segment_rows.append(
            {
                "segment_id": segment_id,
                "relative_path": first["relative_path"],
                "source_key": first["source_key"],
                "audit_piece_count": len(rows),
                "ready_piece_count": ready_count,
                "blocked_piece_count": blocked_count,
                "uncovered_piece_count": uncovered_count,
                "composition_status": status,
                "segment_closure_candidate": closure_candidate,
                "ready_sources": dict(ready_sources),
                "missing_agents": dict(missing_agents),
                "block_reasons": dict(block_reasons),
                "english_text": ctx.get("english_text") or "",
                "spanish_text": ctx.get("spanish_text") or "",
                "current_text": ctx.get("current_text") or "",
                "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
            }
        )
    return segment_rows


def write_reports(
    *,
    run_id: int,
    source_audit_csv: Path,
    segment_rows: list[dict[str, Any]],
    piece_rows: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
) -> None:
    status_counts = Counter(row["composition_status"] for row in segment_rows)
    missing_agent_counts = Counter()
    block_counts = Counter()
    source_counts = Counter()
    for row in segment_rows:
        missing_agent_counts.update(row["missing_agents"])
        block_counts.update(row["block_reasons"])
        source_counts.update(row["ready_sources"])

    lines = [
        "Select_CString partial composition checkpoint",
        f"Run: {run_id}",
        f"Rule version: {RULE_VERSION}",
        f"Composition status: {COMPOSITION_STATUS}",
        f"Source audit CSV: {source_audit_csv}",
        "",
        "Summary",
        f"- candidate_segments: {len(segment_rows)}",
        f"- audit_pieces: {len(piece_rows)}",
        f"- ready_pieces: {sum(row['ready_piece_count'] for row in segment_rows)}",
        f"- blocked_pieces: {sum(row['blocked_piece_count'] for row in segment_rows)}",
        f"- uncovered_pieces: {sum(row['uncovered_piece_count'] for row in segment_rows)}",
        f"- segment_composition_ready: {status_counts.get('segment_composition_ready', 0)}",
        f"- segment_partial_ready: {status_counts.get('segment_partial_ready', 0)}",
        f"- segment_blocked_no_ready: {status_counts.get('segment_blocked_no_ready', 0)}",
        f"- production_release_allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Ready sources",
        *[f"- {key}: {value}" for key, value in sorted(source_counts.items())],
        "",
        "Missing agents",
        *[f"- {key}: {value}" for key, value in sorted(missing_agent_counts.items())],
        "",
        "Block reasons",
        *[f"- {key}: {value}" for key, value in sorted(block_counts.items())],
        "",
        "Segments",
    ]
    for row in segment_rows:
        lines.append(
            "- segment_id={segment_id} key={source_key} status={composition_status} "
            "pieces={audit_piece_count} ready={ready_piece_count} blocked={blocked_piece_count} "
            "uncovered={uncovered_piece_count} closure={segment_closure_candidate}".format(**row)
        )
        if row["ready_sources"]:
            lines.append(f"  ready_sources: {json.dumps(row['ready_sources'], ensure_ascii=False, sort_keys=True)}")
        if row["missing_agents"]:
            lines.append(f"  missing_agents: {json.dumps(row['missing_agents'], ensure_ascii=False, sort_keys=True)}")
        if row["block_reasons"]:
            lines.append(f"  block_reasons: {json.dumps(row['block_reasons'], ensure_ascii=False, sort_keys=True)}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fields = [
        "run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "audit_piece_count",
        "ready_piece_count",
        "blocked_piece_count",
        "uncovered_piece_count",
        "composition_status",
        "segment_closure_candidate",
        "ready_sources_json",
        "missing_agents_json",
        "block_reasons_json",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in segment_rows:
            writer.writerow(
                {
                    "run_id": run_id,
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "audit_piece_count": row["audit_piece_count"],
                    "ready_piece_count": row["ready_piece_count"],
                    "blocked_piece_count": row["blocked_piece_count"],
                    "uncovered_piece_count": row["uncovered_piece_count"],
                    "composition_status": row["composition_status"],
                    "segment_closure_candidate": row["segment_closure_candidate"],
                    "ready_sources_json": json.dumps(row["ready_sources"], ensure_ascii=False, sort_keys=True),
                    "missing_agents_json": json.dumps(row["missing_agents"], ensure_ascii=False, sort_keys=True),
                    "block_reasons_json": json.dumps(row["block_reasons"], ensure_ascii=False, sort_keys=True),
                    "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                }
            )
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in segment_rows:
            handle.write(json.dumps({"run_id": run_id, **row}, ensure_ascii=False, sort_keys=True) + "\n")


def build_checkpoint(audit_csv: Path | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    source_audit_csv = audit_csv or latest_audit_csv(settings)
    source_audit_csv = source_audit_csv if source_audit_csv.is_absolute() else db.PROJECT_ROOT / source_audit_csv
    created_at = datetime.now().isoformat(timespec="seconds")
    report_path, csv_path, jsonl_path = report_paths(settings)

    with db.connect(settings) as conn:
        ensure_tables(conn)
        components = fetch_components(conn)
        segment_composers = fetch_segment_composers(conn)
        audit_rows = load_audit_rows(source_audit_csv)
        candidate_audit_rows = candidate_rows_from_audit(audit_rows, components)
        piece_rows = [classify_piece(row, components) for row in candidate_audit_rows]
        context = latest_context_by_segment(conn, sorted({row["segment_id"] for row in piece_rows}))
        segment_rows = summarize_segments(piece_rows, context, segment_composers)

        status_counts = Counter(row["composition_status"] for row in segment_rows)
        source_counts = Counter()
        missing_agent_counts = Counter()
        block_counts = Counter()
        for row in segment_rows:
            source_counts.update(row["ready_sources"])
            missing_agent_counts.update(row["missing_agents"])
            block_counts.update(row["block_reasons"])

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_partial_composition_checkpoint_runs (
                created_at, rule_version, composition_name, composition_status,
                composition_action, source_audit_csv, candidate_segment_count,
                audit_piece_count, ready_piece_count, blocked_piece_count,
                uncovered_piece_count, segment_ready_count, segment_partial_count,
                segment_blocked_count, production_release_allowed, source_counts_json,
                status_counts_json, missing_agent_counts_json, block_counts_json,
                report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                RULE_VERSION,
                COMPOSITION_NAME,
                COMPOSITION_STATUS,
                COMPOSITION_ACTION,
                str(source_audit_csv),
                len(segment_rows),
                len(piece_rows),
                sum(row["ready_piece_count"] for row in segment_rows),
                sum(row["blocked_piece_count"] for row in segment_rows),
                sum(row["uncovered_piece_count"] for row in segment_rows),
                status_counts.get("segment_composition_ready", 0),
                status_counts.get("segment_partial_ready", 0),
                status_counts.get("segment_blocked_no_ready", 0),
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(missing_agent_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(report_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)

        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_partial_composition_checkpoint_items (
                run_id, segment_id, relative_path, source_key, audit_piece_count,
                ready_piece_count, blocked_piece_count, uncovered_piece_count,
                composition_status, segment_closure_candidate, ready_sources_json,
                missing_agents_json, block_reasons_json, english_text, spanish_text,
                current_text, production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["audit_piece_count"],
                    row["ready_piece_count"],
                    row["blocked_piece_count"],
                    row["uncovered_piece_count"],
                    row["composition_status"],
                    row["segment_closure_candidate"],
                    json.dumps(row["ready_sources"], ensure_ascii=False, sort_keys=True),
                    json.dumps(row["missing_agents"], ensure_ascii=False, sort_keys=True),
                    json.dumps(row["block_reasons"], ensure_ascii=False, sort_keys=True),
                    row["english_text"],
                    row["spanish_text"],
                    row["current_text"],
                    created_at,
                )
                for row in segment_rows
            ],
        )
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_partial_composition_checkpoint_pieces (
                run_id, segment_id, relative_path, source_key, queue_item_id,
                ledger_item_id, literal_subtype, suggested_microagent, left_literal,
                right_literal, piece_status, component_source, component_item_id,
                proposed_left_literal, proposed_right_literal, block_reason,
                production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row["literal_subtype"],
                    row["suggested_microagent"],
                    row["left_literal"],
                    row["right_literal"],
                    row["piece_status"],
                    row["component_source"],
                    row["component_item_id"],
                    row["proposed_left_literal"],
                    row["proposed_right_literal"],
                    row["block_reason"],
                    created_at,
                )
                for row in piece_rows
            ],
        )
        conn.commit()

    write_reports(
        run_id=run_id,
        source_audit_csv=source_audit_csv,
        segment_rows=segment_rows,
        piece_rows=piece_rows,
        report_path=report_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
    )
    return {
        "run_id": run_id,
        "candidate_segment_count": len(segment_rows),
        "audit_piece_count": len(piece_rows),
        "ready_piece_count": sum(row["ready_piece_count"] for row in segment_rows),
        "blocked_piece_count": sum(row["blocked_piece_count"] for row in segment_rows),
        "uncovered_piece_count": sum(row["uncovered_piece_count"] for row in segment_rows),
        "segment_ready_count": sum(1 for row in segment_rows if row["composition_status"] == "segment_composition_ready"),
        "segment_partial_count": sum(1 for row in segment_rows if row["composition_status"] == "segment_partial_ready"),
        "segment_blocked_count": sum(1 for row in segment_rows if row["composition_status"] == "segment_blocked_no_ready"),
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure partial Select_CString microagent composition coverage.")
    parser.add_argument("--audit-csv", type=Path, help="Dynamic Select_CString literal subtype audit CSV.")
    args = parser.parse_args()
    print(json.dumps(build_checkpoint(args.audit_csv), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
