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


RULE_VERSION = "issue_dynamic_select_cstring_pattern_audit_v1"
AGENT_KEY = "micro_dynamic_ck3_expression"
ISSUE_FAMILY = "dynamic_ck3_expression_microagent"
TARGET_BUCKETS = {"dynamic_select_cstring_long", "dynamic_select_cstring_short"}

SELECT_CSTRING_RE = re.compile(
    r"Select_CString\(\s*([^,]+?)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_select_cstring_pattern_audit"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_dynamic_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND issue_family = ?
          AND selected_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, ISSUE_FAMILY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed dynamic issue review queue found.")
    return int(row["id"])


def fetch_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            q.id AS queue_item_id,
            q.ledger_item_id,
            q.segment_id,
            q.relative_path,
            q.source_key,
            q.source_line_number,
            q.queue_bucket,
            q.issue_kind,
            q.evidence_text,
            q.english_text,
            q.confirmed_text,
            l.evidence_json AS ledger_evidence_json
        FROM ml_issue_review_queue_items q
        JOIN ml_issue_ledger_items l ON l.id = q.ledger_item_id
        WHERE q.run_id = ?
          AND q.agent_key = ?
          AND q.issue_family = ?
          AND q.queue_bucket IN ('dynamic_select_cstring_long', 'dynamic_select_cstring_short')
        ORDER BY q.queue_bucket, q.relative_path, q.source_line_number, q.source_key
        """,
        (queue_run_id, AGENT_KEY, ISSUE_FAMILY),
    ).fetchall()
    return [dict(row) for row in rows]


def normalize_condition(value: str) -> str:
    text = " ".join(value.strip().split())
    text = re.sub(r"\bROOT\.Char\b", "ROOT.Char", text)
    return text


def condition_family(condition: str) -> str:
    text = condition.casefold()
    if "islocalplayer" in text:
        return "local_player_branch"
    if "isfemale" in text:
        return "gender_branch"
    if "isplayer" in text:
        return "player_branch"
    if text.startswith("or(") or " or(" in text:
        return "compound_or_branch"
    if text.startswith("and(") or " and(" in text:
        return "compound_and_branch"
    return "other_branch"


def payload_family(left: str, right: str) -> str:
    joined = f"{left} || {right}".casefold()
    if left == right:
        return "same_literal_both_branches"
    if "você" in joined or "vocÃª" in joined:
        return "local_player_second_person_literal"
    if any(marker in joined for marker in ("getshortuiname", "gettitledfirstname", "getfirstname", "getname")):
        return "name_reference_literal"
    if any(marker in joined for marker in ("gobernador", "gobernadora", "rey", "reina")):
        return "spanish_title_literal"
    if any(marker in joined for marker in ("te ", "se ", "tu ", "su ", "tus ", "sus ")):
        return "spanish_pronoun_or_verb_literal"
    return "other_literal_payload"


def evaluate_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    text = row.get("confirmed_text") or row.get("evidence_text") or ""
    matches = list(SELECT_CSTRING_RE.finditer(text))
    outputs: list[dict[str, Any]] = []
    for index, match in enumerate(matches, start=1):
        condition = normalize_condition(match.group(1))
        left = match.group(2)
        right = match.group(3)
        outputs.append(
            {
                **row,
                "select_index": index,
                "condition": condition,
                "left_literal": left,
                "right_literal": right,
                "condition_family": condition_family(condition),
                "payload_family": payload_family(left, right),
                "same_payload": 1 if left == right else 0,
                "spanish_suspect": 1
                if payload_family(left, right) in {"spanish_title_literal", "spanish_pronoun_or_verb_literal"}
                else 0,
            }
        )
    if not outputs:
        outputs.append(
            {
                **row,
                "select_index": 0,
                "condition": "",
                "left_literal": "",
                "right_literal": "",
                "condition_family": "no_select_cstring_match",
                "payload_family": "no_select_cstring_match",
                "same_payload": 0,
                "spanish_suspect": 0,
            }
        )
    return outputs


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    queue_run_id: int,
    rows: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]],
) -> None:
    fields = [
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "select_index",
        "condition_family",
        "payload_family",
        "same_payload",
        "spanish_suspect",
        "condition",
        "left_literal",
        "right_literal",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in audit_rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in audit_rows:
            payload = {field: row.get(field) for field in fields}
            payload["text_preview"] = short(row.get("confirmed_text") or row.get("evidence_text"))
            payload["english_preview"] = short(row.get("english_text"))
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    condition_counts = Counter(row["condition_family"] for row in audit_rows)
    payload_counts = Counter(row["payload_family"] for row in audit_rows)
    combo_counts = Counter((row["condition_family"], row["payload_family"]) for row in audit_rows)
    path_counts = Counter(row["relative_path"] for row in audit_rows)
    suspect_count = sum(int(row["spanish_suspect"]) for row in audit_rows)
    same_payload_count = sum(int(row["same_payload"]) for row in audit_rows)
    lines = [
        "Issue dynamic Select_CString pattern audit",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        "",
        "Summary:",
        f"- Queue rows: {len(rows):,}",
        f"- Select_CString observations: {len(audit_rows):,}",
        f"- Same payload observations: {same_payload_count:,}",
        f"- Spanish-suspect payload observations: {suspect_count:,}",
        "",
        "Condition families:",
        *[f"- {key}: {value:,}" for key, value in condition_counts.most_common()],
        "",
        "Payload families:",
        *[f"- {key}: {value:,}" for key, value in payload_counts.most_common()],
        "",
        "Top condition/payload combos:",
        *[f"- {condition} / {payload}: {count:,}" for (condition, payload), count in combo_counts.most_common(20)],
        "",
        "Top paths:",
        *[f"- {path}: {count:,}" for path, count in path_counts.most_common(20)],
        "",
        "Samples:",
    ]
    for row in audit_rows[:40]:
        lines.append(
            f"- {row['condition_family']} / {row['payload_family']} | "
            f"{row['relative_path']}::{row['source_key']} | {short(row.get('confirmed_text') or row.get('evidence_text'), 180)}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_queue_run_id = queue_run_id or latest_dynamic_queue_run_id(conn)
        rows = fetch_rows(conn, queue_run_id=selected_queue_run_id)
    audit_rows = [item for row in rows for item in evaluate_row(row)]
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        queue_run_id=selected_queue_run_id,
        rows=rows,
        audit_rows=audit_rows,
    )
    print("[issue_dynamic_select_cstring_pattern_audit] Audit generated")
    print(f"[issue_dynamic_select_cstring_pattern_audit] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_select_cstring_pattern_audit] Queue run id: {selected_queue_run_id}")
    print(f"[issue_dynamic_select_cstring_pattern_audit] Rows: {len(rows):,}")
    print(f"[issue_dynamic_select_cstring_pattern_audit] Observations: {len(audit_rows):,}")
    print(f"[issue_dynamic_select_cstring_pattern_audit] Report: {txt_path}")
    print(f"[issue_dynamic_select_cstring_pattern_audit] CSV: {csv_path}")
    print(f"[issue_dynamic_select_cstring_pattern_audit] JSONL: {jsonl_path}")
    return {
        "queue_run_id": selected_queue_run_id,
        "rows": len(rows),
        "observations": len(audit_rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit dynamic Select_CString patterns from a dynamic issue queue.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
