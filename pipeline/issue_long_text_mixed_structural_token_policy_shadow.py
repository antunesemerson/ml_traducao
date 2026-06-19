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


RULE_VERSION = "issue_long_text_mixed_structural_token_policy_shadow_v1"
POLICY_NAME = "long_text_mixed_structural_token_policy_shadow_v1"
POLICY_STATUS = "shadow"
SOURCE_POLICY_NAME = "long_text_mixed_structural_split_shadow_v1"

ARTICLE_AGENT = "long_text_article_gender_token_microagent"
CONCEPT_LINK_AGENT = "long_text_concept_link_reference_guard"
LEXICAL_GENDER_AGENT = "long_text_lexical_gender_select_cstring_microagent"
SELECT_CSTRING_LITERAL_AGENT = "long_text_select_cstring_literal_microagent"
SPEAKER_GENDER_AGENT = "long_text_speaker_gender_alignment_microagent"
SUBJECT_REFERENCE_AGENT = "long_text_subject_reference_token_microagent"

STATUS_READY = "token_shadow_ready"
STATUS_SUPERSEDED = "token_superseded_by_sibling_component"
STATUS_BLOCKED = "token_blocked"

ACTION_ARTICLE_SWAP = "observe_article_es_oa_to_es_xa_partial_shadow"
ACTION_LEXICAL_SELECT = "observe_single_select_cstring_lexical_gender_phrase_shadow"
ACTION_SUPERSEDED_ARTICLE = "covered_by_article_gender_token_microagent"
ACTION_SUPERSEDED_OBJECT = "covered_by_object_pronoun_partial_lifecycle"
ACTION_BLOCK_CONTEXT = "hold_contextual_token_policy_for_more_evidence"

CUSTOM_TOKEN_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.Custom\('([^']+)'\)(?:\|[^\]]+)?\]$")
SELECT_CSTRING_RE = re.compile(r"^\[Select_CString\(\s*([A-Za-z_][\w.]*)\.IsFemale,\s*'<TEXT>',\s*'<TEXT>'\s*\)\]$")
GET_SHEHE_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetSheHe(?:\|[^\]]+)?\]$")
GET_HERHIM_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetHerHim(?:\|[^\]]+)?\]$")
GET_WOMANMAN_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetWomanMan(?:\|[^\]]+)?\]$")


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def latest_split_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_mixed_structural_split_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND needs_token_policy_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No token-policy split run found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_token_policy_shadow_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            split_run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            superseded_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_token_policy_shadow_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            split_run_id INTEGER NOT NULL,
            split_item_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            structural_shadow_item_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_checkpoint_item_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            original_subpolicy_name TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            micro_issue_kind TEXT NOT NULL,
            token_policy_status TEXT NOT NULL,
            token_policy_action TEXT NOT NULL,
            shadow_ready INTEGER NOT NULL DEFAULT 0,
            superseded_by TEXT,
            block_reason TEXT,
            partial_component_only INTEGER NOT NULL DEFAULT 1,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            reasons_json TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_mixed_structural_token_policy_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], split_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_mixed_structural_token_policy_shadow_split_run_{split_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_split_run(conn, *, split_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_runs
        WHERE id = ?
        """,
        (split_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Mixed structural split run not found: {split_run_id}")
    return dict(row)


def fetch_rows(conn, *, split_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_items
        WHERE run_id = ?
          AND split_status = 'needs_token_policy'
        ORDER BY priority DESC, microagent_key, relative_path, source_line_number, source_key
        """,
        (split_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def token_parts(delta: dict[str, Any]) -> tuple[list[str], list[str]]:
    return [str(item) for item in delta.get("added") or []], [str(item) for item in delta.get("removed") or []]


def custom_scope_and_name(token: str) -> tuple[str, str] | None:
    match = CUSTOM_TOKEN_RE.match(token)
    if not match:
        return None
    return match.group(1), match.group(2)


def token_scope(token: str) -> str | None:
    for pattern in (CUSTOM_TOKEN_RE, SELECT_CSTRING_RE, GET_SHEHE_RE, GET_HERHIM_RE, GET_WOMANMAN_RE):
        match = pattern.match(token)
        if match:
            return match.group(1)
    return None


def exact_article_swap(delta: dict[str, Any]) -> bool:
    added, removed = token_parts(delta)
    if len(added) != 1 or len(removed) != 1:
        return False
    added_custom = custom_scope_and_name(added[0])
    removed_custom = custom_scope_and_name(removed[0])
    if not added_custom or not removed_custom:
        return False
    return added_custom[0] == removed_custom[0] and removed_custom[1] == "ES_OA" and added_custom[1] == "ES_XA"


def single_lexical_select_cstring(delta: dict[str, Any]) -> bool:
    added, removed = token_parts(delta)
    if len(added) != 1 or len(removed) < 1:
        return False
    select = SELECT_CSTRING_RE.match(added[0])
    if not select:
        return False
    scope = select.group(1)
    removed_customs = [custom_scope_and_name(token) for token in removed]
    if not removed_customs or any(item is None for item in removed_customs):
        return False
    return all(item == (scope, "ES_OA") for item in removed_customs if item is not None)


def object_pronoun_delta(delta: dict[str, Any]) -> bool:
    added, removed = token_parts(delta)
    if not added or not removed:
        return False
    added_scopes = {token_scope(token) for token in added if GET_HERHIM_RE.match(token)}
    removed_scopes = {token_scope(token) for token in removed if GET_SHEHE_RE.match(token)}
    if not added_scopes or not removed_scopes:
        return False
    return added_scopes == removed_scopes


def complex_scope_change(delta: dict[str, Any]) -> bool:
    added, removed = token_parts(delta)
    added_scopes = {token_scope(token) for token in added if token_scope(token)}
    removed_scopes = {token_scope(token) for token in removed if token_scope(token)}
    return bool(added_scopes and removed_scopes and added_scopes != removed_scopes)


def classify(row: dict[str, Any]) -> dict[str, Any]:
    microagent = row.get("microagent_key") or ""
    delta = parse_json_obj(row.get("token_delta_json"))
    reasons = [microagent, row.get("micro_issue_kind") or "", row.get("split_action") or ""]

    if microagent == ARTICLE_AGENT and exact_article_swap(delta):
        return {
            "token_policy_status": STATUS_READY,
            "token_policy_action": ACTION_ARTICLE_SWAP,
            "shadow_ready": 1,
            "superseded_by": "",
            "block_reason": "",
            "reasons": [*reasons, "exact_es_oa_to_es_xa_same_scope"],
        }
    if microagent == LEXICAL_GENDER_AGENT and single_lexical_select_cstring(delta):
        return {
            "token_policy_status": STATUS_READY,
            "token_policy_action": ACTION_LEXICAL_SELECT,
            "shadow_ready": 1,
            "superseded_by": "",
            "block_reason": "",
            "reasons": [*reasons, "single_select_cstring_added_removed_es_oa_same_scope"],
        }
    if microagent == SELECT_CSTRING_LITERAL_AGENT and exact_article_swap(delta):
        return {
            "token_policy_status": STATUS_SUPERSEDED,
            "token_policy_action": ACTION_SUPERSEDED_ARTICLE,
            "shadow_ready": 0,
            "superseded_by": ARTICLE_AGENT,
            "block_reason": "",
            "reasons": [*reasons, "token_delta_is_article_swap_not_select_cstring_literal"],
        }
    if microagent == SELECT_CSTRING_LITERAL_AGENT and object_pronoun_delta(delta):
        return {
            "token_policy_status": STATUS_SUPERSEDED,
            "token_policy_action": ACTION_SUPERSEDED_OBJECT,
            "shadow_ready": 0,
            "superseded_by": "long_text_object_pronoun_case_microagent",
            "block_reason": "",
            "reasons": [*reasons, "token_delta_is_object_pronoun_component"],
        }
    if microagent == CONCEPT_LINK_AGENT:
        return {
            "token_policy_status": STATUS_BLOCKED,
            "token_policy_action": ACTION_BLOCK_CONTEXT,
            "shadow_ready": 0,
            "superseded_by": "",
            "block_reason": "concept_reference_added_requires_semantic_context",
            "reasons": [*reasons, "added_concept_reference_inside_semantic_paragraph"],
        }
    if microagent in {LEXICAL_GENDER_AGENT, SUBJECT_REFERENCE_AGENT}:
        return {
            "token_policy_status": STATUS_BLOCKED,
            "token_policy_action": ACTION_BLOCK_CONTEXT,
            "shadow_ready": 0,
            "superseded_by": "",
            "block_reason": "complex_lexical_gender_multi_token_requires_subsplit",
            "reasons": [*reasons, "multiple_added_or_removed_tokens", json.dumps(delta, ensure_ascii=False, sort_keys=True)],
        }
    if microagent == SPEAKER_GENDER_AGENT and complex_scope_change(delta):
        return {
            "token_policy_status": STATUS_BLOCKED,
            "token_policy_action": ACTION_BLOCK_CONTEXT,
            "shadow_ready": 0,
            "superseded_by": "",
            "block_reason": "speaker_gender_scope_change_requires_context",
            "reasons": [*reasons, "added_removed_scope_sets_differ", json.dumps(delta, ensure_ascii=False, sort_keys=True)],
        }
    return {
        "token_policy_status": STATUS_BLOCKED,
        "token_policy_action": ACTION_BLOCK_CONTEXT,
        "shadow_ready": 0,
        "superseded_by": "",
        "block_reason": "unclassified_token_policy_component",
        "reasons": [*reasons, json.dumps(delta, ensure_ascii=False, sort_keys=True)],
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    split_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fields = [
        "token_policy_item_id",
        "split_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "original_subpolicy_name",
        "repair_route",
        "microagent_key",
        "micro_issue_kind",
        "token_policy_status",
        "token_policy_action",
        "shadow_ready",
        "superseded_by",
        "block_reason",
        "partial_component_only",
        "production_release_allowed",
        "token_delta_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fields},
                "reasons": row.get("reasons") or [],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["token_policy_status"] for row in rows)
    by_microagent = Counter(row["microagent_key"] for row in rows)
    ready = [row for row in rows if row["shadow_ready"]]
    superseded = [row for row in rows if row["token_policy_status"] == STATUS_SUPERSEDED]
    blocked = [row for row in rows if row["token_policy_status"] == STATUS_BLOCKED]
    lines = [
        "Issue long-text mixed structural token-policy shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Shadow run id: {run_id}",
        f"Split run id: {split_run['id']}",
        f"Structural shadow run id: {split_run['structural_shadow_run_id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {len(ready):,}",
        f"- Superseded by sibling component: {len(superseded):,}",
        f"- Blocked: {len(blocked):,}",
        f"- By status: {json.dumps(dict(by_status), ensure_ascii=False, sort_keys=True)}",
        f"- By microagent: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        "",
        "Shadow-ready token components:",
    ]
    for row in ready:
        lines.extend(
            [
                f"- {row['microagent_key']} / {row['micro_issue_kind']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}",
                f"  action={row['token_policy_action']}",
            ]
        )
    if not ready:
        lines.append("- none")
    lines.extend(["", "Superseded components:"])
    for row in superseded:
        lines.append(
            f"- {row['microagent_key']} -> {row['superseded_by']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
        )
    if not superseded:
        lines.append("- none")
    lines.extend(["", "Blocked token components:"])
    for row in blocked:
        lines.append(
            f"- {row['microagent_key']} / {row['micro_issue_kind']} | block={row['block_reason']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- Shadow-ready means token-policy observation only, not output application.",
            "- Superseded rows are intentionally not counted as new capability because a sibling component already owns the token delta.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, split_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_split_run_id = split_run_id or latest_split_run_id(conn)
        split_run = fetch_split_run(conn, split_run_id=selected_split_run_id)
        rows = fetch_rows(conn, split_run_id=selected_split_run_id)
        if not rows:
            raise RuntimeError(f"Split run {selected_split_run_id} has no needs_token_policy items.")

        for row in rows:
            classified = classify(row)
            row.update(classified)
            row["partial_component_only"] = 1
            row["production_release_allowed"] = 0

        by_status = Counter(row["token_policy_status"] for row in rows)
        by_microagent = Counter(row["microagent_key"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_split_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_mixed_structural_token_policy_shadow_runs (
                rule_version,
                split_run_id,
                structural_shadow_run_id,
                policy_name,
                policy_status,
                candidate_count,
                shadow_ready_count,
                superseded_count,
                blocked_count,
                microagent_counts_json,
                status_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_split_run_id,
                int(split_run["structural_shadow_run_id"]),
                POLICY_NAME,
                POLICY_STATUS,
                len(rows),
                by_status[STATUS_READY],
                by_status[STATUS_SUPERSEDED],
                by_status[STATUS_BLOCKED],
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
        for row in rows:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_long_text_mixed_structural_token_policy_shadow_items (
                    run_id,
                    split_run_id,
                    split_item_id,
                    structural_shadow_run_id,
                    structural_shadow_item_id,
                    source_checkpoint_run_id,
                    source_checkpoint_item_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    original_subpolicy_name,
                    repair_route,
                    microagent_key,
                    micro_issue_kind,
                    token_policy_status,
                    token_policy_action,
                    shadow_ready,
                    superseded_by,
                    block_reason,
                    partial_component_only,
                    production_release_allowed,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    reasons_json,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_split_run_id,
                    int(row["id"]),
                    int(row["structural_shadow_run_id"]),
                    int(row["structural_shadow_item_id"]),
                    int(row["source_checkpoint_run_id"]),
                    int(row["source_checkpoint_item_id"]),
                    int(row["decision_run_id"]),
                    int(row["decision_id"]),
                    int(row["queue_item_id"]),
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["original_subpolicy_name"],
                    row["repair_route"],
                    row["microagent_key"],
                    row["micro_issue_kind"],
                    row["token_policy_status"],
                    row["token_policy_action"],
                    int(row["shadow_ready"]),
                    row.get("superseded_by") or "",
                    row.get("block_reason") or "",
                    1,
                    0,
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    row.get("token_delta_json") or "{}",
                    json.dumps(row.get("reasons") or [], ensure_ascii=False, sort_keys=True),
                    row.get("notes") or "",
                    created_at,
                ),
            )
            row["token_policy_item_id"] = int(item_cur.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            split_run=split_run,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    print("[issue_long_text_mixed_structural_token_policy_shadow] Shadow generated")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Shadow run id: {run_id}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Split run id: {selected_split_run_id}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Candidates: {len(rows):,}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Shadow ready: {by_status[STATUS_READY]:,}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Superseded: {by_status[STATUS_SUPERSEDED]:,}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Blocked: {by_status[STATUS_BLOCKED]:,}")
    print(f"[issue_long_text_mixed_structural_token_policy_shadow] Report: {txt_path}")
    return {
        "run_id": run_id,
        "split_run_id": selected_split_run_id,
        "candidate_count": len(rows),
        "shadow_ready_count": by_status[STATUS_READY],
        "superseded_count": by_status[STATUS_SUPERSEDED],
        "blocked_count": by_status[STATUS_BLOCKED],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shadow policy for token-policy components from mixed structural long-text split.")
    parser.add_argument("--split-run-id", type=int, default=None)
    args = parser.parse_args()
    main(split_run_id=args.split_run_id)
