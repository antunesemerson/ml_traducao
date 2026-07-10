from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "candidate_generation_preflight_guard_v1"

BLOCKING_LOCAL_LABELS = {
    "semantic_error",
    "structure_error",
    "token_mismatch",
    "wrong",
    "rejected",
    "rejected_suggestion",
}
CONTEXT_DECISIONS = {"needs_more_context"}
CORRECTION_LABELS = {"minor_fix", "major_fix", "semantic_error", "residual_spanish"}
CONTEXT_RISK_PATTERNS = [
    {
        "pattern_key": "spanish_muchos_mas_family",
        "regex": r"\bmuch[oa]s?\s+m(?:á|Ã¡|a)s\b",
        "policy": (
            "context-risk: do not auto-convert to 'muitos mais'; use 'muito mais' only "
            "with human approval or an explicit contextual rule"
        ),
    }
]


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def latest_files(pattern: str, limit: int) -> list[Path]:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:limit]


def short(text: str, limit: int = 180) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def local_learning_signals(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            id,
            segment_id,
            relative_path,
            source_key,
            human_label,
            corrected_text,
            suggested_text,
            current_output_text,
            queue_source,
            origin,
            reviewer,
            reviewed_at,
            updated_at
        FROM local_learning_candidates
        WHERE human_label IS NOT NULL
          AND trim(human_label) != ''
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    blocked: dict[int, list[dict[str, Any]]] = defaultdict(list)
    superseded: dict[int, list[dict[str, Any]]] = defaultdict(list)
    label_counts: Counter[str] = Counter()
    corrected_rows = 0
    for row in rows:
        item = dict(row)
        segment_id = int(item["segment_id"])
        label = str(item.get("human_label") or "")
        corrected = str(item.get("corrected_text") or "").strip()
        label_counts[label] += 1
        if label in BLOCKING_LOCAL_LABELS:
            blocked[segment_id].append(
                {
                    "source": "local_learning_candidates",
                    "reason": f"blocked_human_label:{label}",
                    "local_learning_id": item["id"],
                    "corrected_text": corrected or None,
                    "reviewed_at": item.get("reviewed_at"),
                }
            )
        if corrected:
            corrected_rows += 1
            reason = "superseded_by_human_correction"
            if label in CORRECTION_LABELS:
                blocked[segment_id].append(
                    {
                        "source": "local_learning_candidates",
                        "reason": reason,
                        "local_learning_id": item["id"],
                        "human_label": label,
                        "corrected_text": corrected,
                        "reviewed_at": item.get("reviewed_at"),
                    }
                )
            superseded[segment_id].append(
                {
                    "source": "local_learning_candidates",
                    "reason": reason,
                    "local_learning_id": item["id"],
                    "human_label": label,
                    "corrected_text": corrected,
                    "reviewed_at": item.get("reviewed_at"),
                }
            )
    return {
        "reviewed_learning_rows": len(rows),
        "learning_label_counts": dict(sorted(label_counts.items())),
        "learning_corrected_text_rows": corrected_rows,
        "blocked": blocked,
        "superseded": superseded,
    }


def decision_file_signals(limit: int) -> dict[str, Any]:
    patterns = [
        "*candidate_human_review_decision_record.jsonl",
        "*gender_semantic_literal_residue_human_decisions.jsonl",
        "*human_decisions.jsonl",
    ]
    paths: list[Path] = []
    seen_paths: set[Path] = set()
    for pattern in patterns:
        for path in latest_files(pattern, limit):
            if path not in seen_paths:
                seen_paths.add(path)
                paths.append(path)

    blocked: dict[int, list[dict[str, Any]]] = defaultdict(list)
    superseded: dict[int, list[dict[str, Any]]] = defaultdict(list)
    decision_counts: Counter[str] = Counter()
    row_count = 0
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        for row in read_jsonl(path):
            if "segment_id" not in row:
                continue
            row_count += 1
            segment_id = int(row["segment_id"])
            decision = str(row.get("human_review_decision") or row.get("human_decision") or "")
            corrected = str(row.get("corrected_text") or row.get("candidate_text") or "").strip()
            decision_counts[decision or "unknown"] += 1
            if decision in CONTEXT_DECISIONS or row.get("requires_more_context"):
                blocked[segment_id].append(
                    {
                        "source": str(path),
                        "reason": "needs_more_context_without_new_explicit_approval",
                        "decision": decision,
                        "note": row.get("human_review_note") or row.get("human_reason"),
                    }
                )
            if decision in {"duplicate_of_existing_candidate", "human_rejected_false_positive_no_change"}:
                blocked[segment_id].append(
                    {
                        "source": str(path),
                        "reason": f"blocked_human_decision:{decision}",
                        "decision": decision,
                        "note": row.get("human_review_note") or row.get("human_reason"),
                    }
                )
            if corrected and decision in {"human_approved_for_protected_apply", "approve_for_future_apply"}:
                superseded[segment_id].append(
                    {
                        "source": str(path),
                        "reason": "candidate_text_is_human_approved_signal",
                        "decision": decision,
                        "corrected_text": corrected,
                    }
                )
    return {
        "decision_files": [str(path) for path in paths],
        "decision_rows_scanned": row_count,
        "decision_counts": dict(sorted(decision_counts.items())),
        "blocked": blocked,
        "superseded": superseded,
    }


def closeout_signals(limit: int) -> dict[str, Any]:
    paths = latest_files("*post_apply_candidate_closeout_status*.jsonl", limit)
    blocked: dict[int, list[dict[str, Any]]] = defaultdict(list)
    status_counts: Counter[str] = Counter()
    row_count = 0
    for path in paths:
        for row in read_jsonl(path):
            if "segment_id" not in row:
                continue
            row_count += 1
            segment_id = int(row["segment_id"])
            status = str(row.get("status") or "")
            status_counts[status or "unknown"] += 1
            if status in {"applied", "human_corrected", "superseded_by_human_correction"}:
                blocked[segment_id].append(
                    {
                        "source": str(path),
                        "reason": f"closed_out:{status}",
                        "status": status,
                    }
                )
    summaries = [read_json(path) for path in latest_files("*post_apply_candidate_closeout_status_summary.json", limit)]
    return {
        "closeout_files": [str(path) for path in paths],
        "closeout_rows_scanned": row_count,
        "closeout_status_counts": dict(sorted(status_counts.items())),
        "latest_closeout_summaries": summaries[:3],
        "blocked": blocked,
    }


def context_risk_scan(conn: sqlite3.Connection, segment_ids: list[int], sample_limit: int) -> dict[str, Any]:
    if not segment_ids:
        return {"context_risk_segment_count": 0, "context_risk_samples": []}
    compiled = [(item, re.compile(item["regex"], re.IGNORECASE)) for item in CONTEXT_RISK_PATTERNS]
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT s.id AS segment_id, s.relative_path, s.source_key, s.spanish_text,
               s.old_text, o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    samples: list[dict[str, Any]] = []
    risk_ids: set[int] = set()
    for row in rows:
        haystacks = [str(row["spanish_text"] or ""), str(row["old_text"] or ""), str(row["current_output_text"] or "")]
        for pattern, regex in compiled:
            if any(regex.search(text) for text in haystacks):
                risk_ids.add(int(row["segment_id"]))
                if len(samples) < sample_limit:
                    samples.append(
                        {
                            "segment_id": int(row["segment_id"]),
                            "relative_path": row["relative_path"],
                            "source_key": row["source_key"],
                            "pattern_key": pattern["pattern_key"],
                            "policy": pattern["policy"],
                            "spanish_text": short(row["spanish_text"]),
                            "current_output_text": short(row["current_output_text"]),
                        }
                    )
                break
    return {"context_risk_segment_count": len(risk_ids), "context_risk_samples": samples}


def merge_reason_map(*maps: dict[int, list[dict[str, Any]]]) -> dict[int, list[dict[str, Any]]]:
    merged: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for mapping in maps:
        for segment_id, reasons in mapping.items():
            merged[int(segment_id)].extend(reasons)
    return dict(sorted(merged.items()))


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    with connect_readonly() as conn:
        learning = local_learning_signals(conn)
        decisions = decision_file_signals(args.decision_file_limit)
        closeout = closeout_signals(args.closeout_file_limit)
        blocked = merge_reason_map(learning["blocked"], decisions["blocked"], closeout["blocked"])
        superseded = merge_reason_map(learning["superseded"], decisions["superseded"])
        context_risk = context_risk_scan(conn, sorted(set(blocked) | set(superseded)), args.context_risk_sample_limit)
    blocked_reason_counts: Counter[str] = Counter()
    for reasons in blocked.values():
        for reason in reasons:
            blocked_reason_counts[str(reason.get("reason") or "unknown")] += 1
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reviewed_learning_rows": learning["reviewed_learning_rows"],
        "learning_label_counts": learning["learning_label_counts"],
        "learning_corrected_text_rows": learning["learning_corrected_text_rows"],
        "decision_files": decisions["decision_files"],
        "decision_rows_scanned": decisions["decision_rows_scanned"],
        "decision_counts": decisions["decision_counts"],
        "closeout_files": closeout["closeout_files"],
        "closeout_rows_scanned": closeout["closeout_rows_scanned"],
        "closeout_status_counts": closeout["closeout_status_counts"],
        "latest_closeout_summaries": closeout["latest_closeout_summaries"],
        "blocked_segment_count": len(blocked),
        "superseded_segment_count": len(superseded),
        "blocked_reason_counts": dict(sorted(blocked_reason_counts.items())),
        "blocked_segments": {str(key): value for key, value in blocked.items()},
        "superseded_by_human_correction_segments": {str(key): value for key, value in superseded.items()},
        "context_risk_patterns": CONTEXT_RISK_PATTERNS,
        **context_risk,
        "candidate_generation_allowed_after_guard": True,
        "required_before_candidate_generation": [
            "exclude blocked_segments",
            "mark any same-segment old candidate as superseded_by_human_correction when corrected_text exists",
            "exclude exhausted or blocked cohorts from retarget inputs",
            "route context_risk_patterns to human review unless an explicit contextual rule exists",
        ],
        "production_full_recommended_now": False,
        "apply_recommended_now": False,
        "recommended_next_action": "retarget_readonly_with_preflight_exclusions_then_human_audit_packet_if_candidates_exist",
    }
    return summary


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_candidate_generation_preflight_guard"
    txt_path = base.with_suffix(".txt")
    blocked_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with blocked_path.open("w", encoding="utf-8", newline="\n") as handle:
        for segment_id, reasons in summary["blocked_segments"].items():
            handle.write(
                json.dumps(
                    {
                        "segment_id": int(segment_id),
                        "candidate_status": "blocked_before_generation",
                        "reasons": reasons,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        for segment_id, reasons in summary["superseded_by_human_correction_segments"].items():
            if segment_id in summary["blocked_segments"]:
                continue
            handle.write(
                json.dumps(
                    {
                        "segment_id": int(segment_id),
                        "candidate_status": "superseded_by_human_correction",
                        "reasons": reasons,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "candidate generation preflight guard",
        f"source={RULE_VERSION}",
        f"reviewed_learning_rows={summary['reviewed_learning_rows']}",
        f"learning_corrected_text_rows={summary['learning_corrected_text_rows']}",
        f"decision_rows_scanned={summary['decision_rows_scanned']}",
        f"closeout_rows_scanned={summary['closeout_rows_scanned']}",
        f"blocked_segment_count={summary['blocked_segment_count']}",
        f"superseded_segment_count={summary['superseded_segment_count']}",
        f"context_risk_segment_count={summary['context_risk_segment_count']}",
        f"blocked_reason_counts={json.dumps(summary['blocked_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Policy:",
        "- Never re-present candidates corrected by humans, marked semantic_error, replaced by corrected_text, or needs_more_context without new explicit approval.",
        "- Same segment with human corrected_text must become superseded_by_human_correction, not pending_apply.",
        "- 'muchos más' family is context-risk; do not auto-convert to 'muitos mais'.",
        "- Use 'muito mais' only with human approval or explicit contextual rule.",
        "",
        f"apply_recommended_now={str(summary['apply_recommended_now']).lower()}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"recommended_next_action={summary['recommended_next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, blocked_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision-file-limit", type=int, default=30)
    parser.add_argument("--closeout-file-limit", type=int, default=20)
    parser.add_argument("--context-risk-sample-limit", type=int, default=20)
    args = parser.parse_args()
    summary = build_summary(args)
    txt_path, blocked_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={blocked_path}")
    print(f"summary={summary_path}")
    for key in [
        "reviewed_learning_rows",
        "learning_corrected_text_rows",
        "decision_rows_scanned",
        "closeout_rows_scanned",
        "blocked_segment_count",
        "superseded_segment_count",
        "context_risk_segment_count",
        "apply_recommended_now",
        "production_full_recommended_now",
        "recommended_next_action",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
