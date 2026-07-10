from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_escaped_quote_only_diagnostic_v1"
INPUT_JSONL = Path("reports/20260630_153501_236308_domain_policy_vote_candidate_same_token_apply_plan_diagnostic.jsonl")
EXPECTED_SEGMENT_STATE_RUN_ID = 514
EXPECTED_ESCAPED_QUOTE_COUNT = 3853


DENSE_MARKERS = (
    "$EFFECT_LIST_BULLET$",
    "Select_CString",
    "ES_",
    "GetTrait(",
    "GetCultureTradition(",
    "ScriptValue(",
    "GetVassalStance(",
    "ROOT.",
    "Scope.",
    "GetPlayerHeir",
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def token_total(row: dict[str, Any]) -> int:
    return sum(int(value) for value in (row.get("output_tokens") or {}).values())


def normalized_output(text: str) -> str:
    return text.replace('\\"', '"')


def quote_delta_count(output: str, confirmed: str) -> int:
    return output.count('\\"') - confirmed.count('\\"')


def structural_subtype(row: dict[str, Any]) -> str:
    output = str(row.get("output_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    blob = output + "\n" + confirmed
    if normalized_output(output) != confirmed:
        return "not_pure_escaped_quote"
    if any(marker in blob for marker in DENSE_MARKERS):
        return "pure_escaped_quote_dense_dynamic"
    if "\\n" in blob or "\n" in blob:
        return "pure_escaped_quote_multiline"
    if token_total(row) > 0:
        return "pure_escaped_quote_light_token"
    return "pure_escaped_quote_plain"


def safety_decision(row: dict[str, Any]) -> str:
    subtype = row["structural_subtype"]
    if subtype == "pure_escaped_quote_plain":
        return "candidate_for_tiny_protected_dry_run"
    if subtype == "pure_escaped_quote_light_token":
        return "candidate_for_tiny_protected_dry_run_after_sample"
    if subtype == "pure_escaped_quote_multiline":
        return "hold_for_multiline_serialization_policy"
    if subtype == "pure_escaped_quote_dense_dynamic":
        return "hold_for_dynamic_serialization_policy"
    return "hold_not_pure_escaped_quote"


def representative_examples(rows: list[dict[str, Any]], limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = row["structural_subtype"]
        if len(grouped[key]) >= limit:
            continue
        grouped[key].append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "confirmation_level": row["confirmation_level"],
                "confirmation_source": row["confirmation_source"],
                "confirmation_label": row["confirmation_label"],
                "token_total": row["token_total"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
            }
        )
    return dict(grouped)


def main() -> None:
    plan_rows = read_jsonl(INPUT_JSONL)
    escaped = [row for row in plan_rows if row.get("change_shape") == "escaped_quote_only"]
    if len(escaped) != EXPECTED_ESCAPED_QUOTE_COUNT:
        raise SystemExit(f"escaped quote count guard failed: {len(escaped)}")

    rows: list[dict[str, Any]] = []
    for row in escaped:
        if int(row.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
            raise SystemExit("segment_state_run_id guard failed")
        subtype = structural_subtype(row)
        record = {
            **row,
            "structural_subtype": subtype,
            "safety_decision": "",
            "quote_delta_count": quote_delta_count(str(row.get("output_text") or ""), str(row.get("confirmed_text") or "")),
            "token_total": token_total(row),
            "normalized_output_equals_confirmed": normalized_output(str(row.get("output_text") or "")) == str(row.get("confirmed_text") or ""),
        }
        record["safety_decision"] = safety_decision(record)
        rows.append(record)

    subtype_counts = Counter(row["structural_subtype"] for row in rows)
    decision_counts = Counter(row["safety_decision"] for row in rows)
    confirmation_counts = Counter(
        f"{row.get('confirmation_level')}|{row.get('confirmation_source')}|{row.get('confirmation_label')}|locked={row.get('locked')}"
        for row in rows
    )
    path_counts = Counter(str(row.get("relative_path") or "").split("/", 1)[0] for row in rows)
    pure_low_risk = [
        row
        for row in rows
        if row["safety_decision"] in {
            "candidate_for_tiny_protected_dry_run",
            "candidate_for_tiny_protected_dry_run_after_sample",
        }
    ]
    pure_low_risk.sort(key=lambda row: (row["safety_decision"], row["token_total"], int(row["segment_id"])))

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_escaped_quote_only_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "input_jsonl": str(INPUT_JSONL),
        "escaped_quote_count": len(rows),
        "structural_subtype_counts": dict(subtype_counts),
        "safety_decision_counts": dict(decision_counts),
        "confirmation_counts_top": [{"key": key, "count": count} for key, count in confirmation_counts.most_common(20)],
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_counts.most_common(20)],
        "low_risk_candidate_count": len(pure_low_risk),
        "first_review_sample_segment_ids": [int(row["segment_id"]) for row in pure_low_risk[:40]],
        "representative_examples": representative_examples(rows),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "If low_risk_candidate_count is nonzero, create a small protected dry-run/diff preview only for pure plain/light-token escaped quote rows. "
            "Keep multiline and dense dynamic escaped quote rows in hold until a serialization policy exists."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_escaped_quote_only_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    sample_path = Path(str(base) + "_low_risk_sample.jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    write_jsonl(sample_path, pure_low_risk[:40])
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "low_risk_sample_jsonl": str(sample_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate escaped_quote_only diagnostic",
        "",
        f"segment_state_run_id: {EXPECTED_SEGMENT_STATE_RUN_ID}",
        f"escaped_quote_count: {len(rows)}",
        f"low_risk_candidate_count: {len(pure_low_risk)}",
        "",
        "structural_subtype_counts:",
        *[f"- {count} | {key}" for key, count in subtype_counts.most_common()],
        "",
        "safety_decision_counts:",
        *[f"- {count} | {key}" for key, count in decision_counts.most_common()],
        "",
        "guards:",
        "- candidate_generation: not_run",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- segment_state: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        f"recommendation: {summary['single_operational_recommendation']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
