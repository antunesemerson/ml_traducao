from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_dynamic_getter_residual_diagnostic_v1"
ARCHITECTURE_PACKET_JSONL = Path(
    "reports/20260701_170147_208806_domain_policy_vote_candidate_closure_debt_architecture_packet_512_533.jsonl"
)
STRUCTURAL_JSONL = Path("reports/20260701_170354_834884_domain_policy_vote_candidate_phase3_remaining_structural_diagnostic.jsonl")
CURRENT_RUN_ID = 533
EXPECTED_COUNT = 908
SAMPLE_LIMIT = 8

BRACKET_TOKEN_RE = re.compile(r"\[[^\]]+\]")
GETTER_RE = re.compile(r"\[[^\]]*(?:\bGet[A-Za-z0-9_]*|\.Custom\(|ScriptValue|Concept\(|ROOT\.|FROM\.|SCOPE\.|TARGET\.)[^\]]*\]")
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"](ES_[A-Za-z0-9_]+)['\"]\)\]")
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString", re.IGNORECASE)
SCOPE_RE = re.compile(r"\b(?:ROOT|FROM|SCOPE|TARGET|THIS)\.")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only phase 3 dynamic getter residual diagnostic.")
    parser.add_argument("--architecture-packet-jsonl", type=Path, default=ARCHITECTURE_PACKET_JSONL)
    parser.add_argument("--structural-jsonl", type=Path, default=STRUCTURAL_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
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


def text_for(row: dict[str, Any]) -> str:
    return str(row.get("confirmed_text") or row.get("output_text") or "")


def has_newline(text: str) -> bool:
    return "\\n" in text or "\n" in text


def getter_tokens(text: str) -> list[str]:
    return GETTER_RE.findall(text or "")


def bracket_tokens(text: str) -> list[str]:
    return BRACKET_TOKEN_RE.findall(text or "")


def getter_role(text: str) -> str:
    if ES_TOKEN_RE.search(text):
        return "gender_es_helper"
    if SELECT_RE.search(text):
        return "select_mixed_getter"
    if any(marker in text for marker in ("GetFaith", "Faith.")):
        return "faith_getter"
    if any(marker in text for marker in ("GetCulture", "Culture.")):
        return "culture_getter"
    if any(marker in text for marker in ("GetPrimaryTitle", "GetTitle", "Title.")):
        return "title_or_realm_getter"
    if any(marker in text for marker in ("GetShortUIName", "GetFirstName", "GetTitledFirstName", "GetName")):
        return "character_or_name_getter"
    if any(marker in text for marker in ("GetSheHe", "GetHerHis", "GetHerHim", "GetWomanMan", "GetWifeHusband")):
        return "pronoun_or_relation_getter"
    if ".Custom(" in text or "Custom('" in text or 'Custom("' in text:
        return "custom_getter"
    if "ScriptValue" in text:
        return "script_value_getter"
    if "Concept(" in text:
        return "concept_getter"
    if SCOPE_RE.search(text):
        return "scope_getter"
    return "generic_getter"


def density_bucket(text: str) -> str:
    getter_count = len(getter_tokens(text))
    bracket_count = len(bracket_tokens(text))
    if has_newline(text):
        return "multiline_getter"
    if getter_count <= 1 and bracket_count <= 2 and not ES_TOKEN_RE.search(text):
        return "single_getter_light"
    if getter_count <= 2 and bracket_count <= 4:
        return "medium_getter"
    return "dense_getter"


def operational_bucket(text: str) -> str:
    density = density_bucket(text)
    role = getter_role(text)
    if density == "single_getter_light" and role in {
        "character_or_name_getter",
        "faith_getter",
        "culture_getter",
        "title_or_realm_getter",
        "pronoun_or_relation_getter",
        "custom_getter",
        "generic_getter",
    }:
        return "review_single_getter_policy_candidate"
    if density == "medium_getter" and role in {"character_or_name_getter", "title_or_realm_getter", "faith_getter", "culture_getter"}:
        return "review_medium_getter_route_candidate"
    if density == "multiline_getter":
        return "hold_multiline_getter_parser_later"
    if role in {"gender_es_helper", "select_mixed_getter"}:
        return "hold_mixed_gender_or_select_getter"
    return "hold_dense_or_complex_getter"


def compact(row: dict[str, Any], structural_row: dict[str, Any], current_run_id: int) -> dict[str, Any]:
    text = text_for(row)
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
        "structural_family": structural_row.get("structural_family"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "getter_role": getter_role(text),
        "density_bucket": density_bucket(text),
        "operational_bucket": operational_bucket(text),
        "getter_count": len(getter_tokens(text)),
        "bracket_token_count": len(bracket_tokens(text)),
        "has_newline": has_newline(text),
        "has_es_token": bool(ES_TOKEN_RE.search(text)),
        "has_select": bool(SELECT_RE.search(text)),
        "es_token_types": sorted(set(ES_TOKEN_RE.findall(text))),
        "sample_getter_tokens": getter_tokens(text)[:10],
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(
    architecture_packet_jsonl: Path,
    structural_jsonl: Path,
    current_run_id: int,
    expected_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    structural_rows = {
        int(row["segment_id"]): row
        for row in read_jsonl(structural_jsonl)
        if row.get("token_surface") == "dynamic_getter"
    }
    arch_rows = {int(row["segment_id"]): row for row in read_jsonl(architecture_packet_jsonl)}
    records = [
        compact(arch_rows[segment_id], structural_rows[segment_id], current_run_id)
        for segment_id in sorted(structural_rows)
        if segment_id in arch_rows
    ]
    if len(records) != expected_count:
        raise SystemExit(f"dynamic_getter residual count guard failed: {len(records)}")

    role_counts = Counter(row["getter_role"] for row in records)
    density_counts = Counter(row["density_bucket"] for row in records)
    operational_counts = Counter(row["operational_bucket"] for row in records)
    family_counts = Counter(row["structural_family"] for row in records)
    source_counts = Counter(row["confirmation_source"] for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["operational_bucket"]]) < SAMPLE_LIMIT:
            samples[row["operational_bucket"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_dynamic_getter_residual_diagnostic",
        "architecture_packet_jsonl": str(architecture_packet_jsonl),
        "structural_jsonl": str(structural_jsonl),
        "current_run_id": current_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "getter_role_counts": dict(role_counts.most_common()),
        "density_bucket_counts": dict(density_counts.most_common()),
        "operational_bucket_counts": dict(operational_counts.most_common()),
        "structural_family_counts": dict(family_counts.most_common()),
        "confirmation_source_counts": dict(source_counts.most_common(40)),
        "samples_by_operational_bucket": samples,
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
            "Review the single_getter_light and medium_getter route candidates first. Keep multiline, ES-helper/select-mixed and dense getter rows in hold/parser-later."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_dynamic_getter_residual_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 dynamic getter residual diagnostic",
        f"record_count={summary['record_count']}",
        "",
        "Density buckets:",
    ]
    for key, count in summary["density_bucket_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Operational buckets:"])
    for key, count in summary["operational_bucket_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Getter roles:"])
    for key, count in summary["getter_role_counts"].items():
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
