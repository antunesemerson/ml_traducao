from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name_human_micro_review_packet_v1"
AGENT_KEY = "religion_faith_doctrine_holy_site_effect_name_preposition_policy"
INPUT_JSONL_PATH = Path(
    "reports/20260630_141422_155163_religion_faith_doctrine_holy_site_effect_name_preposition_policy_dry_run.jsonl"
)
EXPECTED_SEGMENT_STATE_RUN_ID = 509
EXPECTED_INPUT_COUNT = 73
TARGET_SAMPLE_COUNT = 16


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def validate_registry(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_agent_registry WHERE agent_key = ?", (AGENT_KEY,)).fetchone()
    if row is None:
        raise SystemExit(f"missing registry agent: {AGENT_KEY}")
    payload = dict(row)
    notes = json.loads(payload.get("notes_json") or "{}")
    if payload.get("operational_state") != "shadow":
        raise SystemExit("registry operational_state guard failed")
    if payload.get("decision_role") != "route_and_split":
        raise SystemExit("registry decision_role guard failed")
    if notes.get("candidate_generation_allowed") is not False:
        raise SystemExit("registry candidate_generation guard failed")
    if notes.get("auto_apply_allowed") is not False:
        raise SystemExit("registry auto_apply guard failed")
    if notes.get("lifecycle_allowed") is not False:
        raise SystemExit("registry lifecycle guard failed")
    return {
        "agent_key": payload.get("agent_key"),
        "agent_type": payload.get("agent_type"),
        "operational_state": payload.get("operational_state"),
        "decision_role": payload.get("decision_role"),
        "scope_group": payload.get("scope_group"),
    }


def prefix_class(text: str) -> str:
    if text.startswith('"A partir de') or text.startswith('\\"A partir de'):
        return "quoted_a_partir_de"
    if text.startswith("Do [holy_site|E]"):
        return "do_singular_upper_token"
    if text.startswith("Do [holy_site|lE]"):
        return "do_singular_lower_token"
    if text.startswith("De [holy_site|lE]"):
        return "de_singular_lower_token"
    if text.startswith("De [holy_sites|lE]"):
        return "de_plural_lower_token"
    return "other"


def select_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_prefix: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_prefix.setdefault(prefix_class(str(row.get("current_output_text") or "")), []).append(row)
    for bucket in by_prefix.values():
        bucket.sort(key=lambda item: str(item.get("source_key") or ""))

    sample: list[dict[str, Any]] = []
    edge_classes = [
        "quoted_a_partir_de",
        "do_singular_upper_token",
        "de_plural_lower_token",
        "de_singular_lower_token",
        "do_singular_lower_token",
    ]
    seen: set[int] = set()
    for cls in edge_classes:
        for row in by_prefix.get(cls, [])[:3 if cls == "de_plural_lower_token" else 1]:
            sid = int(row["segment_id"])
            if sid not in seen:
                sample.append(row)
                seen.add(sid)

    remaining_classes = ["do_singular_lower_token", "de_singular_lower_token", "de_plural_lower_token"]
    while len(sample) < TARGET_SAMPLE_COUNT:
        progressed = False
        for cls in remaining_classes:
            for row in by_prefix.get(cls, []):
                sid = int(row["segment_id"])
                if sid in seen:
                    continue
                sample.append(row)
                seen.add(sid)
                progressed = True
                break
            if len(sample) >= TARGET_SAMPLE_COUNT:
                break
        if not progressed:
            break

    if len(sample) != TARGET_SAMPLE_COUNT:
        raise SystemExit(f"sample count guard failed: {len(sample)}")
    return sample


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    rows = read_jsonl(INPUT_JSONL_PATH)
    if len(rows) != EXPECTED_INPUT_COUNT:
        raise SystemExit(f"input count guard failed: {len(rows)}")
    bad_flags = [
        int(row["segment_id"])
        for row in rows
        if int(row.get("latest_needs_output_apply") or 0) != 0
        or row.get("candidate_generation_allowed") is not False
        or row.get("auto_apply_allowed") is not False
        or row.get("lifecycle_allowed") is not False
        or row.get("production_release_allowed") is not False
    ]
    if bad_flags:
        raise SystemExit(f"row guard flags failed: {bad_flags[:10]}")
    with connect_readonly() as conn:
        registry = validate_registry(conn)

    sample_source = select_sample(rows)
    packet: list[dict[str, Any]] = []
    for index, row in enumerate(sample_source, start=1):
        current = str(row.get("current_output_text") or "")
        packet.append(
            {
                "record_type": "human_micro_review_item",
                "source": SOURCE,
                "agent_key": AGENT_KEY,
                "review_index": index,
                "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "route": row.get("route"),
                "prefix_class": prefix_class(current),
                "current_output_text": current,
                "english_text": row.get("english_text"),
                "latest_state_group": row.get("latest_state_group"),
                "latest_final_state": row.get("latest_final_state"),
                "latest_needs_output_apply": int(row.get("latest_needs_output_apply") or 0),
                "human_review_question": (
                    "Confirmar se a formulação atual é aceitável para PT-BR como nome de efeito de local sagrado, "
                    "ou informar corrected_text/hold_context."
                ),
                "suggested_decision_options": [
                    "approve_already_ok",
                    "corrected_text",
                    "hold_context",
                    "architecture_parser_later",
                ],
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
            }
        )

    prefix_counts = Counter(row["prefix_class"] for row in packet)
    route_counts = Counter(row["route"] for row in packet)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_micro_review_packet",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "agent_key": AGENT_KEY,
        "registry": registry,
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "input_jsonl": str(INPUT_JSONL_PATH),
        "input_count": len(rows),
        "sample_count": len(packet),
        "prefix_class_counts": dict(prefix_counts),
        "route_counts": dict(route_counts),
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
            "Review the 16 sampled rows with human decisions. If most are approve_already_ok, ingest confirmations "
            "for reviewed rows only; do not generalize to all 73 until the article/preposition pattern is explicitly approved."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_religion_faith_doctrine_holy_site_effect_name_human_micro_review_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, packet)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "religion_faith_doctrine holy-site effect-name human micro review packet",
        "",
        f"agent_key: {AGENT_KEY}",
        f"input_count: {len(rows)}",
        f"sample_count: {len(packet)}",
        "",
        "prefix_class_counts:",
        *[f"- {count} | {key}" for key, count in prefix_counts.most_common()],
        "",
        "items:",
    ]
    for row in packet:
        lines.extend(
            [
                "",
                f"## {row['review_index']}. segment_id {row['segment_id']} | {row['prefix_class']} | {row['source_key']}",
                f"- english: {row['english_text']}",
                f"- output: {row['current_output_text']}",
                "- decision: approve_already_ok / corrected_text / hold_context / architecture_parser_later",
            ]
        )
    lines.extend(
        [
            "",
            "guards:",
            "- candidate_generation: not_run",
            "- apply: not_run",
            "- lifecycle: not_run",
            "- segment_state: not_run",
            "- reindex: not_run",
            "- full_production: not_run",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
