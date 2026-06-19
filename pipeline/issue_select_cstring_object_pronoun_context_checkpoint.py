from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "issue_select_cstring_object_pronoun_context_checkpoint_v1"
POLICY_NAME = "select_cstring_object_pronoun_context_shadow_v1"
POLICY_STATUS = "shadow_partial_repair"
AGENT_KEY = "micro_select_cstring_object_pronoun_context"
PRODUCTION_RELEASE_ALLOWED = 0

BLOCKING_VALIDATION_CODES = {
    "spanish_residue",
    "spanish_residue_in_literal",
    "spanish_punctuation",
    "mojibake_or_unexpected_script",
    "utf8_mojibake_sequence",
    "replacement_question_mark_mojibake",
    "token_breakage",
    "placeholder_breakage",
}

SELECT_CSTRING_RE = re.compile(r"Select_CString\((?P<body>.*?)\)", re.IGNORECASE)
STRING_LITERAL_RE = re.compile(r"'([^']*)'")
SPANISH_LITERAL_MARKERS = {
    "congeniaron",
    "congeniasteis",
    "contaste",
    "conto",
    "diste",
    "dio",
    "ensuciaste",
    "ensucio",
    "eres",
    "es",
    "estas",
    "esta",
    "hiciste",
    "hizo",
    "inmovilice",
    "inmovilizo",
    "le",
    "les",
    "me",
    "mi persona",
    "olvides",
    "olvide",
    "os",
    "os unisteis",
    "pasaste",
    "paso",
    "perdiste",
    "perdio",
    "prefieres",
    "prefiere",
    "quedaste",
    "quedo",
    "se unieron",
    "te",
    "te lanzas",
    "ti",
    "tu",
    "tus",
    "sus",
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def normalize(value: str | None) -> str:
    text = " ".join((value or "").strip().casefold().split())
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def short(value: str | None, limit: int = 220) -> str:
    text = value or ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_context_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_context_microagent_diagnostic_runs
        WHERE candidate_observations > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("No context microagent diagnostic run with observations found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_object_pronoun_context_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_context_run_id INTEGER NOT NULL,
            source_queue_run_id INTEGER NOT NULL,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            candidate_observations INTEGER NOT NULL DEFAULT 0,
            candidate_segments INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidates INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            block_counts_json TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_object_pronoun_context_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_context_run_id INTEGER NOT NULL,
            source_context_item_id INTEGER NOT NULL,
            source_queue_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            select_index INTEGER NOT NULL,
            left_literal TEXT,
            right_literal TEXT,
            rewrite_kind TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_select_text TEXT,
            corrected_select_text TEXT,
            current_text TEXT,
            corrected_text TEXT,
            validation_issues_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def fetch_rows(conn, context_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS source_context_item_id,
            item.run_id AS source_context_run_id,
            item.source_queue_run_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.select_index,
            item.left_literal,
            item.right_literal,
            item.current_select_text,
            item.current_text,
            item.english_text,
            item.spanish_text,
            item.context_category
        FROM ml_issue_select_cstring_context_microagent_diagnostic_items item
        WHERE item.run_id = ?
          AND item.context_category = 'object_pronoun_context'
        ORDER BY item.relative_path, item.source_key, item.select_index, item.segment_id
        """,
        (context_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def replace_select_literals(select_text: str, first: str, second: str) -> str:
    return re.sub(
        r"'[^']*'\s*,\s*'[^']*'",
        f"'{first}', '{second}'",
        select_text,
        count=1,
    )


def wrapped_replace(text: str, current_select: str, corrected_select: str) -> str:
    wrapped_current = f"[{current_select}]"
    wrapped_corrected = f"[{corrected_select}]"
    if wrapped_current in text:
        return text.replace(wrapped_current, wrapped_corrected, 1)
    return text.replace(current_select, corrected_select, 1)


def tail_after_select(text: str, current_select: str) -> str:
    wrapped = f"[{current_select}]"
    index = text.find(wrapped)
    if index >= 0:
        return text[index + len(wrapped) :].lstrip()
    index = text.find(current_select)
    if index < 0:
        return ""
    tail = text[index + len(current_select) :].lstrip()
    if tail.startswith("]"):
        tail = tail[1:].lstrip()
    return tail


def previous_text(text: str, current_select: str, limit: int = 120) -> str:
    wrapped = f"[{current_select}]"
    index = text.find(wrapped)
    if index < 0:
        index = text.find(current_select)
    if index < 0:
        return ""
    return text[max(0, index - limit) : index]


def propose(row: dict[str, Any]) -> tuple[str, str, str, str]:
    text = row.get("current_text") or ""
    select_text = row.get("current_select_text") or ""
    left = normalize(row.get("left_literal"))
    right = normalize(row.get("right_literal"))
    tail = tail_after_select(text, select_text)
    before = previous_text(text, select_text)
    tail_norm = normalize(tail[:80])
    before_norm = normalize(before)
    pair = (left, right)

    if not text.strip() or not select_text.strip():
        return "", "", "missing_current_text_or_select", "missing"

    if pair == ("te", "le"):
        if tail_norm.startswith("permite "):
            corrected_select = replace_select_literals(select_text, "lhe", "lhe")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "te_le_to_lhe_before_permite", "te_le_to_lhe"
        if tail_norm.startswith("juraram "):
            corrected_select = replace_select_literals(select_text, "lhe", "lhe")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "te_le_to_lhe_before_juraram", "te_le_to_lhe"
        if tail_norm.startswith("visto"):
            corrected_select = replace_select_literals(select_text, "", "")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "neutralize_before_passive_visto", "neutralize_before_passive"
        if tail_norm.startswith("crescessem "):
            corrected_select = replace_select_literals(select_text, "lhe", "lhe")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "te_le_to_lhe_before_crescessem", "te_le_to_lhe"
        return "", "", f"no_te_le_object_rule_after:{tail_norm[:30] or 'missing'}", "blocked"

    if pair == ("te", "se"):
        if "por se embebedar" in before_norm or tail_norm.startswith("e se recusar") or tail_norm.startswith("o cabelo") or tail_norm.startswith("as esporas"):
            corrected_select = replace_select_literals(select_text, "", "")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "neutralize_reflexive_suffix_after_portuguese_reflexive", "neutralize_reflexive_suffix"
        if "ter-" in before or "ter-" in before_norm:
            corrected_select = replace_select_literals(select_text, "se", "se")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "te_se_to_se_after_ter_hyphen", "te_se_to_se"
        return "", "", f"no_te_se_reflexive_rule_after:{tail_norm[:30] or 'missing'}", "blocked"

    if pair == ("os", "les"):
        if tail_norm.startswith("flagrou ") or tail_norm.startswith("ameacava "):
            corrected_select = replace_select_literals(select_text, "os", "os")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "os_les_to_os_object_plural", "os_les_to_os"
        if tail_norm.startswith("continuam "):
            corrected_select = replace_select_literals(select_text, "", "")
            return corrected_select, wrapped_replace(text, select_text, corrected_select), "neutralize_before_plural_verb_continuam", "neutralize_before_plural_verb"
        return "", "", f"no_os_les_rule_after:{tail_norm[:30] or 'missing'}", "blocked"

    return "", "", f"unsupported_pair:{left}/{right}", "blocked"


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in BLOCKING_VALIDATION_CODES
    ]


def residual_select_cstring_literals(text: str | None) -> list[str]:
    hits: list[str] = []
    for match in SELECT_CSTRING_RE.finditer(text or ""):
        body = match.group("body")
        for literal_match in STRING_LITERAL_RE.finditer(body):
            literal = literal_match.group(1)
            if normalize(literal) in SPANISH_LITERAL_MARKERS:
                hits.append(literal)
    return hits


def classify(row: dict[str, Any]) -> dict[str, Any]:
    current_text = row.get("current_text") or ""
    corrected_select, corrected_text, reason, rewrite_kind = propose(row)
    block_reasons: list[str] = []
    validation_issues: list[dict[str, Any]] = []

    if not corrected_select and corrected_select != "":
        block_reasons.append(reason)
    if not corrected_text:
        block_reasons.append(reason)
    else:
        if corrected_text == current_text:
            block_reasons.append("no_text_delta")
        if structural_tokens(current_text) != structural_tokens(corrected_text):
            block_reasons.append("structural_tokens_changed")
        validation_issues = blocking_validation_issues(corrected_text)
        residual_literals = residual_select_cstring_literals(corrected_text)
        if residual_literals:
            validation_issues.append(
                {
                    "code": "remaining_spanish_select_cstring_literal",
                    "severity": "high",
                    "message": "Another Select_CString literal still looks Spanish after object pronoun repair.",
                    "matches": residual_literals,
                }
            )

    allowed = 1 if not block_reasons and corrected_text else 0
    # Object pronoun repairs often require surrounding sentence rewrites
    # ("a você e a X", "vocês", "os/as", etc.). Keep this neuron partial-only
    # until a composition layer validates the whole sentence semantically.
    closure_candidate = 0
    return {
        **row,
        "rewrite_kind": rewrite_kind,
        "checkpoint_allowed": allowed,
        "segment_closure_candidate": closure_candidate,
        "block_reason": ";".join(dict.fromkeys(block_reasons)),
        "corrected_select_text": corrected_select,
        "corrected_text": corrected_text,
        "validation_issues": validation_issues,
    }


def report_paths(context_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    stem = f"{now_stamp()}_issue_select_cstring_object_pronoun_context_checkpoint_context_{context_run_id}"
    return reports_dir / f"{stem}.txt", reports_dir / f"{stem}.csv", reports_dir / f"{stem}.jsonl"


def write_reports(rows: list[dict[str, Any]], txt_path: Path, csv_path: Path, jsonl_path: Path, context_run_id: int) -> None:
    block_counts = Counter(row["block_reason"] or "allowed" for row in rows)
    kind_counts = Counter(row["rewrite_kind"] for row in rows)

    lines = [
        "Issue Select_CString object pronoun context checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Source context run id: {context_run_id}",
        "",
        "Summary:",
        f"- Candidate observations: {len(rows):,}",
        f"- Candidate segments: {len({row['segment_id'] for row in rows}):,}",
        f"- Checkpoint allowed: {sum(row['checkpoint_allowed'] for row in rows):,}",
        f"- Segment closure candidates: {sum(row['segment_closure_candidate'] for row in rows):,}",
        f"- Blocked: {sum(1 for row in rows if not row['checkpoint_allowed']):,}",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Rewrite kinds:",
    ]
    for kind, count in kind_counts.most_common():
        lines.append(f"- {kind}: {count:,}")
    lines.extend(["", "Block reasons:"])
    for reason, count in block_counts.most_common():
        lines.append(f"- {reason}: {count:,}")
    lines.extend(["", "Allowed samples:"])
    for row in [row for row in rows if row["checkpoint_allowed"]][:30]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"{row['left_literal']}/{row['right_literal']} kind={row['rewrite_kind']} "
            f"closure_candidate={row['segment_closure_candidate']}"
        )
        lines.append(f"  current: {short(row.get('current_text'), 260)}")
        lines.append(f"  corrected: {short(row.get('corrected_text'), 260)}")
    lines.extend(["", "Blocked samples:"])
    for row in [row for row in rows if not row["checkpoint_allowed"]][:25]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"pair={row['left_literal']}/{row['right_literal']} reason={row['block_reason']}"
        )

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "select_index",
        "left_literal",
        "right_literal",
        "rewrite_kind",
        "checkpoint_allowed",
        "segment_closure_candidate",
        "block_reason",
        "current_select_text",
        "corrected_select_text",
        "current_text",
        "corrected_text",
        "validation_issues_json",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "validation_issues_json": json.dumps(row.get("validation_issues") or [], ensure_ascii=False),
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = dict(row)
            payload["validation_issues_json"] = json.dumps(row.get("validation_issues") or [], ensure_ascii=False)
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(*, context_run_id: int | None = None) -> dict[str, Any]:
    conn = db.connect()
    conn.row_factory = db.sqlite3.Row
    try:
        ensure_tables(conn)
        selected_context_run_id = context_run_id or latest_context_run_id(conn)
        source_rows = fetch_rows(conn, selected_context_run_id)
        rows = [classify(row) for row in source_rows]
        txt_path, csv_path, jsonl_path = report_paths(selected_context_run_id)
        write_reports(rows, txt_path, csv_path, jsonl_path, selected_context_run_id)

        source_queue_run_id = int(rows[0]["source_queue_run_id"]) if rows else 0
        block_counts = Counter(row["block_reason"] or "allowed" for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_object_pronoun_context_checkpoint_runs (
                source_context_run_id,
                source_queue_run_id,
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                candidate_observations,
                candidate_segments,
                checkpoint_allowed,
                segment_closure_candidates,
                blocked_count,
                block_counts_json,
                production_release_allowed,
                report_path,
                csv_path,
                jsonl_path,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                selected_context_run_id,
                source_queue_run_id,
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                AGENT_KEY,
                len(rows),
                len({row["segment_id"] for row in rows}),
                sum(row["checkpoint_allowed"] for row in rows),
                sum(row["segment_closure_candidate"] for row in rows),
                sum(1 for row in rows if not row["checkpoint_allowed"]),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_object_pronoun_context_checkpoint_items (
                    run_id,
                    source_context_run_id,
                    source_context_item_id,
                    source_queue_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    select_index,
                    left_literal,
                    right_literal,
                    rewrite_kind,
                    checkpoint_allowed,
                    segment_closure_candidate,
                    block_reason,
                    current_select_text,
                    corrected_select_text,
                    current_text,
                    corrected_text,
                    validation_issues_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_context_run_id,
                    row["source_context_item_id"],
                    row["source_queue_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["select_index"],
                    row["left_literal"],
                    row["right_literal"],
                    row["rewrite_kind"],
                    row["checkpoint_allowed"],
                    row["segment_closure_candidate"],
                    row["block_reason"],
                    row["current_select_text"],
                    row["corrected_select_text"],
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["validation_issues"], ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    print("[issue_select_cstring_object_pronoun_context_checkpoint] Checkpoint generated")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Run id: {run_id}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Source context run id: {selected_context_run_id}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Candidate observations: {len(rows):,}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Candidate segments: {len({row['segment_id'] for row in rows}):,}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Checkpoint allowed: {sum(row['checkpoint_allowed'] for row in rows):,}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Segment closure candidates: {sum(row['segment_closure_candidate'] for row in rows):,}")
    print("[issue_select_cstring_object_pronoun_context_checkpoint] Apply allowed: 0")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] Report: {txt_path}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] CSV: {csv_path}")
    print(f"[issue_select_cstring_object_pronoun_context_checkpoint] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "source_context_run_id": selected_context_run_id,
        "candidate_observations": len(rows),
        "candidate_segments": len({row["segment_id"] for row in rows}),
        "checkpoint_allowed": sum(row["checkpoint_allowed"] for row in rows),
        "segment_closure_candidates": sum(row["segment_closure_candidate"] for row in rows),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a read-only Select_CString object pronoun context checkpoint.")
    parser.add_argument("--context-run-id", type=int, default=None)
    args = parser.parse_args()
    main(context_run_id=args.context_run_id)
