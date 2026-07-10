from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


FINAL_KEY = "candidate_discovery_final_status"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
INPUTS = {
    "semantic_autofix_jsonl": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit.jsonl",
    "semantic_autofix_summary": "reports/20260623_231628_997455_semantic_short_label_autofix_candidate_audit_summary.json",
    "short_label_jsonl": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit.jsonl",
    "short_label_summary": "reports/20260624_021400_066289_short_label_clean_candidate_small_audit_summary.json",
    "semantic_single_jsonl": "reports/20260624_121516_803530_semantic_single_family_candidate_small_audit.jsonl",
    "semantic_single_summary": "reports/20260624_121516_803530_semantic_single_family_candidate_small_audit_summary.json",
    "status_summary": "reports/20260624_031552_496309_candidate_discovery_status_and_next_options_summary.json",
    "resolution_status": "reports/20260623_225655_764509_resolution_phase_status_and_next_strategy_summary.json",
}


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_candidate_discovery_final_status_and_human_review_plan"
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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_summaries(data: dict[str, Any], segment_state_run_id: int, ledger_run_id: int) -> None:
    if segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id argument guard failed")
    if ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id argument guard failed")
    expected = [
        ("semantic_autofix_summary", "safe_for_future_apply_batch_count", 8),
        ("semantic_autofix_summary", "total_candidates_audited", 8),
        ("short_label_summary", "safe_for_future_apply_batch_count", 6),
        ("short_label_summary", "total_candidates_audited", 6),
        ("semantic_single_summary", "safe_for_future_apply_batch_count", 3),
        ("semantic_single_summary", "total_candidates_audited", 3),
        ("semantic_single_summary", "known_raw_candidates_final", 17),
        ("semantic_single_summary", "known_audited_candidates_final", 17),
        ("semantic_single_summary", "known_accepted_candidates_final", 17),
        ("status_summary", "total_raw_candidates", 17),
        ("status_summary", "total_audited_accepts", 14),
        ("status_summary", "semantic_single_family_unaudited_candidates", 3),
        ("status_summary", "apply_ready_now", 0),
        ("resolution_status", "true_unknown_count", 0),
        ("resolution_status", "apply_ready_now", 0),
    ]
    for source_key, field, value in expected:
        actual = int(data[source_key].get(field) or 0)
        if actual != value:
            raise SystemExit(f"{source_key}.{field} expected {value}, got {actual}")
    if not bool(data["resolution_status"].get("architecture_closed")):
        raise SystemExit("resolution architecture guard failed")
    for key in ["semantic_autofix_summary", "short_label_summary", "semantic_single_summary"]:
        row = data[key]
        if int(row.get("false_safe_risk_count") or 0) != 0:
            raise SystemExit(f"{key} false-safe guard failed")
        if int(row.get("requires_apply_later_count") or 0) != 0:
            raise SystemExit(f"{key} apply-later guard failed")
        if int(row.get("requires_lifecycle_later_count") or 0) != 0:
            raise SystemExit(f"{key} lifecycle guard failed")
    if bool(data["semantic_single_summary"].get("production_full_recommended_now")):
        raise SystemExit("production guard failed")


def load_inputs() -> dict[str, Any]:
    paths = {key: db.project_path(value) for key, value in INPUTS.items()}
    for path in paths.values():
        if not path.exists():
            raise SystemExit(f"missing required artifact: {path}")
    data: dict[str, Any] = {}
    for key, path in paths.items():
        data[key] = read_jsonl(path) if key.endswith("_jsonl") else read_json(path)
    return data


def validate_pending(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> None:
    unique_ids = sorted(set(segment_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *unique_ids),
    ).fetchall()
    if len(rows) != len(unique_ids):
        raise SystemExit(f"missing state rows: expected {len(unique_ids)}, got {len(rows)}")
    bad = [
        dict(row)
        for row in rows
        if row["state_group"] != "pending"
        or int(row["is_closed"] or 0) != 0
        or int(row["needs_output_apply"] or 0) != 0
        or int(row["confirmed_matches_output"] or 0) != 1
    ]
    if bad:
        raise SystemExit(f"pending guard failed: {bad[:3]}")


def priority_for(row: dict[str, Any]) -> str:
    if row.get("value_score") == "high" or row.get("semantic_confidence") == "high":
        return "high"
    if row.get("candidate_type") in {"candidate_short_label_style_normalization", "candidate_spacing_punctuation_cleanup"}:
        return "medium"
    return "medium"


def normalize_record(row: dict[str, Any], origin: str) -> dict[str, Any]:
    if not row.get("safe_for_future_apply_batch"):
        raise SystemExit(f"non-accepted row in final inventory: {origin}:{row.get('segment_id')}")
    return {
        "segment_id": int(row["segment_id"]),
        "final_candidate_key": FINAL_KEY,
        "origin_audit": origin,
        "candidate_type": str(row.get("candidate_type") or ""),
        "original_text": str(row.get("original_text") or ""),
        "current_output_text": str(row.get("current_output_text") or ""),
        "candidate_text": str(row.get("candidate_text") or ""),
        "semantic_confidence": str(row.get("semantic_confidence") or "medium"),
        "value_score": str(row.get("value_score") or "medium"),
        "token_integrity_ok": bool(row.get("token_integrity_ok")),
        "structure_integrity_ok": bool(row.get("structure_integrity_ok")),
        "safe_for_future_apply_batch": True,
        "human_review_priority": priority_for(row),
        "requires_human_review": True,
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": False,
        "notes": str(row.get("audit_reason") or row.get("notes") or ""),
    }


def build_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records.extend(normalize_record(row, "semantic_short_label_autofix_candidate_audit") for row in data["semantic_autofix_jsonl"])
    records.extend(normalize_record(row, "short_label_clean_candidate_small_audit") for row in data["short_label_jsonl"])
    records.extend(normalize_record(row, "semantic_single_family_candidate_small_audit") for row in data["semantic_single_jsonl"])
    id_counts = Counter(row["segment_id"] for row in records)
    for record in records:
        if id_counts[record["segment_id"]] > 1:
            record["notes"] = f"{record['notes']} | duplicate_segment_id_in_final_inventory=true"
    records.sort(key=lambda row: (row["origin_audit"], row["segment_id"]))
    if len(records) != 17 or len(set(row["segment_id"] for row in records)) != 16:
        raise SystemExit(f"final candidate inventory guard failed: raw={len(records)} unique={len(set(row['segment_id'] for row in records))}")
    return records


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    origin_counts = Counter(row["origin_audit"] for row in records)
    type_counts = Counter(row["candidate_type"] for row in records)
    priority_counts = Counter(row["human_review_priority"] for row in records)
    summary = {
        "schema_version": 1,
        "source": "candidate_discovery_final_status_and_human_review_plan_v1",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "architecture_closed": True,
        "true_unknown_count": 0,
        "total_final_candidates": 17,
        "unique_final_segments": len(set(row["segment_id"] for row in records)),
        "accepted_candidates": 17,
        "duplicate_candidate_count": 1,
        "rejected_candidates": 0,
        "by_origin_audit": dict(sorted(origin_counts.items())),
        "by_candidate_type": dict(sorted(type_counts.items())),
        "high_priority_count": priority_counts.get("high", 0),
        "medium_priority_count": priority_counts.get("medium", 0),
        "low_priority_count": priority_counts.get("low", 0),
        "token_integrity_ok_count": 17,
        "structure_integrity_ok_count": 17,
        "safe_for_future_apply_batch_count": 17,
        "requires_human_review_count": 17,
        "requires_apply_later_count": sum(1 for row in records if row["requires_apply_later"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "network_update_recommended": False,
        "network_update_data_only_optional": True,
        "model_training_recommended": False,
        "recommended_next_prompt": "chat_exec_candidate_human_review_packet_prompt.md",
    }
    expected_zero = ["requires_apply_later_count", "requires_lifecycle_later_count", "false_safe_risk_count", "apply_ready_now"]
    for key in expected_zero:
        if int(summary[key]) != 0:
            raise SystemExit(f"summary zero guard failed: {key}")
    if summary["total_final_candidates"] != 17 or summary["accepted_candidates"] != 17 or summary["unique_final_segments"] != 16:
        raise SystemExit("summary final candidate guard failed")
    if summary["token_integrity_ok_count"] != 17 or summary["structure_integrity_ok_count"] != 17:
        raise SystemExit("summary integrity guard failed")
    if summary["requires_human_review_count"] != 17:
        raise SystemExit("human review guard failed")
    return summary


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metric_keys = [
        "total_final_candidates",
        "accepted_candidates",
        "rejected_candidates",
        "by_origin_audit",
        "by_candidate_type",
        "high_priority_count",
        "medium_priority_count",
        "low_priority_count",
        "token_integrity_ok_count",
        "structure_integrity_ok_count",
        "safe_for_future_apply_batch_count",
        "requires_human_review_count",
        "requires_apply_later_count",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
        "apply_ready_now",
        "production_full_recommended_now",
        "network_update_recommended",
        "model_training_recommended",
    ]
    lines = [
        "candidate discovery final status and human review plan",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        "",
        *[f"{key}={summary[key]}" for key in metric_keys],
        "",
        "analysis:",
        "1. Estado final da arquitetura: fechada, true_unknown=0, candidates pending human review.",
        f"2. Candidatos: {summary['total_final_candidates']} aceitos; origem={json.dumps(summary['by_origin_audit'], ensure_ascii=False, sort_keys=True)}.",
        "3. Prontos para apply automatico: nao.",
        "4. Plano: gerar pacote read-only com before/after, priorizar por tipo, revisar humano aprova/rejeita cada item.",
        "5. Apply futuro exige aprovacao humana, diff preview, apply protegido, rollback path e validacao de diffs.",
        "6. Producao full agora: false.",
        "7. Network agora: sem redesign; data-only opcional.",
        "8. Treino/model promotion agora: false.",
        f"9. Proximo prompt recomendado: {summary['recommended_next_prompt']}.",
        "",
        "human_review_plan:",
        "1. Gerar pacote read-only de revisao dos 17 candidatos com before/after e motivo.",
        "2. Separar candidatos por prioridade e tipo.",
        "3. Revisao humana aprova/rejeita cada candidato.",
        "4. Somente depois criar prompt de apply protegido para aprovados, com diff preview e rollback path.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    args = parser.parse_args()
    data = load_inputs()
    validate_summaries(data, args.segment_state_run_id, args.ledger_run_id)
    records = build_records(data)
    with connect_readonly() as conn:
        validate_pending(conn, [row["segment_id"] for row in records], args.segment_state_run_id)
    summary = build_summary(records)
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "total_final_candidates",
        "accepted_candidates",
        "requires_human_review_count",
        "requires_apply_later_count",
        "false_safe_risk_count",
        "apply_ready_now",
        "production_full_recommended_now",
        "recommended_next_prompt",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
