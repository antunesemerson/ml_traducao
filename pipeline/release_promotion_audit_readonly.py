from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_promotion_audit_readonly_v1"
DEFAULT_ENDPOINT = "http://127.0.0.1:8765/api/app-state"

# These candidates improve a local fragment but still leave the full segment visibly incomplete.
# Keep the list explicit so this one-off audit never silently becomes a production policy.
PARTIAL_REPAIR_HOLDS = {
    57298: "A frase continua truncada: 'Se ... ganha vitorioso'.",
    58592: "A frase continua truncada: 'Se ... consegue vitorioso'.",
    58593: "A frase continua truncada: 'Se ... consegue vitorioso'.",
    229510: "A frase ainda contém duplicação: 'não consegue consegue evitar'.",
    30553: "O literal espanhol 'no' permanece no segmento.",
    30554: "O literal espanhol 'no' permanece no segmento.",
    30555: "O literal espanhol 'no' permanece no segmento.",
    30556: "O literal espanhol 'no' permanece no segmento.",
    30557: "O literal espanhol 'no' permanece no segmento.",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the current promotion queue without changing output or database state."
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--expected-count", type=int, default=None)
    return parser.parse_args()


def load_app_state(endpoint: str) -> dict[str, Any]:
    with urlopen(endpoint, timeout=30) as response:  # noqa: S310 - local dashboard endpoint
        return json.loads(response.read().decode("utf-8"))


def short(value: Any, limit: int = 110) -> str:
    text = str(value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def classify(record: dict[str, Any]) -> tuple[str, str, bool, str]:
    segment_id = int(record.get("segment_id") or 0)
    token_status = str(record.get("token_status") or "")
    locked = int(record.get("locked") or 0) == 1
    calibration = str(record.get("score_calibration") or "")
    issue_count = int(record.get("issue_count") or 0)
    high_issue_count = int(record.get("high_issue_count") or 0)

    if token_status != "ok":
        return (
            "hold_token_signature",
            "hold_token_structure_review",
            False,
            "A assinatura de token diverge; revisar parser/token antes de qualquer fechamento.",
        )
    if segment_id in PARTIAL_REPAIR_HOLDS:
        return (
            "partial_repair_review",
            "hold_partial_repair_needs_final_text",
            False,
            PARTIAL_REPAIR_HOLDS[segment_id],
        )
    if locked:
        return (
            "human_locked_evidence",
            "approve_human_evidence_pending_lifecycle_validation",
            True,
            "Decisão humana travada e output igual ao confirmado; validar lifecycle e issues vinculadas.",
        )
    if calibration == "bold_no_to_nao_microrepair":
        return (
            "deterministic_bold_microrepair",
            "approve_pattern_pending_lifecycle_validation",
            True,
            "Microreparo calibrado #bold no#! -> #bold não#!; validar fechamento em lote homogêneo.",
        )
    if issue_count == 0 and high_issue_count == 0:
        return (
            "clean_score_gain",
            "approve_score_gain_pending_lifecycle_validation",
            True,
            "Ganho de score, tokens íntegros e nenhuma issue aberta; validar lifecycle em dry-run.",
        )
    return (
        "issue_resolution_review",
        "review_issue_resolution_before_apply",
        False,
        "A sugestão parece melhor, mas há issue aberta; provar supersessão/fechamento antes de promover.",
    )


def build_records(app_state: dict[str, Any], expected_count: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    review = (
        app_state.get("release", {})
        .get("post_release", {})
        .get("diff_review", {})
    )
    rows = list(review.get("promotion_segments") or [])
    if expected_count is not None and len(rows) != expected_count:
        raise SystemExit(f"Promotion count mismatch: found {len(rows)}, expected {expected_count}.")

    records: list[dict[str, Any]] = []
    for row in rows:
        lane, suggested_decision, eligible, next_action = classify(row)
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        output_exactly_equals_confirmed = output_text == confirmed_text
        output_canonically_equals_confirmed = (
            canonical_localization_text(output_text)
            == canonical_localization_text(confirmed_text)
        )
        records.append(
            {
                "source": SOURCE,
                "record_type": "release_promotion_audit",
                "segment_state_run_id": review.get("segment_state_run_id"),
                "old_score_run_id": review.get("old_score_run_id"),
                "score_run_id": review.get("score_run_id"),
                "segment_id": int(row.get("segment_id") or 0),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "old_text": row.get("old_text"),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "output_exactly_equals_confirmed": output_exactly_equals_confirmed,
                "output_canonically_equals_confirmed": output_canonically_equals_confirmed,
                "raw_old_score": row.get("raw_old_score"),
                "raw_new_score": row.get("raw_new_score"),
                "effective_new_score": row.get("effective_new_score"),
                "effective_score_delta": row.get("effective_score_delta"),
                "score_calibration": row.get("score_calibration"),
                "score_comparison_status": row.get("score_comparison_status"),
                "promotion_gate": row.get("promotion_gate"),
                "token_status": row.get("token_status"),
                "score_issue_count": int(row.get("issue_count") or 0),
                "score_high_issue_count": int(row.get("high_issue_count") or 0),
                "locked": int(row.get("locked") or 0),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "final_state": row.get("final_state"),
                "package_integrity_status": row.get("package_integrity_status"),
                "package_integrity_reason": row.get("package_integrity_reason"),
                "lane": lane,
                "suggested_decision": suggested_decision,
                "eligible_for_controlled_validation": eligible,
                "ready_for_apply": False,
                "next_action": next_action,
                "apply_count": 0,
                "ingest_count": 0,
                "issue_closure_count": 0,
                "segment_state_count": 0,
                "output_changed": False,
            }
        )

    lane_counts = Counter(record["lane"] for record in records)
    source_counts = Counter(str(record["confirmation_source"] or "<null>") for record in records)
    eligible_count = sum(1 for record in records if record["eligible_for_controlled_validation"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "segment_state_run_id": review.get("segment_state_run_id"),
        "old_score_run_id": review.get("old_score_run_id"),
        "score_run_id": review.get("score_run_id"),
        "promotion_count": len(records),
        "eligible_for_controlled_validation_count": eligible_count,
        "review_or_hold_count": len(records) - eligible_count,
        "ready_for_apply_count": 0,
        "lane_counts": dict(lane_counts),
        "confirmation_source_counts": dict(source_counts),
        "token_ok_count": sum(1 for record in records if record["token_status"] == "ok"),
        "output_exactly_equals_confirmed_count": sum(
            1 for record in records if record["output_exactly_equals_confirmed"]
        ),
        "output_canonically_equals_confirmed_count": sum(
            1 for record in records if record["output_canonically_equals_confirmed"]
        ),
        "score_issue_free_count": sum(
            1
            for record in records
            if record["score_issue_count"] == 0 and record["score_high_issue_count"] == 0
        ),
        "human_locked_count": sum(1 for record in records if record["locked"] == 1),
        "closed_count": sum(1 for record in records if str(record["final_state"]).startswith("closed")),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "ingest_count": 0,
        "issue_closure_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            f"Run a separate read-only lifecycle/issue validation for the {eligible_count} eligible records. "
            "Keep issue-resolution, partial-repair and token-signature review lanes out of apply."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_promotion_audit_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["lane"]].append(record)

    lines = [
        "# Release Promotion Audit (read-only)",
        "",
        f"- segment-state: `{summary['segment_state_run_id']}`",
        f"- score old/new: `{summary['old_score_run_id']} -> {summary['score_run_id']}`",
        f"- promotions: `{summary['promotion_count']}`",
        f"- eligible for controlled validation: `{summary['eligible_for_controlled_validation_count']}`",
        f"- review/hold: `{summary['review_or_hold_count']}`",
        f"- ready for apply now: `{summary['ready_for_apply_count']}`",
        "",
        "## Lanes",
        "",
    ]
    for lane, count in summary["lane_counts"].items():
        lines.append(f"- `{lane}`: `{count}`")

    for lane in (
        "human_locked_evidence",
        "deterministic_bold_microrepair",
        "clean_score_gain",
        "partial_repair_review",
        "issue_resolution_review",
        "hold_token_signature",
    ):
        lane_rows = groups.get(lane, [])
        if not lane_rows:
            continue
        lines.extend(
            [
                "",
                f"## {lane}",
                "",
                "| ID | arquivo/chave | old | output | score old -> efetivo | sinais score/high | decisão sugerida |",
                "|---:|---|---|---|---:|---:|---|",
            ]
        )
        for record in lane_rows:
            old_score = float(record.get("raw_old_score") or 0.0)
            effective_score = float(record.get("effective_new_score") or 0.0)
            issues = f"{record['score_issue_count']}/{record['score_high_issue_count']} high"
            lines.append(
                "| {id} | `{path} :: {key}` | {old} | {output} | {old_score:.2%} -> {new_score:.2%} | {issues} | `{decision}` |".format(
                    id=record["segment_id"],
                    path=short(record["relative_path"], 42).replace("|", "\\|"),
                    key=short(record["source_key"], 38).replace("|", "\\|"),
                    old=short(record["old_text"]).replace("|", "\\|"),
                    output=short(record["output_text"]).replace("|", "\\|"),
                    old_score=old_score,
                    new_score=effective_score,
                    issues=issues,
                    decision=record["suggested_decision"],
                )
            )

    lines.extend(
        [
            "",
            "## Guards",
            "",
            "- candidate generation: `0`",
            "- apply: `0`",
            "- learning ingest: `0`",
            "- issue closure: `0`",
            "- segment-state: `0`",
            "- reindex: `0`",
            "- full production: `0`",
            "- source/output changed: `false`",
            "",
            f"Recommendation: {summary['recommendation']}",
        ]
    )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return md_path, jsonl_path, summary_path


def main() -> int:
    args = parse_args()
    app_state = load_app_state(args.endpoint)
    records, summary = build_records(app_state, args.expected_count)
    paths = write_reports(records, summary)
    print(json.dumps({"summary": summary, "artifacts": [str(path) for path in paths]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
