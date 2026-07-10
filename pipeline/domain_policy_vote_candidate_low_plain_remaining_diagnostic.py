from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_deep_diagnostic as deep_diagnostic
import domain_policy_vote_candidate_human_packet as human_packet


TARGET_LANE = "domain_policy_vote_candidate"
TARGET_RISK_SURFACE = "low_plain_domain"
BASE_SEGMENT_STATE_RUN_ID = 493
KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS = {10476, 10532, 10540, 39106}
RESOLVER_FILES = (
    Path("pipeline/domain_policy_vote_candidate_culture_tradition_grammar_resolver_dry_run.py"),
    Path("pipeline/domain_policy_vote_candidate_religion_faith_style_resolver_dry_run.py"),
    Path("pipeline/domain_policy_vote_candidate_religion_faith_grammar_resolver_dry_run.py"),
)

STRUCTURAL_RE = re.compile(
    r"(Select_CString|\.Custom\('ES_|Get(?:Name|FirstName|ShortUIName|HerHis|SheHe|WomanMan|GirlBoy)|\$[A-Z0-9_]+\$|\[[^\]]+\]|#(?:EMP|D|!|E|BER))"
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


def short(text: str | None, limit: int = 900) -> str:
    value = str(text or "")
    compact = value.replace("\r\n", "\\n").replace("\n", "\\n")
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def load_exact_rules() -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    for path in RESOLVER_FILES:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                (isinstance(target, ast.Name) and target.id == "EXACT_LEARNED_REPLACEMENTS")
                or (isinstance(target, ast.Attribute) and target.attr == "EXACT_LEARNED_REPLACEMENTS")
                for target in node.targets
            ):
                continue
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            for item in value:
                if isinstance(item, dict) and "old" in item and "new" in item:
                    rules.append({str(key): str(val) for key, val in item.items()})
    return rules


def text_blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("english_text", "spanish_text", "current_output_text", "relative_path", "source_key")
    )


def abstract_pattern_tags(row: dict[str, Any]) -> list[str]:
    current = str(row.get("current_output_text") or "")
    english = str(row.get("english_text") or "")
    spanish = str(row.get("spanish_text") or "")
    key = str(row.get("source_key") or "")
    tags: list[str] = []
    if re.search(r"\b(?:flavor pack|Music Pack|The Royal Court|Crusader Kings|Songs of the Realm)\b", current):
        tags.append("product_or_pack_name_localization")
    if re.search(r"\b(?:reino|domínio|suserano|senhorio|governo|lei romana)\b", current, re.IGNORECASE):
        tags.append("realm_governance_term_choice")
    if re.search(r"\b(?:fé|sagrado|santa|santo|Igreja|Diabo|deuses|piedade)\b", current, re.IGNORECASE):
        tags.append("religion_term_or_capitalization")
    if re.search(r"\b(?:cultura|tradição|ascendência|estrangeiros|herança)\b", current, re.IGNORECASE):
        tags.append("culture_term_choice")
    if re.search(r"\b(?:Heralds|flavor pack|Music Pack|East Mebon|The Canterbury Tales)\b", current):
        tags.append("unlocalized_or_style_name_surface")
    if re.search(r"\b(?:através deste condado|menor jogo|mal-apropriado|estábulos experientes|recebe crédito|domínio com o flavor pack)\b", current, re.IGNORECASE):
        tags.append("literal_or_awkward_portuguese")
    if len(current) <= 120:
        tags.append("short_plain_text")
    if len(current) >= 450:
        tags.append("long_plain_prose")
    if re.search(r"\b(?:this county|realm|faith|culture|tradition|kingdom)\b", english, re.IGNORECASE) and re.search(
        r"\b(?:señorío|fe|cultura|tradición|reino)\b", spanish, re.IGNORECASE
    ):
        tags.append("cross_source_domain_alignment")
    if re.search(r"(desc|modifier|decision|historical_character)", key, re.IGNORECASE):
        tags.append("descriptive_prose_key")
    return tags


def recommendation(row: dict[str, Any], exclusion_reasons: list[str], abstract_tags: list[str]) -> tuple[str, str]:
    if "already_learned_or_hold_or_corrected" in exclusion_reasons:
        return "hold_context", "already learned, corrected, rejected, or held locally"
    if "known_structural_blocked" in exclusion_reasons:
        return "hold_context", "known structural block from earlier preflight"
    if "open_high_severity_issue" in exclusion_reasons:
        return "hold_context", "open high-severity issue requires separate handling"
    if "structural_or_dynamic_surface" in exclusion_reasons:
        return "hold_context", "surface is not plain enough despite low bucket"
    if any(tag in abstract_tags for tag in ("product_or_pack_name_localization", "realm_governance_term_choice", "religion_term_or_capitalization", "culture_term_choice")):
        return "abstract_pattern_diagnostic", "may generalize, but needs non-exact pattern design and false-safe review"
    return "small_human_packet", "plain low-risk prose, no learned exact match, best handled by compact human review"


def exclusion_reasons(row: dict[str, Any], learned_or_hold: set[int], high_severity: set[int]) -> list[str]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    blob = text_blob(row)
    current = str(row.get("current_output_text") or "")
    if segment_id in learned_or_hold:
        reasons.append("already_learned_or_hold_or_corrected")
    if segment_id in KNOWN_STRUCTURAL_BLOCKED_SEGMENT_IDS:
        reasons.append("known_structural_blocked")
    if segment_id in high_severity:
        reasons.append("open_high_severity_issue")
    if STRUCTURAL_RE.search(blob) or deep_diagnostic.TOKEN_RE.findall(current):
        reasons.append("structural_or_dynamic_surface")
    return reasons


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    by_sublane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_sublane[str(row["surface_bucket"])].append(row)

    lines = [
        "domain_policy_vote_candidate low_plain_domain remaining diagnostic",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"ledger_run_id: {summary['ledger_run_id']}",
        f"remaining_low_plain_count: {summary['remaining_low_plain_count']}",
        f"reviewable_low_plain_count: {summary['reviewable_low_plain_count']}",
        f"exact_rule_count_checked: {summary['exact_rule_count_checked']}",
        "",
        "recommendation_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["recommendation_counts"])
    lines.extend(["", "sublane_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["sublane_counts"])
    lines.extend(["", "abstract_pattern_tag_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["abstract_pattern_tag_counts"])
    lines.extend(["", "items_by_sublane:"])

    for sublane in sorted(by_sublane):
        lines.extend(["", f"## {sublane}"])
        for row in by_sublane[sublane]:
            lines.extend(
                [
                    "",
                    f"### segment_id {row['segment_id']}",
                    f"- recommendation: {row['recommendation']}",
                    f"- recommendation_reason: {row['recommendation_reason']}",
                    f"- no_exact_match_reason: {row['no_exact_match_reason']}",
                    f"- abstract_pattern_tags: {', '.join(row['abstract_pattern_tags']) or 'none'}",
                    f"- exclusion_reasons: {', '.join(row['exclusion_reasons']) or 'none'}",
                    f"- source_key: {row.get('source_key')}",
                    f"- relative_path: {row.get('relative_path')}",
                    f"- english_text: {row.get('english_text')}",
                    f"- spanish_text: {row.get('spanish_text')}",
                    f"- current_output_text: {row.get('current_output_text')}",
                ]
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def main() -> None:
    run_ts = timestamp()
    exact_rules = load_exact_rules()
    with connect_readonly() as conn:
        segment_state_run_id = deep_diagnostic.latest_segment_state_run_id(conn)
        ledger_run_id = deep_diagnostic.latest_ledger_run_id(conn)
        if segment_state_run_id != BASE_SEGMENT_STATE_RUN_ID:
            raise SystemExit(f"latest segment_state_run_id is {segment_state_run_id}, expected {BASE_SEGMENT_STATE_RUN_ID}")
        preflight_path, preflight_excluded_ids = deep_diagnostic.load_preflight_exclusions()
        all_rows = [
            deep_diagnostic.enrich_row(row)
            for row in deep_diagnostic.fetch_rows(conn, segment_state_run_id, ledger_run_id, preflight_excluded_ids)
        ]
        low_rows = [row for row in all_rows if row.get("risk_bucket") == TARGET_RISK_SURFACE]
        learned_or_hold = human_packet.known_learned_or_hold_segment_ids(conn)
        high_severity = human_packet.high_severity_open_issue_segment_ids(conn, [int(row["segment_id"]) for row in low_rows])

    records: list[dict[str, Any]] = []
    for row in low_rows:
        current = str(row.get("current_output_text") or "")
        matching_rule_ids = [rule.get("rule_id", "") for rule in exact_rules if rule.get("old", "") and rule["old"] in current]
        exclusions = exclusion_reasons(row, learned_or_hold, high_severity)
        tags = abstract_pattern_tags(row)
        rec, reason = recommendation(row, exclusions, tags)
        records.append(
            {
                "segment_id": int(row["segment_id"]),
                "lane": TARGET_LANE,
                "surface_bucket": row.get("surface_bucket"),
                "risk_bucket": row.get("risk_bucket"),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "english_text": short(row.get("english_text")),
                "spanish_text": short(row.get("spanish_text")),
                "current_output_text": short(row.get("current_output_text")),
                "matching_exact_rule_ids": matching_rule_ids,
                "no_exact_match_reason": "no learned exact replacement substring matched" if not matching_rule_ids else "matched exact rule but no candidate generated in prior narrow resolver scope",
                "abstract_pattern_tags": tags,
                "exclusion_reasons": exclusions,
                "recommendation": rec,
                "recommendation_reason": reason,
            }
        )

    records.sort(key=lambda row: (str(row["surface_bucket"]), str(row["recommendation"]), int(row["segment_id"])))

    sublane_counts = Counter(str(row["surface_bucket"]) for row in records)
    recommendation_counts = Counter(str(row["recommendation"]) for row in records)
    tag_counts = Counter(tag for row in records for tag in row["abstract_pattern_tags"])
    reviewable_count = sum(1 for row in records if row["recommendation"] in {"small_human_packet", "abstract_pattern_diagnostic"})
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_diagnostic",
        "lane": TARGET_LANE,
        "risk_surface": TARGET_RISK_SURFACE,
        "segment_state_run_id": segment_state_run_id,
        "ledger_run_id": ledger_run_id,
        "preflight_exclusion_report": str(preflight_path) if preflight_path else None,
        "preflight_excluded_count": len(preflight_excluded_ids),
        "evaluated_pending_count": len(all_rows),
        "remaining_low_plain_count": len(records),
        "reviewable_low_plain_count": reviewable_count,
        "exact_rule_count_checked": len(exact_rules),
        "sublane_counts": top_counter(sublane_counts),
        "recommendation_counts": top_counter(recommendation_counts),
        "abstract_pattern_tag_counts": top_counter(tag_counts),
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "decision_options": {
            "option_1_small_human_packet": "recommended for rows marked small_human_packet",
            "option_2_non_exact_pattern_diagnostic": "recommended for rows marked abstract_pattern_diagnostic before any resolver",
            "option_3_hold_lane": "recommended only for rows marked hold_context, not for all low_plain_domain",
        },
        "output_files": {},
    }

    base = reports_dir() / f"{run_ts}_{TARGET_LANE}_{TARGET_RISK_SURFACE}_remaining_diagnostic"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, records)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
