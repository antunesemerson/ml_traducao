from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


TARGET_FAMILIES = {"autofix_unknown_microagent", "semantic_review_router"}
READY_DECISIONS = {
    "companion_ready_autofix_semantic_plain_sentence",
    "companion_ready_autofix_semantic_ui_tooltip",
    "companion_ready_autofix_semantic_short_phrase",
}
COMPOSITION_DECISIONS = {"composition_ready_plain_prose", "composition_ready_event_context"}
DYNAMIC_RE = re.compile(r"Select_CString|Custom\(|(?:^|[.\[])\s*Get[A-Za-z_]*\b|Concept\(|ScriptValue\(", re.IGNORECASE)
GENDER_RE = re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|A|O)|GetSheHe|GetHerHis|GetWomanMan", re.IGNORECASE)
DOMAIN_RE = re.compile(r"culture|religion|title|nicknames?|traits?|laws?|accolades?", re.IGNORECASE)
RESIDUAL_RE = re.compile(
    r"\b(the|will|shall|must|can|should|and|or)\b|"
    r"\b(el|la|los|las|una|uno|fuerza|exquisit[ao]|se le hace)\b",
    re.IGNORECASE,
)


def text_value(row: dict[str, Any]) -> str:
    return str(row.get("confirmed_text") or row.get("portuguese_text") or row.get("spanish_text") or "")


def word_count(text: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#\w+|#!", " ", text)
    return len(re.findall(r"\b\w+\b", cleaned, flags=re.UNICODE))


def read_reviewed_segment_ids(path_values: list[str] | None) -> set[int]:
    if not path_values:
        return set()
    segment_ids: set[int] = set()
    for path_value in path_values:
        path = Path(path_value)
        if not path.exists():
            raise SystemExit(f"exclude-reviewed JSONL not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid exclude-reviewed JSONL at {path}:{line_number}: {exc}") from exc
                if "segment_id" in item:
                    segment_ids.add(int(item["segment_id"]))
    return segment_ids


def classify(row: dict[str, Any], issue_families: list[str], issue_kinds: list[str]) -> tuple[str, str, bool]:
    current = text_value(row)
    relative_path = str(row["relative_path"])
    key = str(row["source_key"])
    wc = word_count(current)
    issue_text = " ".join(issue_kinds).lower()

    if row["state_group"] != "pending":
        return "blocked_uncertain", "segment is not pending in requested snapshot", False
    if int(row["needs_output_apply"] or 0) != 0 or int(row["confirmed_matches_output"] or 0) != 1:
        return "blocked_uncertain", "state guards are not clean for companion review", False
    if DYNAMIC_RE.search(current):
        return "needs_dynamic_expression_agent", "dynamic CK3 expression blocks companion-ready closure", False
    if GENDER_RE.search(current):
        return "needs_gender_or_custom_loc_policy", "gender/custom-loc expression requires policy before closure", False
    if DOMAIN_RE.search(relative_path) or DOMAIN_RE.search(key):
        return "needs_domain_context", "domain-sensitive file/key needs context policy", False
    if RESIDUAL_RE.search(current):
        return "needs_residual_repair", "visible residual language or suspicious lexical residue", False
    if "dialogue" in issue_text or re.search(r"^[\"']", current.strip()):
        return "needs_event_context_composer", "quoted/event dialogue needs event context", False
    if wc <= 5 and not re.search(r"[.!?;:]", current):
        return (
            "companion_ready_autofix_semantic_short_phrase",
            "short phrase with aligned confirmation/output and exact autofix+semantic pair",
            True,
        )
    if wc <= 12 and any(marker in relative_path for marker in ("triggers", "effects", "modifiers", "decisions")):
        return (
            "companion_ready_autofix_semantic_ui_tooltip",
            "compact UI/tooltip-like text with aligned confirmation/output and exact pair",
            True,
        )
    if wc <= 18 and not re.search(r"\[[^\]]+\]", current):
        return (
            "companion_ready_autofix_semantic_plain_sentence",
            "plain sentence with aligned confirmation/output and exact pair",
            True,
        )
    if wc <= 28:
        return "composition_ready_plain_prose", "plain prose likely governable by composer/lifecycle", True
    if any(marker in relative_path for marker in ("events", "event_localization", "dlc/")):
        return "composition_ready_event_context", "event/DLC context likely needs governed composer", True
    return "needs_context_composer", "text needs context before safe companion closure", False


def prepare_excluded_segments(conn, segment_ids: set[int]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp_autofix_semantic_companion_excluded_reviewed")
    conn.execute(
        """
        CREATE TEMP TABLE temp_autofix_semantic_companion_excluded_reviewed (
            segment_id INTEGER PRIMARY KEY
        )
        """
    )
    if segment_ids:
        conn.executemany(
            "INSERT OR IGNORE INTO temp_autofix_semantic_companion_excluded_reviewed (segment_id) VALUES (?)",
            [(segment_id,) for segment_id in sorted(segment_ids)],
        )


def fetch_rows(
    conn, state_run_id: int, ledger_run_id: int, limit: int, exclude_segment_ids: set[int]
) -> tuple[int, int, int, list[dict[str, Any]]]:
    prepare_excluded_segments(conn, exclude_segment_ids)
    eligible_before = conn.execute(
        """
        WITH families AS (
            SELECT state.segment_id, COUNT(DISTINCT issue.issue_family) AS family_count
            FROM segment_state_items state
            JOIN ml_issue_ledger_items issue
              ON issue.run_id = ?
             AND issue.status = 'open'
             AND issue.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.state_group = 'pending'
            GROUP BY state.segment_id
            HAVING family_count = 2
               AND SUM(issue.issue_family = 'autofix_unknown_microagent') > 0
               AND SUM(issue.issue_family = 'semantic_review_router') > 0
        )
        SELECT COUNT(*) AS c
        FROM families
        """,
        (ledger_run_id, state_run_id),
    ).fetchone()["c"]
    excluded_from_eligible = conn.execute(
        """
        WITH families AS (
            SELECT state.segment_id, COUNT(DISTINCT issue.issue_family) AS family_count
            FROM segment_state_items state
            JOIN ml_issue_ledger_items issue
              ON issue.run_id = ?
             AND issue.status = 'open'
             AND issue.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.state_group = 'pending'
            GROUP BY state.segment_id
            HAVING family_count = 2
               AND SUM(issue.issue_family = 'autofix_unknown_microagent') > 0
               AND SUM(issue.issue_family = 'semantic_review_router') > 0
        )
        SELECT COUNT(*) AS c
        FROM families
        JOIN temp_autofix_semantic_companion_excluded_reviewed excluded
          ON excluded.segment_id = families.segment_id
        """,
        (ledger_run_id, state_run_id),
    ).fetchone()["c"]
    eligible_after = int(eligible_before) - int(excluded_from_eligible)
    rows = conn.execute(
        """
        WITH families AS (
            SELECT state.segment_id, COUNT(DISTINCT issue.issue_family) AS family_count
            FROM segment_state_items state
            JOIN ml_issue_ledger_items issue
              ON issue.run_id = ?
             AND issue.status = 'open'
             AND issue.segment_id = state.segment_id
            WHERE state.run_id = ?
              AND state.state_group = 'pending'
            GROUP BY state.segment_id
            HAVING family_count = 2
               AND SUM(issue.issue_family = 'autofix_unknown_microagent') > 0
               AND SUM(issue.issue_family = 'semantic_review_router') > 0
        )
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.state_group,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.needs_reopen,
            source.spanish_text,
            source.english_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM families
        JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = families.segment_id
        LEFT JOIN temp_autofix_semantic_companion_excluded_reviewed excluded
          ON excluded.segment_id = state.segment_id
        JOIN source_segments source
          ON source.id = state.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
        WHERE excluded.segment_id IS NULL
        ORDER BY
            CASE
                WHEN state.relative_path LIKE '%events%' OR state.relative_path LIKE 'dlc/%' THEN 1
                ELSE 0
            END ASC,
            LENGTH(COALESCE(confirmation.confirmed_text, output.portuguese_text, source.spanish_text, '')) ASC,
            state.segment_id ASC
        LIMIT ?
        """,
        (ledger_run_id, state_run_id, state_run_id, limit),
    ).fetchall()
    return int(eligible_before), int(excluded_from_eligible), int(eligible_after), [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--exclude-reviewed-jsonl", action="append")
    parser.add_argument("--output-suffix", default="")
    args = parser.parse_args()

    excluded_segment_ids = read_reviewed_segment_ids(args.exclude_reviewed_jsonl)
    conn = db.connect()
    eligible_before, excluded_from_eligible, eligible_after, rows = fetch_rows(
        conn,
        args.segment_state_run_id,
        args.ledger_run_id,
        args.limit,
        excluded_segment_ids,
    )
    segment_ids = [int(row["segment_id"]) for row in rows]
    issues_by_segment: dict[int, list[dict[str, Any]]] = {segment_id: [] for segment_id in segment_ids}
    if segment_ids:
        placeholders = ",".join("?" for _ in segment_ids)
        for issue in conn.execute(
            f"""
            SELECT segment_id, issue_family, issue_kind, issue_severity
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
              AND segment_id IN ({placeholders})
            ORDER BY segment_id, issue_family, issue_kind
            """,
            (args.ledger_run_id, *segment_ids),
        ).fetchall():
            issues_by_segment[int(issue["segment_id"])].append(dict(issue))
    conn.close()

    reviewed = []
    for row in rows:
        segment_id = int(row["segment_id"])
        issue_rows = issues_by_segment[segment_id]
        families = sorted({issue["issue_family"] for issue in issue_rows})
        kinds = sorted({issue["issue_kind"] for issue in issue_rows})
        decision, notes, lifecycle_later = classify(row, families, kinds)
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["source_key"],
                "relative_path": row["relative_path"],
                "current_text": text_value(row),
                "english_text": row.get("english_text") or "",
                "open_issue_families": families,
                "open_issue_kinds": kinds,
                "decision": decision,
                "subpolicy": decision.replace("companion_ready_autofix_semantic_", "").replace("composition_ready_", "").replace("needs_", ""),
                "requires_lifecycle_later": lifecycle_later,
                "requires_apply_later": False,
                "corrected_text": "",
                "notes": notes,
            }
        )

    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = f"_{args.output_suffix}" if args.output_suffix else ""
    jsonl_path = reports_dir / f"{stamp}_autofix_semantic_companion_high_impact_review{suffix}.jsonl"
    txt_path = reports_dir / f"{stamp}_autofix_semantic_companion_high_impact_review{suffix}.txt"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in reviewed) + "\n",
        encoding="utf-8",
    )
    decisions = Counter(row["decision"] for row in reviewed)
    lifecycle_candidates = sum(count for decision, count in decisions.items() if decision in READY_DECISIONS)
    composition_candidates = sum(count for decision, count in decisions.items() if decision in COMPOSITION_DECISIONS)
    lines = [
        "Autofix + semantic companion high-impact review",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"segment_state_run_id: {args.segment_state_run_id}",
        f"ledger_run_id: {args.ledger_run_id}",
        f"eligible_exact_pair_before_exclusions: {eligible_before}",
        f"excluded_by_previous_review: {excluded_from_eligible}",
        f"eligible_exact_pair_after_exclusions: {eligible_after}",
        f"reviewed: {len(reviewed)}",
        f"lifecycle_candidates_future: {lifecycle_candidates}",
        f"composition_candidates_future: {composition_candidates}",
        "apply_candidates_future: 0",
        "",
        "Counts by decision:",
    ]
    for decision, count in decisions.most_common():
        lines.append(f"- {decision}: {count}")
    lines.extend(
        [
            "",
            "Main usable patterns:",
            "- exact autofix_unknown_microagent + semantic_review_router pair with aligned confirmation/output",
            "- short phrases and compact UI labels without dynamic/domain/residual markers",
            "",
            "Main blockers:",
            "- domain-sensitive accolade/title/culture/religion paths",
            "- dynamic CK3 expressions and gender/custom-loc tokens",
            "- event/dialogue context where semantic intent cannot be inferred from the short surface",
            "",
            "Recommendation:",
        ]
    )
    if lifecycle_candidates >= 70:
        lines.append("- Prepare a lifecycle read-only companion bridge for companion_ready_autofix_semantic_* decisions.")
    elif composition_candidates >= 70:
        lines.append("- Prepare a governed composer/lifecycle pass for composition_ready_* decisions.")
    else:
        lines.append("- Lifecycle/composer yield is low; split by blocker sublane before closing.")
    lines.append("")
    lines.append("Safety confirmation: read-only review only; no production, no apply, no lifecycle, no segment-state, no confirmations, no reindex, no training/model promotion, no source/output edits.")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"eligible_exact_pair_before_exclusions={eligible_before}")
    print(f"excluded_by_previous_review={excluded_from_eligible}")
    print(f"eligible_exact_pair_after_exclusions={eligible_after}")
    print(f"reviewed={len(reviewed)}")
    print(f"lifecycle_candidates_future={lifecycle_candidates}")
    print(f"composition_candidates_future={composition_candidates}")
    print(f"decision_counts={dict(decisions)}")
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
