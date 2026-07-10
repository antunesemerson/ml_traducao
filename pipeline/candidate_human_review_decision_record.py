from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


APPROVED = {1, 2, 3, 4, 5, 7, 8, 13, 14, 15, 16, 17}
NEEDS_MORE_CONTEXT = {6, 9, 10, 12}
DUPLICATES = {11: 2}
CONTEXT_NOTES = {
    6: 'Duvida: talvez "muito mais" soe mais natural em PT-BR, mas isso mudaria numero/forma da expressao.',
    9: "Duvida: pode ser termo mecanico/conceito do jogo com maiuscula intencional.",
    10: "Duvida: parece correto, mas pode perder nuance de trabalho/obrigacao dependendo do contexto.",
    12: "Duvida: pode haver nome canonico/capitalizacao especifica da mecanica/DLC.",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_candidate_human_review_decision_record"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir / f"{base.name}_summary.json"


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
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def validate_packet(rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    expected_summary = {
        "raw_candidate_count": 17,
        "unique_segment_count": 16,
        "duplicate_count": 1,
        "pending_human_review_count": 17,
        "token_integrity_ok_count": 17,
        "structure_integrity_ok_count": 17,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_ready_now": 0,
    }
    for key, expected in expected_summary.items():
        actual = int(summary.get(key) or 0)
        if actual != expected:
            raise SystemExit(f"packet summary guard failed: {key} expected {expected}, got {actual}")
    if len(rows) != 17:
        raise SystemExit(f"packet row guard failed: {len(rows)}")
    indexes = {int(row["candidate_index"]) for row in rows}
    if indexes != set(range(1, 18)):
        raise SystemExit(f"candidate index guard failed: {sorted(indexes)}")
    for row in rows:
        if row.get("requires_apply_later") or row.get("requires_lifecycle_later") or row.get("false_safe_risk"):
            raise SystemExit(f"packet row future/risk guard failed: {row.get('candidate_index')}")


def decision_for(index: int) -> str:
    if index in APPROVED:
        return "approve_for_future_apply"
    if index in NEEDS_MORE_CONTEXT:
        return "needs_more_context"
    if index in DUPLICATES:
        return "duplicate_of_existing_candidate"
    raise SystemExit(f"unmapped candidate decision: {index}")


def note_for(index: int, decision: str) -> str:
    if decision == "needs_more_context":
        return CONTEXT_NOTES[index]
    if decision == "duplicate_of_existing_candidate":
        return f"Duplicata do candidate {DUPLICATES[index]}; nao somar como mudanca independente."
    return "Aprovado pela revisao humana para plano futuro; ainda sem apply automatico."


def build_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item["candidate_index"])):
        index = int(row["candidate_index"])
        decision = decision_for(index)
        duplicate_target = DUPLICATES.get(index)
        records.append(
            {
                "candidate_index": index,
                "segment_id": int(row["segment_id"]),
                "duplicate_group_id": str(row.get("duplicate_group_id") or ""),
                "origin_audit": str(row.get("origin_audit") or ""),
                "candidate_type": str(row.get("candidate_type") or ""),
                "original_text": str(row.get("original_text") or ""),
                "current_output_text": str(row.get("current_output_text") or ""),
                "candidate_text": str(row.get("candidate_text") or ""),
                "human_review_decision": decision,
                "human_review_note": note_for(index, decision),
                "eligible_for_future_apply_plan": decision == "approve_for_future_apply",
                "is_duplicate": bool(row.get("is_duplicate")),
                "duplicate_of_candidate_index": duplicate_target,
                "requires_more_context": decision == "needs_more_context",
                "requires_apply_later": False,
                "requires_lifecycle_later": False,
                "false_safe_risk": False,
            }
        )
    return records


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter(row["human_review_decision"] for row in records)
    unique_segments = len({row["segment_id"] for row in records})
    summary = {
        "schema_version": 1,
        "source": "candidate_human_review_decision_record_v1",
        "raw_candidate_count": len(records),
        "unique_segment_count": unique_segments,
        "approve_for_future_apply_count": decisions.get("approve_for_future_apply", 0),
        "needs_more_context_count": decisions.get("needs_more_context", 0),
        "duplicate_of_existing_candidate_count": decisions.get("duplicate_of_existing_candidate", 0),
        "reject_count": decisions.get("reject", 0),
        "eligible_for_future_apply_plan_count": sum(1 for row in records if row["eligible_for_future_apply_plan"]),
        "requires_more_context_count": sum(1 for row in records if row["requires_more_context"]),
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "model_training_recommended": False,
        "network_update_recommended": False,
        "recommended_next_prompt": "chat_exec_candidate_apply_plan_preview_prompt.md",
    }
    expected = {
        "raw_candidate_count": 17,
        "unique_segment_count": 16,
        "approve_for_future_apply_count": 12,
        "needs_more_context_count": 4,
        "duplicate_of_existing_candidate_count": 1,
        "reject_count": 0,
        "eligible_for_future_apply_plan_count": 12,
        "requires_more_context_count": 4,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
        "apply_ready_now": 0,
    }
    for key, value in expected.items():
        if int(summary[key]) != value:
            raise SystemExit(f"decision summary guard failed: {key}")
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "candidate human review decision record",
        f"raw_candidate_count={summary['raw_candidate_count']}",
        f"unique_segment_count={summary['unique_segment_count']}",
        f"approve_for_future_apply_count={summary['approve_for_future_apply_count']}",
        f"needs_more_context_count={summary['needs_more_context_count']}",
        f"duplicate_of_existing_candidate_count={summary['duplicate_of_existing_candidate_count']}",
        f"reject_count={summary['reject_count']}",
        f"eligible_for_future_apply_plan_count={summary['eligible_for_future_apply_plan_count']}",
        f"requires_more_context_count={summary['requires_more_context_count']}",
        f"requires_apply_later_count={summary['requires_apply_later_count']}",
        f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
        f"false_safe_risk_count={summary['false_safe_risk_count']}",
        f"apply_ready_now={summary['apply_ready_now']}",
        "",
        "analysis:",
        "1. Candidatos aprovados para plano futuro: 12.",
        "2. Candidatos que precisam de contexto: 4.",
        "3. Duplicata: candidate 11 marcado como duplicate_of_existing_candidate de candidate 2.",
        "4. Apply agora: nao.",
        f"5. Proximo prompt recomendado: {summary['recommended_next_prompt']}.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-jsonl", required=True)
    parser.add_argument("--packet-summary-json", required=True)
    args = parser.parse_args()
    packet_rows = read_jsonl(db.project_path(args.packet_jsonl))
    packet_summary = read_json(db.project_path(args.packet_summary_json))
    validate_packet(packet_rows, packet_summary)
    records = build_records(packet_rows)
    summary = build_summary(records)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "raw_candidate_count",
        "unique_segment_count",
        "approve_for_future_apply_count",
        "needs_more_context_count",
        "duplicate_of_existing_candidate_count",
        "eligible_for_future_apply_plan_count",
        "requires_apply_later_count",
        "false_safe_risk_count",
        "apply_ready_now",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
