from __future__ import annotations

import json
import re
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_gender_suffix_review_v1"
ARCHITECTURE_PACKET_JSONL = Path("reports/20260701_155604_125890_domain_policy_vote_candidate_closure_debt_architecture_packet_512_532.jsonl")
STRUCTURAL_JSONL = Path("reports/20260701_155931_140243_domain_policy_vote_candidate_phase3_remaining_structural_diagnostic.jsonl")
CURRENT_RUN_ID = 532
EXPECTED_COUNT = 376
SAMPLE_LIMIT = 8
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"](ES_[A-Z_]+)['\"]\)\]")


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
    parser = argparse.ArgumentParser(description="Read-only phase 3 gender suffix review.")
    parser.add_argument("--architecture-packet-jsonl", type=Path, default=ARCHITECTURE_PACKET_JSONL)
    parser.add_argument("--structural-jsonl", type=Path, default=STRUCTURAL_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def es_tokens(text: str) -> list[str]:
    return ES_TOKEN_RE.findall(text or "")


def token_context(text: str) -> str:
    tokens = es_tokens(text)
    unique = sorted(set(tokens))
    if not unique:
        return "no_es_token_detected"
    if len(unique) > 1:
        return "multiple_es_token_types"
    token = unique[0]
    if token == "ES_OA":
        return "es_oa_adjective_or_participle"
    if token == "ES_XA":
        return "es_xa_article_or_noun_gender"
    if token == "ES_A":
        return "es_a_suffix"
    if token == "ES_O":
        return "es_o_suffix"
    return f"other_{token.lower()}"


def surrounding_pattern(text: str) -> str:
    if "Select_CString" in text or "SelectLocalization" in text:
        return "has_select_localization"
    if "$EFFECT_LIST_BULLET$" in text:
        return "effect_list"
    if "\\n" in text or "\n" in text:
        return "contains_newline"
    if re.search(r"\bquerid\[[^\]]+Custom\(['\"]ES_OA['\"]\)\]", text or ""):
        return "querido_a_pattern"
    if re.search(r"\bum\[[^\]]+Custom\(['\"]ES_XA['\"]\)\]", text or ""):
        return "um_uma_pattern"
    if re.search(r"\b[ao]mad\[[^\]]+Custom\(['\"]ES_OA['\"]\)\]", text or ""):
        return "amado_a_pattern"
    if re.search(r"\b[ée] [^.\n]{0,30}\[[^\]]+Custom\(['\"]ES_OA['\"]\)\]", text or ""):
        return "copula_adjective_suffix"
    return "generic_gender_suffix_context"


def parser_readiness(row: dict[str, Any], text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if row.get("token_surface") != "dynamic_getter":
        reasons.append("not_dynamic_getter")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_count_not_0")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_count_not_0")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_0")
    if row.get("to_final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("final_state_not_reopen_auto_confirmed_autofix")
    if not es_tokens(text):
        reasons.append("missing_es_token")
    if "Select_CString" in text or "SelectLocalization" in text:
        reasons.append("select_localization_mixed")
    if "\\n" in text or "\n" in text:
        reasons.append("multiline_mixed")
    if reasons:
        return "hold_or_parser_later", reasons
    if token_context(text) == "multiple_es_token_types":
        return "needs_parser_policy_review", ["multiple_es_token_types"]
    return "candidate_for_es_suffix_lifecycle_policy", []


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
        if row.get("structural_family") == "dynamic_getter_gender_suffix"
    }
    arch_rows = {int(row["segment_id"]): row for row in read_jsonl(architecture_packet_jsonl)}
    records: list[dict[str, Any]] = []
    for segment_id in sorted(ids):
        row = arch_rows[segment_id]
        text = str(row.get("confirmed_text") or row.get("output_text") or "")
        readiness, block_reasons = parser_readiness(row, text)
        record = {
            "source": SOURCE,
            "current_run_id": current_run_id,
            "segment_id": segment_id,
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
            "es_token_types": sorted(set(es_tokens(text))),
            "es_token_count": len(es_tokens(text)),
            "es_token_context": token_context(text),
            "surrounding_pattern": surrounding_pattern(text),
            "parser_readiness": readiness,
            "parser_block_reasons": block_reasons,
            "output_text": row.get("output_text"),
            "confirmed_text": row.get("confirmed_text"),
            "candidate_generation_count": 0,
            "apply_count": 0,
            "lifecycle_count": 0,
        }
        records.append(record)

    readiness_counts = Counter(row["parser_readiness"] for row in records)
    token_context_counts = Counter(row["es_token_context"] for row in records)
    surrounding_counts = Counter(row["surrounding_pattern"] for row in records)
    source_counts = Counter(row["confirmation_source"] for row in records)
    label_counts = Counter(row["confirmation_label"] for row in records)
    es_token_type_counts = Counter(token for row in records for token in row["es_token_types"])
    block_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        reasons = row["parser_block_reasons"] or ["ready"]
        for reason in reasons:
            block_counts[reason] += 1
            if len(samples[reason]) < SAMPLE_LIMIT:
                samples[reason].append(row)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_gender_suffix_review",
        "architecture_packet_jsonl": str(architecture_packet_jsonl),
        "structural_jsonl": str(structural_jsonl),
        "current_run_id": current_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "parser_readiness_counts": dict(sorted(readiness_counts.items())),
        "es_token_context_counts": dict(sorted(token_context_counts.items())),
        "es_token_type_counts": dict(sorted(es_token_type_counts.items())),
        "surrounding_pattern_counts": dict(sorted(surrounding_counts.items())),
        "confirmation_source_counts": dict(source_counts.most_common(40)),
        "confirmation_label_counts": dict(label_counts.most_common(50)),
        "parser_block_reason_counts": dict(block_counts.most_common()),
        "samples_by_reason": samples,
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
            "Do not materialize yet. If architecture agrees, create a very narrow ES suffix lifecycle policy only for candidate_for_es_suffix_lifecycle_policy rows, keeping multiline, SelectLocalization and mixed token-type rows out."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_gender_suffix_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 gender suffix review",
        f"record_count={summary['record_count']}",
        "",
        "Parser readiness:",
    ]
    for key, count in summary["parser_readiness_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "ES token contexts:"])
    for key, count in summary["es_token_context_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Surrounding patterns:"])
    for key, count in summary["surrounding_pattern_counts"].items():
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
        raise SystemExit(f"gender suffix count guard failed: {summary['record_count']}")
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
