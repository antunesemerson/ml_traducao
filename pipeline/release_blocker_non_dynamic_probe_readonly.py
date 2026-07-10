from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_blocker_non_dynamic_probe_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_182038_026776_release_readiness_post544_diagnostic.jsonl")
DEFAULT_RUN_ID = 585
EXCLUDED_SEGMENT_IDS = {120831, 126552, 127174}

DYNAMIC_RE = re.compile(r"Select_CString|SelectLocalization|LocalPlayerString|\.Get|\.Custom|SCOPE\.|ROOT\.|CHARACTER\.")
HIGH_COMPLEXITY_RE = re.compile(r"Concept\(|Glossary\(|GetScriptedGui|GetTrait|GetModifier|\[[^\]]{80,}\]")
SPANISH_RE = re.compile(
    r"\b(el|la|los|las|un|una|unos|unas|ese|esa|este|esta|mucho|ganando|aplicarlo|robarme|maravilloso|"
    r"nombrad[oa]?|preocupad[oa]?|encantad[oa]?|escabulliste|respondió|buf[oó]n|ladrona|ladr[oó]n)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only probe for non-dynamic release blockers.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--limit", type=int, default=2000)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def release_group(row: dict[str, Any]) -> str:
    group = row.get("visibility_group") or ""
    path = str(row.get("relative_path") or "").lower()
    issues = str(row.get("issue_families") or "").lower()
    if group in {"narrative_events", "ui_tooltips_short_labels", "religion_faith_doctrine", "culture_tradition_innovation"}:
        return group
    if "nickname" in path or "nickname" in issues:
        return "nicknames"
    if any(marker in path for marker in ("scheme", "contract", "interaction")):
        return "interactions_schemes_contracts"
    return "other_visible_or_system"


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(str(row.get(key) or "") for key in ("source_text", "spanish_text", "output_text", "confirmed_text", "issue_kinds", "source_key"))


def excluded_reason(row: dict[str, Any]) -> str:
    segment_id = int(row.get("segment_id") or 0)
    if segment_id in EXCLUDED_SEGMENT_IDS:
        return "explicit_hold_segment"
    if row.get("release_class") != "release_blocker":
        return "not_release_blocker"
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        return "dynamic_or_non_plain_surface"
    text = text_blob(row)
    if DYNAMIC_RE.search(text):
        return "getter_select_conditional_overlap"
    if HIGH_COMPLEXITY_RE.search(text):
        return "high_complexity_token_literal"
    if "parser_later" in str(row.get("residual_diagnostic_bucket") or ""):
        return "parser_later_excluded"
    return ""


def classify(row: dict[str, Any]) -> str:
    if int(row.get("high_issue_count") or 0) > 0 or "high_issue" in str(row.get("issue_families") or ""):
        return "high_issue_non_dynamic"
    output = str(row.get("output_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    if row.get("spanish_residue_visible") or SPANISH_RE.search(output):
        return "spanish_residue_plain"
    issue_text = f"{row.get('issue_families') or ''} {row.get('issue_kinds') or ''}".lower()
    if "short_label" in issue_text or len(output) <= 90:
        if int(row.get("confirmed_matches_output") or 0) == 1 and int(row.get("needs_output_apply") or 0) == 0:
            return "approve_already_ok_ready"
        if confirmed and output != confirmed:
            return "corrected_text_ready"
        return "short_label/plain_fluency"
    if int(row.get("confirmed_matches_output") or 0) == 1 and int(row.get("needs_output_apply") or 0) == 0:
        return "approve_already_ok_ready"
    if confirmed and output != confirmed:
        return "corrected_text_ready"
    return "needs_more_context"


def roi_score(row: dict[str, Any], decision: str) -> int:
    score = int(row.get("impact_score") or 0)
    if decision == "approve_already_ok_ready":
        score += 80
    elif decision == "corrected_text_ready":
        score += 60
    elif decision == "short_label/plain_fluency":
        score += 30
    if release_group(row) == "ui_tooltips_short_labels":
        score += 20
    if release_group(row) == "narrative_events":
        score += 10
    return score


def record(row: dict[str, Any]) -> dict[str, Any]:
    decision = classify(row)
    return {
        "source": SOURCE,
        "record_type": "release_blocker_non_dynamic_probe_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "release_group": release_group(row),
        "classification": decision,
        "roi_score": roi_score(row, decision),
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "review_state": row.get("review_state"),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": row.get("spanish_text") or row.get("source_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def examples(rows: list[dict[str, Any]], field: str, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r[field], -r["roi_score"], r["segment_id"])):
        key = row[field]
        if len(grouped[key]) >= limit:
            continue
        grouped[key].append(
            {
                "segment_id": row["segment_id"],
                "release_group": row["release_group"],
                "source_key": row["source_key"],
                "classification": row["classification"],
                "roi_score": row["roi_score"],
                "output_text": row["output_text"],
            }
        )
    return dict(grouped)


def top_roi_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["release_group"]].append(row)
    out = []
    for group, items in grouped.items():
        ready = sum(1 for row in items if row["classification"] in {"approve_already_ok_ready", "corrected_text_ready"})
        attackable = sum(1 for row in items if row["classification"] not in {"high_issue_non_dynamic", "needs_more_context"})
        out.append(
            {
                "release_group": group,
                "total": len(items),
                "attackable": attackable,
                "ready": ready,
                "avg_roi_score": round(sum(row["roi_score"] for row in items) / max(len(items), 1), 2),
            }
        )
    return sorted(out, key=lambda r: (r["ready"], r["attackable"], r["avg_roi_score"]), reverse=True)


def recommendation(top_groups: list[dict[str, Any]]) -> dict[str, Any]:
    best = top_groups[0] if top_groups else {"release_group": "none", "ready": 0, "attackable": 0}
    limit = 40 if best.get("ready", 0) >= 30 else 30
    if best.get("ready", 0) >= 20:
        action = f"Gerar pacote humano/readiness para {best['release_group']} plain/light, limite {limit}, priorizando approve_already_ok_ready e corrected_text_ready."
    elif best.get("attackable", 0) >= 20:
        action = f"Gerar pacote pequeno de triagem para {best['release_group']} plain/light, limite 30, sem apply."
    else:
        action = "Volume atacavel baixo; migrar para triagem high_issue nao dinamica ou voltar para arquitetura/parser."
    return {"release_group": best.get("release_group"), "limit": limit, "action": action}


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Release Blocker Non-Dynamic Probe",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Total atacavel filtrado: {summary['attackable_total']}",
        "- Acoes: read-only; sem apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Classificacao",
    ]
    for key, count in summary["classification_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Grupos"])
    for key, count in summary["release_group_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Top ROI"])
    for group in summary["top_groups_by_roi"]:
        lines.append(
            f"- {group['release_group']}: total={group['total']} attackable={group['attackable']} ready={group['ready']} avg_roi={group['avg_roi_score']}"
        )
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["next_packet_recommendation"]["action"])
    lines.extend(["", "## Exemplos Por Classificacao"])
    for key, items in summary["examples_by_classification"].items():
        lines.append(f"### {key}")
        for item in items[:4]:
            lines.append(f"- {item['segment_id']} | {item['release_group']} | {item['source_key']} | roi={item['roi_score']}")
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    source_rows = read_jsonl(args.input_jsonl)
    excluded_counts: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    for row in source_rows:
        reason = excluded_reason(row)
        if reason:
            excluded_counts[reason] += 1
            continue
        eligible.append(record(row))
    eligible = sorted(eligible, key=lambda r: (-r["roi_score"], r["release_group"], r["segment_id"]))[: args.limit]
    classification_counts = Counter(row["classification"] for row in eligible)
    group_counts = Counter(row["release_group"] for row in eligible)
    token_counts = Counter(row["token_surface"] for row in eligible)
    top_groups = top_roi_groups(eligible)
    ready_count = classification_counts.get("approve_already_ok_ready", 0) + classification_counts.get("corrected_text_ready", 0)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_release_blocker_non_dynamic_probe",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "release_class": "release_blocker",
            "included_token_surface": ["plain_text", "light_token"],
            "excluded_segment_ids": sorted(EXCLUDED_SEGMENT_IDS),
            "excluded_surfaces": ["dynamic_getter", "dynamic_select", "multiline", "parser_later"],
            "candidate_generation_allowed": False,
            "apply_allowed": False,
            "learning_ingest_allowed": False,
            "issue_closure_allowed": False,
            "lifecycle_or_materializer_allowed": False,
            "segment_state_allowed": False,
            "reindex_allowed": False,
            "production_full_allowed": False,
        },
        "attackable_total": len(eligible),
        "ready_total": ready_count,
        "classification_counts": dict(classification_counts.most_common()),
        "release_group_counts": dict(group_counts.most_common()),
        "token_surface_counts": dict(token_counts.most_common()),
        "excluded_counts": dict(excluded_counts.most_common()),
        "top_groups_by_roi": top_groups,
        "examples_by_classification": examples(eligible, "classification"),
        "examples_by_release_group": examples(eligible, "release_group"),
        "next_packet_recommendation": recommendation(top_groups),
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
        "single_operational_recommendation": recommendation(top_groups)["action"],
    }
    return eligible, summary, markdown(summary, eligible)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_non_dynamic_probe_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    jsonl_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text(md, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows, summary, md = build(args)
    outputs = write(rows, summary, md)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"summary={outputs['summary']}")
    print(f"attackable_total={summary['attackable_total']}")
    print(f"ready_total={summary['ready_total']}")
    print(f"classification_counts={json.dumps(summary['classification_counts'], ensure_ascii=False)}")
    print(f"release_group_counts={json.dumps(summary['release_group_counts'], ensure_ascii=False)}")
    print(f"next_packet_recommendation={json.dumps(summary['next_packet_recommendation'], ensure_ascii=False)}")
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
