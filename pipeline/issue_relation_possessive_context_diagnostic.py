from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_relation_possessive_context_diagnostic_v1"

RELATION_TOKEN_RE = re.compile(
    r"\[[^\]]*(?:Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)|"
    r"Custom\(\s*['\"](?:GetAuntUncle|MotherFather|GetMotherFather)['\"]\s*\))[^\]]*\]",
    re.IGNORECASE,
)
POSSESSIVE_WORDS = (
    "meu",
    "minha",
    "meus",
    "minhas",
    "seu",
    "sua",
    "seus",
    "suas",
    "nosso",
    "nossa",
    "vossa",
    "vosso",
)
POSSESSIVE_BEFORE_RELATION_RE = re.compile(
    r"\b(?P<poss>meu|minha|meus|minhas|seu|sua|seus|suas|nosso|nossa|vossa|vosso)"
    r"\s*(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])",
    re.IGNORECASE,
)
PAIR_FORM_BEFORE_RELATION_RE = re.compile(
    r"\b(?P<pair>meu/minha|minha/meu|meu\(a\)|sua/seu|seu/sua)"
    r"\s*(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])",
    re.IGNORECASE,
)
GLUED_POSSESSIVE_RELATION_RE = re.compile(
    r"\b(?P<poss>meu|minha|meus|minhas|seu|sua|seus|suas|nosso|nossa|vossa|vosso)"
    r"(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])",
    re.IGNORECASE,
)
RELATION_SPACE_BEFORE_COMMA_RE = re.compile(
    r"(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])\s+,",
    re.IGNORECASE,
)
LEADING_RELATION_POSSESSIVE_RE = re.compile(
    r"^\s*(?P<poss>Meu|Minha|Seu|Sua|meu|minha|seu|sua)\s+"
    r"(?P<token>\[[^\]]*Custom2\(\s*['\"]RelationToMe(?:Short)?['\"][^\)]*\)[^\]]*\])",
    re.IGNORECASE,
)
LETTER_GREETING_KEY_RE = re.compile(r"greeting_family|greeting_", re.IGNORECASE)
LONG_TEXT_THRESHOLD = 420


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], state_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_relation_possessive_context_diagnostic_state_{state_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished segment_state_runs found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_relation_possessive_context_diagnostic_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            candidate_segments INTEGER NOT NULL DEFAULT 0,
            repair_candidate_count INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            action_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_relation_possessive_context_diagnostic_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            final_state TEXT,
            route_key TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            repair_candidate INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_text TEXT NOT NULL,
            proposed_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            token_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_relation_possessive_context_diagnostic_runs(id) ON DELETE CASCADE
        )
        """
    )


def add_uppercase_filter(token: str) -> str:
    if token.endswith("]") and "|U" not in token:
        return token[:-1] + "|U]"
    return token


def classify(row: dict[str, Any]) -> dict[str, Any]:
    text = row.get("current_text") or ""
    key = row.get("source_key") or ""
    path = row.get("relative_path") or ""
    proposed = ""
    block_reason = ""
    repair_candidate = 0

    if PAIR_FORM_BEFORE_RELATION_RE.search(text):
        def replace_pair(match: re.Match[str]) -> str:
            return add_uppercase_filter(match.group("token"))

        proposed = PAIR_FORM_BEFORE_RELATION_RE.sub(replace_pair, text, count=1)
        route_key = "visible_pair_possessive_before_relation"
        action = "rewrite_pair_possessive_to_capitalized_relation"
        risk = "medium"
        repair_candidate = 1
    elif GLUED_POSSESSIVE_RELATION_RE.search(text):
        proposed = GLUED_POSSESSIVE_RELATION_RE.sub(
            lambda match: f"{match.group('poss')} {match.group('token')}",
            text,
        )
        route_key = "possessive_glued_to_relation_token"
        action = "repair_missing_space_before_relation_token"
        risk = "low"
        repair_candidate = 1
    elif LETTER_GREETING_KEY_RE.search(key) or "greeting_custom_loc" in path:
        route_key = "letter_greeting_relation_salutation"
        action = "rewrite_greeting_to_capitalized_relation_salutation"
        risk = "medium"
        repair_candidate = 0
        block_reason = "needs_explicit_greeting_mapping"
    elif LEADING_RELATION_POSSESSIVE_RE.search(text):
        def replace_leading(match: re.Match[str]) -> str:
            return add_uppercase_filter(match.group("token"))

        proposed = LEADING_RELATION_POSSESSIVE_RE.sub(replace_leading, text, count=1)
        route_key = "leading_possessive_relation_with_name"
        action = "drop_possessive_and_capitalize_relation"
        risk = "medium"
        repair_candidate = 1
    elif RELATION_SPACE_BEFORE_COMMA_RE.search(text):
        proposed = RELATION_SPACE_BEFORE_COMMA_RE.sub(
            lambda match: f"{match.group('token')},",
            text,
        )
        route_key = "relation_token_space_before_comma"
        action = "repair_relation_comma_spacing"
        risk = "low"
        repair_candidate = 1
    elif POSSESSIVE_BEFORE_RELATION_RE.search(text):
        route_key = "possessive_relation_context"
        action = "route_relation_possessive_context_composer"
        risk = "medium"
        block_reason = "possessive_agreement_depends_on_relation_rendering"
    elif len(text) >= LONG_TEXT_THRESHOLD or text.count("\\n") >= 2:
        route_key = "longform_relation_context"
        action = "route_longform_relation_context_composer"
        risk = "low"
        block_reason = "longform_context_requires_sentence_composition"
    else:
        route_key = "plain_relation_reference_context"
        action = "route_relation_reference_review"
        risk = "low"
        block_reason = "no_safe_repair_pattern"

    if proposed and structural_tokens(text) != structural_tokens(proposed):
        repair_candidate = 0
        block_reason = "structural_tokens_changed"

    return {
        "segment_state_run_id": int(row["segment_state_run_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "source_line_number": int(row.get("source_line_number") or 0),
        "final_state": row.get("final_state") or "",
        "route_key": route_key,
        "recommended_action": action,
        "risk_level": risk,
        "repair_candidate": repair_candidate,
        "block_reason": block_reason,
        "current_text": text,
        "proposed_text": proposed if repair_candidate else "",
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "token_count": len(RELATION_TOKEN_RE.findall(text)),
        "text_length": len(text),
    }


def fetch_candidates(conn, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            st.run_id AS segment_state_run_id,
            st.segment_id,
            st.final_state,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS current_text
        FROM segment_state_items st
        JOIN source_segments s ON s.id = st.segment_id
        JOIN output_segments o ON o.segment_id = st.segment_id
        WHERE st.run_id = ?
          AND st.state_group = 'pending'
          AND (
              o.portuguese_text LIKE '%RelationToMe%'
              OR o.portuguese_text LIKE '%GetAuntUncle%'
              OR o.portuguese_text LIKE '%MotherFather%'
              OR o.portuguese_text LIKE '%GetMotherFather%'
              OR s.source_key LIKE '%greeting_family%'
          )
        ORDER BY st.priority_score DESC, s.relative_path, s.source_line_number, st.segment_id
        """,
        (state_run_id,),
    ).fetchall()
    return [classify(dict(row)) for row in rows]


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    state_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "route_key",
        "recommended_action",
        "risk_level",
        "repair_candidate",
        "block_reason",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "text_length",
        "token_count",
        "current_text",
        "proposed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    route_counts = Counter(row["route_key"] for row in rows)
    action_counts = Counter(row["recommended_action"] for row in rows)
    lines = [
        "Issue relation possessive context diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Segment-state run id: {state_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidate observations: {len(rows):,}",
        f"- Candidate segments: {len({row['segment_id'] for row in rows}):,}",
        f"- Repair candidates: {sum(row['repair_candidate'] for row in rows):,}",
        "- Production release allowed: 0",
        "",
        "Routes:",
        *[f"- {label}: {count:,}" for label, count in route_counts.most_common()],
        "",
        "Actions:",
        *[f"- {label}: {count:,}" for label, count in action_counts.most_common()],
        "",
        "Repair candidate samples:",
    ]
    for row in [item for item in rows if item["repair_candidate"]][:25]:
        lines.extend(
            [
                f"- {row['route_key']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  proposed: {short(row['proposed_text'], 220)}",
            ]
        )
    lines.extend(["", "Blocked/context samples:"])
    for row in [item for item in rows if not item["repair_candidate"]][:25]:
        lines.append(
            f"- {row['route_key']} | {row['block_reason']} | segment={row['segment_id']} "
            f"{row['relative_path']}::{row['source_key']} | {short(row['current_text'], 180)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Diagnostic only: no confirmations, no source/output writes, no production run.",
            "- Repair candidates are proposals for a future protected checkpoint/apply.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, segment_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        state_run_id = segment_state_run_id or latest_segment_state_run_id(conn)
        rows = fetch_candidates(conn, state_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings, state_run_id)
        route_counts = Counter(row["route_key"] for row in rows)
        action_counts = Counter(row["recommended_action"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_relation_possessive_context_diagnostic_runs (
                rule_version,
                segment_state_run_id,
                candidate_count,
                candidate_segments,
                repair_candidate_count,
                route_counts_json,
                action_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                state_run_id,
                len(rows),
                len({row["segment_id"] for row in rows}),
                sum(row["repair_candidate"] for row in rows),
                json.dumps(route_counts, ensure_ascii=False, sort_keys=True),
                json.dumps(action_counts, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_relation_possessive_context_diagnostic_items (
                    run_id,
                    segment_state_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    final_state,
                    route_key,
                    recommended_action,
                    risk_level,
                    repair_candidate,
                    block_reason,
                    current_text,
                    proposed_text,
                    english_text,
                    spanish_text,
                    token_count,
                    text_length,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["segment_state_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["final_state"],
                    row["route_key"],
                    row["recommended_action"],
                    row["risk_level"],
                    row["repair_candidate"],
                    row["block_reason"],
                    row["current_text"],
                    row["proposed_text"],
                    row["english_text"],
                    row["spanish_text"],
                    row["token_count"],
                    row["text_length"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            state_run_id=state_run_id,
            rows=rows,
        )
        conn.commit()
    print(f"Relation possessive context diagnostic run: {run_id}")
    print(f"Segment-state run id: {state_run_id}")
    print(f"Candidates: {len(rows):,}")
    print(f"Repair candidates: {sum(row['repair_candidate'] for row in rows):,}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "segment_state_run_id": state_run_id,
        "candidate_count": len(rows),
        "repair_candidate_count": sum(row["repair_candidate"] for row in rows),
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose pending RelationToMe possessive/context surfaces.")
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    main(segment_state_run_id=args.segment_state_run_id)
