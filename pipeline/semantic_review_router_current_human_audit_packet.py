from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_review_router_current_human_audit_packet_v1"
INPUT_PATTERN = "*_semantic_review_router_pending_deep_diagnostic.jsonl"
LANE_LIMITS = {
    "manual_semantic_triage": 16,
    "semantic_review_policy_design_candidate": 12,
    "domain_policy_vote_candidate": 12,
    "human_review_spanish_residue_context_risk": 8,
    "human_review_required_dynamic_gender_or_branching": 8,
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_jsonl() -> Path:
    matches = sorted(reports_dir().glob(INPUT_PATTERN), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise SystemExit(f"missing input report matching {INPUT_PATTERN}")
    return matches[0]


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


def compact(text: str | None, limit: int = 260) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def priority(row: dict[str, Any]) -> tuple[int, int, str, int]:
    lane = str(row.get("policy_lane") or "")
    risk = str(row.get("risk_bucket") or "")
    surface = str(row.get("surface_bucket") or "")
    lane_rank = {
        "manual_semantic_triage": 0,
        "semantic_review_policy_design_candidate": 1,
        "domain_policy_vote_candidate": 2,
        "human_review_spanish_residue_context_risk": 3,
        "human_review_required_dynamic_gender_or_branching": 4,
    }.get(lane, 9)
    risk_rank = {
        "low_plain_text": 0,
        "medium_dynamic_light": 1,
        "context_risk_spanish_residue": 2,
        "medium_dynamic_dense": 3,
        "high_context_es_helper": 4,
        "high_context_select_cstring": 5,
    }.get(risk, 9)
    return (lane_rank, risk_rank, surface, int(row.get("segment_id") or 0))


def select_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=priority):
        lane = str(row.get("policy_lane") or "")
        if lane not in LANE_LIMITS:
            continue
        if len(by_lane[lane]) >= LANE_LIMITS[lane]:
            continue
        by_lane[lane].append(row)
    selected: list[dict[str, Any]] = []
    for lane in LANE_LIMITS:
        selected.extend(by_lane.get(lane, []))
    return selected


def summarize(input_path: Path, rows: list[dict[str, Any]], selected: list[dict[str, Any]]) -> dict[str, Any]:
    lane_counts = Counter(str(row.get("policy_lane") or "") for row in rows)
    surface_counts = Counter(str(row.get("surface_bucket") or "") for row in rows)
    risk_counts = Counter(str(row.get("risk_bucket") or "") for row in rows)
    selected_lane_counts = Counter(str(row.get("policy_lane") or "") for row in selected)
    selected_surface_counts = Counter(str(row.get("surface_bucket") or "") for row in selected)

    review_rows = []
    for row in selected:
        review_rows.append(
            {
                "segment_id": int(row.get("segment_id") or 0),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "policy_lane": row.get("policy_lane"),
                "surface_bucket": row.get("surface_bucket"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": row.get("token_count"),
                "bracket_token_count": row.get("bracket_token_count"),
                "variable_count": row.get("variable_count"),
                "current_output_text": compact(row.get("current_output_text"), 500),
                "english_text": compact(row.get("english_text"), 500),
                "spanish_text": compact(row.get("spanish_text"), 500),
            }
        )

    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "rows_available": len(rows),
        "review_count": len(review_rows),
        "lane_counts": [{"key": key, "count": value} for key, value in lane_counts.most_common()],
        "surface_counts": [{"key": key, "count": value} for key, value in surface_counts.most_common()],
        "risk_counts": [{"key": key, "count": value} for key, value in risk_counts.most_common()],
        "selected_lane_counts": [{"key": key, "count": value} for key, value in selected_lane_counts.most_common()],
        "selected_surface_counts": [{"key": key, "count": value} for key, value in selected_surface_counts.most_common()],
        "review_rows": review_rows,
        "apply_ready_now": 0,
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_production_full_now": False,
        "production_full_recommended_now": False,
        "next_action": "human_review_selected_rows_then_ingest_confirmed_only",
    }


def write_outputs(packet: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_router_current_human_audit_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in packet["review_rows"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "semantic review router current human audit packet",
        f"source={SOURCE}",
        f"input_jsonl={packet['input_jsonl']}",
        f"rows_available={packet['rows_available']}",
        f"review_count={packet['review_count']}",
        "",
        "selected_lane_counts:",
    ]
    for item in packet["selected_lane_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "review_rows:"])
    for row in packet["review_rows"]:
        lines.append(
            "- "
            f"{row['segment_id']} | {row['policy_lane']} | {row['surface_bucket']} | "
            f"{row['risk_bucket']} | {row['relative_path']} | {row['source_key']} | "
            f"{compact(row['current_output_text'], 220)}"
        )
    lines.extend(
        [
            "",
            f"apply_ready_now={packet['apply_ready_now']}",
            f"run_segment_state_now={str(packet['run_segment_state_now']).lower()}",
            f"run_reindex_now={str(packet['run_reindex_now']).lower()}",
            f"production_full_recommended_now={str(packet['production_full_recommended_now']).lower()}",
            f"next_action={packet['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_jsonl()
    rows = read_jsonl(input_path)
    selected = select_rows(rows)
    packet = summarize(input_path, rows, selected)
    txt_path, jsonl_path, summary_path = write_outputs(packet)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_available={packet['rows_available']}")
    print(f"review_count={packet['review_count']}")
    print(f"selected_lane_counts={packet['selected_lane_counts']}")
    print(f"next_action={packet['next_action']}")


if __name__ == "__main__":
    main()
