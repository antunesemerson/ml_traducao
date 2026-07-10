from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_multiline_terminal_hold_diagnostic_v1"
STRUCTURAL_JSONL = Path("reports/20260701_170354_834884_domain_policy_vote_candidate_phase3_remaining_structural_diagnostic.jsonl")
GETTER_DIAGNOSTIC_JSONL = Path("reports/20260701_183708_568780_domain_policy_vote_candidate_phase3_dynamic_getter_residual_diagnostic.jsonl")
EXPECTED_COUNT = 1064
SAMPLE_LIMIT = 8

SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString", re.IGNORECASE)
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"]ES_[A-Za-z0-9_]+['\"]\)\]")
EFFECT_LIST_RE = re.compile(r"\$EFFECT_LIST_BULLET\$|\$EFFECT\$|\$BULLET\$", re.IGNORECASE)
GUI_RE = re.compile(r"TOOLTIP|WINDOW|VIEW|CONFIRM|BUTTON|TITLE_|SORT|EXECUTE", re.IGNORECASE)
NARRATIVE_RE = re.compile(r"\.desc(?:\.|$)|_desc$|events?\.", re.IGNORECASE)
QUOTE_RE = re.compile(r"\\\"|\"|«|»")
GETTER_RE = re.compile(r"\[[^\]]*(?:\bGet[A-Za-z0-9_]*|\.Custom\(|ScriptValue|Concept\(|ROOT\.|FROM\.|SCOPE\.|TARGET\.)[^\]]*\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only terminal hold diagnostic for phase 3 multiline residuals.")
    parser.add_argument("--structural-jsonl", type=Path, default=STRUCTURAL_JSONL)
    parser.add_argument("--getter-diagnostic-jsonl", type=Path, default=GETTER_DIAGNOSTIC_JSONL)
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


def line_count(text: str) -> int:
    if "\\n" in text:
        return text.count("\\n") + 1
    return text.count("\n") + 1


def terminal_family(row: dict[str, Any]) -> str:
    text = text_for(row)
    source_key = str(row.get("source_key") or "")
    if EFFECT_LIST_RE.search(text):
        return "terminal_hold_effect_list_or_bullet_multiline"
    if SELECT_RE.search(text):
        return "terminal_hold_select_multiline"
    if ES_TOKEN_RE.search(text):
        return "terminal_hold_gender_es_helper_multiline"
    if int(row.get("getter_count") or len(GETTER_RE.findall(text))) >= 3:
        return "terminal_hold_dense_getter_multiline"
    if GUI_RE.search(source_key):
        return "terminal_hold_gui_tooltip_multiline"
    if NARRATIVE_RE.search(source_key) or QUOTE_RE.search(text):
        return "terminal_hold_narrative_multiline"
    return "terminal_hold_generic_multiline"


def parser_recommendation(family: str) -> str:
    if family == "terminal_hold_effect_list_or_bullet_multiline":
        return "parser_later_effect_list_structure"
    if family == "terminal_hold_select_multiline":
        return "parser_later_select_multiline"
    if family == "terminal_hold_gender_es_helper_multiline":
        return "parser_later_gender_es_multiline"
    if family == "terminal_hold_dense_getter_multiline":
        return "parser_later_dense_getter_multiline"
    if family == "terminal_hold_gui_tooltip_multiline":
        return "terminal_hold_gui_multiline"
    if family == "terminal_hold_narrative_multiline":
        return "terminal_hold_narrative_multiline"
    return "terminal_hold_generic_multiline"


def compact(row: dict[str, Any], source_bucket: str) -> dict[str, Any]:
    text = text_for(row)
    family = terminal_family(row)
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_bucket": source_bucket,
        "terminal_hold_family": family,
        "parser_recommendation": parser_recommendation(family),
        "token_surface": row.get("token_surface"),
        "structural_family": row.get("structural_family"),
        "getter_role": row.get("getter_role"),
        "density_bucket": row.get("density_bucket"),
        "line_count": line_count(text),
        "getter_count": int(row.get("getter_count") or len(GETTER_RE.findall(text))),
        "has_select": bool(row.get("has_select")) or bool(SELECT_RE.search(text)),
        "has_es_token": bool(row.get("has_es_token")) or bool(ES_TOKEN_RE.search(text)),
        "has_effect_list": bool(EFFECT_LIST_RE.search(text)),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(structural_jsonl: Path, getter_diagnostic_jsonl: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    structural_rows = read_jsonl(structural_jsonl)
    getter_rows = read_jsonl(getter_diagnostic_jsonl)
    non_getter_multiline = [
        compact(row, "phase3_multiline_non_getter")
        for row in structural_rows
        if row.get("token_surface") == "multiline"
    ]
    getter_multiline = [
        compact(row, "dynamic_getter_multiline")
        for row in getter_rows
        if row.get("operational_bucket") == "hold_multiline_getter_parser_later"
    ]
    records = sorted(non_getter_multiline + getter_multiline, key=lambda row: (row["terminal_hold_family"], row["segment_id"]))
    if len(records) != expected_count:
        raise SystemExit(f"multiline terminal hold count guard failed: {len(records)}")

    family_counts = Counter(row["terminal_hold_family"] for row in records)
    parser_counts = Counter(row["parser_recommendation"] for row in records)
    source_bucket_counts = Counter(row["source_bucket"] for row in records)
    surface_counts = Counter(row["token_surface"] for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["terminal_hold_family"]]) < SAMPLE_LIMIT:
            samples[row["terminal_hold_family"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_multiline_terminal_hold_diagnostic",
        "structural_jsonl": str(structural_jsonl),
        "getter_diagnostic_jsonl": str(getter_diagnostic_jsonl),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "terminal_hold_family_counts": dict(family_counts.most_common()),
        "parser_recommendation_counts": dict(parser_counts.most_common()),
        "source_bucket_counts": dict(source_bucket_counts.most_common()),
        "token_surface_counts": dict(surface_counts.most_common()),
        "samples_by_terminal_hold_family": samples,
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
            "Catalog these multiline rows as terminal hold/parser-later; do not lifecycle-close them. "
            "Only return to architecture for a specific parser family if one family becomes strategically important."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_multiline_terminal_hold_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 multiline terminal hold diagnostic",
        f"record_count={summary['record_count']}",
        "",
        "Terminal hold families:",
    ]
    for key, count in summary["terminal_hold_family_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Parser recommendations:"])
    for key, count in summary["parser_recommendation_counts"].items():
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
    records, summary = build(args.structural_jsonl, args.getter_diagnostic_jsonl, args.expected_count)
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
