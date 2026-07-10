from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_policy_design_current_subtriage_v1"
TARGET_LANE = "semantic_review_policy_design_candidate"
SAMPLE_PER_GROUP = 6


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


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get("policy_lane") == TARGET_LANE:
                    rows.append(row)
    return rows


def classify_group(surface: str, risk: str, count: int) -> tuple[str, str]:
    if risk == "low_plain_text" and surface in {"general_semantic_prose", "accolade_knight_glory", "activity_contract_event"}:
        return "policy_review_best_first", "plain semantic prose with low token risk"
    if risk == "medium_dynamic_light" and surface in {"accolade_knight_glory", "activity_contract_event"}:
        return "guarded_policy_review_possible", "light CK3 token surface; validate token-preserving closure rule"
    if risk == "medium_dynamic_light" and surface == "general_semantic_prose":
        return "human_sample_before_policy", "plain prose with light dynamic token; sample meaning before rule"
    if risk == "medium_dynamic_dense":
        return "architecture_or_domain_specific_policy_first", "dense token surface is too broad for generic policy"
    if count < 10:
        return "low_volume_manual_or_hold", "low volume; not worth policy before larger groups"
    return "human_sample_before_policy", "mixed surface needs human/semantic sampling"


def build_summary(input_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("surface_bucket") or ""), str(row.get("risk_bucket") or ""))
        group = groups.setdefault(
            key,
            {
                "surface_bucket": key[0],
                "risk_bucket": key[1],
                "count": 0,
                "text_length_total": 0,
                "token_count_total": 0,
                "files": Counter(),
                "samples": [],
            },
        )
        group["count"] += 1
        group["text_length_total"] += int(row.get("text_length") or 0)
        group["token_count_total"] += int(row.get("token_count") or 0)
        group["files"][str(row.get("relative_path") or "")] += 1
        if len(group["samples"]) < SAMPLE_PER_GROUP:
            group["samples"].append(row)

    subgroups: list[dict[str, Any]] = []
    for group in groups.values():
        count = int(group["count"])
        decision, rationale = classify_group(group["surface_bucket"], group["risk_bucket"], count)
        avg_tokens = round(group["token_count_total"] / max(count, 1), 3)
        avg_text_length = round(group["text_length_total"] / max(count, 1), 1)
        subgroups.append(
            {
                "surface_bucket": group["surface_bucket"],
                "risk_bucket": group["risk_bucket"],
                "count": count,
                "decision": decision,
                "rationale": rationale,
                "avg_token_count": avg_tokens,
                "avg_text_length": avg_text_length,
                "top_files": [{"key": key, "count": value} for key, value in group["files"].most_common(10)],
                "samples": group["samples"],
            }
        )
    decision_counts = Counter(row["decision"] for row in subgroups for _ in range(int(row["count"])))
    subgroups.sort(
        key=lambda row: (
            row["decision"] != "policy_review_best_first",
            row["decision"] != "guarded_policy_review_possible",
            -int(row["count"]),
        )
    )
    recommended = subgroups[0] if subgroups else None
    architecture_needed = bool(
        recommended and recommended["decision"] == "architecture_or_domain_specific_policy_first"
    )
    next_action = (
        "prepare_readonly_policy_review_for_best_subgroup"
        if recommended and not architecture_needed
        else "prepare_architecture_prompt_for_dense_dynamic_subgroup"
        if recommended
        else "hold_no_rows"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "target_lane": TARGET_LANE,
        "rows_reviewed": len(rows),
        "subgroup_count": len(subgroups),
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "subgroups": subgroups,
        "recommended_subgroup": recommended,
        "architecture_needed_before_next_step": architecture_needed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "discovery_recommended_now": False,
        "retarget_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_policy_design_current_subtriage"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for subgroup in summary["subgroups"]:
            handle.write(json.dumps(subgroup, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recommended = summary["recommended_subgroup"] or {}
    lines = [
        "semantic policy design current subtriage",
        f"source={SOURCE}",
        f"input_jsonl={summary['input_jsonl']}",
        f"target_lane={summary['target_lane']}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"subgroup_count={summary['subgroup_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "top_subgroups:"])
    for subgroup in summary["subgroups"][:12]:
        lines.append(
            "- "
            f"{subgroup['count']} | {subgroup['surface_bucket']} | {subgroup['risk_bucket']} | "
            f"{subgroup['decision']} | avg_tokens={subgroup['avg_token_count']}"
        )
    lines.extend(["", "recommended_subgroup:"])
    if recommended:
        lines.extend(
            [
                f"- surface_bucket={recommended['surface_bucket']}",
                f"- risk_bucket={recommended['risk_bucket']}",
                f"- count={recommended['count']}",
                f"- decision={recommended['decision']}",
                f"- rationale={recommended['rationale']}",
            ]
        )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
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
    rows = read_rows(input_path)
    summary = build_summary(input_path, rows)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    recommended = summary["recommended_subgroup"] or {}
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_reviewed={summary['rows_reviewed']}")
    print(f"subgroup_count={summary['subgroup_count']}")
    print(f"recommended_surface_bucket={recommended.get('surface_bucket')}")
    print(f"recommended_risk_bucket={recommended.get('risk_bucket')}")
    print(f"recommended_count={recommended.get('count')}")
    print(f"recommended_decision={recommended.get('decision')}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
