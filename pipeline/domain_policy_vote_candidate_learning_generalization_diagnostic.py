from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_learning_generalization_diagnostic_v1"
QUEUE_SOURCES = (
    "domain-policy-vote-candidate-human-packet-v3",
    "domain-policy-vote-candidate-human-packet-v4",
    "domain-policy-vote-candidate-human-packet-v5",
    "domain-policy-vote-candidate-human-packet-v6",
    "domain-policy-vote-candidate-human-packet-v7",
)
CURRENT_SEGMENT_STATE_RUN_ID = 493

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z][A-Za-z0-9_:.{};,|]*|#!|@[A-Za-z0-9_]+!|"
    r"Select_CString\([^)]*\)|\.Custom\('ES_[A-Za-z0-9_]+'\)|"
    r"\b(?:ROOT|FROM|SCOPE|TARGET)\.|Get[A-Za-z0-9_]+"
)
SPANISH_RESIDUE_RE = re.compile(
    r"\b(?:cielos|maravilloso|hacerte|hacerle|eres|estancia|galard[oó]n|"
    r"coste|actual|siguiente|elige|del|la|los|las|tu|tus|su|sus)\b",
    re.IGNORECASE,
)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_learned(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in QUEUE_SOURCES)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                lc.id AS candidate_id,
                lc.run_id AS learning_run_id,
                lc.segment_id,
                lc.queue_source,
                lc.match_type,
                lc.current_output_text,
                lc.suggested_text,
                s.relative_path,
                s.source_key,
                s.english_text,
                s.spanish_text,
                state.final_state,
                state.state_group,
                state.needs_output_apply
            FROM local_learning_candidates lc
            JOIN source_segments s ON s.id = lc.segment_id
            LEFT JOIN segment_state_items state
              ON state.segment_id = lc.segment_id
             AND state.run_id = ?
            WHERE lc.queue_source IN ({placeholders})
              AND lc.human_label = 'correct'
              AND lc.local_status = 'high_confidence'
            ORDER BY lc.segment_id, lc.id
            """,
            (CURRENT_SEGMENT_STATE_RUN_ID, *QUEUE_SOURCES),
        )
    ]


def domain(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(k) or "") for k in ("relative_path", "source_key", "english_text", "spanish_text"))
    if re.search(r"\b(?:faith|religion|doctrine|holy|caliph|prayer|dakhma|orthodoxy)\b", text, re.I):
        return "religion_faith_doctrine"
    if re.search(r"\b(?:culture|tradition|innovation|cultural|maa|bow|steel|guild|rice)\b", text, re.I):
        return "culture_tradition_innovation"
    if re.search(r"\b(?:realm|kingdom|empire|county|barony|liege|vassal|governor|sovereign)\b", text, re.I):
        return "title_realm_governance"
    return "other_domain"


def token_surface(text: str) -> str:
    token_count = len(TOKEN_RE.findall(text))
    if "Select_CString(" in text or ".Custom('ES_" in text:
        return "high_branching_token"
    if "\n" in text:
        return "multiline"
    if token_count >= 4:
        return "high_structural_token_density"
    if token_count >= 2:
        return "medium_dynamic_dense"
    if token_count == 1:
        return "medium_dynamic_light"
    return "plain_text"


def risk_surface(text: str) -> str:
    if token_surface(text) in {"high_branching_token", "high_structural_token_density", "multiline"}:
        return "structural_risk"
    if SPANISH_RESIDUE_RE.search(text):
        return "spanish_residue_context"
    return "low_plain_domain"


def pattern_label(old: str, new: str, match_type: str) -> str:
    if match_type.endswith("_already_ok"):
        return "already_ok_confirmation"
    old_l = old.lower()
    new_l = new.lower()
    if old_l == new_l:
        return "case_or_punctuation_only"
    if re.search(r"\b(excitado|designs|carcereiro|arcos mestres|fazem|farta|godos|tutor)\b", old_l):
        return "lexical_false_friend_or_wrong_term"
    if re.search(r"\b(é|são|faz|feitos|cada uma|fartas|alvarás|anteparo)\b", new_l) and old != new:
        return "grammar_agreement_or_number"
    if len(new) > len(old) * 1.18 or len(old) > len(new) * 1.18:
        return "semantic_rewrite_or_completion"
    if any(mark in old + new for mark in ['"', "'", "“", "”"]):
        return "quote_or_punctuation_normalization"
    return "style_smoothing"


def top(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_summary(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        old = str(row.get("current_output_text") or "")
        new = str(row.get("suggested_text") or "")
        old_surface = token_surface(old)
        new_surface = token_surface(new)
        combined_surface = old_surface if old_surface == new_surface else f"{old_surface}->{new_surface}"
        old_risk = risk_surface(old)
        new_risk = risk_surface(new)
        combined_risk = "low_plain_domain" if old_risk == "low_plain_domain" and new_risk == "low_plain_domain" else f"{old_risk}->{new_risk}"
        rec = {
            **row,
            "domain": domain(row),
            "token_surface": combined_surface,
            "risk_surface": combined_risk,
            "pattern_label": pattern_label(old, new, str(row.get("match_type") or "")),
            "is_correction": old != new,
            "closed_now": row.get("state_group") == "closed",
        }
        records.append(rec)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        grouped[(rec["pattern_label"], rec["domain"], rec["token_surface"], rec["risk_surface"])].append(rec)

    group_rows: list[dict[str, Any]] = []
    for (pattern, dom, surface, risk), items in grouped.items():
        false_safe_risk = 0 if risk == "low_plain_domain" and surface == "plain_text" else len(items)
        correction_count = sum(1 for item in items if item["is_correction"])
        closed_count = sum(1 for item in items if item["closed_now"])
        recommended_action = (
            "resolver_dry_run_candidate"
            if false_safe_risk == 0 and correction_count >= 3 and closed_count == len(items)
            else "keep_as_learning_signal_only"
        )
        group_rows.append(
            {
                "pattern_label": pattern,
                "domain": dom,
                "token_surface": surface,
                "risk_surface": risk,
                "learned_count": len(items),
                "correction_count": correction_count,
                "already_ok_count": len(items) - correction_count,
                "closed_count": closed_count,
                "false_safe_risk": false_safe_risk,
                "estimated_automatic_coverage": len(items) if recommended_action == "resolver_dry_run_candidate" else 0,
                "recommended_action": recommended_action,
                "sample_segment_ids": [int(item["segment_id"]) for item in items[:10]],
            }
        )
    group_rows.sort(key=lambda r: (r["recommended_action"] != "resolver_dry_run_candidate", -r["learned_count"], r["pattern_label"]))

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "queue_sources": list(QUEUE_SOURCES),
        "segment_state_run_id": CURRENT_SEGMENT_STATE_RUN_ID,
        "learned_count": len(records),
        "correction_count": sum(1 for rec in records if rec["is_correction"]),
        "already_ok_count": sum(1 for rec in records if not rec["is_correction"]),
        "closed_count": sum(1 for rec in records if rec["closed_now"]),
        "domain_counts": top(Counter(rec["domain"] for rec in records)),
        "pattern_counts": top(Counter(rec["pattern_label"] for rec in records)),
        "token_surface_counts": top(Counter(rec["token_surface"] for rec in records)),
        "risk_surface_counts": top(Counter(rec["risk_surface"] for rec in records)),
        "groups": group_rows,
        "resolver_dry_run_candidate_count": sum(1 for row in group_rows if row["recommended_action"] == "resolver_dry_run_candidate"),
        "estimated_total_automatic_coverage": sum(row["estimated_automatic_coverage"] for row in group_rows),
        "ran_apply": False,
        "ran_lifecycle": False,
        "ran_segment_state": False,
        "ran_reindex": False,
        "ran_production_full": False,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    return summary, records


def main() -> None:
    with connect_readonly() as conn:
        rows = fetch_learned(conn)
    summary, records = build_summary(rows)
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_learning_generalization_diagnostic"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    txt_path = base.with_suffix(".txt")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain policy vote candidate learning generalization diagnostic",
        f"learned_count={summary['learned_count']}",
        f"correction_count={summary['correction_count']}",
        f"already_ok_count={summary['already_ok_count']}",
        f"closed_count={summary['closed_count']}",
        f"resolver_dry_run_candidate_count={summary['resolver_dry_run_candidate_count']}",
        f"estimated_total_automatic_coverage={summary['estimated_total_automatic_coverage']}",
        "source_changed=false",
        "output_changed=false",
        "production_full_recommended_now=false",
        "",
        "candidate groups:",
    ]
    for group in summary["groups"][:20]:
        lines.append(
            f"- {group['recommended_action']} | count={group['learned_count']} | false_safe_risk={group['false_safe_risk']} | "
            f"{group['pattern_label']} | {group['domain']} | {group['token_surface']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"learned_count={summary['learned_count']}")
    print(f"correction_count={summary['correction_count']}")
    print(f"already_ok_count={summary['already_ok_count']}")
    print(f"resolver_dry_run_candidate_count={summary['resolver_dry_run_candidate_count']}")
    print(f"estimated_total_automatic_coverage={summary['estimated_total_automatic_coverage']}")
    print("source_changed=false")
    print("output_changed=false")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
