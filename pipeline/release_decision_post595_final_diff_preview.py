from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post595_final_diff_preview_v1"
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!")
SPANISH_RE = re.compile(r"\b(intento|llamar|atenci[oó]n|inter[eé]s|bueno|ciertamente|debido)\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for final post595 corrected_text decisions.")
    parser.add_argument("--extract-jsonl", type=Path, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def strip_tokens(text: str) -> str:
    return TOKEN_RE.sub(" ", text or "")


def canonical(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def structure_signature(text: str) -> dict[str, int]:
    text = text or ""
    return {
        "line_count": text.count("\n") + 1 if text else 0,
        "open_brackets": text.count("["),
        "close_brackets": text.count("]"),
        "open_tags": len(re.findall(r"#[A-Za-z0-9_]+", text)),
        "close_tags": text.count("#!"),
        "dollar_count": text.count("$"),
    }


def db_output(conn, segment_id: int) -> str:
    row = conn.execute("SELECT portuguese_text FROM output_segments WHERE segment_id=?", (segment_id,)).fetchone()
    return str(row["portuguese_text"] or "") if row else ""


def main() -> None:
    args = parse_args()
    rows = read_jsonl(args.extract_jsonl)
    conn = db.connect(db.load_settings())
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        if row.get("validation_status") != "ready_for_diff_preview":
            records.append({"source": SOURCE, "segment_id": segment_id, "validation_status": "hold_not_corrected_text", "human_decision": row.get("human_decision"), "block_reasons": row.get("block_reasons") or []})
            continue
        output = str(row.get("output_text") or "")
        corrected = str(row.get("corrected_text") or "")
        reasons: list[str] = []
        if tokens(output) != tokens(corrected):
            reasons.append("token_integrity_mismatch")
        if structure_signature(output) != structure_signature(corrected):
            reasons.append("structure_integrity_mismatch")
        if canonical(output) == canonical(corrected):
            reasons.append("canonical_l10n_no_change")
        if canonical(db_output(conn, segment_id)) != canonical(output):
            reasons.append("source_output_changed_during_preview")
        if SPANISH_RE.search(strip_tokens(corrected)):
            reasons.append("spanish_residue_still_visible")
        status = "ready_for_protected_apply" if not reasons else "blocked"
        diff = "\n".join(difflib.unified_diff(output.splitlines(), corrected.splitlines(), fromfile=f"{segment_id}:current_output", tofile=f"{segment_id}:corrected_text", lineterm=""))
        records.append({
            "source": SOURCE,
            "segment_id": segment_id,
            "source_key": row.get("source_key"),
            "relative_path": row.get("relative_path"),
            "current_output_text": output,
            "corrected_text": corrected,
            "diff_preview": diff,
            "token_integrity_ok": "token_integrity_mismatch" not in reasons,
            "structure_integrity_ok": "structure_integrity_mismatch" not in reasons,
            "canonical_l10n_ok": "canonical_l10n_no_change" not in reasons,
            "source_output_unchanged": "source_output_changed_during_preview" not in reasons,
            "issue_resolution_status": "resolvable_by_corrected_text_or_superseded" if not reasons else "blocked_issue_resolution_uncertain",
            "validation_status": status,
            "block_reasons": reasons,
        })
    status_counts = Counter(row["validation_status"] for row in records)
    reason_counts = Counter(reason for row in records for reason in row.get("block_reasons", []))
    ready_ids = [row["segment_id"] for row in records if row["validation_status"] == "ready_for_protected_apply"]
    blocked_ids = [row["segment_id"] for row in records if row["validation_status"] == "blocked"]
    hold_ids = [row["segment_id"] for row in records if row["validation_status"] == "hold_not_corrected_text"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_diff_preview",
        "corrected_text_count": status_counts.get("ready_for_protected_apply", 0) + status_counts.get("blocked", 0),
        "ready_count": status_counts.get("ready_for_protected_apply", 0),
        "blocked_count": status_counts.get("blocked", 0),
        "hold_count": status_counts.get("hold_not_corrected_text", 0),
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(reason_counts.most_common()),
        "ready_ids": ready_ids,
        "blocked_ids": blocked_ids,
        "hold_ids": hold_ids,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    base = reports_dir() / f"{stamp()}_release_decision_post595_final_diff_preview"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = ["# Post-595 Final Diff Preview", "", f"- ready_ids: {ready_ids}", f"- blocked_ids: {blocked_ids}", f"- hold_ids: {hold_ids}", ""]
    for record in records:
        md_lines.extend([f"## {record['segment_id']} - {record.get('validation_status')}", f"- reasons: `{record.get('block_reasons')}`", ""])
        if record.get("diff_preview"):
            md_lines.extend(["```diff", record["diff_preview"], "```", ""])
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"corrected_text_count={summary['corrected_text_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"hold_count={summary['hold_count']}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False)}")
    print(f"ready_ids={ready_ids}")
    print(f"blocked_ids={blocked_ids}")
    print(f"hold_ids={hold_ids}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
