from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_long_text_mixed_structural_token_policy_blocker_subsplit_v1"
SOURCE_POLICY_NAME = "long_text_mixed_structural_token_policy_shadow_v1"
POLICY_NAME = "long_text_mixed_structural_token_policy_blocker_subsplit_v1"
POLICY_STATUS = "shadow"

STATUS_READY = "subsplit_ready"
STATUS_PHRASE = "needs_phrase_mapping_review"
STATUS_CONTEXT = "needs_context_scope_review"
STATUS_SEMANTIC = "needs_semantic_review"

SUBJECT_PRONOUN_AGENT = "long_text_subject_pronoun_form_microagent"
LEXICAL_BUNDLE_AGENT = "long_text_lexical_select_cstring_bundle_guard"
SPEAKER_SCOPE_AGENT = "long_text_speaker_scope_retarget_guard"
CONCEPT_GUARD_AGENT = "long_text_concept_link_reference_guard"

COMPLEX_REASON = "complex_lexical_gender_multi_token_requires_subsplit"
SPEAKER_REASON = "speaker_gender_scope_change_requires_context"
CONCEPT_REASON = "concept_reference_added_requires_semantic_context"

GET_WOMANMAN_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetWomanMan(?:\|[^\]]+)?\]$")
GET_SHEHE_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetSheHe(?:\|[^\]]+)?\]$")
SELECT_CSTRING_RE = re.compile(r"^\[Select_CString\(\s*([A-Za-z_][\w.]*)\.IsFemale,\s*'<TEXT>',\s*'<TEXT>'\s*\)\]$")
CUSTOM_TOKEN_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.Custom\('([^']+)'\)(?:\|[^\]]+)?\]$")
CONCEPT_TOKEN_RE = re.compile(r"^\$game_concept_[A-Za-z0-9_]+\$$")


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def token_parts(delta: dict[str, Any]) -> tuple[list[str], list[str]]:
    return [str(item) for item in delta.get("added") or []], [str(item) for item in delta.get("removed") or []]


def token_scope(token: str) -> str | None:
    for pattern in (GET_WOMANMAN_RE, GET_SHEHE_RE, SELECT_CSTRING_RE, CUSTOM_TOKEN_RE):
        match = pattern.match(token)
        if match:
            return match.group(1)
    return None


def canonical_delta(value: str | None) -> str:
    return json.dumps(parse_json_obj(value), ensure_ascii=False, sort_keys=True)


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND blocked_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No token-policy blocked shadow run found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            token_policy_shadow_run_id INTEGER NOT NULL,
            split_run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            source_blocked_count INTEGER NOT NULL DEFAULT 0,
            duplicate_input_count INTEGER NOT NULL DEFAULT 0,
            subsplit_unit_count INTEGER NOT NULL DEFAULT 0,
            subsplit_ready_count INTEGER NOT NULL DEFAULT 0,
            needs_phrase_mapping_count INTEGER NOT NULL DEFAULT 0,
            needs_context_review_count INTEGER NOT NULL DEFAULT 0,
            needs_semantic_review_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            microagent_counts_json TEXT,
            status_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            token_policy_shadow_run_id INTEGER NOT NULL,
            token_policy_shadow_item_ids_json TEXT NOT NULL,
            split_run_id INTEGER NOT NULL,
            split_item_ids_json TEXT NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            structural_shadow_item_ids_json TEXT NOT NULL,
            source_checkpoint_run_ids_json TEXT NOT NULL,
            source_checkpoint_item_ids_json TEXT NOT NULL,
            decision_run_ids_json TEXT NOT NULL,
            decision_ids_json TEXT NOT NULL,
            queue_item_ids_json TEXT NOT NULL,
            ledger_item_ids_json TEXT NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            source_microagents_json TEXT NOT NULL,
            source_block_reasons_json TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            micro_issue_kind TEXT NOT NULL,
            subcomponent_kind TEXT NOT NULL,
            subsplit_status TEXT NOT NULL,
            subsplit_action TEXT NOT NULL,
            subsplit_ready INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            review_route TEXT,
            partial_component_only INTEGER NOT NULL DEFAULT 1,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            extracted_token_delta_json TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], shadow_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_mixed_structural_token_policy_blocker_subsplit_shadow_run_{shadow_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Token-policy shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_blocked_rows(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_items
        WHERE run_id = ?
          AND token_policy_status = 'token_blocked'
        ORDER BY block_reason, microagent_key, relative_path, source_line_number, source_key
        """,
        (shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def row_group_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(row["segment_id"]),
        row["relative_path"],
        row["source_key"],
        row.get("source_line_number"),
        row["current_text_hash"],
        row["corrected_text_hash"],
        canonical_delta(row.get("token_delta_json")),
    )


def collect_ids(rows: list[dict[str, Any]], key: str) -> list[int]:
    return sorted({int(row[key]) for row in rows if row.get(key) is not None})


def collect_values(rows: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(row[key]) for row in rows if row.get(key)})


def base_unit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = rows[0]
    return {
        "token_policy_shadow_run_id": int(first["run_id"]),
        "token_policy_shadow_item_ids": collect_ids(rows, "id"),
        "split_run_id": int(first["split_run_id"]),
        "split_item_ids": collect_ids(rows, "split_item_id"),
        "structural_shadow_run_id": int(first["structural_shadow_run_id"]),
        "structural_shadow_item_ids": collect_ids(rows, "structural_shadow_item_id"),
        "source_checkpoint_run_ids": collect_ids(rows, "source_checkpoint_run_id"),
        "source_checkpoint_item_ids": collect_ids(rows, "source_checkpoint_item_id"),
        "decision_run_ids": collect_ids(rows, "decision_run_id"),
        "decision_ids": collect_ids(rows, "decision_id"),
        "queue_item_ids": collect_ids(rows, "queue_item_id"),
        "ledger_item_ids": collect_ids(rows, "ledger_item_id"),
        "segment_id": int(first["segment_id"]),
        "relative_path": first["relative_path"],
        "source_key": first["source_key"],
        "source_line_number": first.get("source_line_number"),
        "source_microagents": collect_values(rows, "microagent_key"),
        "source_block_reasons": collect_values(rows, "block_reason"),
        "current_text_hash": first["current_text_hash"],
        "corrected_text_hash": first["corrected_text_hash"],
        "token_delta": parse_json_obj(first.get("token_delta_json")),
        "partial_component_only": 1,
        "production_release_allowed": 0,
    }


def subject_pronoun_component(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    unit = base_unit(rows)
    delta = unit["token_delta"]
    added, removed = token_parts(delta)
    pairs: list[tuple[str, str]] = []
    for removed_token in removed:
        removed_match = GET_WOMANMAN_RE.match(removed_token)
        if not removed_match:
            continue
        removed_scope = removed_match.group(1)
        for added_token in added:
            added_match = GET_SHEHE_RE.match(added_token)
            if added_match and added_match.group(1) == removed_scope:
                pairs.append((added_token, removed_token))
    if not pairs:
        return None
    extracted = {
        "added": [pair[0] for pair in pairs],
        "added_count": len(pairs),
        "removed": [pair[1] for pair in pairs],
        "removed_count": len(pairs),
    }
    return {
        **unit,
        "microagent_key": SUBJECT_PRONOUN_AGENT,
        "micro_issue_kind": "subject_pronoun_form_swap",
        "subcomponent_kind": "getwomanman_to_getshehe_same_scope",
        "subsplit_status": STATUS_READY,
        "subsplit_action": "extract_subject_getwomanman_to_getshehe_same_scope_shadow",
        "subsplit_ready": 1,
        "block_reason": "",
        "review_route": "token_policy_checkpoint_candidate",
        "extracted_token_delta": extracted,
        "reasons": [
            "same_scope_getwomanman_removed_getshehe_added",
            f"source_microagents={','.join(unit['source_microagents'])}",
        ],
    }


def lexical_bundle_component(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    unit = base_unit(rows)
    delta = unit["token_delta"]
    added, removed = token_parts(delta)
    select_added = [token for token in added if SELECT_CSTRING_RE.match(token)]
    custom_removed = [token for token in removed if CUSTOM_TOKEN_RE.match(token)]
    if not select_added and not custom_removed:
        return None
    extracted = {
        "added": select_added,
        "added_count": len(select_added),
        "removed": custom_removed,
        "removed_count": len(custom_removed),
    }
    return {
        **unit,
        "microagent_key": LEXICAL_BUNDLE_AGENT,
        "micro_issue_kind": "lexical_select_cstring_bundle",
        "subcomponent_kind": "multi_select_cstring_custom_bundle",
        "subsplit_status": STATUS_PHRASE,
        "subsplit_action": "hold_multi_select_cstring_phrase_bundle_for_mapping",
        "subsplit_ready": 0,
        "block_reason": "multi_select_cstring_bundle_requires_phrase_mapping",
        "review_route": "phrase_mapping_review",
        "extracted_token_delta": extracted,
        "reasons": [
            "multiple_select_cstring_or_custom_tokens_need_phrase_alignment",
            f"select_added={len(select_added)}",
            f"custom_removed={len(custom_removed)}",
        ],
    }


def speaker_scope_component(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unit = base_unit(rows)
    delta = unit["token_delta"]
    added, removed = token_parts(delta)
    added_scopes = sorted({scope for token in added if (scope := token_scope(token))})
    removed_scopes = sorted({scope for token in removed if (scope := token_scope(token))})
    return {
        **unit,
        "microagent_key": SPEAKER_SCOPE_AGENT,
        "micro_issue_kind": "speaker_scope_retarget",
        "subcomponent_kind": "speaker_gender_scope_change",
        "subsplit_status": STATUS_CONTEXT,
        "subsplit_action": "hold_speaker_scope_retarget_for_context",
        "subsplit_ready": 0,
        "block_reason": "speaker_token_scope_retarget_requires_dialogue_context",
        "review_route": "speaker_scope_context_review",
        "extracted_token_delta": {
            "added": added,
            "added_count": len(added),
            "removed": removed,
            "removed_count": len(removed),
            "added_scopes": added_scopes,
            "removed_scopes": removed_scopes,
        },
        "reasons": [
            "added_removed_scope_sets_differ",
            f"added_scopes={','.join(added_scopes)}",
            f"removed_scopes={','.join(removed_scopes)}",
        ],
    }


def concept_component(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unit = base_unit(rows)
    delta = unit["token_delta"]
    added, removed = token_parts(delta)
    concept_added = [token for token in added if CONCEPT_TOKEN_RE.match(token)]
    return {
        **unit,
        "microagent_key": CONCEPT_GUARD_AGENT,
        "micro_issue_kind": "concept_reference_semantic_delta",
        "subcomponent_kind": "concept_reference_addition",
        "subsplit_status": STATUS_SEMANTIC,
        "subsplit_action": "route_concept_reference_addition_to_semantic_guard",
        "subsplit_ready": 0,
        "block_reason": "concept_reference_addition_requires_semantic_validation",
        "review_route": "semantic_concept_reference_review",
        "extracted_token_delta": {
            "added": concept_added,
            "added_count": len(concept_added),
            "removed": removed,
            "removed_count": len(removed),
        },
        "reasons": [
            "concept_reference_added_inside_semantic_paragraph",
            f"concept_added={','.join(concept_added) if concept_added else 'none'}",
        ],
    }


def build_units(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row_group_key(row)].append(row)

    units: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        reasons = set(collect_values(group_rows, "block_reason"))
        if COMPLEX_REASON in reasons:
            pronoun = subject_pronoun_component(group_rows)
            if pronoun:
                units.append(pronoun)
            lexical = lexical_bundle_component(group_rows)
            if lexical:
                units.append(lexical)
            continue
        if SPEAKER_REASON in reasons:
            units.append(speaker_scope_component(group_rows))
            continue
        if CONCEPT_REASON in reasons:
            units.append(concept_component(group_rows))
            continue
        unit = base_unit(group_rows)
        units.append(
            {
                **unit,
                "microagent_key": "long_text_unknown_token_policy_blocker_guard",
                "micro_issue_kind": "unknown_token_policy_blocker",
                "subcomponent_kind": "unknown",
                "subsplit_status": STATUS_CONTEXT,
                "subsplit_action": "hold_unknown_token_policy_blocker_for_context",
                "subsplit_ready": 0,
                "block_reason": "unknown_token_policy_blocker_requires_review",
                "review_route": "manual_context_review",
                "extracted_token_delta": unit["token_delta"],
                "reasons": ["unclassified_token_policy_blocker"],
            }
        )
    return units


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    shadow_run: dict[str, Any],
    source_rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    duplicate_input_count: int,
) -> None:
    fields = [
        "subsplit_item_id",
        "token_policy_shadow_item_ids_json",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "source_microagents_json",
        "microagent_key",
        "micro_issue_kind",
        "subcomponent_kind",
        "subsplit_status",
        "subsplit_action",
        "subsplit_ready",
        "block_reason",
        "review_route",
        "partial_component_only",
        "production_release_allowed",
        "extracted_token_delta_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for unit in units:
            writer.writerow({field: unit.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for unit in units:
            handle.write(json.dumps({field: unit.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(unit["subsplit_status"] for unit in units)
    by_microagent = Counter(unit["microagent_key"] for unit in units)
    ready = [unit for unit in units if int(unit["subsplit_ready"] or 0) == 1]
    blocked = [unit for unit in units if int(unit["subsplit_ready"] or 0) == 0]
    lines = [
        "Issue long-text mixed structural token-policy blocker subsplit",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Subsplit run id: {run_id}",
        f"Token-policy shadow run id: {shadow_run['id']}",
        f"Split run id: {shadow_run['split_run_id']}",
        f"Structural shadow run id: {shadow_run['structural_shadow_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Source blocked rows: {len(source_rows):,}",
        f"- Duplicate inputs collapsed: {duplicate_input_count:,}",
        f"- Subsplit units: {len(units):,}",
        f"- Subsplit ready: {len(ready):,}",
        f"- Needs phrase mapping: {by_status.get(STATUS_PHRASE, 0):,}",
        f"- Needs context review: {by_status.get(STATUS_CONTEXT, 0):,}",
        f"- Needs semantic review: {by_status.get(STATUS_SEMANTIC, 0):,}",
        f"- By status: {json.dumps(dict(by_status), ensure_ascii=False, sort_keys=True)}",
        f"- By microagent: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        "",
        "Ready partial units:",
    ]
    for unit in ready:
        lines.extend(
            [
                (
                    f"- {unit['microagent_key']} / {unit['subcomponent_kind']} | "
                    f"{unit['relative_path']}:{unit.get('source_line_number') or '?'}:{unit['source_key']}"
                ),
                f"  action={unit['subsplit_action']}",
            ]
        )
    if not ready:
        lines.append("- none")
    lines.extend(["", "Blocked/routed units:"])
    for unit in blocked:
        lines.append(
            (
                f"- {unit['microagent_key']} / {unit['subcomponent_kind']} / {unit['subsplit_status']} | "
                f"block={unit['block_reason']} | route={unit['review_route']} | "
                f"{unit['relative_path']}:{unit.get('source_line_number') or '?'}:{unit['source_key']}"
            )
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Subsplit-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- Ready units are partial token components, not whole-segment release decisions.",
            "- Blocked units are now routed to narrower phrase/context/semantic review paths.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, shadow_run_id: int | None = None, expected_blocked: int = 4) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        source_rows = fetch_blocked_rows(conn, shadow_run_id=selected_shadow_run_id)
        if expected_blocked >= 0 and len(source_rows) != expected_blocked:
            raise RuntimeError(f"Expected {expected_blocked} blocked rows, found {len(source_rows)}.")
        units = build_units(source_rows)
        duplicate_input_count = len(source_rows) - len({row_group_key(row) for row in source_rows})
        by_status = Counter(unit["subsplit_status"] for unit in units)
        by_microagent = Counter(unit["microagent_key"] for unit in units)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_shadow_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs (
                rule_version,
                token_policy_shadow_run_id,
                split_run_id,
                structural_shadow_run_id,
                policy_name,
                policy_status,
                source_blocked_count,
                duplicate_input_count,
                subsplit_unit_count,
                subsplit_ready_count,
                needs_phrase_mapping_count,
                needs_context_review_count,
                needs_semantic_review_count,
                production_release_allowed,
                microagent_counts_json,
                status_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_run_id,
                int(shadow_run["split_run_id"]),
                int(shadow_run["structural_shadow_run_id"]),
                POLICY_NAME,
                POLICY_STATUS,
                len(source_rows),
                duplicate_input_count,
                len(units),
                sum(1 for unit in units if int(unit["subsplit_ready"] or 0) == 1),
                by_status.get(STATUS_PHRASE, 0),
                by_status.get(STATUS_CONTEXT, 0),
                by_status.get(STATUS_SEMANTIC, 0),
                0,
                json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_status), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        created_at = db.utc_now()
        for unit in units:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_items (
                    run_id,
                    token_policy_shadow_run_id,
                    token_policy_shadow_item_ids_json,
                    split_run_id,
                    split_item_ids_json,
                    structural_shadow_run_id,
                    structural_shadow_item_ids_json,
                    source_checkpoint_run_ids_json,
                    source_checkpoint_item_ids_json,
                    decision_run_ids_json,
                    decision_ids_json,
                    queue_item_ids_json,
                    ledger_item_ids_json,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    source_microagents_json,
                    source_block_reasons_json,
                    microagent_key,
                    micro_issue_kind,
                    subcomponent_kind,
                    subsplit_status,
                    subsplit_action,
                    subsplit_ready,
                    block_reason,
                    review_route,
                    partial_component_only,
                    production_release_allowed,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    extracted_token_delta_json,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_shadow_run_id,
                    json.dumps(unit["token_policy_shadow_item_ids"], ensure_ascii=False, sort_keys=True),
                    int(unit["split_run_id"]),
                    json.dumps(unit["split_item_ids"], ensure_ascii=False, sort_keys=True),
                    int(unit["structural_shadow_run_id"]),
                    json.dumps(unit["structural_shadow_item_ids"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["source_checkpoint_run_ids"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["source_checkpoint_item_ids"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["decision_run_ids"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["decision_ids"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["queue_item_ids"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["ledger_item_ids"], ensure_ascii=False, sort_keys=True),
                    int(unit["segment_id"]),
                    unit["relative_path"],
                    unit["source_key"],
                    unit.get("source_line_number"),
                    json.dumps(unit["source_microagents"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["source_block_reasons"], ensure_ascii=False, sort_keys=True),
                    unit["microagent_key"],
                    unit["micro_issue_kind"],
                    unit["subcomponent_kind"],
                    unit["subsplit_status"],
                    unit["subsplit_action"],
                    int(unit["subsplit_ready"]),
                    unit["block_reason"],
                    unit["review_route"],
                    1,
                    0,
                    unit["current_text_hash"],
                    unit["corrected_text_hash"],
                    json.dumps(unit["token_delta"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["extracted_token_delta"], ensure_ascii=False, sort_keys=True),
                    json.dumps(unit["reasons"], ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            unit["subsplit_item_id"] = int(item_cur.lastrowid)
            unit["token_policy_shadow_item_ids_json"] = json.dumps(unit["token_policy_shadow_item_ids"], ensure_ascii=False, sort_keys=True)
            unit["source_microagents_json"] = json.dumps(unit["source_microagents"], ensure_ascii=False, sort_keys=True)
            unit["extracted_token_delta_json"] = json.dumps(unit["extracted_token_delta"], ensure_ascii=False, sort_keys=True)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            shadow_run=shadow_run,
            source_rows=source_rows,
            units=units,
            duplicate_input_count=duplicate_input_count,
        )
        conn.commit()

    print("[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Subsplit generated")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Subsplit run id: {run_id}")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Token-policy shadow run id: {selected_shadow_run_id}")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Source blocked: {len(source_rows):,}")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Units: {len(units):,}")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Ready: {sum(1 for unit in units if unit['subsplit_ready']):,}")
    print(f"[issue_long_text_mixed_structural_token_policy_blocker_subsplit] Report: {txt_path}")
    return {
        "run_id": run_id,
        "token_policy_shadow_run_id": selected_shadow_run_id,
        "source_blocked": len(source_rows),
        "subsplit_units": len(units),
        "ready": sum(1 for unit in units if unit["subsplit_ready"]),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Subsplit real token-policy blockers from mixed long-text repairs.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--expected-blocked", type=int, default=4)
    args = parser.parse_args()
    main(shadow_run_id=args.shadow_run_id, expected_blocked=args.expected_blocked)
