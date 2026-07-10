from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_dynamic_select_review_v1"
ARCHITECTURE_PACKET_JSONL = Path(
    "reports/20260701_170147_208806_domain_policy_vote_candidate_closure_debt_architecture_packet_512_533.jsonl"
)
STRUCTURAL_JSONL = Path("reports/20260701_170354_834884_domain_policy_vote_candidate_phase3_remaining_structural_diagnostic.jsonl")
CURRENT_RUN_ID = 533
EXPECTED_COUNT = 145
SAMPLE_LIMIT = 8

SELECT_CSTRING_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
SELECT_LOCALIZATION_RE = re.compile(r"SelectLocalization\s*\(", re.IGNORECASE)
ADD_LOCALIZATION_IF_RE = re.compile(r"AddLocalizationIf\s*\(", re.IGNORECASE)
LOCAL_PLAYER_STRING_RE = re.compile(r"LocalPlayerString\s*\(", re.IGNORECASE)
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"](ES_[A-Za-z0-9_]+)['\"]\)\]")
GETTER_RE = re.compile(r"\[[^\]]+\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only phase 3 dynamic select review.")
    parser.add_argument("--architecture-packet-jsonl", type=Path, default=ARCHITECTURE_PACKET_JSONL)
    parser.add_argument("--structural-jsonl", type=Path, default=STRUCTURAL_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def text_for(row: dict[str, Any]) -> str:
    return str(row.get("confirmed_text") or row.get("output_text") or "")


def has_newline(text: str) -> bool:
    return "\\n" in text or "\n" in text


def selector_counts(text: str) -> dict[str, int]:
    return {
        "select_cstring": len(SELECT_CSTRING_RE.findall(text)),
        "select_localization": len(SELECT_LOCALIZATION_RE.findall(text)),
        "add_localization_if": len(ADD_LOCALIZATION_IF_RE.findall(text)),
        "local_player_string": len(LOCAL_PLAYER_STRING_RE.findall(text)),
    }


def select_surface(counts: dict[str, int]) -> str:
    active = [key for key, value in counts.items() if value]
    if not active:
        return "no_select_detected"
    if len(active) > 1:
        return "mixed_select_surfaces"
    return active[0]


def subtype(text: str, counts: dict[str, int]) -> str:
    total_selects = sum(counts.values())
    if total_selects > 1:
        return "multiple_select_expressions"
    if SELECT_LOCALIZATION_RE.search(text) or ADD_LOCALIZATION_IF_RE.search(text):
        if "$BULLET$" in text or "$EFFECT" in text:
            return "selectlocalization_effect_or_bullet"
        if "ScriptValue" in text or "GreaterThanOrEqual" in text or "HasDlcFeature" in text:
            return "selectlocalization_condition_branch"
        return "selectlocalization_branch_key"
    if LOCAL_PLAYER_STRING_RE.search(text):
        return "local_player_string_perspective"
    if SELECT_CSTRING_RE.search(text):
        if "IsLocalPlayer" in text or "GetLocalPlayer" in text or ".IsPlayer" in text:
            return "select_cstring_player_perspective"
        if "IsFemale" in text:
            return "select_cstring_gender_literal"
        if "GetSortOrder" in text or "SortOrder" in text:
            return "select_cstring_ui_sort_toggle"
        return "select_cstring_generic_branch"
    return "dynamic_select_unknown"


def risk_bucket(text: str, counts: dict[str, int], structural_subtype: str) -> str:
    total_selects = sum(counts.values())
    if total_selects == 0:
        return "hold_no_select_detected"
    if total_selects > 1:
        return "high_multiple_selects"
    if has_newline(text):
        return "hold_multiline_select"
    if ES_TOKEN_RE.search(text):
        return "hold_select_plus_es_gender"
    if structural_subtype in {"select_cstring_ui_sort_toggle", "selectlocalization_condition_branch", "selectlocalization_branch_key"}:
        return "parser_policy_candidate_read_only"
    if structural_subtype == "select_cstring_player_perspective":
        return "needs_select_cstring_player_policy_review"
    if structural_subtype == "select_cstring_gender_literal":
        return "needs_select_cstring_gender_policy_review"
    return "needs_select_parser_review"


def operational_recommendation(risk: str) -> str:
    if risk == "parser_policy_candidate_read_only":
        return "review_as_read_only_parser_policy_candidate"
    if risk.startswith("hold_") or risk.startswith("high_"):
        return "hold_or_parser_later"
    return "architecture_review_before_any_bridge"


def compact(row: dict[str, Any], current_run_id: int) -> dict[str, Any]:
    text = text_for(row)
    counts = selector_counts(text)
    structural_subtype = subtype(text, counts)
    risk = risk_bucket(text, counts, structural_subtype)
    return {
        "source": SOURCE,
        "current_run_id": current_run_id,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "phase": row.get("phase"),
        "final_state_current": row.get("to_final_state"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "select_surface": select_surface(counts),
        "select_counts": counts,
        "select_total_count": sum(counts.values()),
        "structural_subtype": structural_subtype,
        "risk_bucket": risk,
        "operational_recommendation": operational_recommendation(risk),
        "has_newline": has_newline(text),
        "has_es_token": bool(ES_TOKEN_RE.search(text)),
        "es_token_types": sorted(set(ES_TOKEN_RE.findall(text))),
        "getter_count": len(GETTER_RE.findall(text)),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
    }


def build(
    architecture_packet_jsonl: Path,
    structural_jsonl: Path,
    current_run_id: int,
    expected_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    structural = read_jsonl(structural_jsonl)
    ids = {
        int(row["segment_id"])
        for row in structural
        if row.get("structural_family") == "dynamic_select_localization"
    }
    arch_rows = {int(row["segment_id"]): row for row in read_jsonl(architecture_packet_jsonl)}
    records = [compact(arch_rows[segment_id], current_run_id) for segment_id in sorted(ids)]

    surface_counts = Counter(row["select_surface"] for row in records)
    subtype_counts = Counter(row["structural_subtype"] for row in records)
    risk_counts = Counter(row["risk_bucket"] for row in records)
    op_counts = Counter(row["operational_recommendation"] for row in records)
    source_counts = Counter(row["confirmation_source"] for row in records)
    label_counts = Counter(row["confirmation_label"] for row in records)
    newline_counts = Counter("has_newline" if row["has_newline"] else "single_line" for row in records)
    es_counts = Counter("has_es_token" if row["has_es_token"] else "no_es_token" for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["risk_bucket"]]) < SAMPLE_LIMIT:
            samples[row["risk_bucket"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_dynamic_select_review",
        "architecture_packet_jsonl": str(architecture_packet_jsonl),
        "structural_jsonl": str(structural_jsonl),
        "current_run_id": current_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "select_surface_counts": dict(sorted(surface_counts.items())),
        "structural_subtype_counts": dict(subtype_counts.most_common(40)),
        "risk_bucket_counts": dict(sorted(risk_counts.items())),
        "operational_recommendation_counts": dict(sorted(op_counts.items())),
        "newline_counts": dict(sorted(newline_counts.items())),
        "es_token_presence_counts": dict(sorted(es_counts.items())),
        "confirmation_source_counts": dict(source_counts.most_common(40)),
        "confirmation_label_counts": dict(label_counts.most_common(50)),
        "samples_by_risk_bucket": samples,
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
            "Do not materialize a broad dynamic-select lifecycle bridge. Review the single-line parser-policy candidates first, "
            "and keep multiline, multiple-select, player-perspective and gender-literal cases out of lifecycle until architecture review."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_dynamic_select_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 dynamic select review",
        f"record_count={summary['record_count']}",
        "",
        "Select surfaces:",
    ]
    for key, count in summary["select_surface_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Structural subtypes:"])
    for key, count in summary["structural_subtype_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Risk buckets:"])
    for key, count in summary["risk_bucket_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "segment_state_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "source_changed=false",
            "output_changed=false",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(
        args.architecture_packet_jsonl,
        args.structural_jsonl,
        args.current_run_id,
        args.expected_count,
    )
    if summary["record_count"] != args.expected_count:
        raise SystemExit(f"dynamic select count guard failed: {summary['record_count']}")
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
