from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_review_policy_design_candidate_architecture_v1"
SEGMENT_STATE_RUN_ID = 404
LEDGER_RUN_ID = 76
INPUT_JSONL = "reports/20260624_203837_898189_semantic_review_router_pending_deep_diagnostic.jsonl"
BASE_LANE = "semantic_review_policy_design_candidate"
SAMPLE_PER_SUBLANE = 12

SELECT_CSTRING_RE = re.compile(r"Select_CString\(")
ES_HELPER_RE = re.compile(r"\.Custom\('ES_[A-Za-z0-9_]+'\)|\bES_[A-Za-z0-9_]+\b")
BRACKET_RE = re.compile(r"\[[^\]]+\]")
VARIABLE_RE = re.compile(r"\$[^$]+\$")
TAG_RE = re.compile(r"#[A-Za-z][^#\n]*#!|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!")
SCOPE_GETTER_RE = re.compile(
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|(?:Get|Build|Add|LessThan|StringIsEmpty|SelectLocalization)[A-Za-z0-9_]*\("
)
TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][^#\n]*#!|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|"
    r"@[A-Za-z0-9_]+!|Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|(?:Get|Build|Add|LessThan|StringIsEmpty|SelectLocalization)[A-Za-z0-9_]*\("
)
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:cielos|maravilloso|hacerte|hacerle|eres|estancia|galard[oó]n|coste|actual|"
    r"siguiente|elige|del|los|las|tus|sus|una|uno|este|esta)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def project_path(value: str) -> Path:
    return db.project_path(value)


def reports_dir() -> Path:
    path = project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_policy_design_candidate_architecture"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_summary.json",
        reports_dir() / f"{base.name}_spec.json",
    )


def readonly_conn() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def as_text(value: Any) -> str:
    return str(value or "")


def compact(value: Any, limit: int = 360) -> str:
    text = " ".join(as_text(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def file_family(path: str) -> str:
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if len(parts) >= 2 and parts[0] in {"culture", "religion", "event_localization", "dlc", "activities", "artifacts"}:
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def source_prefix(source_key: str) -> str:
    value = source_key.lower()
    return re.split(r"[_.:]", value)[0] if value else ""


def text_form(row: dict[str, Any]) -> str:
    text = as_text(row.get("current_output_text"))
    token_count = int(row.get("token_count") or len(TOKEN_RE.findall(text)))
    variable_count = int(row.get("variable_count") or len(VARIABLE_RE.findall(text)))
    bracket_count = int(row.get("bracket_token_count") or len(BRACKET_RE.findall(text)))
    if SELECT_CSTRING_RE.search(text) or ES_HELPER_RE.search(text):
        return "blocked_dynamic_branch"
    if token_count == 0 and variable_count == 0 and bracket_count == 0:
        return "plain_text"
    if token_count <= 2 and variable_count == 0:
        return "dynamic_light_no_vars"
    if token_count <= 3:
        return "dynamic_light_guarded"
    return "dynamic_dense_hold"


def sublane_key(row: dict[str, Any], form: str) -> str:
    surface = as_text(row.get("surface_bucket"))
    risk = as_text(row.get("risk_bucket"))
    if form == "blocked_dynamic_branch":
        return "human_queue_dynamic_branching_surface"
    if SPANISH_RESIDUE_RE.search(as_text(row.get("current_output_text"))):
        return "hold_spanish_residue_context_risk"
    if surface == "general_semantic_prose" and risk == "low_plain_text" and form == "plain_text":
        return "general_plain_text_semantic_reopen"
    if surface == "general_semantic_prose" and risk == "medium_dynamic_light" and form == "dynamic_light_no_vars":
        return "general_dynamic_light_no_vars_semantic_reopen"
    if surface == "general_semantic_prose" and risk == "medium_dynamic_light":
        return "general_dynamic_light_guarded_semantic_reopen"
    if surface == "activity_contract_event":
        return "activity_contract_event_semantic_reopen"
    if surface == "accolade_knight_glory":
        return "accolade_knight_glory_semantic_reopen"
    return f"{surface or 'unknown'}_{risk or 'unknown'}_{form}"


def semantic_posture(row: dict[str, Any], form: str) -> str:
    current = as_text(row.get("current_output_text")).strip()
    old = as_text(row.get("old_text")).strip()
    spanish = as_text(row.get("spanish_text")).strip()
    english = as_text(row.get("english_text")).strip()
    if SELECT_CSTRING_RE.search(current) or ES_HELPER_RE.search(current):
        return "human_required_dynamic_gender_or_branching"
    if current == old and int(row.get("confirmed_matches_output") or 0) == 1:
        if form == "plain_text":
            return "already_good_but_reopen_candidate_plain"
        return "already_good_but_reopen_candidate_guarded_dynamic"
    if current and current == spanish:
        return "possible_untranslated_spanish_or_locale_preserve"
    if current and current == english:
        return "possible_untranslated_english_or_token_surface"
    return "semantic_conflict_needs_review"


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    form = text_form(row)
    sublane = sublane_key(row, form)
    current = as_text(row.get("current_output_text"))
    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "file_family": file_family(as_text(row.get("relative_path"))),
        "source_key": row.get("source_key"),
        "source_key_prefix": source_prefix(as_text(row.get("source_key"))),
        "source_line_number": row.get("source_line_number"),
        "surface_bucket": row.get("surface_bucket"),
        "risk_bucket": row.get("risk_bucket"),
        "text_form": form,
        "sublane": sublane,
        "semantic_posture": semantic_posture(row, form),
        "is_multiline": "\\n" in current or "\n" in current,
        "has_variables": bool(VARIABLE_RE.search(current)),
        "has_tags": bool(TAG_RE.search(current)),
        "has_scope_getter": bool(SCOPE_GETTER_RE.search(current)),
        "has_select_cstring": bool(SELECT_CSTRING_RE.search(current)),
        "has_es_helper": bool(ES_HELPER_RE.search(current)),
        "has_spanish_residue_signal": bool(SPANISH_RESIDUE_RE.search(current)),
        "token_count": int(row.get("token_count") or len(TOKEN_RE.findall(current))),
        "bracket_token_count": int(row.get("bracket_token_count") or len(BRACKET_RE.findall(current))),
        "variable_count": int(row.get("variable_count") or len(VARIABLE_RE.findall(current))),
        "text_length": int(row.get("text_length") or len(current)),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "final_state": row.get("final_state"),
        "review_state": row.get("review_state"),
        "issue_kind": row.get("issue_kind"),
        "issue_severity": row.get("issue_severity"),
        "current_equals_old": as_text(row.get("current_output_text")).strip() == as_text(row.get("old_text")).strip(),
        "current_equals_spanish": as_text(row.get("current_output_text")).strip() == as_text(row.get("spanish_text")).strip(),
        "current_equals_english": as_text(row.get("current_output_text")).strip() == as_text(row.get("english_text")).strip(),
        "current_output_text": compact(row.get("current_output_text")),
        "english_text": compact(row.get("english_text")),
        "spanish_text": compact(row.get("spanish_text")),
        "old_text": compact(row.get("old_text")),
    }


def counter_rows(counter: Counter[str], limit: int = 25) -> list[dict[str, Any]]:
    return [{"key": key, "count": value} for key, value in counter.most_common(limit)]


def sublane_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["sublane"]].append(row)
    summaries: list[dict[str, Any]] = []
    for key, items in grouped.items():
        risk = Counter(as_text(row["risk_bucket"]) for row in items)
        surface = Counter(as_text(row["surface_bucket"]) for row in items)
        posture = Counter(as_text(row["semantic_posture"]) for row in items)
        forms = Counter(as_text(row["text_form"]) for row in items)
        dynamic_or_tokenized = sum(1 for row in items if row["token_count"] > 0 or row["has_variables"] or row["has_tags"])
        hard_guard = sum(1 for row in items if row["has_select_cstring"] or row["has_es_helper"])
        spanish_risk = sum(1 for row in items if row["has_spanish_residue_signal"])
        candidate = (
            len(items) >= 100
            and hard_guard == 0
            and spanish_risk == 0
            and key
            in {
                "general_plain_text_semantic_reopen",
                "general_dynamic_light_no_vars_semantic_reopen",
                "general_dynamic_light_guarded_semantic_reopen",
            }
        )
        if candidate and key == "general_plain_text_semantic_reopen":
            recommended_action = "narrow_lifecycle_policy_review_candidate"
        elif candidate:
            recommended_action = "narrow_guarded_policy_review_candidate"
        elif hard_guard:
            recommended_action = "human_queue_dynamic_context"
        else:
            recommended_action = "hold_or_sample_later"
        summaries.append(
            {
                "key": key,
                "count": len(items),
                "candidate_for_next_review": candidate,
                "recommended_action": recommended_action,
                "dominant_surface": surface.most_common(1)[0][0] if surface else "",
                "dominant_risk": risk.most_common(1)[0][0] if risk else "",
                "dominant_text_form": forms.most_common(1)[0][0] if forms else "",
                "dominant_semantic_posture": posture.most_common(1)[0][0] if posture else "",
                "dynamic_or_tokenized_count": dynamic_or_tokenized,
                "hard_guard_count": hard_guard,
                "spanish_residue_signal_count": spanish_risk,
                "multiline_count": sum(1 for row in items if row["is_multiline"]),
                "has_variables_count": sum(1 for row in items if row["has_variables"]),
                "has_tags_count": sum(1 for row in items if row["has_tags"]),
                "has_scope_getter_count": sum(1 for row in items if row["has_scope_getter"]),
                "current_equals_old_count": sum(1 for row in items if row["current_equals_old"]),
                "current_equals_spanish_count": sum(1 for row in items if row["current_equals_spanish"]),
                "top_file_families": counter_rows(Counter(as_text(row["file_family"]) for row in items), 8),
                "top_source_key_prefixes": counter_rows(Counter(as_text(row["source_key_prefix"]) for row in items), 8),
            }
        )
    return sorted(summaries, key=lambda item: (-int(item["count"]), item["key"]))


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["sublane"]].append(row)
    sampled: list[dict[str, Any]] = []
    for sublane in sorted(grouped):
        for row in grouped[sublane][:SAMPLE_PER_SUBLANE]:
            sampled.append(row)
    return sampled


def write_outputs(
    *,
    txt_path: Path,
    jsonl_path: Path,
    summary_path: Path,
    spec_path: Path,
    rows: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    summary: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in samples:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "semantic review policy design candidate architecture",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"input_jsonl={INPUT_JSONL}",
        f"base_lane={BASE_LANE}",
        f"base_count={len(rows)}",
        "",
        "Decision:",
        f"- safe_closure_whole_lane: {summary['safe_closure_whole_lane']}",
        f"- best_candidate_sublane: {summary['best_candidate_sublane']}",
        f"- recommended_next_step: {summary['recommended_next_step']}",
        f"- apply_ready_now: {summary['apply_ready_now']}",
        f"- production_full_recommended_now: {summary['production_full_recommended_now']}",
        "",
        "Why not whole-lane closure:",
    ]
    lines.extend(f"- {reason}" for reason in summary["whole_lane_blockers"])
    lines.append("")
    lines.append("Sublanes:")
    for item in summary["sublane_counts"]:
        lines.append(
            f"- {item['count']} | {item['key']} | action={item['recommended_action']} | "
            f"risk={item['dominant_risk']} | form={item['dominant_text_form']} | posture={item['dominant_semantic_posture']}"
        )
    lines.append("")
    lines.append("Candidate sublanes:")
    if summary["candidate_sublanes"]:
        for item in summary["candidate_sublanes"]:
            lines.append(f"- {item['key']}: {item['count']} | {item['recommended_action']}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Human queue sublanes:")
    if summary["human_queue_sublanes"]:
        for item in summary["human_queue_sublanes"]:
            lines.append(f"- {item['key']}: {item['count']} | {item['recommended_action']}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Hold sublanes:")
    if summary["hold_sublanes"]:
        for item in summary["hold_sublanes"]:
            lines.append(f"- {item['key']}: {item['count']} | {item['recommended_action']}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Generated files:")
    lines.append(f"- jsonl_sample={jsonl_path}")
    lines.append(f"- summary={summary_path}")
    lines.append(f"- spec={spec_path}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    input_path = project_path(INPUT_JSONL)
    if not input_path.exists():
        raise SystemExit(f"missing input JSONL: {input_path}")

    with readonly_conn() as conn:
        latest_run = conn.execute(
            "SELECT id, closed_count, pending_count, output_apply_pending_count FROM segment_state_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not latest_run or int(latest_run["id"]) != SEGMENT_STATE_RUN_ID:
            raise SystemExit(f"expected latest segment_state_run_id={SEGMENT_STATE_RUN_ID}")
        if int(latest_run["output_apply_pending_count"] or 0) != 0:
            raise SystemExit("needs_output_apply is not zero in latest run")

    input_rows = read_jsonl(input_path)
    base_rows = [row for row in input_rows if row.get("policy_lane") == BASE_LANE]
    enriched = [enrich(row) for row in base_rows]
    summaries = sublane_summaries(enriched)
    samples = sample_rows(enriched)

    candidate_sublanes = [item for item in summaries if item["candidate_for_next_review"]]
    human_queue_sublanes = [item for item in summaries if item["recommended_action"] == "human_queue_dynamic_context"]
    hold_sublanes = [item for item in summaries if item["recommended_action"] == "hold_or_sample_later"]
    best = candidate_sublanes[0] if candidate_sublanes else None
    recommended_next_step = (
        "chat_exec_semantic_review_general_plain_text_policy_review_prompt.md"
        if best and best["key"] == "general_plain_text_semantic_reopen"
        else "chat_exec_semantic_review_manual_sampling_packet_prompt.md"
    )

    whole_lane_blockers = [
        "lane mixes plain text, dynamic-light expressions, activity surfaces and accolade UI surfaces",
        "834 cases have medium_dynamic_light risk and require token/dynamic-expression guards",
        "not all cases are low-risk plain text; dynamic and multiline surfaces need separate policies",
        "the issue is semantic conflict/lifecycle classification, not output apply",
        "whole-lane closure would hide real semantic disagreements that still need narrow review",
    ]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "input_jsonl": str(input_path),
        "base_lane": BASE_LANE,
        "base_count": len(enriched),
        "safe_closure_whole_lane": False,
        "best_candidate_sublane": best["key"] if best else "",
        "recommended_next_step": recommended_next_step,
        "candidate_sublanes": candidate_sublanes,
        "human_queue_sublanes": human_queue_sublanes,
        "hold_sublanes": hold_sublanes,
        "sublane_counts": summaries,
        "surface_counts": counter_rows(Counter(as_text(row["surface_bucket"]) for row in enriched)),
        "risk_counts": counter_rows(Counter(as_text(row["risk_bucket"]) for row in enriched)),
        "text_form_counts": counter_rows(Counter(as_text(row["text_form"]) for row in enriched)),
        "semantic_posture_counts": counter_rows(Counter(as_text(row["semantic_posture"]) for row in enriched)),
        "file_family_counts": counter_rows(Counter(as_text(row["file_family"]) for row in enriched)),
        "source_key_prefix_counts": counter_rows(Counter(as_text(row["source_key_prefix"]) for row in enriched)),
        "whole_lane_blockers": whole_lane_blockers,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "lifecycle_now_recommended": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
    }

    spec = {
        "schema_version": 1,
        "source": SOURCE,
        "spec_type": "preliminary_sublane_policy_design",
        "registration_recommended_now": False,
        "apply_allowed": False,
        "production_full_allowed": False,
        "lifecycle_allowed_now": False,
        "base_lane": BASE_LANE,
        "selected_sublane": best["key"] if best else "",
        "selected_sublane_count": best["count"] if best else 0,
        "policy_shape": "narrow_readonly_lifecycle_review_candidate" if best else "manual_sampling_required",
        "required_next_prompt": recommended_next_step,
        "hard_exclusions": [
            "Select_CString",
            "ES_* helpers",
            "dynamic gender or perspective branch",
            "spanish residue context risk",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "source/output token signature mismatch",
        ],
        "notes": [
            "Do not close the full semantic_review_policy_design_candidate lane.",
            "Review the selected sublane before proposing any lifecycle bridge.",
            "No output apply is expected for this architecture step.",
        ],
    }

    txt_path, jsonl_path, summary_path, spec_path = output_paths()
    write_outputs(
        txt_path=txt_path,
        jsonl_path=jsonl_path,
        summary_path=summary_path,
        spec_path=spec_path,
        rows=enriched,
        samples=samples,
        summary=summary,
        spec=spec,
    )
    print(txt_path)
    print(jsonl_path)
    print(summary_path)
    print(spec_path)


if __name__ == "__main__":
    main()
