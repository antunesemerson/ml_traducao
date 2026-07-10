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


SOURCE = "release_decision_post592_top15_diff_preview_v1"
DEFAULT_EXTRACT_JSONL = Path("reports/20260704_005603_682993_release_decision_post592_top15_human_decision_extract.jsonl")
SPANISH_RE = re.compile(
    r"\b(ninguna de tus|puede sostener|obligaci[oó]n|puestos de la corte|por las|tu c[oó]nyuge|"
    r"ya no puedes|un halago|podr[ií]a ofenderse|compa[ñn]ero|v[ií]nculo|guantelete|la gente|"
    r"mayor\b|esto terminar[aá])",
    re.IGNORECASE,
)
PROTECTED_RE = re.compile(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for post-592 top15 corrected_text decisions.")
    parser.add_argument("--extract-jsonl", type=Path, default=DEFAULT_EXTRACT_JSONL)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def tokens(text: str) -> list[str]:
    return PROTECTED_RE.findall(text or "")


def strip_protected(text: str) -> str:
    return PROTECTED_RE.sub(" ", text or "")


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


def canonical(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def db_output(conn, segment_id: int) -> str:
    row = conn.execute("SELECT portuguese_text FROM output_segments WHERE segment_id=?", (segment_id,)).fetchone()
    return str(row["portuguese_text"] or "") if row else ""


def classify_issue_resolution(row: dict[str, Any], corrected: str) -> tuple[str, list[str]]:
    kinds = str(row.get("issue_kinds") or "")
    families = str(row.get("issue_families") or "")
    reasons: list[str] = []
    outside = strip_protected(corrected)
    if SPANISH_RE.search(outside):
        reasons.append("spanish_residue_still_visible_outside_tokens")
    if "concept_expression" in kinds and "Concept(" in corrected:
        reasons.append("concept_expression_preserved_for_token_integrity")
    if "short_label" in families or "short_label" in kinds:
        reasons.append("short_label_issue_resolved_by_ptbr_rewrite")
    if "spanish_residual" in families or "spanish_residue" in kinds:
        reasons.append("spanish_residue_issue_resolved_by_ptbr_rewrite")
    if not reasons:
        reasons.append("human_corrected_text_resolves_visible_issue")
    blocking = [r for r in reasons if r == "spanish_residue_still_visible_outside_tokens"]
    if blocking:
        return "blocked_issue_resolution_uncertain", reasons
    return "resolvable_by_corrected_text_or_superseded", reasons


def main() -> None:
    args = parse_args()
    rows = read_jsonl(args.extract_jsonl)
    conn = db.connect(db.load_settings())
    records: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        if row.get("validation_status") != "ready_for_diff_preview":
            records.append(
                {
                    "source": SOURCE,
                    "segment_id": segment_id,
                    "validation_status": "hold_not_corrected_text",
                    "human_decision": row.get("human_decision"),
                    "block_reasons": row.get("block_reasons") or [],
                }
            )
            continue
        original = str(row.get("output_text") or "")
        current = db_output(conn, segment_id)
        corrected = str(row.get("corrected_text") or "")
        token_ok = tokens(original) == tokens(corrected)
        structure_ok = structure_signature(original) == structure_signature(corrected)
        canonical_ok = canonical(original) != canonical(corrected)
        current_output_ok = canonical(current) == canonical(original)
        issue_status, issue_reasons = classify_issue_resolution(row, corrected)
        reasons: list[str] = []
        if not token_ok:
            reasons.append("token_integrity_mismatch")
        if not structure_ok:
            reasons.append("structure_integrity_mismatch")
        if not canonical_ok:
            reasons.append("canonical_l10n_no_change")
        if not current_output_ok:
            reasons.append("current_output_does_not_match_preview_base")
        if issue_status.startswith("blocked"):
            reasons.append(issue_status)
        status = "ready_for_protected_apply" if not reasons else "blocked"
        diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                corrected.splitlines(),
                fromfile=f"{segment_id}:current_output",
                tofile=f"{segment_id}:corrected_text",
                lineterm="",
            )
        )
        records.append(
            {
                "source": SOURCE,
                "segment_id": segment_id,
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "human_decision": row.get("human_decision"),
                "current_output_text": original,
                "corrected_text": corrected,
                "diff_preview": diff,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": structure_ok,
                "canonical_l10n_ok": canonical_ok,
                "current_output_text_ok": current_output_ok,
                "issue_resolution_status": issue_status,
                "issue_resolution_reasons": issue_reasons,
                "validation_status": status,
                "block_reasons": reasons,
            }
        )
    status_counts = Counter(row["validation_status"] for row in records)
    reason_counts = Counter(reason for row in records for reason in row.get("block_reasons", []))
    ready_ids = [row["segment_id"] for row in records if row["validation_status"] == "ready_for_protected_apply"]
    hold_ids = [row["segment_id"] for row in records if row["validation_status"] == "hold_not_corrected_text"]
    blocked_ids = [row["segment_id"] for row in records if row["validation_status"] == "blocked"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_diff_preview",
        "input_extract_jsonl": str(args.extract_jsonl),
        "record_count": len(records),
        "corrected_text_count": status_counts.get("ready_for_protected_apply", 0) + status_counts.get("blocked", 0),
        "ready_count": status_counts.get("ready_for_protected_apply", 0),
        "blocked_count": status_counts.get("blocked", 0),
        "hold_count": status_counts.get("hold_not_corrected_text", 0),
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(reason_counts.most_common()),
        "ready_ids": ready_ids,
        "blocked_ids": blocked_ids,
        "hold_concept_ids": hold_ids,
        "token_integrity_ok_count": sum(1 for row in records if row.get("token_integrity_ok") is True),
        "structure_integrity_ok_count": sum(1 for row in records if row.get("structure_integrity_ok") is True),
        "canonical_l10n_ok_count": sum(1 for row in records if row.get("canonical_l10n_ok") is True),
        "open_high_issues_resolvable_count": sum(
            1 for row in records if row.get("issue_resolution_status") == "resolvable_by_corrected_text_or_superseded"
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "single_operational_recommendation": "Run protected apply only for ready_ids in a separate approved cycle.",
    }
    base = reports_dir() / f"{stamp()}_release_decision_post592_top15_diff_preview"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Post-592 Top 15 Diff Preview",
        "",
        f"- corrected_text_count: {summary['corrected_text_count']}",
        f"- ready_count: {summary['ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        f"- hold_count: {summary['hold_count']}",
        f"- ready_ids: {ready_ids}",
        f"- blocked_ids: {blocked_ids}",
        f"- hold_concept_ids: {hold_ids}",
        "",
    ]
    for record in records:
        md_lines.extend(
            [
                f"## {record['segment_id']} - {record.get('validation_status')}",
                f"- source_key: `{record.get('source_key')}`",
                f"- token_integrity_ok: `{record.get('token_integrity_ok')}`",
                f"- structure_integrity_ok: `{record.get('structure_integrity_ok')}`",
                f"- canonical_l10n_ok: `{record.get('canonical_l10n_ok')}`",
                f"- issue_resolution_status: `{record.get('issue_resolution_status')}`",
                f"- reasons: `{record.get('block_reasons')}`",
                "",
            ]
        )
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
    print(f"hold_concept_ids={hold_ids}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
