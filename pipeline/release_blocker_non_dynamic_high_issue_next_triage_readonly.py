from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_blocker_non_dynamic_high_issue_next_triage_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_203819_715777_release_blocker_non_dynamic_probe_readonly.jsonl")
APPROVE_OK_PROCESSED_IDS = {33718, 37839, 42196, 48500, 54856, 67282, 67319, 76756, 99715, 105133, 105383, 114115}
CORRECTED_TEXT_HOLD_IDS = {
    65282,
    130189,
    30464,
    54888,
    68315,
    76377,
    99428,
    100383,
    104983,
    112620,
    114261,
    114264,
    114265,
    114271,
    114285,
    114297,
    121588,
    121728,
}
EXPLICIT_HOLD_IDS = {120831, 126552, 127174}
EXCLUDED_IDS = APPROVE_OK_PROCESSED_IDS | CORRECTED_TEXT_HOLD_IDS | EXPLICIT_HOLD_IDS

SPANISH_LITERAL_RE = re.compile(
    r"\b(el|la|los|las|un|una|unos|unas|mayor|menor|este|esta|ese|esa|se hace|se roba|"
    r"anta[ñn]o|condemadamente|maravilloso|robarme|escabulliste|respondió|buf[oó]n|ladrona|ladr[oó]n|"
    r"verdader[oa]|nuestr[oa]s?|vuestr[oa]s?|suyo|suya)\b",
    re.IGNORECASE,
)
PT_READY_RE = re.compile(r"\b(ção|ções|você|vocês|muito|senhorio|personagem|coroa|frente|vozes|eras|vingança|amor)\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Next read-only triage for non-dynamic high issue narrative release blockers.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_number"] = line_number
            rows.append(row)
    return rows


def classify(row: dict[str, Any]) -> str:
    output = str(row.get("output_text") or "")
    issue = f"{row.get('issue_families') or ''} {row.get('issue_kinds') or ''}".lower()
    has_explicit_corrected = bool(row.get("corrected_text") or row.get("suggested_corrected_text"))
    if "parser_later" in issue:
        return "parser_later_despite_plain_light"
    if SPANISH_LITERAL_RE.search(output):
        return "corrected_text_ready" if has_explicit_corrected else "corrected_text_needs_human_text"
    if int(row.get("confirmed_matches_output") or 0) == 1 and int(row.get("needs_output_apply") or 0) == 0:
        if "spanish_residue" in issue and PT_READY_RE.search(output):
            return "high_issue_false_positive_output_ok"
        return "high_issue_resolved_by_existing_output"
    if has_explicit_corrected:
        return "corrected_text_ready"
    return "needs_human_context"


def suggested_decision(cls: str) -> str:
    if cls in {"high_issue_false_positive_output_ok", "high_issue_resolved_by_existing_output"}:
        return "approve_already_ok"
    if cls == "corrected_text_ready":
        return "corrected_text"
    if cls == "corrected_text_needs_human_text":
        return "hold_human_correction_pending"
    if cls == "parser_later_despite_plain_light":
        return "parser_later"
    return "needs_more_context"


def reason(row: dict[str, Any], cls: str) -> str:
    if cls in {"high_issue_false_positive_output_ok", "high_issue_resolved_by_existing_output"}:
        return "output atual igual a confirmed/output e parece resolver ou invalidar o high issue sem apply."
    if cls == "corrected_text_ready":
        return "ha corrected_text explicito para diff preview futuro."
    if cls == "corrected_text_needs_human_text":
        return "residuo espanhol/plain visivel, mas sem Corrected text explicito; nao contar como ready."
    if cls == "parser_later_despite_plain_light":
        return "surface plain/light, mas issue indica parser_later."
    return "contexto humano necessario antes de qualquer acao."


def eligible(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    filtered = []
    for row in rows:
        segment_id = int(row.get("segment_id") or 0)
        if segment_id in EXCLUDED_IDS:
            continue
        if row.get("release_group") != "narrative_events":
            continue
        if row.get("classification") != "high_issue_non_dynamic":
            continue
        if row.get("token_surface") not in {"plain_text", "light_token"}:
            continue
        filtered.append(row)
    return sorted(filtered, key=lambda r: (-int(r.get("roi_score") or 0), -int(r.get("high_issue_count") or 0), int(r.get("segment_id") or 0)))[:limit]


def record(row: dict[str, Any]) -> dict[str, Any]:
    cls = classify(row)
    return {
        "source": SOURCE,
        "record_type": "next_high_issue_non_dynamic_triage_item",
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "triage_class": cls,
        "suggested_human_decision": suggested_decision(cls),
        "ready_for_processing": cls in {"high_issue_false_positive_output_ok", "high_issue_resolved_by_existing_output", "corrected_text_ready"},
        "triage_reason": reason(row, cls),
        "release_group": row.get("release_group"),
        "token_surface": row.get("token_surface"),
        "roi_score": int(row.get("roi_score") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": row.get("source_text"),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "corrected_text": row.get("corrected_text") or row.get("suggested_corrected_text") or "",
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


def hold_record(segment_id: int) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "record_type": "hold_marker",
        "segment_id": segment_id,
        "hold_state": "hold_human_correction_pending",
        "reason": "corrected_text_possible packet requires explicit Corrected text in Markdown before diff preview/apply.",
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


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Next High Issue Non-Dynamic Narrative Triage",
        "",
        f"- Itens triados: {summary['record_count']}",
        f"- Holds human correction pending: {summary['hold_human_correction_pending_count']}",
        "- Acoes: read-only; sem apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Classes",
    ]
    for key, count in summary["triage_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Ready IDs"])
    lines.append(", ".join(str(i) for i in summary["ready_segment_ids"]) or "nenhum")
    lines.extend(["", "## Itens"])
    for row in rows:
        lines.extend(
            [
                f"### {row['segment_id']} | {row['source_key']}",
                f"- Classe: `{row['triage_class']}`",
                f"- Decisao sugerida: `{row['suggested_human_decision']}`",
                f"- Ready: `{row['ready_for_processing']}`",
                f"- Issues: `{row['issue_families']} | {row['issue_kinds']}`",
                f"- Razao: {row['triage_reason']}",
                f"- Source: {row['source_text']}",
                f"- Output: {row['output_text']}",
                f"- Confirmed: {row['confirmed_text']}",
                "",
            ]
        )
    return "\n".join(lines)


def write(rows: list[dict[str, Any]], hold_rows: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_non_dynamic_high_issue_next_triage_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    hold_path = Path(str(base) + "_hold_markers.jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    hold_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in hold_rows), encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "hold_markers": str(hold_path), "summary": str(summary_path)}
    md_path.write_text(markdown(summary, rows), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows = [record(row) for row in eligible(read_jsonl(args.input_jsonl), args.limit)]
    hold_rows = [hold_record(segment_id) for segment_id in sorted(CORRECTED_TEXT_HOLD_IDS)]
    class_counts = Counter(row["triage_class"] for row in rows)
    decision_counts = Counter(row["suggested_human_decision"] for row in rows)
    ready_ids = [row["segment_id"] for row in rows if row["ready_for_processing"]]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_next_high_issue_non_dynamic_triage",
        "input_jsonl": str(args.input_jsonl),
        "record_count": len(rows),
        "limit": args.limit,
        "triage_class_counts": dict(class_counts.most_common()),
        "suggested_human_decision_counts": dict(decision_counts.most_common()),
        "ready_count": len(ready_ids),
        "ready_segment_ids": ready_ids,
        "hold_human_correction_pending_count": len(CORRECTED_TEXT_HOLD_IDS),
        "hold_human_correction_pending_segment_ids": sorted(CORRECTED_TEXT_HOLD_IDS),
        "excluded_approve_ok_processed_segment_ids": sorted(APPROVE_OK_PROCESSED_IDS),
        "excluded_explicit_hold_segment_ids": sorted(EXPLICIT_HOLD_IDS),
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
        "single_operational_recommendation": (
            "Processar em ciclo separado somente ready approve_already_ok/corrected_text_ready; corrected_text_needs_human_text fica em hold."
        ),
    }
    outputs = write(rows, hold_rows, summary)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"hold_markers={outputs['hold_markers']}")
    print(f"summary={outputs['summary']}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"triage_class_counts={json.dumps(summary['triage_class_counts'], ensure_ascii=False)}")
    print(f"ready_segment_ids={json.dumps(summary['ready_segment_ids'], ensure_ascii=False)}")
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
