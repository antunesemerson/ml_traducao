from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "activity_context_composer_guarded_dry_run_v1"
READY_DECISIONS = {
    "activity_context_ready_medium_plain_context",
    "activity_context_ready_short_plain_context",
    "activity_context_ready_light_token_composer",
    "activity_context_ready_tournament_short_context",
}
EXCLUDED_DECISIONS = {
    "needs_activity_long_context_human_or_composer",
    "needs_ptbr_fluency_review",
}
ALLOWED_OUTPUT_DECISIONS = {
    "composer_candidate_contextual_minor_repair",
    "composer_candidate_ptbr_naturalness",
    "guarded_no_apply_activity_context_ok",
    "blocked_by_context_ambiguity",
    "blocked_by_token_integrity",
    "blocked_by_long_context",
    "blocked_by_fluency_review",
}
SAMPLE_PER_DECISION = 8

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET|CHARACTER|THIS)\.|Get[A-Za-z0-9_]+|"
    r"ScriptValue|Concept|AddLocalizationIf|SelectLocalization",
    re.IGNORECASE,
)
SPANISH_RE = re.compile(
    r"\b(?:adem[aá]s|ahora|alg[uú]n|aunque|caballero|cielos|consejo|coste|cualquier|"
    r"elige|eres|hacerte|hacerle|maravilloso|mientras|ning[uú]n|puede|pueden|quieres|"
    r"siguiente|tambi[eé]n|vuestro|vuestra|vuestras|vuestros)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


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


def short(text: str | None, limit: int = 360) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def token_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def structure_signature(text: str) -> dict[str, Any]:
    value = text or ""
    return {
        "tokens": token_signature(value),
        "newlines": value.count("\n") + value.count("\\n"),
        "brackets_open": value.count("["),
        "brackets_close": value.count("]"),
        "dollars": value.count("$"),
        "hashes": value.count("#"),
        "pipes": value.count("|"),
    }


def structure_preserved(original: str, candidate: str) -> bool:
    return structure_signature(original) == structure_signature(candidate)


def word_count(text: str) -> int:
    return len(WORD_RE.findall(TOKEN_RE.sub(" ", text or "")))


def load_inputs() -> tuple[Path, Path | None, list[dict[str, Any]], dict[str, Any]]:
    jsonl_path = latest_file("*_activity_context_composer_architecture.jsonl")
    if not jsonl_path:
        raise SystemExit("missing *_activity_context_composer_architecture.jsonl")
    summary_path = latest_file("*_activity_context_composer_architecture_summary.json")
    summary = read_json(summary_path) if summary_path else {}
    rows = read_jsonl(jsonl_path)
    return jsonl_path, summary_path, rows, summary


def validate_architecture(rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    if summary:
        expected = {
            "reviewed_count": 105,
            "composer_ready_for_dry_run_count": 77,
            "hold_count": 28,
            "direct_lifecycle_candidate_count": 0,
        }
        for key, value in expected.items():
            if int(summary.get(key) or 0) != value:
                raise SystemExit(f"architecture summary guard failed: {key}")
        for key in (
            "apply_allowed_now",
            "lifecycle_allowed_now",
            "registry_registration_recommended_now",
            "production_full_recommended_now",
        ):
            if bool(summary.get(key)):
                raise SystemExit(f"architecture summary guard failed: {key}")
    ready = [row for row in rows if row.get("allowed_for_next_composer_dry_run") is True]
    hold = [row for row in rows if row.get("allowed_for_next_composer_dry_run") is not True]
    if len(ready) != 77 or len(hold) != 28:
        raise SystemExit(f"architecture JSONL count guard failed: ready={len(ready)} hold={len(hold)}")


def fetch_live_state(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            s.state_group,
            s.final_state,
            s.needs_output_apply,
            s.confirmed_matches_output,
            s.review_state,
            src.english_text,
            src.spanish_text,
            src.old_text,
            o.portuguese_text AS current_output_text
        FROM segment_state_items s
        LEFT JOIN source_segments src ON src.id = s.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.segment_id
        WHERE s.run_id = 406
          AND s.segment_id IN ({placeholders})
        """,
        tuple(ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def select_ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        decision = str(row.get("review_decision") or "")
        if row.get("allowed_for_next_composer_dry_run") is not True:
            continue
        if decision not in READY_DECISIONS:
            raise SystemExit(f"unexpected ready decision: {decision} segment={row.get('segment_id')}")
        if decision in EXCLUDED_DECISIONS:
            raise SystemExit(f"excluded decision leaked into ready set: {decision}")
        selected.append(row)
    selected.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    return selected


def propose(text: str, row: dict[str, Any]) -> tuple[str, str, str]:
    candidate = text
    reasons: list[str] = []
    # Only deterministic, tiny PT-BR cleanups. No semantic rewrites, no inferred context.
    replacements = [
        ("em um renomado centro de estudos", "em um renomado centro de estudo", "singular naturalness for 'centro de estudo'"),
        ("no meu Nerge", "para meu Nerge", "preposition naturalness"),
        ("Juntem-se a mim", "Junte-se a mim", "imperative agreement naturalness"),
        ("experiência que estávamos buscando", "experiência que procurávamos", "minor naturalness"),
        ("A discrição é a melhor parte da coragem", "A prudência é a melhor parte da coragem", "idiom naturalness"),
    ]
    for before, after, reason in replacements:
        if before in candidate:
            candidate = candidate.replace(before, after, 1)
            reasons.append(reason)
            break
    if candidate != text:
        return candidate, "composer_candidate_ptbr_naturalness", "; ".join(reasons)
    return text, "guarded_no_apply_activity_context_ok", "current output is acceptable or any improvement would require context"


def classify(row: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    original = str(row.get("current_output_text") or "")
    live_text = str((live or {}).get("current_output_text") or "")
    decision = "guarded_no_apply_activity_context_ok"
    reason = "current output is acceptable or any improvement would require context"
    candidate_text = original
    false_safe_risk = False

    state_ok = bool(
        live
        and live.get("state_group") == "pending"
        and int(live.get("needs_output_apply") or 0) == 0
        and int(live.get("confirmed_matches_output") or 0) == 1
    )
    if not state_ok or live_text != original:
        decision = "blocked_by_context_ambiguity"
        reason = "live state/text guard failed"
        false_safe_risk = True
    elif str(row.get("review_decision") or "") in EXCLUDED_DECISIONS:
        decision = "blocked_by_fluency_review"
        reason = "excluded architecture decision"
    elif word_count(original) > 36 or "\\n" in original or "\n" in original:
        decision = "blocked_by_long_context"
        reason = "long or multiline context was not eligible for this dry-run"
    elif not structure_preserved(original, original):
        decision = "blocked_by_token_integrity"
        reason = "source structure signature is internally inconsistent"
        false_safe_risk = True
    elif SPANISH_RE.search(original):
        decision = "blocked_by_context_ambiguity"
        reason = "Spanish residue signal requires human context"
        false_safe_risk = True
    else:
        candidate_text, decision, reason = propose(original, row)
        if candidate_text != original and not structure_preserved(original, candidate_text):
            decision = "blocked_by_token_integrity"
            reason = "candidate does not preserve token/structure signature"
            false_safe_risk = True
            candidate_text = original
        elif candidate_text != original and word_count(original) > 28:
            decision = "blocked_by_context_ambiguity"
            reason = "candidate change found, but text is too context-heavy for safe dry-run suggestion"
            candidate_text = original

    if decision not in ALLOWED_OUTPUT_DECISIONS:
        raise AssertionError(f"unexpected decision: {decision}")
    return {
        "segment_id": int(row["segment_id"]),
        "decision": decision,
        "reason": reason,
        "source_architecture": SOURCE,
        "review_decision": row.get("review_decision"),
        "architecture_decision": row.get("architecture_decision"),
        "activity_family": row.get("activity_family"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "requires_apply_later": False,
        "requires_lifecycle_later": False,
        "false_safe_risk": false_safe_risk,
        "token_integrity_ok": structure_preserved(original, candidate_text),
        "has_suggestion": candidate_text != original and decision.startswith("composer_candidate_"),
        "current_output_text": original,
        "candidate_text": candidate_text,
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "word_count": int(row.get("word_count") or word_count(original)),
        "token_count": int(row.get("token_count") or len(token_signature(original))),
    }


def counter_list(counter: Counter[str], limit: int = 30) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_summary(reviewed: list[dict[str, Any]], input_path: Path, summary_path: Path | None) -> dict[str, Any]:
    decision_counts = Counter(row["decision"] for row in reviewed)
    suggestion_candidates = sum(1 for row in reviewed if row["has_suggestion"])
    guarded_no_apply = decision_counts["guarded_no_apply_activity_context_ok"]
    context_ambiguity = decision_counts["blocked_by_context_ambiguity"]
    token_blocked = decision_counts["blocked_by_token_integrity"]
    false_safe = sum(1 for row in reviewed if row["false_safe_risk"])
    human_packet = suggestion_candidates >= 10 and false_safe == 0
    if human_packet:
        next_prompt = "chat_exec_activity_context_composer_human_audit_packet_prompt.md"
        recommendation = "recommend_readonly_human_audit_packet"
    elif suggestion_candidates < 10:
        next_prompt = "chat_exec_post_activity_composer_hold_or_global_priority_prompt.md"
        recommendation = "hold_or_choose_new_cohort_strategy"
    else:
        next_prompt = "chat_exec_activity_context_composer_integrity_followup_prompt.md"
        recommendation = "hold_due_to_integrity_or_false_safe_risk"
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "input_summary_json": str(summary_path) if summary_path else None,
        "segment_state_run_id": 406,
        "ledger_run_id": 76,
        "total_reviewed": len(reviewed),
        "decision_counts": counter_list(decision_counts),
        "suggestion_candidates": suggestion_candidates,
        "guarded_no_apply": guarded_no_apply,
        "blocked_by_context_ambiguity": context_ambiguity,
        "blocked_by_token_integrity": token_blocked,
        "blocked_by_long_context": decision_counts["blocked_by_long_context"],
        "blocked_by_fluency_review": decision_counts["blocked_by_fluency_review"],
        "false_safe_risk_count": false_safe,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "human_audit_packet_recommended": human_packet,
        "recommendation": recommendation,
        "next_prompt_recommended": next_prompt,
    }


def write_outputs(reviewed: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_activity_context_composer_guarded_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "activity context composer guarded dry-run",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"total_reviewed={summary['total_reviewed']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"suggestion_candidates={summary['suggestion_candidates']}",
            f"guarded_no_apply={summary['guarded_no_apply']}",
            f"blocked_by_context_ambiguity={summary['blocked_by_context_ambiguity']}",
            f"blocked_by_token_integrity={summary['blocked_by_token_integrity']}",
            f"false_safe_risk_count={summary['false_safe_risk_count']}",
            f"requires_apply_later_count={summary['requires_apply_later_count']}",
            f"requires_lifecycle_later_count={summary['requires_lifecycle_later_count']}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"human_audit_packet_recommended={str(summary['human_audit_packet_recommended']).lower()}",
            f"recommendation={summary['recommendation']}",
            f"next_prompt_recommended={summary['next_prompt_recommended']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path, input_summary_path, rows, arch_summary = load_inputs()
    validate_architecture(rows, arch_summary)
    ready_rows = select_ready(rows)
    ids = [int(row["segment_id"]) for row in ready_rows]
    with connect_readonly() as conn:
        live = fetch_live_state(conn, ids)
    reviewed = [classify(row, live.get(int(row["segment_id"]))) for row in ready_rows]
    summary = build_summary(reviewed, input_path, input_summary_path)
    txt_path, jsonl_path, summary_path = write_outputs(reviewed, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"total_reviewed={summary['total_reviewed']}")
    print(f"suggestion_candidates={summary['suggestion_candidates']}")
    print(f"false_safe_risk_count={summary['false_safe_risk_count']}")
    print(f"human_audit_packet_recommended={summary['human_audit_packet_recommended']}")
    print(f"next_prompt_recommended={summary['next_prompt_recommended']}")


if __name__ == "__main__":
    main()
