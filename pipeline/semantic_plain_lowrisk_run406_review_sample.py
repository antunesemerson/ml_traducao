from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_plain_lowrisk_current_review_sample_v1"
TARGET_LANE = "semantic_review_policy_design_candidate"
TARGET_SURFACE = "general_semantic_prose"
TARGET_RISK = "low_plain_text"
SAMPLE_LIMIT = 50

SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:adem[aá]s|aunque|caballero|cielos|coste|cualquier|elige|eres|hacerte|hacerle|"
    r"maravilloso|mientras|ning[uú]n|puede|pueden|quieres|siguiente|tambi[eé]n|vuestro|"
    r"vuestra|vuestras|vuestros|pieza|extraño|extraña)\b",
    re.IGNORECASE,
)
PTBR_FLUENCY_RE = re.compile(
    r"\b(?:levies|in-doors|parentela|Rumor diz|meio que|qualquer pessoa que aspire|"
    r"em um lugar fechado|tomar partido por|fica do lado)\b",
    re.IGNORECASE,
)
CONTEXT_RE = re.compile(
    r"\b(?:este|esta|desse|dessa|dele|dela|seu|sua|suas|seus|personagem|estrangeiro|"
    r"conquistador|cavaleiro|guarda)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_input_jsonl() -> Path:
    matches = sorted(
        [
            *reports_dir().glob("*_semantic_review_router_pending_deep_diagnostic.jsonl"),
            *reports_dir().glob("*_semantic_review_router_run406_sublane_diagnostic.jsonl"),
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing semantic_review_router_run406_sublane_diagnostic jsonl")
    return matches[0]


def read_target_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if (
                row.get("policy_lane") == TARGET_LANE
                and row.get("surface_bucket") == TARGET_SURFACE
                and row.get("risk_bucket") == TARGET_RISK
            ):
                rows.append(row)
    return rows


def source_domain(row: dict[str, Any]) -> str:
    path = str(row.get("relative_path") or "")
    key = str(row.get("source_key") or "")
    if path.startswith("traits") or key.startswith("trait"):
        return "traits"
    if "historical_characters" in path:
        return "historical_characters"
    if "activities/" in path or "event_localization" in path:
        return "event_prose"
    if "artifacts" in path:
        return "artifacts"
    if "religion" in path:
        return "religion"
    if "culture" in path:
        return "culture"
    return "general"


def classify(row: dict[str, Any]) -> tuple[str, str, bool]:
    current = str(row.get("current_output_text") or "")
    english = str(row.get("english_text") or "")
    spanish = str(row.get("spanish_text") or "")
    haystack = " ".join([current, english, spanish])
    if SPANISH_RESIDUE_RE.search(current):
        return "semantic_error_or_spanish_residue", "possible Spanish residue or unnatural lexical carryover in PT-BR", True
    if PTBR_FLUENCY_RE.search(current):
        return "needs_ptbr_fluency", "PT-BR wording looks unnatural or overly literal", False
    if CONTEXT_RE.search(current) and len(current) > 120:
        return "needs_more_context", "pronoun/domain wording may depend on event speaker or target", False
    if len(current) <= 130 and not CONTEXT_RE.search(haystack):
        return "policy_closure_candidate", "short plain prose with no visible context dependency", False
    return "already_ok_or_policy_candidate", "plain prose appears structurally safe; needs human semantic spot-check", False


def choose_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_domain.setdefault(source_domain(row), []).append(row)
    sample: list[dict[str, Any]] = []
    domain_order = sorted(by_domain, key=lambda domain: (-len(by_domain[domain]), domain))
    while len(sample) < SAMPLE_LIMIT:
        added = False
        for domain in domain_order:
            bucket = by_domain[domain]
            if bucket:
                sample.append(bucket.pop(0))
                added = True
                if len(sample) >= SAMPLE_LIMIT:
                    break
        if not added:
            break
    return sample


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    initial_classification, rationale, false_safe_risk = classify(row)
    return {
        "segment_id": row.get("segment_id"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "source_domain": source_domain(row),
        "initial_classification": initial_classification,
        "classification_rationale": rationale,
        "false_safe_risk": false_safe_risk,
        "human_review_required": initial_classification
        in {"semantic_error_or_spanish_residue", "needs_ptbr_fluency", "needs_more_context"},
        "policy_closure_candidate": initial_classification == "policy_closure_candidate",
        "current_output_text": row.get("current_output_text"),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "confirmed_matches_output": row.get("confirmed_matches_output"),
    }


def build_summary(input_path: Path, rows: list[dict[str, Any]], sample: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = [enrich(row) for row in sample]
    class_counts = Counter(row["initial_classification"] for row in enriched)
    domain_counts_all = Counter(source_domain(row) for row in rows)
    domain_counts_sample = Counter(row["source_domain"] for row in enriched)
    false_safe_risk_count = sum(1 for row in enriched if row["false_safe_risk"])
    policy_closure_candidates = sum(1 for row in enriched if row["policy_closure_candidate"])
    human_review_required = sum(1 for row in enriched if row["human_review_required"])
    next_action = (
        "human_review_sample_before_policy"
        if false_safe_risk_count or human_review_required > policy_closure_candidates
        else "prepare_policy_closure_dry_run_design_readonly"
        if policy_closure_candidates >= 10
        else "hold_insufficient_policy_signal"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "target_lane": TARGET_LANE,
        "target_surface": TARGET_SURFACE,
        "target_risk": TARGET_RISK,
        "population_count": len(rows),
        "sample_count": len(enriched),
        "classification_counts": [{"key": key, "count": value} for key, value in class_counts.most_common()],
        "population_domain_counts": [{"key": key, "count": value} for key, value in domain_counts_all.most_common()],
        "sample_domain_counts": [{"key": key, "count": value} for key, value in domain_counts_sample.most_common()],
        "false_safe_risk_count": false_safe_risk_count,
        "policy_closure_candidate_count": policy_closure_candidates,
        "human_review_required_count": human_review_required,
        "sample": enriched,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "discovery_recommended_now": False,
        "retarget_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_plain_lowrisk_current_review_sample"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["sample"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic plain lowrisk current review sample",
        f"source={SOURCE}",
        f"input_jsonl={summary['input_jsonl']}",
        f"population_count={summary['population_count']}",
        f"sample_count={summary['sample_count']}",
        "",
        "classification_counts:",
    ]
    for item in summary["classification_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "sample_domain_counts:"])
    for item in summary["sample_domain_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"false_safe_risk_count={summary['false_safe_risk_count']}",
            f"policy_closure_candidate_count={summary['policy_closure_candidate_count']}",
            f"human_review_required_count={summary['human_review_required_count']}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_input_jsonl()
    rows = read_target_rows(input_path)
    sample = choose_sample(rows)
    summary = build_summary(input_path, rows, sample)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"population_count={summary['population_count']}")
    print(f"sample_count={summary['sample_count']}")
    print(f"false_safe_risk_count={summary['false_safe_risk_count']}")
    print(f"policy_closure_candidate_count={summary['policy_closure_candidate_count']}")
    print(f"human_review_required_count={summary['human_review_required_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
