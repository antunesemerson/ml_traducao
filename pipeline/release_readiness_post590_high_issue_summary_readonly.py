from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_post590_high_issue_summary_readonly_v1"
DEFAULT_READINESS_SUMMARY = Path("reports/20260703_221837_969667_release_readiness_post544_diagnostic_summary.json")
DEFAULT_READINESS_JSONL = Path("reports/20260703_221837_969667_release_readiness_post544_diagnostic.jsonl")
DEFAULT_DELTAS = [
    Path("reports/20260703_235519_199204_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json"),
    Path("reports/20260704_003039_394750_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json"),
    Path("reports/20260704_010154_903965_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json"),
    Path("reports/20260704_011557_637521_release_readiness_ui_tooltips_batch1_segment_state_delta_summary.json"),
]
DEFAULT_CLOSURES = [
    Path("reports/20260703_204712_379624_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json"),
    Path("reports/20260703_212505_604250_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json"),
    Path("reports/20260703_215556_559684_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json"),
    Path("reports/20260703_221019_302650_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_apply_summary.json"),
]

PROCESSED_OR_HOLD_IDS = {
    33718, 37839, 42196, 48500, 54856, 67282, 67319, 76756, 99715, 105133, 105383, 114115,
    126472, 138588, 7486, 41767, 41770, 74879, 78382, 78398, 79242, 101019, 112751, 115034,
    125899, 246153,
    77508, 30353, 32275, 32460, 32724, 32951, 33009, 34000, 34575, 34576, 36405, 36599, 42215,
    42294, 42312, 42468, 42665, 43126, 43127, 45147, 45357, 47386, 47758, 48006, 50923, 52456,
    55152, 55344, 58171, 58673,
    59502, 59516, 60793, 61005, 61042, 62598, 65377, 66438, 66439, 67236, 67277, 68282, 70297,
    71075, 76766, 77644, 77660, 78282, 78527, 79537, 98631, 99568, 100707, 101265, 101424,
    101927, 104534, 105245, 105326, 105327,
    65282, 130189, 30464, 54888, 68315, 76377, 99428, 100383, 104983, 112620, 114261, 114264,
    114265, 114271, 114285, 114297, 121588, 121728,
    120831, 126552, 127174,
}

SPANISH_RE = re.compile(r"\b(el|la|los|las|un|una|mayor|menor|este|esta|se hace|se roba|con creces|anta[ñn]o)\b", re.I)
PT_READY_RE = re.compile(r"\b(ção|ções|você|vocês|muito|amor|coroa|frente|vozes|eras|vingança|desprezo)\b", re.I)
DYNAMIC_RE = re.compile(r"Select_CString|SelectLocalization|\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\.")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--readiness-summary", type=Path, default=DEFAULT_READINESS_SUMMARY)
    parser.add_argument("--readiness-jsonl", type=Path, default=DEFAULT_READINESS_JSONL)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def high_issue_class(row: dict[str, Any]) -> str:
    text = str(row.get("output_text") or "")
    issue = f"{row.get('issue_families') or ''} {row.get('issue_kinds') or ''}".lower()
    if row.get("token_surface") not in {"plain_text", "light_token"} or DYNAMIC_RE.search(text):
        return "parser_later_or_dynamic_excluded"
    if SPANISH_RE.search(text):
        return "corrected_text_needs_human_text"
    if int(row.get("confirmed_matches_output") or 0) == 1 and int(row.get("needs_output_apply") or 0) == 0:
        if "spanish_residue" in issue and PT_READY_RE.search(text):
            return "approve_already_ok_possible"
        return "approve_already_ok_possible"
    return "needs_context_or_parser_later"


def remaining_probe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        segment_id = int(row.get("segment_id") or 0)
        if segment_id in PROCESSED_OR_HOLD_IDS:
            continue
        if row.get("release_class") != "release_blocker":
            continue
        if row.get("visibility_group") != "narrative_events":
            continue
        if int(row.get("high_issue_count") or 0) <= 0:
            continue
        cls = high_issue_class(row)
        out.append(
            {
                "segment_id": segment_id,
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "token_surface": row.get("token_surface"),
                "classification": cls,
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "issue_families": row.get("issue_families") or "",
                "issue_kinds": row.get("issue_kinds") or "",
            }
        )
    return out


def main() -> None:
    args = parse_args()
    readiness = load_json(args.readiness_summary)
    rows = read_jsonl(args.readiness_jsonl)
    deltas = [load_json(path) for path in DEFAULT_DELTAS]
    closures = [load_json(path) for path in DEFAULT_CLOSURES]
    remaining = remaining_probe(rows)
    remaining_counts = Counter(row["classification"] for row in remaining)
    final_state_counts = Counter(row.get("final_state") for row in rows)
    closed_total = sum(int(delta["global_delta"]["closed_count"]) for delta in deltas)
    issues_closed_total = sum(int(closure.get("closed_issue_count") or 0) for closure in closures)
    approve_possible = remaining_counts.get("approve_already_ok_possible", 0)
    if approve_possible >= 10:
        rec = "Gerar validação read-only limite 30 para approve_already_ok_possible restante."
    elif remaining_counts.get("corrected_text_needs_human_text", 0) > 0:
        rec = "Migrar para preencher/revisar corrected_text em hold; approve_already_ok provável caiu abaixo do limiar."
    else:
        rec = "Parar ciclos high_issue_non_dynamic e preparar decisão de release/readiness."
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post590_release_readiness_summary",
        "pending_total": readiness.get("pending_global_count"),
        "reopen_auto_confirmed_autofix": final_state_counts.get("reopen_auto_confirmed_autofix", 0),
        "pending_apply_confirmed": final_state_counts.get("pending_apply_confirmed", 0),
        "needs_output_apply": readiness.get("needs_output_apply_count", 1),
        "release_blocker": readiness.get("release_blocker_count"),
        "review_before_release": readiness.get("review_before_release_count"),
        "known_non_blocking_hold": readiness.get("known_non_blocking_hold_count"),
        "parser_later": readiness.get("parser_later_count"),
        "high_issue_batches": {
            "closed_total": closed_total,
            "issues_closed_total": issues_closed_total,
            "release_blocker_reduced_total": closed_total,
            "batch_closed_counts": [int(delta["global_delta"]["closed_count"]) for delta in deltas],
            "batch_issue_closed_counts": [int(closure.get("closed_issue_count") or 0) for closure in closures],
        },
        "remaining_high_issue_non_dynamic_total": len(remaining),
        "remaining_high_issue_non_dynamic_counts": dict(remaining_counts.most_common()),
        "remaining_high_issue_non_dynamic_ready_ids": [row["segment_id"] for row in remaining if row["classification"] == "approve_already_ok_possible"][:40],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": rec,
    }
    base = reports_dir() / f"{stamp()}_release_readiness_post590_high_issue_summary_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in remaining), encoding="utf-8")
    md_path.write_text(
        "\n".join(
            [
                "# Release Readiness Post-590 Summary",
                "",
                f"- pending_total: {summary['pending_total']}",
                f"- release_blocker: {summary['release_blocker']}",
                f"- review_before_release: {summary['review_before_release']}",
                f"- known_non_blocking_hold: {summary['known_non_blocking_hold']}",
                f"- parser_later: {summary['parser_later']}",
                f"- needs_output_apply: {summary['needs_output_apply']}",
                "",
                f"- high_issue closed total: {closed_total}",
                f"- high_issue issues closed total: {issues_closed_total}",
                f"- remaining: {json.dumps(summary['remaining_high_issue_non_dynamic_counts'], ensure_ascii=False)}",
                "",
                summary["single_operational_recommendation"],
                "",
            ]
        ),
        encoding="utf-8",
    )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"pending_total={summary['pending_total']}")
    print(f"release_blocker={summary['release_blocker']}")
    print(f"remaining_high_issue_non_dynamic_counts={json.dumps(summary['remaining_high_issue_non_dynamic_counts'], ensure_ascii=False)}")
    print(f"single_operational_recommendation={summary['single_operational_recommendation']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
