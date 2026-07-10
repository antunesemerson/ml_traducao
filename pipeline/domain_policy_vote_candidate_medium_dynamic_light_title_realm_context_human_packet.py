from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_human_packet"
REVIEW_PATH = Path("reports/20260629_145809_362931_domain_policy_vote_candidate_medium_dynamic_light_title_realm_route_review.jsonl")
TARGET_SUBTYPE = "requires_article_or_preposition_context"
EXPECTED_COUNT = 19


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


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_packet(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [row for row in rows if row.get("operational_subtype") == TARGET_SUBTYPE]
    if len(selected) != EXPECTED_COUNT:
        raise SystemExit(f"packet count guard failed: {len(selected)} expected {EXPECTED_COUNT}")
    selected.sort(key=lambda row: (str(row.get("source_family") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    packet: list[dict[str, Any]] = []
    for index, row in enumerate(selected, start=1):
        packet.append(
            {
                **row,
                "packet_index": index,
                "queue_source": SOURCE,
                "human_decision": "",
                "corrected_text": "",
                "review_notes": "",
                "allowed_decisions": [
                    "approve_already_ok",
                    "approve_correction",
                    "needs_more_context",
                    "hold_context_article_preposition",
                    "reject",
                ],
            }
        )
    return packet


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light title/realm context human packet",
        "",
        f"source_review: {summary['source_review']}",
        f"packet_count: {summary['packet_count']}",
        f"target_subtype: {summary['target_subtype']}",
        "",
        "source_family_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["source_family_counts"])
    lines.extend(["", "token_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["token_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## {row['packet_index']}. segment_id {row['segment_id']}",
                f"- source_family: {row.get('source_family')}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
                "- human_decision:",
                "- corrected_text:",
                "- review_notes:",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows = build_packet(read_jsonl(REVIEW_PATH))
    source_family_counts = Counter(str(row.get("source_family") or "") for row in rows)
    token_counts = Counter(str(row.get("output_token") or "") for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_packet",
        "source_review": str(REVIEW_PATH),
        "policy_parent": "medium_dynamic_light_getter_role_policy",
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "route": "route_getter_title_or_realm_name",
        "target_subtype": TARGET_SUBTYPE,
        "packet_count": len(rows),
        "source_family_counts": top_counter(source_family_counts),
        "token_counts": top_counter(token_counts),
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
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
