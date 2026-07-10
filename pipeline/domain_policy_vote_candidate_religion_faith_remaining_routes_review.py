from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_remaining_routes_review_v1"
INPUT_PATH = Path("reports/20260630_083519_141707_domain_policy_vote_candidate_religion_faith_getter_role_policy_dry_run.jsonl")
TARGET_ROUTES = {
    "route_faith_getter_faith_adjective",
    "route_faith_getter_faith_name",
    "route_faith_getter_religion_family_name",
}
EXPECTED_COUNT = 8

REVIEW_NOTES: dict[int, tuple[str, str, str]] = {
    22171: (
        "needs_more_context",
        "",
        "Opinion reason uses trait + faith adjective; 'um(a)' may be intentional fallback but is awkward and needs UI context.",
    ),
    164058: (
        "needs_more_context",
        "",
        "Great holy war name plus faith adjective order may depend on GHWName runtime expansion.",
    ),
    164063: (
        "needs_more_context",
        "",
        "Second-person variant of great holy war name plus faith adjective; keep paired with 164058.",
    ),
    111890: (
        "approve_correction_candidate",
        "[6090_main_county.GetName] será concedido a uma pessoa local da fé [6090_main_county.GetCountyData.GetFaith.GetName|l]. Você terá uma [hook|lE] sobre ela.",
        "Fix 'um local' as person and missing sentence break; preserves tokens.",
    ),
    38677: (
        "approve_already_ok_candidate",
        "",
        "Current wording is acceptable: 'local sagrado para [faith]' is natural enough.",
    ),
    20748: (
        "needs_more_context",
        "",
        "Religion family adjective/name placement is unclear; current text is awkward but token expansion must be known.",
    ),
    75630: (
        "approve_already_ok_candidate",
        "",
        "Current requirement text is acceptable.",
    ),
    75631: (
        "approve_already_ok_candidate",
        "",
        "Current requirement text is acceptable.",
    ),
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate religion faith remaining routes review",
        "",
        f"input_path: {summary['input_path']}",
        f"review_count: {summary['review_count']}",
        f"expected_count: {summary['expected_count']}",
        f"count_matches_expected: {str(summary['count_matches_expected']).lower()}",
        "",
        "route_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["route_counts"])
    lines.extend(["", "recommended_decision_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["recommended_decision_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['route']} | {row['recommended_decision']}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- english_text: {row.get('english_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
                f"- suggested_text: {row.get('suggested_text') or ''}",
                f"- review_notes: {row.get('review_notes')}",
            ]
        )
    lines.extend(
        [
            "",
            "operational_recommendation:",
            f"- {summary['single_operational_recommendation']}",
            "",
            "gates:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows = [
        row
        for row in read_jsonl(INPUT_PATH)
        if row.get("route") in TARGET_ROUTES and row.get("route_status") == "split_only_no_candidate"
    ]
    if len(rows) != EXPECTED_COUNT:
        raise SystemExit(f"review count guard failed: {len(rows)} expected {EXPECTED_COUNT}")
    missing = sorted(set(int(row["segment_id"]) for row in rows) - set(REVIEW_NOTES))
    if missing:
        raise SystemExit(f"missing review notes for segment ids: {missing}")

    reviewed: list[dict[str, Any]] = []
    for row in rows:
        decision, suggested_text, notes = REVIEW_NOTES[int(row["segment_id"])]
        reviewed.append(
            {
                **row,
                "review_source": SOURCE,
                "recommended_decision": decision,
                "suggested_text": suggested_text,
                "review_notes": notes,
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )
    reviewed.sort(key=lambda row: (str(row["route"]), int(row["segment_id"])))

    route_counts = Counter(str(row["route"]) for row in reviewed)
    decision_counts = Counter(str(row["recommended_decision"]) for row in reviewed)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_remaining_route_review",
        "input_path": str(INPUT_PATH),
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "route_counts": top_counter(route_counts),
        "recommended_decision_counts": top_counter(decision_counts),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "single_operational_recommendation": (
            "Ask human confirmation for the 1 correction candidate and 3 already-ok candidates; keep the 4 context-risk "
            "items held. If approved, run protected apply only for segment 111890 and ingest the 3 already-ok signals."
        ),
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_remaining_routes_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, reviewed)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, reviewed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
