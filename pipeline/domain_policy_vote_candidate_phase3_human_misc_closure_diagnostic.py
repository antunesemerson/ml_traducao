from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "domain_policy_vote_candidate_phase3_human_misc_closure_diagnostic_v1"
DEFAULT_PACKET_JSONL = Path("reports/20260702_114315_480874_domain_policy_vote_candidate_closure_debt_architecture_packet_512_541.jsonl")
PHASE = "phase_3_human_misc_equal_output_bridge"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only phase 3 human misc equal-output closure diagnostic.")
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def dynamic_flags(text: str | None) -> dict[str, int]:
    tokens = protected_tokens(text or "")
    token_blob = " ".join(tokens)
    return {
        "has_select_cstring": int("Select_CString" in token_blob),
        "has_select_localization": int("SelectLocalization" in token_blob),
        "has_scope_getter": int(any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE."])),
        "has_multiline": int("\\n" in tokens or "\n" in (text or "")),
        "protected_token_count": len(tokens),
    }


def operational_bucket(row: dict[str, Any]) -> str:
    surface = str(row.get("token_surface") or "")
    text = str(row.get("confirmed_text") or row.get("output_text") or "")
    flags = dynamic_flags(text)
    if surface == "plain_text":
        return "plain_text_safe_surface"
    if surface == "light_token" and not any(
        flags[key] for key in ("has_select_cstring", "has_select_localization", "has_scope_getter", "has_multiline")
    ):
        return "light_token_safe_surface"
    if flags["has_multiline"]:
        return "multiline_hold_or_later_split"
    if flags["has_select_cstring"] or flags["has_select_localization"]:
        return "dynamic_select_hold_or_policy"
    if flags["has_scope_getter"]:
        return "dynamic_getter_hold_or_policy"
    return "other_structural_hold"


def bridge_guard_ok(row: dict[str, Any]) -> bool:
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    bucket = operational_bucket(row)
    return (
        bucket in {"plain_text_safe_surface", "light_token_safe_surface"}
        and int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("open_issue_count") or 0) == 0
        and int(row.get("high_issue_count") or 0) == 0
        and str(row.get("confirmation_level") or "") == "human_confirmed"
        and canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)
    )


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": row.get("segment_id"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": row.get("locked"),
        "open_issue_count": row.get("open_issue_count"),
        "high_issue_count": row.get("high_issue_count"),
        "operational_bucket": operational_bucket(row),
        "bridge_guard_ok": bridge_guard_ok(row),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    packet_rows = read_jsonl(args.packet_jsonl)
    phase_rows = [row for row in packet_rows if row.get("phase") == PHASE]
    records: list[dict[str, Any]] = []
    for row in phase_rows:
        flags = dynamic_flags(str(row.get("confirmed_text") or row.get("output_text") or ""))
        records.append(
            {
                "source": SOURCE,
                "record_type": "phase3_human_misc_row",
                **compact(row),
                **flags,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    bucket_counts = Counter(str(record["operational_bucket"]) for record in records)
    surface_counts = Counter(str(record["token_surface"]) for record in records)
    label_counts = Counter(str(record["confirmation_label"]) for record in records)
    source_counts = Counter(str(record["confirmation_source"]) for record in records)
    locked_counts = Counter(f"locked={int(record.get('locked') or 0)}" for record in records)
    path_counts = Counter(str(record.get("relative_path") or "").split("/", 1)[0] for record in records)
    guard_ok_rows = [record for record in records if record["bridge_guard_ok"]]
    samples_by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        bucket = str(record["operational_bucket"])
        if len(samples_by_bucket[bucket]) < 8:
            samples_by_bucket[bucket].append(
                {
                    "segment_id": record["segment_id"],
                    "relative_path": record["relative_path"],
                    "source_key": record["source_key"],
                    "confirmation_source": record["confirmation_source"],
                    "confirmation_label": record["confirmation_label"],
                    "output_text": record["output_text"],
                    "confirmed_text": record["confirmed_text"],
                }
            )

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_human_misc_closure_diagnostic",
        "input_packet_jsonl": str(args.packet_jsonl),
        "phase": PHASE,
        "record_count": len(records),
        "bridge_guard_ok_count": len(guard_ok_rows),
        "bridge_guard_blocked_count": len(records) - len(guard_ok_rows),
        "operational_bucket_counts": dict(bucket_counts.most_common()),
        "token_surface_counts": dict(surface_counts.most_common()),
        "confirmation_label_counts": dict(label_counts.most_common(40)),
        "confirmation_source_counts": dict(source_counts.most_common(40)),
        "locked_counts": dict(locked_counts.most_common()),
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_counts.most_common(20)],
        "samples_by_bucket": dict(samples_by_bucket),
        "ready_bridge_segment_ids": [int(record["segment_id"]) for record in guard_ok_rows],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    if len(guard_ok_rows) > 0:
        summary["single_operational_recommendation"] = (
            "Prepare a separate narrow bridge dry-run for phase3 human misc plain/light safe-surface rows only. "
            "Keep dynamic getter/select/multiline buckets in hold or later architecture-specific split."
        )
    else:
        summary["single_operational_recommendation"] = (
            "Do not create a bridge from phase3 now; use human/architecture split for remaining structural buckets."
        )
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_human_misc_closure_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Phase3 human misc equal-output closure diagnostic",
        f"record_count={summary['record_count']}",
        f"bridge_guard_ok_count={summary['bridge_guard_ok_count']}",
        f"bridge_guard_blocked_count={summary['bridge_guard_blocked_count']}",
        f"operational_bucket_counts={json.dumps(summary['operational_bucket_counts'], ensure_ascii=False, sort_keys=True)}",
        f"token_surface_counts={json.dumps(summary['token_surface_counts'], ensure_ascii=False, sort_keys=True)}",
        f"confirmation_label_counts={json.dumps(summary['confirmation_label_counts'], ensure_ascii=False, sort_keys=True)}",
        f"confirmation_source_counts={json.dumps(summary['confirmation_source_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"bridge_guard_ok_count={summary['bridge_guard_ok_count']}")
    print(f"bridge_guard_blocked_count={summary['bridge_guard_blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
