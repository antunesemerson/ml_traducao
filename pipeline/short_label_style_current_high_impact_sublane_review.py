from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import structural_tokens


ISSUE_FAMILY = "short_label_style_microagent"
BAD_ENCODING_RE = re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã¯Â¿Â½")
QUESTION_INSIDE_WORD_RE = re.compile(r"\w\?\w", re.UNICODE)
RECENT_REVIEW_TABLES = {
    # Best-effort exclusion by report-driven segment ids is intentionally not
    # hardcoded here; current state/ledger guards remain authoritative.
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def latest_finished_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No finished segment_state_runs available")
    return int(row["id"])


def latest_nonempty_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
          AND ledger_item_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No non-empty ml_issue_ledger_runs available")
    return int(row["id"])


def report_paths(settings: dict[str, Any], *, output_suffix: str = "") -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = f"_{output_suffix}" if output_suffix else ""
    base = reports_dir / f"{stamp}_short_label_style_current_high_impact_sublane_review{suffix}"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def load_excluded_ids(paths: list[Path]) -> set[int]:
    excluded: set[int] = set()
    for path in paths:
        if not path:
            continue
        if not path.exists():
            raise RuntimeError(f"Excluded review JSONL not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            excluded.add(int(row["segment_id"]))
    return excluded


def has_dynamic_expression(text: str) -> bool:
    return any(marker in text for marker in ("[Select_CString", "[Concept(", "GetTrait(", "ScriptValue(", "GetActivityType("))


def word_count(text: str) -> int:
    cleaned = re.sub(r"\[[^\]]+\]|\$[^$]+\$|#\w+|#!", " ", text)
    return len(re.findall(r"\b\w+\b", cleaned, flags=re.UNICODE))


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current = as_text(row["current_text"])
    english = as_text(row["english_text"])
    lower = current.lower()
    wc = word_count(current)
    risks: list[str] = []
    corrected = ""
    requires_apply_later = False
    lifecycle_candidate = False

    if BAD_ENCODING_RE.search(current):
        risks.append("current_text_encoding_marker")
    if any(term in lower for term in (" si ", " una ", " el ", " la ", " los ", " las ", " ahora", " carácter", "caracter")):
        decision = "spanish_residual_repair_needed"
        sublane = "residual_language"
        risks.append("possible_spanish_residual")
        rationale = "visible Spanish residual or orthographic residue needs text repair review"
    elif re.search(r"\b(the|will|can|must|not|and|or)\b", current, flags=re.IGNORECASE):
        decision = "english_residual_repair_needed"
        sublane = "residual_language"
        risks.append("possible_english_residual")
        rationale = "visible English residual needs repair review"
    elif has_dynamic_expression(current) or has_dynamic_expression(english):
        decision = "needs_dynamic_expression_agent"
        sublane = "dynamic_expression"
        risks.append("dynamic_expression")
        rationale = "dynamic CK3 expression requires a specialized expression agent before closure"
    elif "[" in current and "]" in current and wc <= 8:
        decision = "lifecycle_ready_compact_ui_label"
        sublane = "compact_ui_label"
        lifecycle_candidate = True
        rationale = "compact tokenized label appears stable with current confirmation/output alignment"
    elif row["relative_path"].startswith("triggers/") or row["relative_path"].startswith("effects"):
        decision = "lifecycle_ready_system_tooltip"
        sublane = "system_tooltip"
        lifecycle_candidate = True
        rationale = "system tooltip-like surface appears stable with current confirmation/output alignment"
    elif wc <= 5 and not re.search(r"[.!?;:]", current):
        decision = "lifecycle_ready_plain_noop"
        sublane = "plain_noop"
        lifecycle_candidate = True
        rationale = "short plain label appears stable with current confirmation/output alignment"
    elif wc <= 10 and not current.endswith("."):
        decision = "lifecycle_ready_short_phrase"
        sublane = "short_phrase"
        lifecycle_candidate = True
        rationale = "short phrase appears stable with current confirmation/output alignment"
    elif any(term in row["relative_path"] for term in ("culture", "religion", "accolades", "activities", "traits")):
        decision = "needs_domain_context"
        sublane = "domain_context"
        risks.append("domain_context")
        rationale = "domain-specific label needs context before lifecycle closure"
    else:
        decision = "needs_context_composer"
        sublane = "context_composer"
        risks.append("needs_context")
        rationale = "surface needs context composition before safe closure"

    tokens_preserved = True
    if corrected:
        tokens_preserved = structural_tokens(current) == structural_tokens(corrected)

    return {
        "segment_id": int(row["segment_id"]),
        "source_key": as_text(row["source_key"]),
        "relative_path": as_text(row["relative_path"]),
        "ledger_run_id": int(row["ledger_run_id"]),
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "issue_family": as_text(row["issue_family"]),
        "issue_kind": as_text(row["issue_kind"]),
        "decision": decision,
        "sublane": sublane,
        "current_text": current,
        "english_text": english,
        "spanish_text": as_text(row["spanish_text"]),
        "corrected_text": corrected,
        "tokens_preserved": tokens_preserved,
        "requires_apply_later": requires_apply_later,
        "lifecycle_candidate": lifecycle_candidate,
        "risk_flags": risks,
        "rationale": rationale,
    }


def fetch_rows(
    conn,
    *,
    state_run_id: int,
    ledger_run_id: int,
    limit: int,
    excluded_ids: set[int],
) -> tuple[int, int, int, list[dict[str, Any]]]:
    total = conn.execute(
        """
        SELECT COUNT(DISTINCT item.segment_id) AS c
        FROM ml_issue_ledger_items item
        JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        WHERE item.run_id = ?
          AND item.status = 'open'
          AND item.issue_family = ?
          AND state.state_group = 'pending'
          AND state.needs_output_apply = 0
          AND state.confirmed_matches_output = 1
        """,
        (state_run_id, ledger_run_id, ISSUE_FAMILY),
    ).fetchone()["c"]
    excluded_current = 0
    if excluded_ids:
        placeholders = ",".join("?" for _ in excluded_ids)
        excluded_current = conn.execute(
            f"""
            SELECT COUNT(DISTINCT item.segment_id) AS c
            FROM ml_issue_ledger_items item
            JOIN segment_state_items state
              ON state.segment_id = item.segment_id
             AND state.run_id = ?
            WHERE item.run_id = ?
              AND item.status = 'open'
              AND item.issue_family = ?
              AND state.state_group = 'pending'
              AND state.needs_output_apply = 0
              AND state.confirmed_matches_output = 1
              AND item.segment_id IN ({placeholders})
            """,
            (state_run_id, ledger_run_id, ISSUE_FAMILY, *sorted(excluded_ids)),
        ).fetchone()["c"]
    exclude_clause = ""
    params: list[Any] = [state_run_id, ledger_run_id, ISSUE_FAMILY]
    if excluded_ids:
        exclude_clause = "AND item.segment_id NOT IN ({})".format(",".join("?" for _ in excluded_ids))
        params.extend(sorted(excluded_ids))
    params.extend([ledger_run_id, state_run_id, limit])
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                item.segment_id,
                item.source_key,
                item.relative_path,
                item.issue_family,
                item.issue_kind,
                item.issue_severity,
                source.english_text,
                source.spanish_text,
                output.portuguese_text AS current_text,
                ROW_NUMBER() OVER (
                    PARTITION BY item.segment_id
                    ORDER BY
                        CASE item.issue_kind
                            WHEN 'short_or_compact_label_reopened' THEN 0
                            WHEN 'short_label_style_issue' THEN 1
                            ELSE 2
                        END,
                        item.id
                ) AS rn
            FROM ml_issue_ledger_items item
            JOIN segment_state_items state
              ON state.segment_id = item.segment_id
             AND state.run_id = ?
            JOIN source_segments source
              ON source.id = item.segment_id
            JOIN output_segments output
              ON output.segment_id = item.segment_id
            WHERE item.run_id = ?
              AND item.status = 'open'
              AND item.issue_family = ?
              AND state.state_group = 'pending'
              AND state.needs_output_apply = 0
              AND state.confirmed_matches_output = 1
              {exclude_clause}
        )
        SELECT
            ? AS ledger_run_id,
            ? AS segment_state_run_id,
            *
        FROM ranked
        WHERE rn = 1
        ORDER BY
            CASE
                WHEN length(current_text) <= 60 THEN 0
                WHEN current_text LIKE '%[%]%' THEN 1
                ELSE 2
            END,
            segment_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return int(total), int(excluded_current), int(total) - int(excluded_current), [dict(row) for row in rows]


def validate(rows: list[dict[str, Any]]) -> None:
    required = {
        "segment_id",
        "source_key",
        "relative_path",
        "ledger_run_id",
        "segment_state_run_id",
        "issue_family",
        "issue_kind",
        "decision",
        "sublane",
        "current_text",
        "english_text",
        "spanish_text",
        "corrected_text",
        "tokens_preserved",
        "requires_apply_later",
        "lifecycle_candidate",
        "risk_flags",
        "rationale",
    }
    errors: list[str] = []
    for row in rows:
        missing = required - set(row)
        if missing:
            errors.append(f"missing_fields:{row.get('segment_id')}:{sorted(missing)}")
        corrected = as_text(row.get("corrected_text"))
        if row.get("requires_apply_later") is True and not corrected:
            errors.append(f"apply_later_without_corrected_text:{row.get('segment_id')}")
        if corrected:
            if BAD_ENCODING_RE.search(corrected):
                errors.append(f"bad_encoding_corrected:{row.get('segment_id')}")
            if QUESTION_INSIDE_WORD_RE.search(corrected):
                errors.append(f"question_inside_word:{row.get('segment_id')}")
            if row.get("tokens_preserved") is not True:
                errors.append(f"tokens_not_preserved:{row.get('segment_id')}")
        if row.get("lifecycle_candidate") is True and not as_text(row.get("decision")).startswith("lifecycle_ready_"):
            errors.append(f"bad_lifecycle_flag:{row.get('segment_id')}")
    if errors:
        raise RuntimeError("; ".join(errors))


def write_reports(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    total_eligible_before_exclusion: int,
    excluded_by_prior_review: int,
    total_eligible_after_exclusion: int,
    state_run_id: int,
    ledger_run_id: int,
    output_suffix: str = "",
) -> tuple[Path, Path]:
    jsonl_path, txt_path = report_paths(settings, output_suffix=output_suffix)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    by_decision = Counter(row["decision"] for row in rows)
    by_sublane = Counter(row["sublane"] for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    patterns = Counter(
        f"{row['sublane']} | {row['relative_path'].split('/')[0]} | {row['issue_kind']}"
        for row in rows
    )
    blockers = Counter(flag for row in rows for flag in row["risk_flags"])

    if lifecycle_count >= 70:
        recommendation = "Prepare a lifecycle read-only prompt for lifecycle_ready short-label style candidates."
    elif apply_count >= 10:
        recommendation = "Prepare a separate protected apply prompt for small textual repairs."
    elif by_decision["needs_context_composer"] >= max(by_decision.values() or [0]):
        recommendation = "Prepare a context composer for the dominant short-label style sublane."
    elif by_decision["needs_domain_context"] >= max(by_decision.values() or [0]):
        recommendation = "Split the next review by domain context."
    else:
        recommendation = "Start with lifecycle candidates and keep repair prompts separate."

    lines = [
        "Short-label style current high-impact sublane review",
        f"Segment state run id: {state_run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Total eligible before exclusion: {total_eligible_before_exclusion}",
        f"Excluded by prior review: {excluded_by_prior_review}",
        f"Total eligible after exclusion: {total_eligible_after_exclusion}",
        f"Reviewed: {len(rows)}",
        "",
        "Counts by decision:",
    ]
    for key, value in sorted(by_decision.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Counts by sublane:"])
    for key, value in sorted(by_sublane.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Lifecycle candidates futuros: {lifecycle_count}",
            f"Apply candidates futuros: {apply_count}",
            "",
            "Top 5 reusable patterns:",
        ]
    )
    for key, value in patterns.most_common(5):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Top 5 blockers/risks:"])
    if blockers:
        for key, value in blockers.most_common(5):
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "Recommendation:", f"- {recommendation}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def run(
    *,
    limit: int,
    segment_state_run_id: int | None = None,
    ledger_run_id: int | None = None,
    exclude_reviewed_jsonl: list[Path] | None = None,
    output_suffix: str = "",
) -> tuple[int, int, int, int, int, list[dict[str, Any]], tuple[Path, Path]]:
    settings = db.load_settings()
    excluded_ids = load_excluded_ids(exclude_reviewed_jsonl or [])
    with db.connect(settings) as conn:
        state_run_id = segment_state_run_id or latest_finished_state_run_id(conn)
        ledger_id = ledger_run_id or latest_nonempty_ledger_run_id(conn)
        total_before, excluded_current, total_after, raw_rows = fetch_rows(
            conn,
            state_run_id=state_run_id,
            ledger_run_id=ledger_id,
            limit=limit,
            excluded_ids=excluded_ids,
        )
    rows = [classify(row) for row in raw_rows]
    validate(rows)
    paths = write_reports(
        settings,
        rows=rows,
        total_eligible_before_exclusion=total_before,
        excluded_by_prior_review=excluded_current,
        total_eligible_after_exclusion=total_after,
        state_run_id=state_run_id,
        ledger_run_id=ledger_id,
        output_suffix=output_suffix,
    )
    return state_run_id, ledger_id, total_before, excluded_current, total_after, rows, paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--exclude-reviewed-jsonl", action="append", type=Path, default=[])
    parser.add_argument("--output-suffix", default="")
    args = parser.parse_args()
    state_run_id, ledger_run_id, total_before, excluded_current, total_after, rows, (jsonl_path, txt_path) = run(
        limit=args.limit,
        segment_state_run_id=args.segment_state_run_id,
        ledger_run_id=args.ledger_run_id,
        exclude_reviewed_jsonl=args.exclude_reviewed_jsonl,
        output_suffix=args.output_suffix,
    )
    print("[short_label_style_current_high_impact_sublane_review] Completed")
    print(f"segment_state_run_id={state_run_id}")
    print(f"ledger_run_id={ledger_run_id}")
    print(f"total_eligible_before_exclusion={total_before}")
    print(f"excluded_by_prior_review={excluded_current}")
    print(f"total_eligible_after_exclusion={total_after}")
    print(f"reviewed={len(rows)}")
    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
