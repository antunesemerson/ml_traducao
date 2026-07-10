from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_review_general_plain_text_policy_review_v1"
INPUT_JSONL = Path("reports/20260624_203837_898189_semantic_review_router_pending_deep_diagnostic.jsonl")
ARCHITECTURE_SUMMARY = Path("reports/20260624_214204_233167_semantic_review_policy_design_candidate_architecture_summary.json")
ARCHITECTURE_SPEC = Path("reports/20260624_214204_233167_semantic_review_policy_design_candidate_architecture_spec.json")
SEGMENT_STATE_RUN_ID = 404
LEDGER_RUN_ID = 76
REVIEW_LIMIT = 240

ALLOWED_DECISIONS = {
    "plain_text_lifecycle_candidate",
    "needs_plain_text_human_semantic_review",
    "needs_plain_text_domain_policy",
    "needs_plain_text_context_window",
    "plain_text_blocked_spanish_residue",
    "plain_text_blocked_uncertain",
}

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+"
)
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:"
    r"adem[aá]s|ahora|alg[uú]n|aunque|caballero|cielos|consejo|coste|cualquier|"
    r"elige|eres|hacerte|hacerle|maravilloso|mientras|ning[uú]n|puede|pueden|"
    r"quieres|siguiente|tambi[eé]n|tus|vuestro|vuestra|vuestras|vuestros"
    r")\b",
    re.IGNORECASE,
)
DOMAIN_PATH_RE = re.compile(
    r"(?:^|/)(?:religion|culture|titles|landed_titles|historical_characters|dynasties|names|"
    r"government|laws|struggles)(?:/|_|$)",
    re.IGNORECASE,
)
DOMAIN_KEY_RE = re.compile(
    r"\b(?:religion|faith|doctrine|tenet|culture|tradition|heritage|innovation|title|"
    r"dynasty|house|historical|government|law|struggle)\b",
    re.IGNORECASE,
)
CONTEXT_PATH_RE = re.compile(r"(?:event_localization|dlc/|activities|schemes|travel|court)", re.IGNORECASE)
CONTEXT_TEXT_RE = re.compile(r"[\"“”]|\\n|(^|\s)(eu|meu|minha|nosso|nossa|você|tu|ele|ela)\b", re.IGNORECASE)
PORTUGUESE_SIGNAL_RE = re.compile(
    r"\b(?:a|o|as|os|de|do|da|dos|das|em|no|na|para|por|com|sem|um|uma|"
    r"este|esta|esse|essa|personagem|governante|cavaleiro|condado|reino|"
    r"propriedade|opini[aã]o|prest[ií]gio|piedade|controle)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_file(path: Path) -> Path:
    return db.project_path(str(path))


def output_paths() -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_general_plain_text_policy_review"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".jsonl"),
        reports_dir() / f"{base.name}_spec.json",
        reports_dir() / f"{base.name}_summary.json",
    )


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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


def short(text: str | None, limit: int = 300) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def has_spanish_residue(row: dict[str, Any]) -> bool:
    current = str(row.get("current_output_text") or "")
    if SPANISH_RESIDUE_RE.search(current):
        return True
    spanish = str(row.get("spanish_text") or "")
    return bool(current.strip() and spanish.strip() and current.strip().casefold() == spanish.strip().casefold())


def has_token_or_dynamic(row: dict[str, Any]) -> bool:
    current = str(row.get("current_output_text") or "")
    return (
        bool(TOKEN_RE.search(current))
        or int(row.get("token_count") or 0) != 0
        or int(row.get("bracket_token_count") or 0) != 0
        or int(row.get("variable_count") or 0) != 0
        or "Select_CString" in current
        or "ES_" in current
        or "$" in current
        or "[" in current
        or "]" in current
        or "#!" in current
    )


def select_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        if row.get("policy_lane") != "semantic_review_policy_design_candidate":
            continue
        if row.get("surface_bucket") != "general_semantic_prose":
            continue
        if row.get("risk_bucket") != "low_plain_text":
            continue
        if int(row.get("token_count") or 0) != 0:
            continue
        if int(row.get("bracket_token_count") or 0) != 0:
            continue
        if int(row.get("variable_count") or 0) != 0:
            continue
        if int(row.get("confirmed_matches_output") or 0) != 1:
            continue
        if has_token_or_dynamic(row) or has_spanish_residue(row):
            continue
        selected.append(row)
    selected.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    return selected


def load_state(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
            segment_id,
            state_group,
            final_state,
            review_state,
            is_closed,
            needs_output_apply,
            confirmed_matches_output,
            locked
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (SEGMENT_STATE_RUN_ID, *ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    current = str(row.get("current_output_text") or "")
    relative_path = str(row.get("relative_path") or "")
    source_key = str(row.get("source_key") or "")
    text_len = len(current)
    multiline = "\\n" in current or "\n" in current
    domain = bool(DOMAIN_PATH_RE.search(relative_path) or DOMAIN_KEY_RE.search(source_key))
    context_heavy = bool(CONTEXT_PATH_RE.search(relative_path) and (text_len > 180 or CONTEXT_TEXT_RE.search(current)))
    portuguese_signal = bool(PORTUGUESE_SIGNAL_RE.search(current))
    state_ok = bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("is_closed") or 0) == 0
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("locked") or 0) == 0
    )
    current_equals_old = str(row.get("old_text") or "").strip() == current.strip()
    current_equals_spanish = str(row.get("spanish_text") or "").strip().casefold() == current.strip().casefold()

    decision = "plain_text_lifecycle_candidate"
    reason = "plain text already matches confirmed output; no token, no variable, no Spanish residue, no output apply needed"
    false_safe_risk = False
    requires_lifecycle_later = True
    requires_apply_later = False

    if not state_ok:
        decision = "plain_text_blocked_uncertain"
        reason = "current segment-state guard failed"
        false_safe_risk = True
        requires_lifecycle_later = False
    elif has_token_or_dynamic(row):
        decision = "plain_text_blocked_uncertain"
        reason = "token or dynamic marker found after plain-text filter"
        false_safe_risk = True
        requires_lifecycle_later = False
    elif has_spanish_residue(row) or current_equals_spanish:
        decision = "plain_text_blocked_spanish_residue"
        reason = "Spanish residue signal or output equals Spanish source"
        false_safe_risk = True
        requires_lifecycle_later = False
    elif not current_equals_old:
        decision = "plain_text_blocked_uncertain"
        reason = "current output no longer matches source old_text from diagnostic"
        false_safe_risk = True
        requires_lifecycle_later = False
    elif domain:
        decision = "needs_plain_text_domain_policy"
        reason = "plain text but domain path/source-key needs narrow domain policy before lifecycle closure"
        requires_lifecycle_later = False
    elif context_heavy or multiline or text_len > 260:
        decision = "needs_plain_text_context_window"
        reason = "plain text but event/prose context is broad enough to require a context window"
        requires_lifecycle_later = False
    elif not portuguese_signal:
        decision = "needs_plain_text_human_semantic_review"
        reason = "plain text is token-safe but lacks enough Portuguese plausibility signal for automated lifecycle closure"
        requires_lifecycle_later = False

    if decision not in ALLOWED_DECISIONS:
        raise AssertionError(f"unexpected decision: {decision}")
    return {
        "segment_id": int(row["segment_id"]),
        "decision": decision,
        "reason": reason,
        "requires_apply_later": requires_apply_later,
        "requires_lifecycle_later": requires_lifecycle_later,
        "false_safe_risk": false_safe_risk,
        "relative_path": relative_path,
        "source_key": source_key,
        "source_line_number": row.get("source_line_number"),
        "state_group": state.get("state_group") if state else None,
        "final_state": state.get("final_state") if state else row.get("final_state"),
        "review_state": state.get("review_state") if state else row.get("review_state"),
        "needs_output_apply": int(state.get("needs_output_apply") or 0) if state else None,
        "confirmed_matches_output": int(state.get("confirmed_matches_output") or 0) if state else int(row.get("confirmed_matches_output") or 0),
        "current_equals_old": current_equals_old,
        "current_equals_spanish": current_equals_spanish,
        "has_spanish_residue_signal": has_spanish_residue(row),
        "has_token_or_dynamic": has_token_or_dynamic(row),
        "domain_signal": domain,
        "context_signal": context_heavy or multiline or text_len > 260,
        "portuguese_signal": portuguese_signal,
        "text_length": text_len,
        "english_text": short(row.get("english_text")),
        "spanish_text": short(row.get("spanish_text")),
        "current_output_text": short(current),
    }


def counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def build_summary(reviewed: list[dict[str, Any]], selected_unlimited_count: int) -> dict[str, Any]:
    decision_counts = Counter(row["decision"] for row in reviewed)
    file_counts = Counter(row["relative_path"] for row in reviewed)
    prefix_counts = Counter(str(row["source_key"] or "").split("_", 1)[0] for row in reviewed)
    plain_lifecycle = decision_counts["plain_text_lifecycle_candidate"]
    human_or_context = (
        decision_counts["needs_plain_text_human_semantic_review"]
        + decision_counts["needs_plain_text_domain_policy"]
        + decision_counts["needs_plain_text_context_window"]
    )
    blocked = decision_counts["plain_text_blocked_spanish_residue"] + decision_counts["plain_text_blocked_uncertain"]
    false_safe = sum(1 for row in reviewed if row["false_safe_risk"])
    requires_apply = sum(1 for row in reviewed if row["requires_apply_later"])
    requires_lifecycle = sum(1 for row in reviewed if row["requires_lifecycle_later"])
    register_policy_now = plain_lifecycle >= 120 and false_safe == 0
    if register_policy_now:
        recommendation = "recommend_narrow_readonly_lifecycle_policy"
        next_prompt = "chat_arch_semantic_review_general_plain_text_lifecycle_bridge_policy_prompt.md"
        terminalized = True
    elif human_or_context >= plain_lifecycle and human_or_context >= blocked:
        recommendation = "recommend_human_or_context_review_packet"
        next_prompt = "chat_exec_semantic_review_general_plain_text_human_context_packet_prompt.md"
        terminalized = False
    else:
        recommendation = "recommend_narrower_sublane_split"
        next_prompt = "chat_exec_semantic_review_general_plain_text_sublane_split_prompt.md"
        terminalized = False
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "ledger_run_id": LEDGER_RUN_ID,
        "input_jsonl": str(project_file(INPUT_JSONL)),
        "architecture_summary": str(project_file(ARCHITECTURE_SUMMARY)),
        "architecture_spec": str(project_file(ARCHITECTURE_SPEC)),
        "base_sublane": "general_plain_text_semantic_reopen",
        "selected_unlimited_count": selected_unlimited_count,
        "review_limit": REVIEW_LIMIT,
        "reviewed_count": len(reviewed),
        "decision_counts": counter_rows(decision_counts),
        "plain_text_lifecycle_candidate_count": plain_lifecycle,
        "human_or_context_count": human_or_context,
        "blocked_count": blocked,
        "requires_apply_later_count": requires_apply,
        "requires_lifecycle_later_count": requires_lifecycle,
        "false_safe_risk_count": false_safe,
        "register_policy_now": register_policy_now,
        "semantic_reopen_plain_text_terminalized_as_lifecycle_readonly": terminalized,
        "production_full_recommended_now": False,
        "apply_ready_now": 0,
        "discovery_recommended_now": False,
        "retarget_recommended_now": False,
        "top_files": counter_rows(file_counts)[:20],
        "top_source_key_prefixes": counter_rows(prefix_counts)[:20],
        "recommendation": recommendation,
        "next_prompt_recommended": next_prompt,
    }


def build_spec(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": SOURCE,
        "spec_type": "readonly_lifecycle_policy_review_result",
        "policy_candidate_key": "semantic_review_general_plain_text_reopen_lifecycle",
        "base_sublane": summary["base_sublane"],
        "register_policy_now": summary["register_policy_now"],
        "registration_allowed_by_exec": False,
        "apply_allowed": False,
        "lifecycle_allowed_now": False,
        "production_full_allowed": False,
        "hard_guards": [
            "segment_state_run_id == 404",
            "ledger_run_id == 76",
            "state_group == pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
            "current_output_text == old_text",
            "token_count == 0",
            "bracket_token_count == 0",
            "variable_count == 0",
            "no Select_CString",
            "no ES_* helper",
            "no CK3 brackets, variables, tags or scope getters",
            "no Spanish residue signal",
        ],
        "review_result": {
            "reviewed_count": summary["reviewed_count"],
            "plain_text_lifecycle_candidate_count": summary["plain_text_lifecycle_candidate_count"],
            "false_safe_risk_count": summary["false_safe_risk_count"],
            "recommendation": summary["recommendation"],
        },
        "next_prompt_recommended": summary["next_prompt_recommended"],
    }


def write_outputs(reviewed: list[dict[str, Any]], summary: dict[str, Any], spec: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    txt_path, jsonl_path, spec_path, summary_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    lines = [
        "semantic review general plain text policy review",
        f"source={SOURCE}",
        f"segment_state_run_id={SEGMENT_STATE_RUN_ID}",
        f"ledger_run_id={LEDGER_RUN_ID}",
        f"base_sublane={summary['base_sublane']}",
        f"selected_unlimited_count={summary['selected_unlimited_count']}",
        f"reviewed_count={summary['reviewed_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"plain_text_lifecycle_candidate_count={summary['plain_text_lifecycle_candidate_count']}",
            f"human_or_context_count={summary['human_or_context_count']}",
            f"blocked_count={summary['blocked_count']}",
            f"requires_apply_later_count={summary['requires_apply_later_count']}",
            f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
            f"false_safe_risk_count={summary['false_safe_risk_count']}",
            f"register_policy_now={str(summary['register_policy_now']).lower()}",
            f"semantic_reopen_plain_text_terminalized_as_lifecycle_readonly={str(summary['semantic_reopen_plain_text_terminalized_as_lifecycle_readonly']).lower()}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"recommendation={summary['recommendation']}",
            f"next_prompt_recommended={summary['next_prompt_recommended']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, spec_path, summary_path


def main() -> None:
    input_path = project_file(INPUT_JSONL)
    architecture_summary_path = project_file(ARCHITECTURE_SUMMARY)
    architecture_spec_path = project_file(ARCHITECTURE_SPEC)
    if not input_path.exists():
        raise SystemExit(f"input JSONL not found: {input_path}")
    if not architecture_summary_path.exists() or not architecture_spec_path.exists():
        raise SystemExit("architecture artifacts not found")
    arch_summary = read_json(architecture_summary_path)
    arch_spec = read_json(architecture_spec_path)
    if arch_summary.get("best_candidate_sublane") != "general_plain_text_semantic_reopen":
        raise SystemExit("architecture summary guard failed: best_candidate_sublane")
    if bool(arch_spec.get("apply_allowed")) or bool(arch_spec.get("production_full_allowed")):
        raise SystemExit("architecture spec guard failed: apply/production must be false")

    rows = read_jsonl(input_path)
    selected_unlimited = select_rows(rows)
    selected = selected_unlimited[:REVIEW_LIMIT]
    ids = [int(row["segment_id"]) for row in selected]
    with connect_readonly() as conn:
        states = load_state(conn, ids)
    reviewed = [classify(row, states.get(int(row["segment_id"]))) for row in selected]
    summary = build_summary(reviewed, len(selected_unlimited))
    spec = build_spec(summary)
    txt_path, jsonl_path, spec_path, summary_path = write_outputs(reviewed, summary, spec)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"spec={spec_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"plain_text_lifecycle_candidate_count={summary['plain_text_lifecycle_candidate_count']}")
    print(f"false_safe_risk_count={summary['false_safe_risk_count']}")
    print(f"register_policy_now={summary['register_policy_now']}")
    print(f"next_prompt_recommended={summary['next_prompt_recommended']}")


if __name__ == "__main__":
    main()
