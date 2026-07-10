from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "narrative_select_conditional_overlap_review_readonly_v1"
DEFAULT_INPUT = Path("reports/20260703_193709_147411_narrative_dynamic_getter_gender_splitter_readonly.jsonl")
DEFAULT_RUN_ID = 585
TARGET_ROUTE = "hold_select_or_conditional_overlap"

SELECT_CSTRING_RE = re.compile(r"Select_CString", re.IGNORECASE)
SELECT_LOCALIZATION_RE = re.compile(r"SelectLocalization", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"LocalPlayerString|IsLocalPlayer|GetPlayer", re.IGNORECASE)
ES_HELPER_RE = re.compile(r"ES_[A-Za-z]+|Custom\('ES_[^']+'\)")
GETTER_RE = re.compile(r"\[[^\]]*(?:\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\.)[^\]]+\]")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only review for Select/conditional overlap route.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
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


def blob(row: dict[str, Any]) -> str:
    return "\n".join(str(row.get(key) or "") for key in ("source_text", "output_text", "confirmed_text", "issue_kinds"))


def counts(text: str) -> dict[str, int]:
    return {
        "select_cstring_count": len(SELECT_CSTRING_RE.findall(text)),
        "select_localization_count": len(SELECT_LOCALIZATION_RE.findall(text)),
        "local_player_count": len(LOCAL_PLAYER_RE.findall(text)),
        "es_helper_count": len(ES_HELPER_RE.findall(text)),
        "getter_count": len(GETTER_RE.findall(text)),
    }


def select_class(row: dict[str, Any], c: dict[str, int]) -> str:
    text = blob(row)
    multiline = "\n" in str(row.get("output_text") or "") or "\\n" in str(row.get("output_text") or "")
    if multiline:
        return "multiline_select"
    if c["select_cstring_count"] and c["select_localization_count"]:
        return "mixed_select_surfaces"
    if c["select_localization_count"] > 1:
        return "select_localization_multiple"
    if c["select_localization_count"] == 1:
        return "select_localization_single"
    if c["select_cstring_count"] > 1:
        return "select_cstring_multiple"
    if c["select_cstring_count"] == 1:
        return "select_cstring_single"
    if c["local_player_count"]:
        return "select_player_or_local_perspective"
    if "parser_later" in str(row.get("architecture_roles") or ""):
        return "parser_later"
    return "needs_parser_later"


def flags(row: dict[str, Any], c: dict[str, int]) -> list[str]:
    result: list[str] = []
    if c["getter_count"] > c["select_cstring_count"] + c["select_localization_count"]:
        result.append("select_plus_getter")
    if c["es_helper_count"]:
        result.append("select_plus_es_helper")
    if row.get("gender_or_perspective"):
        result.append("select_plus_gender_perspective")
    if c["local_player_count"]:
        result.append("select_player_or_local_perspective")
    if "\n" in str(row.get("output_text") or "") or "\\n" in str(row.get("output_text") or ""):
        result.append("multiline_select")
    if int(row.get("high_issue_count") or 0) > 0:
        result.append("human_context_required")
    if not result:
        result.append("select_only")
    return result


def decision(select_cls: str, overlap_flags: list[str]) -> str:
    flag_set = set(overlap_flags)
    if "multiline_select" in flag_set:
        return "parser_later_multiline_select"
    if "select_plus_es_helper" in flag_set or "select_plus_gender_perspective" in flag_set:
        return "subpolicy_select_gender_perspective_read_only"
    if "select_player_or_local_perspective" in flag_set:
        return "subpolicy_select_player_perspective_read_only"
    if select_cls == "select_cstring_single" and flag_set <= {"select_plus_getter", "human_context_required", "select_only"}:
        return "architecture_packet_select_cstring_single_line"
    if select_cls in {"select_cstring_multiple", "select_localization_multiple", "mixed_select_surfaces"}:
        return "parser_later_multiple_selects"
    if select_cls == "select_localization_single":
        return "subpolicy_select_localization_single_read_only"
    return "needs_parser_later"


def context_around(text: str, width: int = 180) -> str:
    matches = [m.start() for m in SELECT_CSTRING_RE.finditer(text)] + [m.start() for m in SELECT_LOCALIZATION_RE.finditer(text)]
    if not matches:
        return text[:width].replace("\n", "\\n")
    idx = min(matches)
    start = max(0, idx - width // 2)
    end = min(len(text), idx + width)
    return text[start:end].replace("\n", "\\n")


def record(row: dict[str, Any]) -> dict[str, Any]:
    text = blob(row)
    c = counts(text)
    cls = select_class(row, c)
    overlap_flags = flags(row, c)
    rec = decision(cls, overlap_flags)
    return {
        "source": SOURCE,
        "record_type": "narrative_select_conditional_overlap_review_item",
        "segment_id": int(row.get("segment_id") or 0),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "select_class": cls,
        "overlap_flags": overlap_flags,
        "review_decision": rec,
        **c,
        "context_around_select": context_around(str(row.get("output_text") or row.get("confirmed_text") or row.get("source_text") or "")),
        "getter_tokens": row.get("getter_tokens") or [],
        "token_surface": row.get("token_surface"),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
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


def examples(rows: list[dict[str, Any]], field: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r[field], -r["high_issue_count"], r["segment_id"])):
        key = row[field]
        if len(grouped[key]) >= limit:
            continue
        grouped[key].append(
            {
                "segment_id": row["segment_id"],
                "source_key": row["source_key"],
                "select_class": row["select_class"],
                "overlap_flags": row["overlap_flags"],
                "review_decision": row["review_decision"],
                "context": row["context_around_select"],
            }
        )
    return dict(grouped)


def top_paths_by_decision(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counters[row["review_decision"]][row["relative_path"] or ""] += 1
    return {key: dict(counter.most_common(10)) for key, counter in sorted(counters.items())}


def recommendation(summary: dict[str, Any]) -> str:
    gender = summary["review_decision_counts"].get("subpolicy_select_gender_perspective_read_only", 0)
    single = summary["review_decision_counts"].get("architecture_packet_select_cstring_single_line", 0)
    player = summary["review_decision_counts"].get("subpolicy_select_player_perspective_read_only", 0)
    if gender >= max(single, player):
        return (
            "Primeira subpolicy/parser recomendada: select_gender_perspective_read_only. "
            "Gerar depois pacote arquitetural pequeno apenas dos single-line Select_CString sem multiline."
        )
    if single > 0:
        return (
            "Primeiro pacote de arquitetura: Select_CString single-line. Ainda nao e pacote humano; validar parser/splitter antes."
        )
    return "Manter como parser_later; nao ha subrota clara suficiente para pacote humano seguro."


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Narrative Select/Conditional Overlap Review",
        "",
        f"- Segment-state run base: {summary['segment_state_run_id']}",
        f"- Entrada: `{summary['input_jsonl']}`",
        f"- Registros da rota: {summary['record_count']}",
        "- Acoes: read-only; sem candidato, apply, ingest, issue closure, lifecycle/materializer, segment-state, reindex ou producao full.",
        "",
        "## Classe Select",
    ]
    for key, count in summary["select_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Flags De Overlap"])
    for key, count in summary["overlap_flag_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Decisao"])
    for key, count in summary["review_decision_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Recomendacao"])
    lines.append(summary["single_operational_recommendation"])
    lines.extend(["", "## Exemplos Por Decisao"])
    for key, items in summary["examples_by_review_decision"].items():
        lines.append(f"### {key}")
        for item in items[:4]:
            lines.append(f"- {item['segment_id']} | {item['source_key']} | {item['select_class']} | {', '.join(item['overlap_flags'])}")
    lines.append("")
    return "\n".join(lines)


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    input_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("route") == TARGET_ROUTE]
    rows = [record(row) for row in input_rows]
    class_counts = Counter(row["select_class"] for row in rows)
    flag_counts = Counter(flag for row in rows for flag in row["overlap_flags"])
    decision_counts = Counter(row["review_decision"] for row in rows)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_select_conditional_overlap_review",
        "segment_state_run_id": args.run_id,
        "input_jsonl": str(args.input_jsonl),
        "scope": {
            "route": TARGET_ROUTE,
            "expected_count": 223,
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
        "expected_count_ok": len(rows) == 223,
        "select_class_counts": dict(class_counts.most_common()),
        "overlap_flag_counts": dict(flag_counts.most_common()),
        "review_decision_counts": dict(decision_counts.most_common()),
        "top_paths_by_decision": top_paths_by_decision(rows),
        "examples_by_select_class": examples(rows, "select_class"),
        "examples_by_review_decision": examples(rows, "review_decision"),
        "clear_split_only_subroute_exists": decision_counts.get("subpolicy_select_gender_perspective_read_only", 0) > 0
        or decision_counts.get("subpolicy_select_player_perspective_read_only", 0) > 0,
        "small_architecture_packet_select_cstring_single_line_count": decision_counts.get(
            "architecture_packet_select_cstring_single_line", 0
        ),
        "safe_human_review_sublote_now": False,
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
    }
    summary["single_operational_recommendation"] = recommendation(summary)
    summary["next_subpolicy_parser_recommendation"] = (
        "select_gender_perspective_read_only, then select_cstring_single_line_architecture_packet, "
        "with multiline/multiple/mixed select routed to parser_later."
    )
    return rows, summary, markdown(summary, rows)


def write(rows: list[dict[str, Any]], summary: dict[str, Any], md: str) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_narrative_select_conditional_overlap_review_readonly"
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
    print(f"expected_count_ok={summary['expected_count_ok']}")
    print(f"select_class_counts={json.dumps(summary['select_class_counts'], ensure_ascii=False)}")
    print(f"overlap_flag_counts={json.dumps(summary['overlap_flag_counts'], ensure_ascii=False)}")
    print(f"review_decision_counts={json.dumps(summary['review_decision_counts'], ensure_ascii=False)}")
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
