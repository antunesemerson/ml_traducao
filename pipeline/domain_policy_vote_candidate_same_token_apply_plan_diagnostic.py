from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_same_token_apply_plan_diagnostic_v1"
INPUT_JSONL = Path("reports/20260630_153044_976657_domain_policy_vote_candidate_pending_apply_confirmed_diagnostic.jsonl")
EXPECTED_SEGMENT_STATE_RUN_ID = 514
EXPECTED_SAME_TOKEN_COUNT = 3934
PLAN_LIMIT = 80


DENSE_MARKERS = (
    "$EFFECT_LIST_BULLET$",
    "Select_CString",
    "ES_",
    "GetTrait(",
    "GetCultureTradition(",
    "ScriptValue(",
    "GetVassalStance(",
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


def token_total(tokens: dict[str, int]) -> int:
    return sum(int(value) for value in tokens.values())


def has_newline_escape(text: str) -> bool:
    return "\\n" in text or "\n" in text


def surface_bucket(row: dict[str, Any]) -> str:
    output = str(row.get("output_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    blob = output + "\n" + confirmed
    if any(marker in blob for marker in DENSE_MARKERS):
        return "dense_dynamic_or_effect_list"
    if has_newline_escape(blob):
        return "multiline_same_token"
    tokens = token_total(row.get("output_tokens") or {})
    if tokens >= 4:
        return "tokenized_same_token"
    if tokens > 0:
        return "light_token_same_token"
    return "plain_text_same_token"


def confirmation_bucket(row: dict[str, Any]) -> str:
    level = str(row.get("confirmation_level") or "")
    source = str(row.get("confirmation_source") or "")
    label = str(row.get("confirmation_label") or "")
    locked = int(row.get("locked") or 0)
    if level == "human_confirmed" and locked == 1:
        if source == "local_learning":
            return "human_locked_local_learning"
        return "human_locked_other"
    if level == "human_confirmed":
        return "human_unlocked"
    if level == "auto_confirmed":
        return "auto_confirmed"
    return "other_confirmation"


def safety_bucket(row: dict[str, Any]) -> str:
    surface = surface_bucket(row)
    confirmation = confirmation_bucket(row)
    if surface == "plain_text_same_token" and confirmation.startswith("human_locked"):
        return "tier1_plain_human_locked"
    if surface == "light_token_same_token" and confirmation.startswith("human_locked"):
        return "tier2_light_token_human_locked"
    if surface in {"plain_text_same_token", "light_token_same_token"} and confirmation == "human_unlocked":
        return "tier3_human_unlocked_light"
    if surface in {"plain_text_same_token", "light_token_same_token"} and confirmation == "auto_confirmed":
        return "tier4_auto_light"
    if surface in {"multiline_same_token", "tokenized_same_token"} and confirmation.startswith("human_locked"):
        return "hold_human_locked_structural"
    return "hold_dense_or_auto_structural"


def change_shape(row: dict[str, Any]) -> str:
    output = str(row.get("output_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    if output.replace('\\"', '"') == confirmed:
        return "escaped_quote_only"
    if output.replace("\\n", "\n") == confirmed:
        return "newline_escape_only"
    if output.lower() == confirmed.lower():
        return "case_only"
    if re.sub(r"\s+", " ", output).strip() == re.sub(r"\s+", " ", confirmed).strip():
        return "whitespace_only"
    return "text_replacement_same_token"


def plan_rank(row: dict[str, Any]) -> tuple[int, int, int, int]:
    safety_order = {
        "tier1_plain_human_locked": 0,
        "tier2_light_token_human_locked": 1,
        "tier3_human_unlocked_light": 2,
        "tier4_auto_light": 3,
        "hold_human_locked_structural": 9,
        "hold_dense_or_auto_structural": 10,
    }
    path = str(row.get("relative_path") or "")
    path_penalty = 0 if "/" not in path else 1
    return (
        safety_order.get(row["safety_bucket"], 99),
        token_total(row.get("output_tokens") or {}),
        path_penalty,
        int(row["segment_id"]),
    )


def representative_examples(rows: list[dict[str, Any]], limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = row["safety_bucket"]
        if len(grouped[key]) >= limit:
            continue
        grouped[key].append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "surface_bucket": row["surface_bucket"],
                "confirmation_bucket": row["confirmation_bucket"],
                "change_shape": row["change_shape"],
                "output_text": row["output_text"],
                "confirmed_text": row["confirmed_text"],
            }
        )
    return dict(grouped)


def main() -> None:
    all_rows = read_jsonl(INPUT_JSONL)
    rows = [row for row in all_rows if row.get("diagnostic_bucket") == "same_token_signature_apply_candidate"]
    if len(rows) != EXPECTED_SAME_TOKEN_COUNT:
        raise SystemExit(f"same-token count guard failed: {len(rows)}")

    enriched: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
            raise SystemExit("segment_state_run_id guard failed")
        record = {
            **row,
            "surface_bucket": surface_bucket(row),
            "confirmation_bucket": confirmation_bucket(row),
            "change_shape": change_shape(row),
        }
        record["safety_bucket"] = safety_bucket(record)
        record["planned_batch_candidate"] = record["safety_bucket"] in {
            "tier1_plain_human_locked",
            "tier2_light_token_human_locked",
        }
        enriched.append(record)

    planned_pool = [row for row in enriched if row["planned_batch_candidate"]]
    planned_pool.sort(key=plan_rank)
    first_batch = planned_pool[:PLAN_LIMIT]

    safety_counts = Counter(row["safety_bucket"] for row in enriched)
    surface_counts = Counter(row["surface_bucket"] for row in enriched)
    confirmation_counts = Counter(row["confirmation_bucket"] for row in enriched)
    change_counts = Counter(row["change_shape"] for row in enriched)
    path_counts = Counter(str(row.get("relative_path") or "").split("/", 1)[0] for row in enriched)
    batch_counts = Counter(row["safety_bucket"] for row in first_batch)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_same_token_apply_plan_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "input_jsonl": str(INPUT_JSONL),
        "same_token_count": len(enriched),
        "planned_pool_count": len(planned_pool),
        "first_batch_limit": PLAN_LIMIT,
        "first_batch_count": len(first_batch),
        "safety_bucket_counts": dict(safety_counts),
        "surface_bucket_counts": dict(surface_counts),
        "confirmation_bucket_counts": dict(confirmation_counts),
        "change_shape_counts": dict(change_counts),
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_counts.most_common(20)],
        "first_batch_safety_counts": dict(batch_counts),
        "first_batch_segment_ids": [int(row["segment_id"]) for row in first_batch],
        "representative_examples": representative_examples(enriched),
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
            "Next, generate a protected dry-run/diff preview for only the first 80 same-token human-locked rows. "
            "Do not include structural/multiline/dense rows or token-mismatch rows in the first apply cycle."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_same_token_apply_plan_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    batch_jsonl_path = Path(str(base) + "_first_batch.jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, enriched)
    write_jsonl(batch_jsonl_path, first_batch)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "first_batch_jsonl": str(batch_jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "domain_policy_vote_candidate same-token apply plan diagnostic",
        "",
        f"segment_state_run_id: {EXPECTED_SEGMENT_STATE_RUN_ID}",
        f"same_token_count: {len(enriched)}",
        f"planned_pool_count: {len(planned_pool)}",
        f"first_batch_count: {len(first_batch)}",
        "",
        "safety_bucket_counts:",
        *[f"- {count} | {key}" for key, count in safety_counts.most_common()],
        "",
        "surface_bucket_counts:",
        *[f"- {count} | {key}" for key, count in surface_counts.most_common()],
        "",
        "confirmation_bucket_counts:",
        *[f"- {count} | {key}" for key, count in confirmation_counts.most_common()],
        "",
        "first_batch_safety_counts:",
        *[f"- {count} | {key}" for key, count in batch_counts.most_common()],
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
