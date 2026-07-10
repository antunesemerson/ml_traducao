from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


SOURCE = "domain_policy_vote_candidate_holy_site_token_changing_review_v1"
SEGMENT_STATE_RUN_ID = 526
SEGMENT_IDS = [237388, 239477, 239479, 239507, 239509, 239511]
EXPECTED_OUTPUT_TOKEN = "[holy_sites|lE]"
EXPECTED_CONFIRMED_TOKEN = "[holy_site|lE]"
EFFECT_NAME_RE = re.compile(r"^holy_site_[a-z0-9_]+_effect_name$")
HOLY_SITE_NAME_VAR_RE = re.compile(r"\$holy_site_[a-z0-9_]+_name\$")


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
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def holy_site_name_vars(value: str) -> list[str]:
    return HOLY_SITE_NAME_VAR_RE.findall(value)


def fetch_focus(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            s.english_text,
            s.spanish_text,
            o.output_line_number,
            o.portuguese_text AS output_text,
            c.confirmed_text
        FROM segment_state_items state
        JOIN source_segments s ON s.id = state.segment_id
        JOIN output_segments o ON o.segment_id = state.segment_id
        JOIN segment_confirmations c ON c.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        (SEGMENT_STATE_RUN_ID, *SEGMENT_IDS),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_effect_name_token_evidence(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            s.source_key,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS output_text
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        WHERE s.source_key LIKE 'holy_site_%_effect_name'
        """
    ).fetchall()
    counters = {
        "english_holy_site_singular": 0,
        "english_holy_sites_plural": 0,
        "spanish_source_holy_site_singular": 0,
        "spanish_source_holy_sites_plural": 0,
        "output_holy_site_singular": 0,
        "output_holy_sites_plural": 0,
    }
    for row in rows:
        english_text = str(row["english_text"] or "")
        spanish_text = str(row["spanish_text"] or "")
        output_text = str(row["output_text"] or "")
        counters["english_holy_site_singular"] += int("[holy_site|E]" in english_text)
        counters["english_holy_sites_plural"] += int("[holy_sites|E]" in english_text)
        counters["spanish_source_holy_site_singular"] += int("[holy_site|lE]" in spanish_text)
        counters["spanish_source_holy_sites_plural"] += int("[holy_sites|lE]" in spanish_text)
        counters["output_holy_site_singular"] += int("[holy_site|lE]" in output_text)
        counters["output_holy_sites_plural"] += int("[holy_sites|lE]" in output_text)
    return {
        "effect_name_row_count": len(rows),
        "token_usage_counts": counters,
    }


def classify(row: dict[str, Any]) -> dict[str, Any]:
    source_key = str(row["source_key"] or "")
    english_text = str(row["english_text"] or "")
    spanish_text = str(row["spanish_text"] or "")
    output_text = str(row["output_text"] or "")
    confirmed_text = str(row["confirmed_text"] or "")
    output_vars = holy_site_name_vars(output_text)
    confirmed_vars = holy_site_name_vars(confirmed_text)
    preserved_pipe_casing = EXPECTED_OUTPUT_TOKEN in output_text and EXPECTED_CONFIRMED_TOKEN in confirmed_text
    weak_wrapper_preserved = "#weak (" in output_text and ")#!" in output_text and "#weak (" in confirmed_text and ")#!" in confirmed_text
    source_key_ok = bool(EFFECT_NAME_RE.match(source_key))
    english_source_supports_singular = "[holy_site|E]" in english_text and "[holy_sites|E]" not in english_text
    variable_preserved = output_vars == confirmed_vars and len(output_vars) == 1
    only_expected_text_change = confirmed_text == output_text.replace(EXPECTED_OUTPUT_TOKEN, EXPECTED_CONFIRMED_TOKEN)
    token_delta_only_expected = (
        token_counts(confirmed_text).get(EXPECTED_CONFIRMED_TOKEN, 0) == 1
        and token_counts(output_text).get(EXPECTED_OUTPUT_TOKEN, 0) == 1
    )
    safe = all(
        [
            source_key_ok,
            english_source_supports_singular,
            preserved_pipe_casing,
            weak_wrapper_preserved,
            variable_preserved,
            only_expected_text_change,
            token_delta_only_expected,
        ]
    )
    return {
        "source": SOURCE,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": source_key,
        "output_line_number": row["output_line_number"],
        "final_state": row["final_state"],
        "needs_output_apply": int(row["needs_output_apply"] or 0),
        "confirmed_matches_output": int(row["confirmed_matches_output"] or 0),
        "english_text": english_text,
        "spanish_text": spanish_text,
        "output_text": output_text,
        "confirmed_text": confirmed_text,
        "source_key_pattern_ok": source_key_ok,
        "english_source_supports_singular_holy_site": english_source_supports_singular,
        "output_has_plural_token": EXPECTED_OUTPUT_TOKEN in output_text,
        "confirmed_has_singular_token": EXPECTED_CONFIRMED_TOKEN in confirmed_text,
        "pipe_and_casing_preserved": preserved_pipe_casing,
        "weak_wrapper_preserved": weak_wrapper_preserved,
        "holy_site_name_variable_preserved": variable_preserved,
        "holy_site_name_variables": confirmed_vars,
        "only_expected_text_change": only_expected_text_change,
        "token_delta_only_expected": token_delta_only_expected,
        "output_tokens": token_counts(output_text),
        "confirmed_tokens": token_counts(confirmed_text),
        "review_decision": "safe_for_readonly_allowlist_policy" if safe else "hold_token_policy",
        "policy_action_if_approved_later": "allow_exact_token_replacement_readonly_proposal",
        "apply_allowed_now": False,
        "requires_architecture_policy_before_apply": True,
    }


def main() -> None:
    with connect_readonly() as conn:
        rows = fetch_focus(conn)
        evidence = fetch_effect_name_token_evidence(conn)

    if {int(row["segment_id"]) for row in rows} != set(SEGMENT_IDS):
        raise SystemExit("focus segment id guard failed")

    records = [classify(row) for row in rows]
    decision_counts = Counter(record["review_decision"] for record in records)
    all_safe = decision_counts.get("safe_for_readonly_allowlist_policy", 0) == len(records)
    allowlist_policy_proposal = {
        "agent_key": "holy_site_effect_name_singular_token_allowlist_policy",
        "agent_type": "symbolic_subpolicy",
        "operational_state": "proposal_read_only",
        "decision_role": "token_policy_allowlist",
        "parent_agent_key": "domain_policy_vote_candidate",
        "scope_group": "domain_policy_vote_candidate",
        "dashboard_group": "Issue Network",
        "source_family": "holy_site_token_changing_hold",
        "allowed_exact_change": {
            "from": EXPECTED_OUTPUT_TOKEN,
            "to": EXPECTED_CONFIRMED_TOKEN,
        },
        "required_source_key_regex": r"^holy_site_[a-z0-9_]+_effect_name$",
        "required_text_shape": "PREFIX [holy_sites|lE] #weak ($holy_site_*_name$)#! -> same PREFIX [holy_site|lE] #weak (same variable)#!",
        "candidate_generation_allowed": False,
        "auto_apply_allowed": False,
        "lifecycle_allowed": False,
        "production_release_allowed": False,
        "requires_protected_apply_later": True,
        "requires_architecture_confirmation": True,
        "focus_segment_ids": SEGMENT_IDS,
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_holy_site_token_changing_review",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "record_count": len(records),
        "decision_counts": dict(sorted(decision_counts.items())),
        "safe_count": decision_counts.get("safe_for_readonly_allowlist_policy", 0),
        "hold_count": decision_counts.get("hold_token_policy", 0),
        "all_six_safe_for_readonly_allowlist_policy": all_safe,
        "ck3_token_evidence": evidence,
        "allowlist_policy_proposal": allowlist_policy_proposal if all_safe else None,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Send the read-only allowlist proposal to architecture for approval; do not apply until policy is materialized."
            if all_safe
            else "Keep hold_token_policy; do not apply."
        ),
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_holy_site_token_changing_review"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    proposal_path = Path(str(base) + "_allowlist_policy_proposal.json")
    write_jsonl(jsonl_path, records)
    if all_safe:
        proposal_path.write_text(
            json.dumps(allowlist_policy_proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "allowlist_policy_proposal_json": str(proposal_path) if all_safe else None,
    }

    lines = [
        "Holy-site token-changing read-only review",
        f"Run: {SEGMENT_STATE_RUN_ID}",
        f"Records: {len(records)}",
        f"All safe for read-only allowlist proposal: {all_safe}",
        "",
        "Decision counts:",
    ]
    for key, count in sorted(decision_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "CK3/local token evidence:", json.dumps(evidence, ensure_ascii=False, sort_keys=True), "", "Records:"])
    for record in records:
        lines.extend(
            [
                f"- {record['segment_id']} | {record['review_decision']}",
                f"  key: {record['source_key']}",
                f"  output: {record['output_text']}",
                f"  confirmed: {record['confirmed_text']}",
                f"  variable: {', '.join(record['holy_site_name_variables'])}",
            ]
        )
    lines.extend(["", "Recommendation:", summary["single_operational_recommendation"]])

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
