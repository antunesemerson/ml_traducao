from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_blocker_non_dynamic_high_issue_triage_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_203819_715777_release_blocker_non_dynamic_probe_readonly.jsonl")
DEFAULT_RUN_ID = 585
EXCLUDED_SEGMENT_IDS = {120831, 126552, 127174}

SPANISH_LITERAL_RE = re.compile(
    r"\b(el|la|los|las|un|una|unos|unas|mayor|menor|este|esta|ese|esa|se hace|se roba|"
    r"anta[ñn]o|condemadamente|maravilloso|robarme|escabulliste|respondió|buf[oó]n|ladrona|ladr[oó]n)\b",
    re.IGNORECASE,
)
PT_READY_RE = re.compile(r"\b(ção|ões|você|vocês|muito|senhorio|personagem|coroa|frente|vozes|eras|vingança)\b", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only triage for non-dynamic high issue narrative release blockers.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--limit", type=int, default=30)
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


def reason(row: dict[str, Any]) -> str:
    families = row.get("issue_families") or ""
    kinds = row.get("issue_kinds") or ""
    return f"{families} | {kinds}"


def classify(row: dict[str, Any]) -> str:
    output = str(row.get("output_text") or "")
    source = str(row.get("source_text") or "")
    issue = reason(row).lower()
    if "parser_later" in issue:
        return "parser_later_despite_plain_light"
    if SPANISH_LITERAL_RE.search(output):
        if int(row.get("confirmed_matches_output") or 0) == 1 and output == str(row.get("confirmed_text") or ""):
            return "corrected_text_possible"
        return "high_issue_true_blocker"
    if int(row.get("confirmed_matches_output") or 0) == 1 and int(row.get("needs_output_apply") or 0) == 0:
        if "spanish_residue" in issue and PT_READY_RE.search(output):
            return "high_issue_false_positive_output_ok"
        return "high_issue_resolved_by_existing_output"
    if row.get("confirmed_text") and output != row.get("confirmed_text"):
        return "corrected_text_possible"
    if len(output) > 180 or len(source) > 220:
        return "needs_human_context"
    return "reject_or_defer"


def suggested_human_decision(cls: str) -> str:
    return {
        "high_issue_false_positive_output_ok": "approve_already_ok",
        "high_issue_resolved_by_existing_output": "approve_already_ok",
        "corrected_text_possible": "corrected_text",
        "high_issue_true_blocker": "corrected_text",
        "needs_human_context": "needs_more_context",
        "parser_later_despite_plain_light": "parser_later",
        "reject_or_defer": "reject_or_defer",
    }.get(cls, "needs_more_context")


def triage_reason(row: dict[str, Any], cls: str) -> str:
    output = str(row.get("output_text") or "")
    if cls in {"high_issue_false_positive_output_ok", "high_issue_resolved_by_existing_output"}:
        return "output igual a confirmed_text, sem needs_output_apply; high issue parece residual/ledger."
    if cls == "corrected_text_possible":
        if SPANISH_LITERAL_RE.search(output):
            return "ha residuo espanhol literal visivel no output atual."
        return "output diverge de confirmed_text e parece corrigivel."
    if cls == "high_issue_true_blocker":
        return "high issue aponta residuo espanhol real ou texto nao localizado."
    if cls == "needs_human_context":
        return "texto longo ou contexto narrativo exige decisao humana."
    if cls == "parser_later_despite_plain_light":
        return "marcado como parser_later apesar de surface plain/light."
    return "risco/beneficio baixo para este pacote."


def record(row: dict[str, Any]) -> dict[str, Any]:
    cls = classify(row)
    return {
        "source": SOURCE,
        "record_type": "release_blocker_non_dynamic_high_issue_triage_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "triage_class": cls,
        "suggested_human_decision": suggested_human_decision(cls),
        "triage_reason": triage_reason(row, cls),
        "release_group": row.get("release_group"),
        "token_surface": row.get("token_surface"),
        "roi_score": int(row.get("roi_score") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": row.get("source_text"),
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


def eligible_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    filtered = [
        row
        for row in rows
        if row.get("release_group") == "narrative_events"
        and row.get("classification") == "high_issue_non_dynamic"
        and row.get("token_surface") in {"plain_text", "light_token"}
        and int(row.get("segment_id") or 0) not in EXCLUDED_SEGMENT_IDS
    ]
    filtered = sorted(filtered, key=lambda r: (-int(r.get("roi_score") or 0), -int(r.get("high_issue_count") or 0), int(r.get("segment_id") or 0)))
    return filtered[:limit]


def examples(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if len(grouped[row["triage_class"]]) >= 6:
            continue
        grouped[row["triage_class"]].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "suggested_human_decision": row["suggested_human_decision"],
                "triage_reason": row["triage_reason"],
            }
        )
    return dict(grouped)


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# High Issue Non-Dynamic Narrative Triage",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Itens triados: {summary['record_count']}",
        "- Acoes: read-only; sem apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Contagem Por Classe",
    ]
    for key, count in summary["triage_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## IDs Potencialmente Destravaveis"])
    lines.append(", ".join(str(item) for item in summary["potentially_unlockable_segment_ids"]) or "nenhum")
    lines.extend(["", "## Itens Para Revisao"])
    for row in rows:
        lines.extend(
            [
                f"### {row['segment_id']} | {row['source_key']}",
                f"- Classe: `{row['triage_class']}`",
                f"- Sugestao humana inicial: `{row['suggested_human_decision']}`",
                f"- Issues: `{row['issue_families']} | {row['issue_kinds']}`",
                f"- Razao: {row['triage_reason']}",
                f"- Source: {row['source_text']}",
                f"- Output: {row['output_text']}",
                f"- Confirmed: {row['confirmed_text']}",
                "",
            ]
        )
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    rows = [record(row) for row in eligible_rows(read_jsonl(args.input_jsonl), args.limit)]
    class_counts = Counter(row["triage_class"] for row in rows)
    decision_counts = Counter(row["suggested_human_decision"] for row in rows)
    unlockable_ids = [
        row["segment_id"]
        for row in rows
        if row["triage_class"]
        in {"high_issue_false_positive_output_ok", "high_issue_resolved_by_existing_output", "corrected_text_possible"}
    ]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_high_issue_non_dynamic_narrative_triage",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "base": "release_blocker_non_dynamic_probe_readonly",
            "release_group": "narrative_events",
            "classification": "high_issue_non_dynamic",
            "limit": args.limit,
            "included_token_surface": ["plain_text", "light_token"],
            "excluded_segment_ids": sorted(EXCLUDED_SEGMENT_IDS),
            "candidate_generation_allowed": False,
            "apply_allowed": False,
            "learning_ingest_allowed": False,
            "issue_closure_allowed": False,
            "lifecycle_or_materializer_allowed": False,
            "segment_state_allowed": False,
            "reindex_allowed": False,
            "production_full_allowed": False,
        },
        "record_count": len(rows),
        "triage_class_counts": dict(class_counts.most_common()),
        "suggested_human_decision_counts": dict(decision_counts.most_common()),
        "potentially_unlockable_count": len(unlockable_ids),
        "potentially_unlockable_segment_ids": unlockable_ids,
        "examples_by_class": examples(rows),
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
        "single_operational_recommendation": (
            "Revisar manualmente os IDs potencialmente destravaveis; se aprovados como already_ok/corrected_text, "
            "processar em ciclo separado com ingest/issue closure/materializer conforme guards."
        ),
    }
    return rows, summary, markdown(summary, rows)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_non_dynamic_high_issue_triage_readonly"
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
    print(f"record_count={summary['record_count']}")
    print(f"triage_class_counts={json.dumps(summary['triage_class_counts'], ensure_ascii=False)}")
    print(f"potentially_unlockable_count={summary['potentially_unlockable_count']}")
    print(f"potentially_unlockable_segment_ids={json.dumps(summary['potentially_unlockable_segment_ids'], ensure_ascii=False)}")
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
