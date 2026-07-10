from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PAIR = {"dynamic_ck3_expression_microagent", "gender_token_microagent"}
TOKEN_RE = re.compile(
    r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|Select_CString|Custom\(|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
ES_OA_RE = re.compile(r"ES_OA\b", re.IGNORECASE)
ES_XA_EA_RE = re.compile(r"ES_(?:XA|EA)\b", re.IGNORECASE)
ES_ELLA_RE = re.compile(r"ES_(?:ElLa|DelDela|AlAla)\b", re.IGNORECASE)
SELECT_CSTRING_RE = re.compile(r"Select_CString", re.IGNORECASE)
CUSTOM_RE = re.compile(r"Custom\(", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"culture|tradition|religion|faith|title|GetTitleByKey|trait|artifact|dynasty|house|nickname|"
    r"accolade|legend|capital|vassal|law|government|council|court_position|county",
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r"event|\.desc|desc\.|coronation|hunt_activity|journey_activity|roaming_activity|board_game|"
    r"contract_events|scheme|ongoing|outcome|memory|travel|activity|activities",
    re.IGNORECASE,
)
RESIDUAL_RE = re.compile(
    r"\b(?:descubriste|descubri[oó]|topaste|top[oó]|fracasaste|fracas[oó]|vassalas|vassalos|"
    r"amig[oa]|mejor|segur[oa]|casca|vazia|buf[oó]n|corte|del|de la|los|las)\b",
    re.IGNORECASE,
)
TOKEN_BOUNDARY_RE = re.compile(r"\w\?\w|[\[\]]{2,}|\$\s*\$")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_rows(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            li.segment_id,
            li.issue_family,
            li.issue_kind,
            li.issue_severity,
            li.evidence_text,
            li.relative_path,
            li.source_key,
            li.source_line_number,
            s.state_group,
            s.needs_output_apply,
            s.needs_reopen,
            s.confirmed_matches_output,
            s.is_closed,
            s.priority_score
        FROM ml_issue_ledger_items li
        JOIN segment_state_items s
          ON s.segment_id = li.segment_id
         AND s.run_id = ?
        WHERE li.run_id = ?
          AND li.status = 'open'
          AND s.state_group = 'pending'
          AND li.segment_id IN (
              SELECT segment_id
              FROM ml_issue_ledger_items
              WHERE run_id = ?
                AND status = 'open'
                AND issue_family = 'dynamic_ck3_expression_microagent'
          )
          AND li.segment_id IN (
              SELECT segment_id
              FROM ml_issue_ledger_items
              WHERE run_id = ?
                AND status = 'open'
                AND issue_family = 'gender_token_microagent'
          )
        """,
        (segment_state_run_id, ledger_run_id, ledger_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def group_segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(row)

    segments: list[dict[str, Any]] = []
    for segment_id, segment_rows in grouped.items():
        families = sorted({row["issue_family"] for row in segment_rows})
        dynamic_row = next(
            (row for row in segment_rows if row["issue_family"] == "dynamic_ck3_expression_microagent"),
            segment_rows[0],
        )
        evidence = as_text(dynamic_row.get("evidence_text")) or " ".join(
            as_text(row.get("evidence_text")) for row in segment_rows if row.get("evidence_text")
        )
        has_high_outside_pair = any(
            row["issue_family"] not in PAIR and row["issue_severity"] == "high" for row in segment_rows
        )
        segments.append(
            {
                "segment_id": segment_id,
                "key": dynamic_row["source_key"],
                "relative_path": dynamic_row["relative_path"],
                "source_line_number": dynamic_row["source_line_number"],
                "current_text": evidence,
                "open_issue_families": families,
                "issue_kinds": sorted({row["issue_kind"] for row in segment_rows}),
                "has_high_outside_pair": has_high_outside_pair,
                "needs_output_apply": int(dynamic_row["needs_output_apply"] or 0),
                "needs_reopen": int(dynamic_row["needs_reopen"] or 0),
                "confirmed_matches_output": int(dynamic_row["confirmed_matches_output"] or 0),
                "is_closed": int(dynamic_row["is_closed"] or 0),
                "priority_score": float(dynamic_row["priority_score"] or 0),
            }
        )
    return segments


def tokens_seen(text: str) -> list[str]:
    seen: list[str] = []
    checks = [
        ("ES_OA", ES_OA_RE),
        ("ES_XA_EA", ES_XA_EA_RE),
        ("ES_ElLa_DelDela", ES_ELLA_RE),
        ("Select_CString", SELECT_CSTRING_RE),
        ("Custom", CUSTOM_RE),
    ]
    for label, pattern in checks:
        if pattern.search(text):
            seen.append(label)
    return seen


def selection_score(segment: dict[str, Any]) -> tuple[int, int, int, int, float]:
    families = set(segment["open_issue_families"])
    text = segment["current_text"]
    exact_pair = 1 if families == PAIR else 0
    no_high_outside = 1 if not segment["has_high_outside_pair"] else 0
    surface_tokens = 1 if tokens_seen(text) else 0
    short_surface = 1 if len(text) <= 220 or re.search(r"_tt$|tooltip|_title$|_key$", segment["key"], re.IGNORECASE) else 0
    return (exact_pair, no_high_outside, surface_tokens, short_surface, float(segment["priority_score"]))


def ready_decision(segment: dict[str, Any]) -> str | None:
    text = segment["current_text"]
    haystack = " ".join([segment["relative_path"], segment["key"], text])
    if segment["has_high_outside_pair"]:
        return None
    if segment["needs_output_apply"] != 0 or segment["confirmed_matches_output"] != 1 or segment["is_closed"] != 0:
        return None
    if segment["needs_reopen"] != 1:
        return None
    if RESIDUAL_RE.search(text) or DOMAIN_RE.search(haystack) or EVENT_RE.search(haystack):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if SELECT_CSTRING_RE.search(text):
        return "dynamic_gender_ready_select_cstring_false_reopen"
    if CUSTOM_RE.search(text):
        return "dynamic_gender_ready_custom_loc_false_reopen"
    if ES_OA_RE.search(text):
        return "dynamic_gender_ready_es_oa_false_reopen"
    if len(text) <= 160:
        return "dynamic_gender_ready_short_surface_lifecycle"
    return None


def policy_decision(segment: dict[str, Any]) -> tuple[str, str]:
    text = segment["current_text"]
    haystack = " ".join([segment["relative_path"], segment["key"], text])
    long_event = len(text) > 240 and EVENT_RE.search(haystack)
    if long_event:
        return "needs_event_context_composer", "long_event_context"
    if RESIDUAL_RE.search(text):
        return "needs_residual_repair", "visible_spanish_or_fluency_residual"
    if DOMAIN_RE.search(haystack):
        return "needs_domain_context", "sensitive_domain_context"
    if SELECT_CSTRING_RE.search(text) and (ES_OA_RE.search(text) or ES_XA_EA_RE.search(text) or ES_ELLA_RE.search(text) or CUSTOM_RE.search(text)):
        return "needs_select_cstring_gender_composer", "select_cstring_gender_surface"
    if ES_ELLA_RE.search(text):
        return "needs_es_ella_deldela_policy", "es_ella_deldela_surface"
    if ES_XA_EA_RE.search(text):
        return "needs_es_xa_ea_policy", "es_xa_ea_surface"
    if ES_OA_RE.search(text):
        return "needs_es_oa_policy", "es_oa_gender_surface"
    if CUSTOM_RE.search(text):
        return "needs_custom_loc_gender_policy", "custom_loc_gender_surface"
    if SELECT_CSTRING_RE.search(text):
        return "needs_select_cstring_gender_composer", "select_cstring_gender_surface"
    if EVENT_RE.search(haystack):
        return "needs_event_context_composer", "event_context"
    if TOKEN_RE.search(text):
        return "needs_dynamic_expression_agent", "dynamic_expression"
    return "blocked_uncertain", "blocked_uncertain"


def decide(segment: dict[str, Any]) -> dict[str, Any]:
    ready = ready_decision(segment)
    if ready:
        return {
            "decision": ready,
            "subpolicy": ready.removeprefix("dynamic_gender_ready_").removesuffix("_false_reopen").removesuffix("_lifecycle"),
            "tokens_seen": tokens_seen(segment["current_text"]),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "dynamic+gender surface appears aligned for future narrow lifecycle",
        }
    decision, subpolicy = policy_decision(segment)
    return {
        "decision": decision,
        "subpolicy": subpolicy,
        "tokens_seen": tokens_seen(segment["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": f"routed to {decision}; no apply or lifecycle emitted by this review",
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_combo_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]], universe: dict[str, int]) -> tuple[Path, Path, Counter[str], Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["decision"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy"] for row in rows)
    token_counts: Counter[str] = Counter(token for row in rows for token in row["tokens_seen"])
    ready_count = sum(1 for row in rows if row["decision"].startswith("dynamic_gender_ready_"))
    apply_count = sum(1 for row in rows if row["requires_apply_later"])

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    gender_policy_count = sum(
        decision_counts[key]
        for key in (
            "needs_es_oa_policy",
            "needs_es_xa_ea_policy",
            "needs_es_ella_deldela_policy",
            "needs_custom_loc_gender_policy",
            "needs_select_cstring_gender_composer",
        )
    )
    if ready_count >= 30:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif gender_policy_count >= 50:
        recommendation = "prepare_specific_dynamic_gender_policy_microagent"
    else:
        recommendation = "migrate_to_dynamic_ck3_expression_short_label_style"

    lines = [
        "Dynamic + gender combo review",
        "",
        "Universe:",
        f"- eligible_with_pair: {universe['eligible_with_pair']}",
        f"- exact_pair: {universe['exact_pair']}",
        f"- no_high_outside_pair: {universe['no_high_outside_pair']}",
        "",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(["", "Token/pattern counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(token_counts.items()))
    lines.extend(
        [
            "",
            f"ready_for_future_lifecycle: {ready_count}",
            f"apply_candidates_future: {apply_count}",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts, token_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--ledger-run-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()

    with connect_readonly() as conn:
        raw_rows = fetch_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    segments = group_segments(raw_rows)
    universe = {
        "eligible_with_pair": len(segments),
        "exact_pair": sum(1 for segment in segments if set(segment["open_issue_families"]) == PAIR),
        "no_high_outside_pair": sum(1 for segment in segments if not segment["has_high_outside_pair"]),
    }
    selected = sorted(segments, key=selection_score, reverse=True)[: args.limit]

    reviewed: list[dict[str, Any]] = []
    for segment in selected:
        reviewed.append(
            {
                "segment_id": segment["segment_id"],
                "key": segment["key"],
                "relative_path": segment["relative_path"],
                "current_text": segment["current_text"],
                "open_issue_families": segment["open_issue_families"],
                **decide(segment),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts, token_counts = write_reports(reviewed, universe)
    print(f"eligible_with_pair={universe['eligible_with_pair']}")
    print(f"exact_pair={universe['exact_pair']}")
    print(f"no_high_outside_pair={universe['no_high_outside_pair']}")
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={sum(1 for row in reviewed if row['decision'].startswith('dynamic_gender_ready_'))}")
    print(f"apply_candidates_future={sum(1 for row in reviewed if row['requires_apply_later'])}")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"token_counts={json.dumps(dict(token_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
