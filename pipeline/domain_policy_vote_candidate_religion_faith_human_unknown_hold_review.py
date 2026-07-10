from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_human_unknown_hold_review_v1"
CONSOLIDATED_JSONL_PATH = Path("reports/20260630_115814_048643_domain_policy_vote_candidate_religion_faith_post_pantheon_policy_consolidated_diagnostic.jsonl")
CONSOLIDATED_SUMMARY_PATH = Path("reports/20260630_115814_048643_domain_policy_vote_candidate_religion_faith_post_pantheon_policy_consolidated_diagnostic_summary.json")
EXPECTED_SEGMENT_STATE_RUN_ID = 507
EXPECTED_FAMILY = "human_only_or_unknown_hold"
EXPECTED_COUNT = 16

SPANISH_RESIDUE_RE = re.compile(r"Ã|Â|#low El conjunto|\\b(?:los|las|que|sin contar|disponibles|reivindicarla|restaurarla)\\b", re.I)
TITLE_TAX_LEVY_RE = re.compile(r"taxes|levies|holdings|title|vassals|sort_tt_total", re.I)
LONG_NARRATIVE_RE = re.compile(r"desc$|_desc|decision_desc|events|\\.desc|\\n|\\\\n", re.I)
BUILDING_PLACE_RE = re.compile(r"fortress|building|barony|GetTitleByKey|ruins|fortaleza|paisagem", re.I)
DENSE_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Get[A-Za-z0-9_]+\(")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def validate_inputs(summary: dict, rows: list[dict]) -> list[dict]:
    if summary.get("mode") != "read_only_consolidated_diagnostic":
        raise SystemExit("summary mode guard failed")
    if int(summary.get("segment_state_run_id") or 0) != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if summary.get("next_recommended_family") != EXPECTED_FAMILY:
        raise SystemExit("next recommended family guard failed")
    target = [row for row in rows if row.get("hold_family") == EXPECTED_FAMILY]
    if len(target) != EXPECTED_COUNT:
        raise SystemExit(f"target count guard failed: {len(target)} expected {EXPECTED_COUNT}")
    return target


def classify(row: dict) -> tuple[str, str, list[str], str]:
    blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "current_output_text", "english_text"))
    tags = []
    for label, pattern in (
        ("spanish_residue_or_mojibake", SPANISH_RESIDUE_RE),
        ("title_tax_levy_tooltip", TITLE_TAX_LEVY_RE),
        ("long_narrative", LONG_NARRATIVE_RE),
        ("building_or_place_context", BUILDING_PLACE_RE),
        ("dense_token_surface", DENSE_TOKEN_RE),
    ):
        if pattern.search(blob):
            tags.append(label)
    if "title_tax_levy_tooltip" in tags and "spanish_residue_or_mojibake" in tags:
        return ("spanish_residue_title_tax_levy_tooltip", "small_human_review_possible", tags, "Spanish residue in title/tax/levy tooltip looks human-reviewable.")
    if "building_or_place_context" in tags and "long_narrative" in tags:
        return ("building_place_long_narrative", "hold_context_or_human_later", tags, "Place/building narrative is context-heavy.")
    if "spanish_residue_or_mojibake" in tags:
        return ("spanish_residue_general", "small_human_review_possible", tags, "General Spanish/mojibake residue may be human-reviewed.")
    if "dense_token_surface" in tags:
        return ("dense_unknown_token_context", "hold_or_architecture_later", tags, "Dense token unknown needs architecture/hold.")
    return ("true_human_unknown_context", "hold_human_context", tags, "Unknown context remains human-only.")


def main() -> None:
    summary = read_json(CONSOLIDATED_SUMMARY_PATH)
    rows = validate_inputs(summary, read_jsonl(CONSOLIDATED_JSONL_PATH))
    reviewed = []
    for row in rows:
        subtype, action, tags, reason = classify(row)
        reviewed.append({
            "record_type": "human_unknown_hold_review",
            "source": SOURCE,
            "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
            "segment_id": int(row["segment_id"]),
            "source_key": row.get("source_key"),
            "relative_path": row.get("relative_path"),
            "surface_bucket": row.get("surface_bucket"),
            "hold_family": row.get("hold_family"),
            "risk_bucket": row.get("risk_bucket"),
            "current_output_text": row.get("current_output_text"),
            "english_text": row.get("english_text"),
            "operational_subtype": subtype,
            "recommended_action": action,
            "review_reason": reason,
            "review_tags": tags,
            "candidate_generation_allowed": False,
            "auto_apply_allowed": False,
            "lifecycle_allowed": False,
            "production_release_allowed": False,
        })
    subtype_counts = Counter(row["operational_subtype"] for row in reviewed)
    action_counts = Counter(row["recommended_action"] for row in reviewed)
    risk_counts = Counter(row["risk_bucket"] for row in reviewed)
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_human_unknown_hold_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    result = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_review",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "hold_family": EXPECTED_FAMILY,
        "review_count": len(reviewed),
        "expected_count": EXPECTED_COUNT,
        "count_matches_expected": len(reviewed) == EXPECTED_COUNT,
        "operational_subtype_counts": dict(subtype_counts),
        "recommended_action_counts": dict(action_counts),
        "risk_bucket_counts": dict(risk_counts),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Do not register broad unknown policy; create a small human packet only for Spanish-residue rows and keep dense/narrative rows in hold.",
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join([
            "domain_policy_vote_candidate religion_faith_doctrine human_only_or_unknown_hold review",
            "",
            f"review_count: {len(reviewed)}",
            "",
            "operational_subtype_counts:",
            *[f"- {count} | {key}" for key, count in subtype_counts.most_common()],
            "",
            "recommended_action_counts:",
            *[f"- {count} | {key}" for key, count in action_counts.most_common()],
        ]) + "\n",
        encoding="utf-8",
    )
    print(f"summary={summary_path}")
    print(f"review_count={len(reviewed)}")
    print(f"operational_subtype_counts={json.dumps(dict(subtype_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"recommended_action_counts={json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
