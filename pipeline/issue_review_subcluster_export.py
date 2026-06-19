from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_review_subcluster_export_v1"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_number} is not an object: {path}")
        rows.append(payload)
    return rows


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)


def output_paths(settings: dict[str, Any], source_path: Path, label: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:10]
    stem = safe_name(source_path.stem)[:42]
    base = reports_dir / f"{stamp}_issue_review_subcluster_export_{safe_name(label)}_{stem}_{digest}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def export_rows(
    *,
    subcluster_jsonl: str,
    decision_override: str | None = None,
    reviewer: str | None = None,
    notes_suffix: str | None = None,
    label: str = "cluster_decisions",
) -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(subcluster_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    rows = load_jsonl(source_path)
    output_path, report_path = output_paths(settings, source_path, label)
    counts: Counter[str] = Counter()
    samples: list[str] = []
    exported: list[dict[str, Any]] = []

    for payload in rows:
        decision = dict(payload.get("decision") or {})
        queue = payload.get("queue") or {}
        if not decision:
            counts["skipped_missing_decision"] += 1
            continue
        if decision_override:
            decision["decision"] = decision_override
        if reviewer:
            decision["reviewer"] = reviewer
        suffix_parts = [RULE_VERSION]
        if notes_suffix:
            suffix_parts.append(notes_suffix)
        suffix = "; ".join(suffix_parts)
        decision["notes"] = (str(decision.get("notes") or "").strip() + "; " + suffix).strip("; ")
        exported.append(decision)
        counts["exported"] += 1
        counts[f"decision_{decision.get('decision') or 'unknown'}"] += 1
        if len(samples) < 20:
            samples.append(
                "- "
                f"{decision.get('decision')} | "
                f"{queue.get('relative_path')}:{queue.get('source_line_number')}:{queue.get('source_key')} | "
                f"cluster={payload.get('cluster')} owner={payload.get('owner_agent')}"
            )

    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in exported:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue review subcluster export",
        f"Rule version: {RULE_VERSION}",
        f"Source: {source_path}",
        f"Output: {output_path}",
        f"Decision override: {decision_override or 'none'}",
        f"Reviewer: {reviewer or 'from rows'}",
        "",
        "Counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Samples:",
        *(samples or ["- none"]),
        "",
        "Safety note:",
        "- Export only: no database writes, no source/output writes and no model training.",
        "- The generated JSONL must still pass issue-review-ingest validation.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_review_subcluster_export] Export generated")
    print(f"[issue_review_subcluster_export] Source: {source_path}")
    print(f"[issue_review_subcluster_export] Output: {output_path}")
    print(f"[issue_review_subcluster_export] Report: {report_path}")
    for key, value in counts.most_common():
        print(f"[issue_review_subcluster_export] {key}: {value:,}")
    return {
        "output_path": str(output_path),
        "report_path": str(report_path),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export issue-review subcluster rows as issue-review-ingest decisions.")
    parser.add_argument("--subcluster-jsonl", required=True)
    parser.add_argument("--decision", default=None, help="Optional decision override for all exported rows.")
    parser.add_argument("--reviewer", default=None)
    parser.add_argument("--notes-suffix", default=None)
    parser.add_argument("--label", default="cluster_decisions")
    args = parser.parse_args()
    export_rows(
        subcluster_jsonl=args.subcluster_jsonl,
        decision_override=args.decision,
        reviewer=args.reviewer,
        notes_suffix=args.notes_suffix,
        label=args.label,
    )
