from __future__ import annotations

import difflib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as deep_diagnostic
import domain_policy_vote_candidate_human_packet as human_packet
import domain_policy_vote_candidate_learning_generalization_diagnostic as learning_diagnostic


TARGET_LANE = "domain_policy_vote_candidate"
TARGET_SUBLANE = "culture_tradition_innovation"
TARGET_PATTERN = "grammar_agreement_or_number"
TARGET_TOKEN_SURFACE = "plain_text"
TARGET_RISK_SURFACE = "low_plain_domain"
BASE_SEGMENT_STATE_RUN_ID = 493

KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS = {10476, 10532, 10540, 39106}

STRUCTURAL_RE = re.compile(
    r"(Select_CString|\.Custom\('ES_|Get(?:Name|FirstName|ShortUIName|HerHis|SheHe|WomanMan|GirlBoy)|\$[A-Z0-9_]+\$|\[[^\]]+\]|#(?:EMP|D|!|E|BER))"
)

EXACT_LEARNED_REPLACEMENTS: tuple[dict[str, str], ...] = (
    {
        "rule_id": "cti_grammar_001_nao_importa_probabilidades",
        "old": "Não importa as probabilidades.",
        "new": "Não importam as probabilidades.",
    },
    {
        "rule_id": "cti_grammar_002_ao_ponto_em_que",
        "old": "ao ponto em que",
        "new": "a ponto de",
    },
    {
        "rule_id": "cti_grammar_003_terras_frequentemente_mudam",
        "old": "Terras frequentemente mudam de mãos",
        "new": "As terras frequentemente mudam de mãos",
    },
    {
        "rule_id": "cti_grammar_004_se_torne_joias",
        "old": "cada uma de suas metrópoles se torne joias invejáveis conhecidas",
        "new": "cada uma de suas metrópoles se torne uma joia invejável conhecida",
    },
    {
        "rule_id": "cti_grammar_005_defender_seu_proprio",
        "old": "Defender o que é seu próprio",
        "new": "Defender o que é seu",
    },
    {
        "rule_id": "cti_grammar_006_feito_arcos_compostos",
        "old": "Feito de tiras laminadas de chifre, madeira e tendões, nossos arcos compostos",
        "new": "Feitos de tiras laminadas de chifre, madeira e tendões, nossos arcos compostos",
    },
    {
        "rule_id": "cti_grammar_007_trabalho_dessa_gente",
        "old": "o trabalho árduo dessa gente os fizeram florescer",
        "new": "o trabalho árduo dessa gente a fez florescer",
    },
    {
        "rule_id": "cti_grammar_008_mais_facil_receber_confianca",
        "old": "são fáceis de serem confiados",
        "new": "são mais fáceis de receber confiança",
    },
    {
        "rule_id": "cti_grammar_009_tao_motivado_como",
        "old": "não é tão motivado como aqueles",
        "new": "não é tão motivado quanto aqueles",
    },
    {
        "rule_id": "cti_grammar_010_suas_terras_produzindo",
        "old": "suas terras produzindo colheitas abundantes",
        "new": "suas terras produzem colheitas abundantes",
    },
)


def reports_dir() -> Path:
    path = Path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def protected_tokens(text: str) -> list[str]:
    token_re = getattr(deep_diagnostic, "TOKEN_RE")
    return token_re.findall(text or "")


def is_plain_low_structural(text: str) -> bool:
    if "\n" in text or "\r" in text:
        return False
    if STRUCTURAL_RE.search(text):
        return False
    if protected_tokens(text):
        return False
    return True


def positive_examples(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for row in learning_diagnostic.fetch_learned(conn):
        old = row["current_output_text"] or ""
        new = row["suggested_text"] or ""
        if (
            learning_diagnostic.domain(row) == TARGET_SUBLANE
            and learning_diagnostic.pattern_label(old, new, row["match_type"]) == TARGET_PATTERN
            and learning_diagnostic.token_surface(old) == TARGET_TOKEN_SURFACE
            and learning_diagnostic.token_surface(new) == TARGET_TOKEN_SURFACE
            and learning_diagnostic.risk_surface(old) == TARGET_RISK_SURFACE
            and learning_diagnostic.risk_surface(new) == TARGET_RISK_SURFACE
            and old != new
        ):
            examples.append(
                {
                    "segment_id": int(row["segment_id"]),
                    "match_type": row["match_type"],
                    "old": old,
                    "new": new,
                }
            )
    return examples


def raw_diff(old: str, new: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="current_output_text",
            tofile="suggested_text",
            lineterm="",
        )
    )


def latest_runs(conn: sqlite3.Connection) -> tuple[int, int]:
    state_run_id = deep_diagnostic.latest_segment_state_run_id(conn)
    ledger_run_id = deep_diagnostic.latest_ledger_run_id(conn)
    return state_run_id, ledger_run_id


def fetch_pending_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> tuple[list[dict[str, Any]], Path | None, set[int]]:
    preflight_path, preflight_excluded_ids = deep_diagnostic.load_preflight_exclusions()
    rows = deep_diagnostic.fetch_rows(conn, segment_state_run_id, ledger_run_id, preflight_excluded_ids)
    enriched = [deep_diagnostic.enrich_row(row) for row in rows]
    return enriched, preflight_path, preflight_excluded_ids


def evaluate_row(row: dict[str, Any], learned_or_hold_ids: set[int], high_severity_ids: set[int], positive_ids: set[int]) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    current = str(row.get("current_output_text") or "")
    blob = "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "relative_path", "source_key")
    )

    if segment_id in positive_ids:
        reasons.append("positive_training_example")
    if segment_id in learned_or_hold_ids:
        reasons.append("already_learned_or_hold_or_corrected")
    if segment_id in KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS:
        reasons.append("known_structural_blocked")
    if segment_id in high_severity_ids:
        reasons.append("open_high_severity_issue")
    if row.get("surface_bucket") != TARGET_SUBLANE:
        reasons.append(f"wrong_sublane:{row.get('surface_bucket')}")
    if row.get("risk_bucket") != TARGET_RISK_SURFACE:
        reasons.append(f"wrong_risk:{row.get('risk_bucket')}")
    if not is_plain_low_structural(current) or STRUCTURAL_RE.search(blob):
        reasons.append("structural_or_dynamic_surface")

    matches = [rule for rule in EXACT_LEARNED_REPLACEMENTS if rule["old"] in current]
    if not matches:
        reasons.append("no_exact_learned_replacement_match")

    if reasons:
        return None, reasons

    suggested = current
    applied_rules = []
    for rule in matches:
        suggested = suggested.replace(rule["old"], rule["new"])
        applied_rules.append(rule["rule_id"])

    token_integrity_ok = protected_tokens(current) == protected_tokens(suggested)
    structure_integrity_ok = is_plain_low_structural(suggested) and current.count("\n") == suggested.count("\n")
    false_safe_risk = not (token_integrity_ok and structure_integrity_ok)
    if false_safe_risk:
        reasons.append("false_safe_risk_integrity_failure")
        return None, reasons

    return (
        {
            "segment_id": segment_id,
            "lane": TARGET_LANE,
            "sublane": TARGET_SUBLANE,
            "pattern": TARGET_PATTERN,
            "token_surface": TARGET_TOKEN_SURFACE,
            "risk_surface": TARGET_RISK_SURFACE,
            "relative_path": row.get("relative_path"),
            "source_key": row.get("source_key"),
            "source_line_number": row.get("source_line_number"),
            "rule_ids": applied_rules,
            "english_text": row.get("english_text"),
            "spanish_text": row.get("spanish_text"),
            "current_output_text": current,
            "suggested_text": suggested,
            "token_integrity_ok": token_integrity_ok,
            "structure_integrity_ok": structure_integrity_ok,
            "false_safe_risk": False,
            "requires_apply_later": False,
            "requires_lifecycle_later": False,
            "diff_preview": raw_diff(current, suggested),
        },
        [],
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    lines = [
        f"{summary['lane']} {summary['sublane']} {summary['pattern']} resolver dry-run",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"ledger_run_id: {summary['ledger_run_id']}",
        f"positive_example_count: {summary['positive_example_count']}",
        f"evaluated_pending_count: {summary['evaluated_pending_count']}",
        f"eligible_surface_count: {summary['eligible_surface_count']}",
        f"candidate_count: {summary['candidate_count']}",
        f"false_safe_risk_count: {summary['false_safe_risk_count']}",
        f"token_integrity_ok_count: {summary['token_integrity_ok_count']}",
        f"structure_integrity_ok_count: {summary['structure_integrity_ok_count']}",
        f"requires_apply_later_count: {summary['requires_apply_later_count']}",
        f"requires_lifecycle_later_count: {summary['requires_lifecycle_later_count']}",
        "",
        "gates:",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- segment_state: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        "rule_match_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["rule_match_counts"])
    lines.extend(["", "excluded_reason_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["excluded_reason_counts"][:30])
    if candidates:
        lines.extend(["", "diff_previews:"])
        for candidate in candidates:
            lines.extend(
                [
                    "",
                    f"## segment_id {candidate['segment_id']}",
                    f"- source_key: {candidate.get('source_key')}",
                    f"- relative_path: {candidate.get('relative_path')}",
                    f"- rule_ids: {', '.join(candidate['rule_ids'])}",
                    "```diff",
                    candidate["diff_preview"],
                    "```",
                ]
            )
    else:
        lines.extend(["", "diff_previews: none"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def top_counter(counter: Counter[str], limit: int = 40) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def main() -> None:
    run_ts = timestamp()
    with connect_readonly() as conn:
        latest_state_run_id, ledger_run_id = latest_runs(conn)
        if latest_state_run_id != BASE_SEGMENT_STATE_RUN_ID:
            raise SystemExit(f"latest segment_state_run_id is {latest_state_run_id}, expected {BASE_SEGMENT_STATE_RUN_ID}")
        examples = positive_examples(conn)
        positive_ids = {int(example["segment_id"]) for example in examples}
        rows, preflight_path, preflight_excluded_ids = fetch_pending_rows(conn, latest_state_run_id, ledger_run_id)
        learned_or_hold_ids = human_packet.known_learned_or_hold_segment_ids(conn)
        high_severity_ids = human_packet.high_severity_open_issue_segment_ids(conn, [int(row["segment_id"]) for row in rows])

    candidates: list[dict[str, Any]] = []
    excluded_reasons: Counter[str] = Counter()
    eligible_surface_count = 0
    for row in rows:
        if row.get("surface_bucket") == TARGET_SUBLANE and row.get("risk_bucket") == TARGET_RISK_SURFACE:
            eligible_surface_count += 1
        candidate, reasons = evaluate_row(row, learned_or_hold_ids, high_severity_ids, positive_ids)
        if candidate:
            candidates.append(candidate)
        else:
            for reason in reasons:
                excluded_reasons[reason] += 1

    rule_counts: Counter[str] = Counter()
    for candidate in candidates:
        rule_counts.update(candidate["rule_ids"])

    false_safe_risk_count = sum(1 for candidate in candidates if candidate["false_safe_risk"])
    token_integrity_ok_count = sum(1 for candidate in candidates if candidate["token_integrity_ok"])
    structure_integrity_ok_count = sum(1 for candidate in candidates if candidate["structure_integrity_ok"])
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "lane": TARGET_LANE,
        "sublane": TARGET_SUBLANE,
        "pattern": TARGET_PATTERN,
        "token_surface": TARGET_TOKEN_SURFACE,
        "risk_surface": TARGET_RISK_SURFACE,
        "mode": "dry_run_read_only",
        "segment_state_run_id": latest_state_run_id,
        "ledger_run_id": ledger_run_id,
        "preflight_exclusion_report": str(preflight_path) if preflight_path else None,
        "preflight_excluded_count": len(preflight_excluded_ids),
        "positive_example_count": len(examples),
        "positive_example_segment_ids": sorted(positive_ids),
        "learned_exact_replacement_rule_count": len(EXACT_LEARNED_REPLACEMENTS),
        "evaluated_pending_count": len(rows),
        "eligible_surface_count": eligible_surface_count,
        "candidate_count": len(candidates),
        "false_safe_risk_count": false_safe_risk_count,
        "token_integrity_ok_count": token_integrity_ok_count,
        "structure_integrity_ok_count": structure_integrity_ok_count,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "rule_match_counts": top_counter(rule_counts),
        "excluded_reason_counts": top_counter(excluded_reasons),
        "gates": {
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "output_files": {},
    }

    base = reports_dir() / f"{run_ts}_{TARGET_LANE}_{TARGET_SUBLANE}_{TARGET_PATTERN}_resolver_dry_run"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")

    write_jsonl(jsonl_path, candidates)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, candidates)

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
