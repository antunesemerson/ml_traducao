from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_long_text_structural_subpolicy_shadow_v1"
POLICY_NAME = "long_text_structural_subpolicy_shadow_v1"
POLICY_STATUS = "shadow"
SOURCE_CHECKPOINT_POLICY_NAME = "long_text_repair_route_shadow_v1"
SOURCE_BLOCK_REASON = "structural_token_change_requires_token_policy_review"

OBJECT_PRONOUN_CASE_SUBPOLICY = "long_text_object_pronoun_case_repair"
VISIBLE_ELE_ELA_SUBPOLICY = "long_text_visible_ele_ela_subject_token"
INVARIANT_WORD_TOKEN_REMOVAL_SUBPOLICY = "long_text_invariant_word_gender_token_removal"
MIXED_OBJECT_PRONOUN_SURFACE_SUBPOLICY = "long_text_mixed_object_pronoun_surface_repair"
MIXED_QUOTE_TOKEN_SUBPOLICY = "long_text_mixed_quote_token_surface_repair"
LEXICAL_GENDER_SPLIT_SUBPOLICY = "long_text_lexical_gender_select_cstring_split"
SELECT_CSTRING_STRUCTURAL_SUBPOLICY = "long_text_select_cstring_structural_literal_repair"
CONCEPT_PARAGRAPH_SUBPOLICY = "long_text_concept_paragraph_residual_rewrite"
UNCLASSIFIED_STRUCTURAL_SUBPOLICY = "long_text_unclassified_structural_repair"

SHADOW_ACTIONS = {
    OBJECT_PRONOUN_CASE_SUBPOLICY: "would_observe_object_pronoun_case_repair_shadow",
    VISIBLE_ELE_ELA_SUBPOLICY: "would_observe_visible_ele_ela_subject_token_shadow",
    INVARIANT_WORD_TOKEN_REMOVAL_SUBPOLICY: "would_observe_invariant_word_gender_token_removal_shadow",
    MIXED_OBJECT_PRONOUN_SURFACE_SUBPOLICY: "hold_mixed_object_pronoun_surface_for_more_evidence",
    MIXED_QUOTE_TOKEN_SUBPOLICY: "hold_mixed_quote_token_surface_for_more_evidence",
    LEXICAL_GENDER_SPLIT_SUBPOLICY: "hold_lexical_gender_split_for_token_policy",
    SELECT_CSTRING_STRUCTURAL_SUBPOLICY: "hold_select_cstring_structural_for_token_policy",
    CONCEPT_PARAGRAPH_SUBPOLICY: "hold_concept_paragraph_rewrite_for_semantic_policy",
    UNCLASSIFIED_STRUCTURAL_SUBPOLICY: "hold_unclassified_structural_for_manual_review",
}

GET_SHEHE_TOKEN_RE = re.compile(r"\[([A-Za-z_][\w.]*)\.GetSheHe(?:\|[^\]]+)?\]")
GET_HERHIM_TOKEN_RE = re.compile(r"\[([A-Za-z_][\w.]*)\.GetHerHim(?:\|[^\]]+)?\]")
GET_SUBJECT_TOKEN_RE = re.compile(r"\[([A-Za-z_][\w.]*)\.GetSheHe(?:\|[^\]]+)?\]")
INVARIANT_CERTEZA_RE = re.compile(r"certez\[[^\]]+?\.Custom\('ES_OA'\)[^\]]*\]a")


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_repair_route_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND blocked_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_CHECKPOINT_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No blocked checkpoint found for {SOURCE_CHECKPOINT_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_structural_subpolicy_shadow_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_structural_subpolicy_shadow_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            repair_route TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            shadow_status TEXT NOT NULL,
            shadow_action TEXT NOT NULL,
            shadow_ready INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            reasons_json TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_structural_subpolicy_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_structural_subpolicy_shadow_checkpoint_run_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_repair_route_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_repair_route_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
          AND block_reason = ?
          AND token_status = 'structural_token_change_review_required'
        ORDER BY repair_route, relative_path, source_line_number, source_key
        """,
        (checkpoint_run_id, SOURCE_BLOCK_REASON),
    ).fetchall()
    return [dict(row) for row in rows]


def token_delta(current: str, corrected: str) -> dict[str, Any]:
    current_tokens = structural_tokens(current)
    corrected_tokens = structural_tokens(corrected)
    removed = list((current_tokens - corrected_tokens).elements())
    added = list((corrected_tokens - current_tokens).elements())
    return {
        "removed": removed,
        "added": added,
        "removed_count": len(removed),
        "added_count": len(added),
    }


def exact_object_pronoun_case_repair(current: str, corrected: str) -> bool:
    current_matches = GET_SHEHE_TOKEN_RE.findall(current)
    corrected_matches = GET_HERHIM_TOKEN_RE.findall(corrected)
    if len(current_matches) != 1 or len(corrected_matches) != 1:
        return False
    if current_matches[0] != corrected_matches[0]:
        return False
    current_token = GET_SHEHE_TOKEN_RE.search(current)
    corrected_token = GET_HERHIM_TOKEN_RE.search(corrected)
    if not current_token or not corrected_token:
        return False
    return current.replace(current_token.group(0), corrected_token.group(0), 1) == corrected


def exact_visible_ele_ela_subject_token(current: str, corrected: str) -> bool:
    if current.count("ele/ela") != 1:
        return False
    matches = GET_SUBJECT_TOKEN_RE.findall(corrected)
    if len(matches) != 1:
        return False
    token = GET_SUBJECT_TOKEN_RE.search(corrected)
    if not token:
        return False
    return current.replace("ele/ela", token.group(0), 1) == corrected


def exact_invariant_certeza_token_removal(current: str, corrected: str) -> bool:
    if not INVARIANT_CERTEZA_RE.search(current):
        return False
    return INVARIANT_CERTEZA_RE.sub("certeza", current, count=1) == corrected


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("current_text") or ""
    corrected = row.get("corrected_text") or ""
    repair_route = row.get("repair_route") or ""
    delta = token_delta(current, corrected)
    reasons: list[str] = [repair_route, row.get("token_status") or ""]

    if exact_object_pronoun_case_repair(current, corrected):
        return {
            "subpolicy_name": OBJECT_PRONOUN_CASE_SUBPOLICY,
            "shadow_status": "shadow_ready",
            "shadow_ready": 1,
            "block_reason": "",
            "token_delta": delta,
            "reasons": [*reasons, "exact_getshehe_to_getherhim_only"],
        }
    if exact_visible_ele_ela_subject_token(current, corrected):
        return {
            "subpolicy_name": VISIBLE_ELE_ELA_SUBPOLICY,
            "shadow_status": "shadow_ready",
            "shadow_ready": 1,
            "block_reason": "",
            "token_delta": delta,
            "reasons": [*reasons, "exact_visible_ele_ela_to_subject_token_only"],
        }
    if exact_invariant_certeza_token_removal(current, corrected):
        return {
            "subpolicy_name": INVARIANT_WORD_TOKEN_REMOVAL_SUBPOLICY,
            "shadow_status": "shadow_ready",
            "shadow_ready": 1,
            "block_reason": "",
            "token_delta": delta,
            "reasons": [*reasons, "exact_invariant_certeza_token_removal"],
        }

    if repair_route == "object_pronoun_token_repair":
        subpolicy = MIXED_OBJECT_PRONOUN_SURFACE_SUBPOLICY
        block_reason = "mixed_object_pronoun_and_surface_or_semantic_delta"
    elif repair_route == "quote_surface_normalization":
        subpolicy = MIXED_QUOTE_TOKEN_SUBPOLICY
        block_reason = "mixed_quote_surface_and_token_delta"
    elif repair_route == "lexical_gender_split":
        subpolicy = LEXICAL_GENDER_SPLIT_SUBPOLICY
        block_reason = "lexical_gender_split_requires_more_examples"
    elif repair_route == "spanish_select_cstring_literal":
        subpolicy = SELECT_CSTRING_STRUCTURAL_SUBPOLICY
        block_reason = "select_cstring_structural_change_requires_subpolicy"
    elif repair_route == "concept_paragraph_spanish_residual":
        subpolicy = CONCEPT_PARAGRAPH_SUBPOLICY
        block_reason = "concept_paragraph_rewrite_requires_semantic_policy"
    else:
        subpolicy = UNCLASSIFIED_STRUCTURAL_SUBPOLICY
        block_reason = "unclassified_structural_change"

    return {
        "subpolicy_name": subpolicy,
        "shadow_status": "shadow_blocked",
        "shadow_ready": 0,
        "block_reason": block_reason,
        "token_delta": delta,
        "reasons": [*reasons, block_reason],
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    checkpoint: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fields = [
        "shadow_item_id",
        "checkpoint_item_id",
        "decision_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "repair_route",
        "subpolicy_name",
        "shadow_status",
        "shadow_action",
        "shadow_ready",
        "block_reason",
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
                "current_preview": short(row.get("current_text"), 220),
                "corrected_preview": short(row.get("corrected_text"), 220),
                "reasons": row.get("reasons") or [],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["shadow_status"] for row in rows)
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows)
    by_route = Counter(row["repair_route"] for row in rows)
    lines = [
        "Issue long-text structural subpolicy shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Shadow run id: {run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {by_status['shadow_ready']:,}",
        f"- Blocked: {by_status['shadow_blocked']:,}",
        f"- By subpolicy: {json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True)}",
        f"- By route: {json.dumps(dict(by_route), ensure_ascii=False, sort_keys=True)}",
        "",
        "Ready shadow items:",
    ]
    for row in [item for item in rows if item["shadow_ready"]][:25]:
        lines.extend(
            [
                (
                    f"- {row['subpolicy_name']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:"
                    f"{row['source_key']} | {row['shadow_action']}"
                ),
                f"  delta={row['token_delta_json']}",
                f"  corrected={short(row.get('corrected_text'), 220)}",
            ]
        )
    if not any(row["shadow_ready"] for row in rows):
        lines.append("- none")
    lines.extend(["", "Blocked items:"])
    for row in [item for item in rows if not item["shadow_ready"]][:40]:
        lines.extend(
            [
                (
                    f"- {row['subpolicy_name']} | block={row['block_reason']} | "
                    f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
                f"  delta={row['token_delta_json']}",
                f"  corrected={short(row.get('corrected_text'), 220)}",
            ]
        )
    if all(row["shadow_ready"] for row in rows):
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- This policy observes only narrow structural repair families already reviewed by humans.",
            "- Mixed semantic/token repairs remain blocked until they have a narrower subpolicy or more evidence.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint = fetch_checkpoint(conn, checkpoint_run_id=selected_checkpoint_run_id)
        rows = fetch_rows(conn, checkpoint_run_id=selected_checkpoint_run_id)
        if not rows:
            raise RuntimeError(f"Checkpoint run {selected_checkpoint_run_id} has no structural blocked items.")

        for row in rows:
            classified = classify_row(row)
            row.update(classified)
            row["shadow_action"] = SHADOW_ACTIONS[row["subpolicy_name"]]
            row["token_delta_json"] = json.dumps(classified["token_delta"], ensure_ascii=False, sort_keys=True)

        counts = Counter(row["shadow_status"] for row in rows)
        by_subpolicy = Counter(row["subpolicy_name"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_checkpoint_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_structural_subpolicy_shadow_runs (
                rule_version,
                checkpoint_run_id,
                policy_name,
                policy_status,
                candidate_count,
                shadow_ready_count,
                blocked_count,
                subpolicy_counts_json,
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
                selected_checkpoint_run_id,
                POLICY_NAME,
                POLICY_STATUS,
                len(rows),
                counts["shadow_ready"],
                counts["shadow_blocked"],
                json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True),
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
                INSERT INTO ml_issue_long_text_structural_subpolicy_shadow_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    repair_route,
                    subpolicy_name,
                    shadow_status,
                    shadow_action,
                    shadow_ready,
                    block_reason,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    reasons_json,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_checkpoint_run_id,
                    int(row["id"]),
                    int(row["decision_run_id"]),
                    int(row["decision_id"]),
                    int(row["queue_item_id"]),
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["repair_route"],
                    row["subpolicy_name"],
                    row["shadow_status"],
                    row["shadow_action"],
                    int(row["shadow_ready"]),
                    row["block_reason"],
                    sha256_text(row.get("current_text")),
                    sha256_text(row.get("corrected_text")),
                    row["token_delta_json"],
                    json.dumps(row.get("reasons") or [], ensure_ascii=False, sort_keys=True),
                    row.get("notes") or "",
                    created_at,
                ),
            )
            row["shadow_item_id"] = int(item_cur.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            checkpoint=checkpoint,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    print("[issue_long_text_structural_subpolicy_shadow] Shadow generated")
    print(f"[issue_long_text_structural_subpolicy_shadow] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_structural_subpolicy_shadow] Shadow run id: {run_id}")
    print(f"[issue_long_text_structural_subpolicy_shadow] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_long_text_structural_subpolicy_shadow] Candidates: {len(rows):,}")
    print(f"[issue_long_text_structural_subpolicy_shadow] Shadow ready: {counts['shadow_ready']:,}")
    print(f"[issue_long_text_structural_subpolicy_shadow] Blocked: {counts['shadow_blocked']:,}")
    print(f"[issue_long_text_structural_subpolicy_shadow] Report: {txt_path}")
    return {
        "run_id": run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "candidate_count": len(rows),
        "shadow_ready_count": counts["shadow_ready"],
        "blocked_count": counts["shadow_blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create shadow subpolicy records for structural long-text repair blockers.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
