from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from local_quality_validator import validate_text


RULE_VERSION = "issue_title_landed_adjective_spanish_es_downstream_impact_v1"
QUEUE_RUN_ID = 150
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"


def canonical(value: str | None) -> str:
    text = (value or "").strip()
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text)
    return text


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_landed_adjective_spanish_es_downstream_impact_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_state_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else int(row["id"])


def latest_confirmations(conn) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM segment_confirmations
        ORDER BY segment_id, locked DESC, updated_at DESC, id DESC
        """
    ).fetchall()
    output: dict[int, dict[str, Any]] = {}
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id not in output:
            output[segment_id] = dict(row)
    return output


def fetch_decisions(conn, *, queue_run_id: int, state_run_id: int | None) -> list[dict[str, Any]]:
    state_join = ""
    state_select = """
        NULL AS state_final_state,
        NULL AS state_group,
        NULL AS state_review_state,
        NULL AS state_apply_state,
        NULL AS state_needs_output_apply,
        NULL AS state_is_closed
    """
    params: list[Any] = []
    if state_run_id is not None:
        state_select = """
            state.final_state AS state_final_state,
            state.state_group AS state_group,
            state.review_state AS state_review_state,
            state.apply_state AS state_apply_state,
            state.needs_output_apply AS state_needs_output_apply,
            state.is_closed AS state_is_closed
        """
        state_join = """
        LEFT JOIN segment_state_items state
          ON state.segment_id = decision.segment_id
         AND state.run_id = ?
        """
        params.append(state_run_id)

    rows = conn.execute(
        f"""
        SELECT
          decision.id AS decision_id,
          decision.run_id AS decision_run_id,
          decision.queue_run_id,
          decision.queue_item_id,
          decision.ledger_item_id,
          decision.segment_id,
          decision.relative_path,
          decision.source_key,
          decision.source_line_number,
          decision.issue_family,
          decision.issue_kind,
          decision.queue_bucket,
          decision.normalized_decision,
          decision.evidence_label,
          decision.corrected_text,
          decision.reviewer,
          decision.notes,
          source.spanish_text,
          source.english_text,
          source.old_text,
          output.portuguese_text AS output_text,
          {state_select}
        FROM ml_issue_review_decisions decision
        JOIN source_segments source
          ON source.id = decision.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = decision.segment_id
        {state_join}
        WHERE decision.queue_run_id = ?
          AND decision.agent_key = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.source_key, decision.id
        """,
        [*params, queue_run_id, AGENT_KEY],
    ).fetchall()
    return [dict(row) for row in rows]


def classify_row(row: dict[str, Any], confirmations: dict[int, dict[str, Any]]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    corrected = canonical(row.get("corrected_text"))
    output_text = canonical(row.get("output_text"))
    confirmation = confirmations.get(segment_id)
    confirmed_text = canonical(None if confirmation is None else confirmation.get("confirmed_text"))

    validator = validate_text(corrected)
    issues = validator.get("issues") if isinstance(validator, dict) else []
    issue_codes = [
        str(issue.get("code"))
        for issue in issues or []
        if isinstance(issue, dict) and issue.get("severity") in {"high", "medium"}
    ]

    output_matches = bool(corrected and output_text == corrected)
    confirmation_matches = bool(corrected and confirmed_text == corrected)
    has_confirmation = confirmation is not None
    source_equals_corrected = canonical(row.get("spanish_text")) == corrected

    if issue_codes:
        readiness = "blocked_quality"
    elif output_matches and confirmation_matches:
        readiness = "already_aligned"
    elif confirmation_matches and not output_matches:
        readiness = "needs_output_apply"
    elif output_matches and not confirmation_matches:
        readiness = "needs_confirmation_record"
    else:
        readiness = "needs_confirmation_and_output"

    notes = str(row.get("notes") or "")
    if "stem_overlap" in notes:
        subpolicy = "source_stem_overlap"
    elif "base_same_semantic" in notes:
        subpolicy = "base_same_semantic"
    elif "localized_base_strict" in notes:
        subpolicy = "localized_base_strict"
    elif "high_tier_strict" in notes:
        subpolicy = "high_tier_strict"
    elif "final_exception" in notes:
        subpolicy = "final_exception"
    else:
        reviewer = str(row.get("reviewer") or "")
        if reviewer.startswith("codex_landed_title_spanish_es_"):
            subpolicy = f"legacy_{reviewer.removeprefix('codex_landed_title_spanish_es_')}"
        else:
            subpolicy = "unknown"

    return {
        **row,
        "subpolicy": subpolicy,
        "corrected_hash": sha256_text(corrected),
        "output_hash": sha256_text(output_text),
        "confirmed_hash": sha256_text(confirmed_text),
        "has_confirmation": int(has_confirmation),
        "confirmation_locked": 0 if confirmation is None else int(confirmation.get("locked") or 0),
        "confirmation_label": "" if confirmation is None else str(confirmation.get("confirmation_label") or ""),
        "confirmed_text": confirmed_text,
        "output_matches_corrected": int(output_matches),
        "confirmation_matches_corrected": int(confirmation_matches),
        "source_equals_corrected": int(source_equals_corrected),
        "quality_issue_count": len(issue_codes),
        "quality_issue_codes": ",".join(issue_codes),
        "readiness": readiness,
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    rows: list[dict[str, Any]],
    queue_run_id: int,
    state_run_id: int | None,
) -> None:
    readiness_counts = Counter(row["readiness"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy"] for row in rows)
    subpolicy_readiness = Counter((row["subpolicy"], row["readiness"]) for row in rows)
    state_counts = Counter(str(row.get("state_final_state") or "missing_state") for row in rows)
    reviewer_counts = Counter(str(row.get("reviewer") or "") for row in rows)

    fieldnames = [
        "decision_id",
        "decision_run_id",
        "queue_run_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "subpolicy",
        "readiness",
        "state_final_state",
        "state_group",
        "state_review_state",
        "state_apply_state",
        "state_needs_output_apply",
        "state_is_closed",
        "corrected_text",
        "output_text",
        "confirmed_text",
        "output_matches_corrected",
        "confirmation_matches_corrected",
        "has_confirmation",
        "confirmation_locked",
        "confirmation_label",
        "quality_issue_count",
        "quality_issue_codes",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Queue 150 downstream impact diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Latest segment_state_run used: {state_run_id if state_run_id is not None else 'none'}",
        f"Rows: {len(rows):,}",
        f"CSV: {csv_path}",
        f"JSONL: {jsonl_path}",
        "",
        "Readiness summary:",
        *[f"- {key}: {value:,}" for key, value in readiness_counts.most_common()],
        "",
        "Subpolicy summary:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Subpolicy x readiness:",
        *[
            f"- {subpolicy} / {readiness}: {value:,}"
            for (subpolicy, readiness), value in sorted(subpolicy_readiness.items())
        ],
        "",
        "Current segment-state distribution:",
        *[f"- {key}: {value:,}" for key, value in state_counts.most_common()],
        "",
        "Reviewer distribution:",
        *[f"- {key}: {value:,}" for key, value in reviewer_counts.most_common()],
        "",
        "Interpretation:",
        "- already_aligned means corrected_text already matches both current output and latest confirmation.",
        "- needs_output_apply means a matching confirmation exists, but output still differs.",
        "- needs_confirmation_record means output already matches corrected_text, but no matching confirmation was found.",
        "- needs_confirmation_and_output means both confirmation and output still need a safe promotion/apply path.",
        "- blocked_quality means the local validator found medium/high issues in corrected_text.",
        "",
        "Actionable samples:",
    ]
    for row in rows[:80]:
        lines.append(
            f"- {row['readiness']} | {row['subpolicy']} | segment={row['segment_id']} | "
            f"{row['relative_path']}:{row.get('source_line_number')}:{row['source_key']} | "
            f"{row.get('output_text')!r} -> {row.get('corrected_text')!r} | "
            f"state={row.get('state_final_state')}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This diagnostic is read-only.",
            "- It does not modify source, output, confirmations, lifecycle policies, or segment-state.",
            "- It measures how reviewed learning evidence could be consumed by a later production/policy step.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int = QUEUE_RUN_ID, state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id if state_run_id is not None else latest_state_run_id(conn)
        confirmations = latest_confirmations(conn)
        decisions = fetch_decisions(conn, queue_run_id=queue_run_id, state_run_id=selected_state_run_id)
        rows = [classify_row(row, confirmations) for row in decisions]

    txt_path, csv_path, jsonl_path = report_paths(settings, queue_run_id)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        rows=rows,
        queue_run_id=queue_run_id,
        state_run_id=selected_state_run_id,
    )

    counts = Counter(row["readiness"] for row in rows)
    print("[issue_title_landed_adjective_spanish_es_downstream_impact] Diagnostic generated")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] Rule version: {RULE_VERSION}")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] Queue run id: {queue_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] State run id: {selected_state_run_id}")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] {key}: {value:,}")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] Report: {txt_path}")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] CSV: {csv_path}")
    print(f"[issue_title_landed_adjective_spanish_es_downstream_impact] JSONL: {jsonl_path}")
    return {
        "queue_run_id": queue_run_id,
        "state_run_id": selected_state_run_id,
        "rows": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure downstream impact for queue 150 Spanish -es title repairs.")
    parser.add_argument("--queue-run-id", type=int, default=QUEUE_RUN_ID)
    parser.add_argument("--state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, state_run_id=args.state_run_id)
