from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_select_cstring_same_token_lifecycle_policy_v1"
POLICY_NAME = "select_cstring_same_token_shadow_lifecycle_v1"
POLICY_STATUS = "shadow"
POLICY_ACTION = "observe_select_cstring_same_token_shadow"
PRODUCTION_RELEASE_ALLOWED = 0

SOURCE_SPECS = [
    {
        "family": "dynamic_literal_payload",
        "run_table": "ml_issue_dynamic_token_literal_repair_checkpoint_runs",
        "item_table": "ml_issue_dynamic_token_literal_repair_checkpoint_items",
        "policy_name": "dynamic_token_literal_payload_repair_shadow_v1",
        "run_ref_column": "run_id",
        "item_id_column": "id",
    },
    {
        "family": "auxiliary_sentence",
        "run_table": "ml_issue_select_cstring_auxiliary_rewrite_checkpoint_runs",
        "item_table": "ml_issue_select_cstring_auxiliary_rewrite_checkpoint_items",
        "policy_name": "select_cstring_auxiliary_sentence_rewrite_shadow_v1",
        "run_ref_column": "run_id",
        "item_id_column": "id",
    },
    {
        "family": "visible_label",
        "run_table": "ml_issue_select_cstring_label_rewrite_checkpoint_runs",
        "item_table": "ml_issue_select_cstring_label_rewrite_checkpoint_items",
        "policy_name": "select_cstring_visible_label_rewrite_shadow_v1",
        "run_ref_column": "run_id",
        "item_id_column": "id",
    },
    {
        "family": "object_sentence",
        "run_table": "ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs",
        "item_table": "ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items",
        "policy_name": "select_cstring_object_sentence_rewrite_shadow_v1",
        "run_ref_column": "run_id",
        "item_id_column": "id",
    },
    {
        "family": "antpath_relation",
        "run_table": "ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs",
        "item_table": "ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items",
        "policy_name": "select_cstring_antpath_relation_rewrite_shadow_v1",
        "run_ref_column": "run_id",
        "item_id_column": "id",
    },
]

BLOCKING_VALIDATION_CODES = {
    "spanish_punctuation",
    "mojibake_or_unexpected_script",
    "utf8_mojibake_sequence",
    "replacement_question_mark_mojibake",
    "spanish_residue",
    "spanish_residue_in_literal",
    "gender_token_extra_suffix",
}


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def latest_run_id(conn, spec: dict[str, str], explicit_runs: dict[str, int]) -> int | None:
    if spec["family"] in explicit_runs:
        return explicit_runs[spec["family"]]
    if not table_exists(conn, spec["run_table"]):
        return None
    row = conn.execute(
        f"""
        SELECT id
        FROM {spec['run_table']}
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND allowed_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (spec["policy_name"],),
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_same_token_lifecycle_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            source_run_ids_json TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            source_counts_json TEXT,
            subpolicy_counts_json TEXT,
            block_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_same_token_lifecycle_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_family TEXT NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_checkpoint_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            token_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            validation_issues_json TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_same_token_lifecycle_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_same_token_lifecycle_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows_for_spec(conn, spec: dict[str, str], run_id: int) -> list[dict[str, Any]]:
    if not table_exists(conn, spec["item_table"]):
        return []
    rows = conn.execute(
        f"""
        SELECT
            '{spec['family']}' AS source_family,
            item.{spec['run_ref_column']} AS source_checkpoint_run_id,
            item.{spec['item_id_column']} AS source_checkpoint_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.agent_key,
            item.subpolicy_name,
            item.checkpoint_allowed,
            item.checkpoint_action,
            item.block_reason,
            item.token_status,
            item.current_text,
            item.corrected_text,
            item.reasons_json
        FROM {spec['item_table']} item
        WHERE item.{spec['run_ref_column']} = ?
          AND item.checkpoint_allowed = 1
        ORDER BY item.segment_id, item.id
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in BLOCKING_VALIDATION_CODES
    ]


def evaluate_row(row: dict[str, Any], *, duplicate_segments: set[int], global_reasons: list[str]) -> tuple[int, str, list[dict[str, Any]]]:
    current = row.get("current_text") or ""
    corrected = row.get("corrected_text") or ""
    validation_issues = blocking_validation_issues(corrected)
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons), validation_issues
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("block_reason") or "checkpoint_item_not_allowed", validation_issues
    if row.get("block_reason"):
        return 0, "checkpoint_item_has_block_reason", validation_issues
    if row.get("token_status") != "same_structural_tokens":
        return 0, "token_status_not_same_structural_tokens", validation_issues
    if not current.strip() or not corrected.strip():
        return 0, "missing_text", validation_issues
    if current == corrected:
        return 0, "no_text_delta", validation_issues
    if structural_tokens(current) != structural_tokens(corrected):
        return 0, "structural_token_delta_detected", validation_issues
    if int(row["segment_id"]) in duplicate_segments:
        return 0, "duplicate_segment_requires_composition_arbitration", validation_issues
    if validation_issues:
        return 0, "blocking_validation_issue", validation_issues
    return 1, "", validation_issues


def parse_explicit_run_ids(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    result: dict[str, int] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        family, sep, value = part.partition("=")
        if not sep:
            raise RuntimeError(f"Invalid --source-run format: {part!r}; expected family=id.")
        result[family.strip()] = int(value.strip())
    return result


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    source_run_ids: dict[str, int],
    source_counts: Counter[str],
    subpolicy_counts: Counter[str],
    block_counts: Counter[str],
) -> None:
    fieldnames = [
        "policy_item_id",
        "source_family",
        "source_checkpoint_run_id",
        "source_checkpoint_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "agent_key",
        "subpolicy_name",
        "checkpoint_action",
        "token_status",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "production_release_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "current_hash": row.get("current_text_hash"),
                "corrected_hash": row.get("corrected_text_hash"),
                "validation_issues": row.get("validation_issues") or [],
                "reasons": row.get("reasons") or [],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    released = [row for row in rows if row["policy_allowed"]]
    blocked = [row for row in rows if not row["policy_allowed"]]
    lines = [
        "Issue Select_CString same-token lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy status: {POLICY_STATUS}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy run id: {policy_run_id}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        f"Source run ids: {json.dumps(source_run_ids, ensure_ascii=False, sort_keys=True)}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Released shadow: {len(released):,}",
        f"- Blocked: {len(blocked):,}",
        "",
        "By source family:",
        *[f"- {key}: {value:,}" for key, value in sorted(source_counts.items())],
        "",
        "By subpolicy:",
        *[f"- {key}: {value:,}" for key, value in sorted(subpolicy_counts.items())],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in sorted(block_counts.items())] or ["- none"]),
        "",
        "Released samples:",
    ]
    for row in released[:50]:
        lines.extend(
            [
                (
                    f"- {row['source_family']} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']} subpolicy={row['subpolicy_name']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
            ]
        )
    lines.append("")
    lines.append("Blocked samples:")
    if blocked:
        for row in blocked[:50]:
            lines.extend(
                [
                    (
                        f"- {row['source_family']} segment={row['segment_id']} "
                        f"{row['relative_path']}::{row['source_key']} block={row['block_reason']}"
                    ),
                    f"  token_status: {row['token_status']}",
                    f"  validation_issues: {json.dumps(row.get('validation_issues') or [], ensure_ascii=False)}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Lifecycle observation only; no source/output files read and no output writes.",
            "- Production release remains disabled; a production bridge must explicitly consume this lifecycle later.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Create lifecycle observation for mature same-token Select_CString shadow repairs.")
    parser.add_argument(
        "--source-runs",
        default=None,
        help="Optional comma list family=id, e.g. dynamic_literal_payload=3,auxiliary_sentence=2",
    )
    args = parser.parse_args()

    explicit_runs = parse_explicit_run_ids(args.source_runs)
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        source_run_ids: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        for spec in SOURCE_SPECS:
            run_id = latest_run_id(conn, spec, explicit_runs)
            if run_id is None:
                continue
            source_run_ids[spec["family"]] = run_id
            candidates.extend(fetch_rows_for_spec(conn, spec, run_id))

        now = datetime.now().isoformat(timespec="seconds")
        txt_path, csv_path, jsonl_path = report_paths(settings)
        global_reasons: list[str] = []
        if POLICY_STATUS != "shadow":
            global_reasons.append("policy_status_must_remain_shadow")
        if not candidates:
            global_reasons.append("no_lifecycle_candidates")

        segment_counts = Counter(int(row["segment_id"]) for row in candidates)
        duplicate_segments = {segment_id for segment_id, count in segment_counts.items() if count > 1}

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_same_token_lifecycle_runs (
                rule_version,
                policy_name,
                policy_status,
                policy_action,
                source_run_ids_json,
                production_release_allowed,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                POLICY_ACTION,
                json.dumps(source_run_ids, ensure_ascii=False, sort_keys=True),
                PRODUCTION_RELEASE_ALLOWED,
                now,
                now,
            ),
        )
        policy_run_id = int(cursor.lastrowid)

        rows: list[dict[str, Any]] = []
        source_counts: Counter[str] = Counter()
        subpolicy_counts: Counter[str] = Counter()
        block_counts: Counter[str] = Counter()
        for candidate in candidates:
            allowed, block_reason, validation_issues = evaluate_row(
                candidate,
                duplicate_segments=duplicate_segments,
                global_reasons=global_reasons,
            )
            reasons = json.loads(candidate["reasons_json"]) if candidate.get("reasons_json") else []
            row = {
                **candidate,
                "policy_action": POLICY_ACTION,
                "policy_allowed": allowed,
                "block_reason": block_reason,
                "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                "current_text_hash": sha256_text(candidate.get("current_text")),
                "corrected_text_hash": sha256_text(candidate.get("corrected_text")),
                "validation_issues": validation_issues,
                "reasons": reasons,
            }
            source_counts[row["source_family"]] += 1
            if allowed:
                subpolicy_counts[row["subpolicy_name"]] += 1
            else:
                block_counts[block_reason or "blocked"] += 1
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_same_token_lifecycle_items (
                    run_id,
                    source_family,
                    source_checkpoint_run_id,
                    source_checkpoint_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    subpolicy_name,
                    checkpoint_action,
                    token_status,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    production_release_allowed,
                    current_text_hash,
                    corrected_text_hash,
                    validation_issues_json,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    row["source_family"],
                    row["source_checkpoint_run_id"],
                    row["source_checkpoint_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["token_status"],
                    row["policy_action"],
                    row["policy_allowed"],
                    row["block_reason"],
                    row["production_release_allowed"],
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    json.dumps(row["validation_issues"], ensure_ascii=False, sort_keys=True),
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row["policy_item_id"] = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            rows.append(row)

        finished_at = datetime.now().isoformat(timespec="seconds")
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            rows=rows,
            source_run_ids=source_run_ids,
            source_counts=source_counts,
            subpolicy_counts=subpolicy_counts,
            block_counts=block_counts,
        )
        released_count = sum(1 for row in rows if row["policy_allowed"])
        blocked_count = len(rows) - released_count
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_same_token_lifecycle_runs
            SET candidate_count = ?,
                released_count = ?,
                blocked_count = ?,
                source_counts_json = ?,
                subpolicy_counts_json = ?,
                block_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                released_count,
                blocked_count,
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                policy_run_id,
            ),
        )
        conn.commit()

    payload = {
        "policy_run_id": policy_run_id,
        "candidate_count": len(rows),
        "released_count": released_count,
        "blocked_count": blocked_count,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "source_run_ids": source_run_ids,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    main()
