from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from publication_token_mismatch_old_safe_fix_queue import (
    fetch_rows,
    latest_policy_run_id,
    latest_state_run_id,
)


RULE_VERSION = "publication_token_mismatch_nickname_select_policy_queue_v1"
DECISION = "accept_policy_candidate"
REVIEWER = "codex_publication_nickname_select_policy"


def reports_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_publication_token_mismatch_nickname_select_policy_queue"


def select_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        if row.get("relative_path") != "nicknames_l_spanish.yml":
            continue
        confirmed = row.get("confirmed_text") or ""
        source = row.get("spanish_text") or ""
        if "Select_CString" not in confirmed:
            continue
        if "el/la" not in source and "El " not in source and "el " not in source:
            continue
        selected.append({**row, "route": "accept_nickname_select_cstring_ptbr"})
    return selected


def write_outputs(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    state_run_id: int,
    policy_run_id: int,
) -> tuple[Path, Path, Path, Path, Path]:
    base = reports_base(settings)
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    csv_path = base.with_suffix(".csv")
    decisions_path = base.parent / f"{base.name}_decisions.jsonl"
    summary_path = base.parent / f"{base.name}_summary.json"
    bucket_counts = Counter(row.get("policy_bucket") or "<none>" for row in rows)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                "segment_id": row["segment_id"],
                "policy_item_id": row["policy_item_id"],
                "policy_run_id": policy_run_id,
                "state_run_id": state_run_id,
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "policy_bucket": row.get("policy_bucket"),
                "risk_level": row.get("risk_level"),
                "diff_kind": row.get("diff_kind"),
                "route": row["route"],
                "spanish_text": row.get("spanish_text"),
                "confirmed_text": row.get("confirmed_text"),
                "output_text": row.get("output_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                "policy_item_id": row["policy_item_id"],
                "segment_id": row["segment_id"],
                "decision": DECISION,
                "reviewer": REVIEWER,
                "notes": (
                    "Nickname source uses Spanish el/la helper; confirmed text uses PT-BR article/gender "
                    "handling and is a known visual hotfix family."
                ),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    fieldnames = [
        "segment_id",
        "policy_item_id",
        "relative_path",
        "source_key",
        "policy_bucket",
        "risk_level",
        "diff_kind",
        "spanish_text",
        "confirmed_text",
        "output_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})

    summary = {
        "source": RULE_VERSION,
        "state_run_id": state_run_id,
        "policy_run_id": policy_run_id,
        "selected_count": len(rows),
        "decision": DECISION,
        "bucket_counts": dict(bucket_counts),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# Publication Nickname Select Token Policy Queue\n\n")
        handle.write(f"- Source: `{RULE_VERSION}`\n")
        handle.write(f"- Segment-state run: `{state_run_id}`\n")
        handle.write(f"- Token policy run: `{policy_run_id}`\n")
        handle.write(f"- Selected: `{len(rows)}`\n")
        handle.write(f"- Decision: `{DECISION}`\n\n")
        for row in rows:
            handle.write(f"## Segment `{row['segment_id']}` - `{row['source_key']}`\n\n")
            handle.write(f"- Source: `{row.get('spanish_text')}`\n")
            handle.write(f"- Confirmed: `{row.get('confirmed_text')}`\n")
            handle.write(f"- Output: `{row.get('output_text')}`\n\n")

    return md_path, jsonl_path, csv_path, decisions_path, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-run-id", type=int)
    parser.add_argument("--policy-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    conn = db.connect(settings)
    try:
        state_run_id = args.state_run_id or latest_state_run_id(conn)
        policy_run_id = args.policy_run_id or latest_policy_run_id(conn, state_run_id)
        rows = select_rows(fetch_rows(conn, state_run_id=state_run_id, policy_run_id=policy_run_id))
        md_path, jsonl_path, csv_path, decisions_path, summary_path = write_outputs(
            settings,
            rows=rows,
            state_run_id=state_run_id,
            policy_run_id=policy_run_id,
        )
    finally:
        conn.close()

    print(f"Selected nickname policy rows: {len(rows)}")
    print(f"Markdown: {md_path}")
    print(f"JSONL: {jsonl_path}")
    print(f"CSV: {csv_path}")
    print(f"Decisions: {decisions_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
