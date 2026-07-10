from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import short_label_style_run406_sublane_diagnostic as diagnostic


SOURCE = "accolade_short_label_tooltip_sublane_diagnostic_v1"
SAMPLE_PER_DECISION = 8
RECENT_LEARNING_RUN_IDS = (691, 692, 693, 694, 695, 696, 697, 698, 699)
RECENT_HOLD_SEGMENTS = {
    281274,
    9291,
    3934,
    153501,
    22963,
    22974,
    22977,
    23005,
    34132,
    71234,
    58452,
}

ACCOLADE_RE = re.compile(r"accolade|acclaimed_knight|glory|knight", re.IGNORECASE)
WARNING_RE = re.compile(r"@warning_icon|#X|#warning", re.IGNORECASE)
SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf", re.IGNORECASE)
PIPE_TOKEN_RE = re.compile(r"\[[^\]]+\|(?:E|El|lE|EU|U|V|0|1V|e)\]")
GETTER_RE = re.compile(r"\[[^\]]*Get[A-Za-z0-9_]+[^\]]*\]")
FORMAT_RE = re.compile(r"#T|#X|#!|#indent_newline|@warning_icon|\\n|\n|\$TAB\$")
SPANISH_RESIDUAL_RE = re.compile(
    r"\b(?:actual|siguiente|sin usar|efectos|l[ií]mite|galardones|no cumple|ning[uú]n|nombrar|requisitos)\b",
    re.IGNORECASE,
)
PT_RESIDUAL_RE = re.compile(r"\b(?:sem usar|próximo|atual|efeitos|limite|requisitos|nomeado)\b", re.IGNORECASE)


def configure_diagnostic_module() -> None:
    diagnostic.SOURCE = SOURCE
    diagnostic.RECENT_LEARNING_RUN_IDS = RECENT_LEARNING_RUN_IDS
    diagnostic.RECENT_HOLD_SEGMENTS = RECENT_HOLD_SEGMENTS


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    return diagnostic.reports_dir()


def is_accolade_row(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("relative_path", "source_key", "english_text", "spanish_text", "current_output_text")
    )
    return bool(ACCOLADE_RE.search(haystack))


def short(text: str | None, limit: int = 240) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def classify(row: dict[str, Any]) -> tuple[str, str, bool]:
    text = str(row.get("current_output_text") or "")
    english = str(row.get("english_text") or "")
    spanish = str(row.get("spanish_text") or "")
    key = str(row.get("source_key") or "")
    token_count = len(diagnostic.TOKEN_RE.findall(text))
    multiline = "\n" in text or "\\n" in text
    if SPANISH_RESIDUAL_RE.search(text):
        return "needs_human_residual_review", "visible Spanish residual in current output", False
    if SELECT_RE.search(text):
        return "needs_select_localization_policy_review", "SelectLocalization/branching token surface", True
    if WARNING_RE.search(text):
        return "needs_warning_tooltip_guarded_review", "warning tooltip with CK3 tokens", False
    if "requirement" in key.lower() or "cannot" in key.lower() or "no_acclaimed" in key.lower():
        return "needs_requirement_tooltip_policy_review", "requirement/acclaimed-knight tooltip surface", False
    if multiline or token_count >= 4:
        if GETTER_RE.search(text) and FORMAT_RE.search(text):
            return "needs_dense_multiline_token_policy", "dense multiline tooltip with getters and formatting", True
        return "needs_guarded_multiline_review", "multiline or token-dense accolade tooltip", False
    if PIPE_TOKEN_RE.search(text) and PT_RESIDUAL_RE.search(text):
        return "human_review_pipe_token_label", "short pipe-token UI label with PT wording", False
    if PIPE_TOKEN_RE.search(text):
        return "needs_article_gender_policy_review", "pipe token article/gender surface", False
    if GETTER_RE.search(text) or GETTER_RE.search(english) or GETTER_RE.search(spanish):
        return "needs_getter_context_review", "getter-dependent wording needs guarded review", False
    return "human_review_accolade_plain_or_light_token", "manageable accolade label/tooltip", False


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    decision, rationale, requires_architecture = classify(row)
    text = str(row.get("current_output_text") or "")
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "decision": decision,
        "rationale": rationale,
        "requires_architecture": requires_architecture,
        "token_count": len(diagnostic.TOKEN_RE.findall(text)),
        "has_warning": bool(WARNING_RE.search(text)),
        "has_select_localization": bool(SELECT_RE.search(text)),
        "has_getter": bool(GETTER_RE.search(text)),
        "has_pipe_token": bool(PIPE_TOKEN_RE.search(text)),
        "is_multiline": "\n" in text or "\\n" in text,
        "current_output_text": short(text),
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
    }


def build_summary(rows: list[dict[str, Any]], preflight_path: Path | None, excluded_count: int) -> dict[str, Any]:
    enriched = [enrich(row) for row in rows]
    decision_counts = Counter(row["decision"] for row in enriched)
    file_counts = Counter(str(row["relative_path"]) for row in enriched)
    token_counts = Counter(str(row["token_count"]) for row in enriched)
    sample_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        if len(sample_by_decision[row["decision"]]) < SAMPLE_PER_DECISION:
            sample_by_decision[row["decision"]].append(row)

    architecture_count = sum(1 for row in enriched if row["requires_architecture"])
    recommended_decision = None
    priority = [
        "human_review_pipe_token_label",
        "human_review_accolade_plain_or_light_token",
        "needs_warning_tooltip_guarded_review",
        "needs_requirement_tooltip_policy_review",
        "needs_guarded_multiline_review",
        "needs_article_gender_policy_review",
        "needs_select_localization_policy_review",
        "needs_dense_multiline_token_policy",
    ]
    for decision in priority:
        if decision_counts.get(decision):
            recommended_decision = decision
            break
    next_action = (
        "prepare_human_review_packet_for_accolade_pipe_token_labels"
        if recommended_decision == "human_review_pipe_token_label"
        else "prepare_guarded_policy_review_for_accolade_tooltips"
        if recommended_decision
        else "hold_no_accolade_sublane_available"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": diagnostic.SEGMENT_STATE_RUN_ID,
        "ledger_run_id": diagnostic.LEDGER_RUN_ID,
        "preflight_summary_path": str(preflight_path) if preflight_path else None,
        "excluded_segment_count": excluded_count,
        "rows_reviewed": len(enriched),
        "architecture_required_count": architecture_count,
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "file_counts": [{"key": key, "count": value} for key, value in file_counts.most_common()],
        "token_count_distribution": [{"key": key, "count": value} for key, value in token_counts.most_common()],
        "recommended_decision": recommended_decision,
        "recommended_decision_count": int(decision_counts.get(recommended_decision or "", 0)),
        "architecture_needed_before_next_step": bool(
            recommended_decision in {"needs_select_localization_policy_review", "needs_dense_multiline_token_policy"}
        ),
        "sample_by_decision": dict(sample_by_decision),
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "segment_state_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_accolade_short_label_tooltip_sublane_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(enrich(row), ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "accolade short label tooltip sublane diagnostic",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"rows_reviewed={summary['rows_reviewed']}",
        f"excluded_segment_count={summary['excluded_segment_count']}",
        f"architecture_required_count={summary['architecture_required_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "file_counts:"])
    for item in summary["file_counts"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "token_count_distribution:"])
    for item in summary["token_count_distribution"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"recommended_decision={summary['recommended_decision']}",
            f"recommended_decision_count={summary['recommended_decision_count']}",
            f"architecture_needed_before_next_step={str(summary['architecture_needed_before_next_step']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"segment_state_recommended_now={str(summary['segment_state_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    configure_diagnostic_module()
    preflight_path, preflight_excluded = diagnostic.load_preflight_exclusions()
    with diagnostic.connect_readonly() as conn:
        excluded = preflight_excluded | diagnostic.recent_learning_segments(conn) | RECENT_HOLD_SEGMENTS
        source_rows = diagnostic.fetch_rows(conn, excluded)
    rows = [row for row in source_rows if is_accolade_row(row)]
    summary = build_summary(rows, preflight_path, len(excluded))
    txt_path, jsonl_path, summary_path = write_outputs(summary, rows)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"rows_reviewed={summary['rows_reviewed']}")
    print(f"excluded_segment_count={summary['excluded_segment_count']}")
    print(f"architecture_required_count={summary['architecture_required_count']}")
    print(f"recommended_decision={summary['recommended_decision']}")
    print(f"recommended_decision_count={summary['recommended_decision_count']}")
    print(f"architecture_needed_before_next_step={summary['architecture_needed_before_next_step']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
