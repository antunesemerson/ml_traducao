from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import escape_localization_value, replace_quoted_text


RULE_VERSION = "apply_segment_state_updates_v1"
DEFAULT_REVIEW_STATES = ("human_locked", "human_confirmed")
TOKEN_PATTERN = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")
STRING_LITERAL_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def canonical_localization_text(value: str | None) -> str:
    return escape_localization_value(value or "")


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_bracket_token(token: str) -> str:
    body = token[1:-1].strip()
    if "|" in body:
        left, right = body.rsplit("|", 1)
        left = STRING_LITERAL_PATTERN.sub("'<TEXT>'", left)
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", left.strip()):
            return f"[{left.strip()}|<STYLE>]"
        return f"[{left.strip()}|{right.strip()}]"
    command_name = body.split("(", 1)[0].strip()
    base_name = command_name.split(".")[-1]
    if base_name == "Concept":
        seen = 0

        def replace_concept_literal(match: re.Match) -> str:
            nonlocal seen
            seen += 1
            if seen == 1:
                return match.group(0)
            return "'<TEXT>'"

        return f"[{STRING_LITERAL_PATTERN.sub(replace_concept_literal, body)}]"
    if base_name in {
        "Select_CString",
        "SelectLocalization",
        "LocalPlayerString",
        "PlayerString",
        "GetString",
    } or base_name.startswith("SelectLocalization") or base_name.endswith("String"):
        normalized = STRING_LITERAL_PATTERN.sub("'<TEXT>'", body)
        return f"[{normalized}]"
    return token


def structural_tokens(value: str | None) -> Counter:
    if not value:
        return Counter()
    value = canonical_localization_text(value)
    normalized: list[str] = []
    for token in TOKEN_PATTERN.findall(value):
        if token.startswith("[") and token.endswith("]"):
            normalized.append(normalize_bracket_token(token))
        else:
            normalized.append(token)
    return Counter(normalized)


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE total_segments > 1000
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_state_runs snapshot found. Run `python pipeline\\main.py segment-state` first.")
    return int(row["id"])


def parse_review_states(value: str | None, include_auto_confirmed: bool) -> list[str]:
    if value:
        states = [part.strip() for part in value.split(",") if part.strip()]
    else:
        states = list(DEFAULT_REVIEW_STATES)
    if include_auto_confirmed and "auto_confirmed" not in states:
        states.append("auto_confirmed")
    return states


def parse_segment_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    segment_ids: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            segment_ids.add(int(token))
        except ValueError as exc:
            raise RuntimeError(f"Invalid segment id in --segment-ids: {token}") from exc
    return segment_ids


def fetch_candidates(
    conn,
    *,
    state_run_id: int,
    review_states: list[str],
    limit: int | None,
    path_like: str | None,
    segment_ids: set[int],
    include_intentional_blank: bool,
    require_token_policy_decision: bool,
    allow_token_policy_decision: bool,
    token_policy_run_id: int | None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    placeholders = ", ".join("?" for _ in review_states)
    decision_join = ""
    decision_select = """
            NULL AS token_policy_decision_id,
            NULL AS token_policy_decision,
            NULL AS token_policy_bucket,
            NULL AS token_policy_risk_level,
            NULL AS token_policy_confirmed_text_hash,
            NULL AS token_policy_output_text_hash,
    """
    if require_token_policy_decision or allow_token_policy_decision:
        join_type = "JOIN" if require_token_policy_decision else "LEFT JOIN"
        decision_run_sql = ""
        if token_policy_run_id is not None:
            decision_run_sql = "AND tpd.policy_run_id = ?"
            params.append(token_policy_run_id)
        decision_join = f"""
        {join_type} segment_token_policy_decisions tpd
          ON tpd.segment_id = i.segment_id
         AND tpd.approved_for_apply = 1
         {decision_run_sql}
        """
        decision_select = """
            tpd.id AS token_policy_decision_id,
            tpd.decision AS token_policy_decision,
            tpd.policy_bucket AS token_policy_bucket,
            tpd.risk_level AS token_policy_risk_level,
            tpd.confirmed_text_hash AS token_policy_confirmed_text_hash,
            tpd.output_text_hash AS token_policy_output_text_hash,
        """
    params.extend([state_run_id, *review_states])
    path_sql = ""
    if path_like:
        path_sql = "AND i.relative_path LIKE ?"
        params.append(path_like)
    segment_ids_sql = ""
    if segment_ids:
        segment_placeholders = ", ".join("?" for _ in segment_ids)
        segment_ids_sql = f"AND i.segment_id IN ({segment_placeholders})"
        params.extend(sorted(segment_ids))
    blank_sql = ""
    if not include_intentional_blank:
        blank_sql = "AND TRIM(COALESCE(sc.confirmed_text, '')) <> ''"
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS state_item_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.final_state,
            i.review_state,
            i.output_state,
            i.priority_score,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.output_line_number,
            o.portuguese_text AS current_output_text,
            o.output_raw_line,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked,
            {decision_select}
            1 AS selected_marker
        FROM segment_state_items i
        JOIN source_segments s ON s.id = i.segment_id
        JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        {decision_join}
        WHERE i.run_id = ?
          AND i.needs_output_apply = 1
          AND i.review_state IN ({placeholders})
          {path_sql}
          {segment_ids_sql}
          {blank_sql}
        ORDER BY
          CASE i.review_state
            WHEN 'human_locked' THEN 0
            WHEN 'human_confirmed' THEN 1
            WHEN 'auto_confirmed' THEN 2
            ELSE 9
          END,
          i.priority_score DESC,
          i.relative_path,
          i.source_line_number,
          i.segment_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


def validate_candidate(
    row: dict[str, Any],
    output_root: Path,
    allow_locked_token_override: bool,
    require_token_policy_decision: bool,
    allow_token_policy_decision: bool,
    include_intentional_blank: bool,
) -> tuple[str, str | None, str | None]:
    confirmed_text = row["confirmed_text"]
    if confirmed_text is None:
        return "missing_confirmed_text", None, None
    if confirmed_text.strip() == "":
        blank_is_trusted = (
            include_intentional_blank
            and int(row["locked"] or 0) == 1
            and row["review_state"] in {"human_locked", "human_confirmed"}
        )
        if not blank_is_trusted:
            return "blank_confirmed_text", None, None
    if row["output_line_number"] is None:
        return "missing_output_line", None, None

    current_text = row["current_output_text"] or ""
    if canonical_localization_text(current_text) == canonical_localization_text(confirmed_text):
        return "already_matches", None, None

    output_path = output_root / Path(row["relative_path"])
    if not output_path.exists():
        return "missing_output_file", None, None

    spanish_tokens = structural_tokens(row["spanish_text"])
    current_tokens = structural_tokens(current_text)
    confirmed_tokens = structural_tokens(confirmed_text)
    token_mismatch = spanish_tokens != confirmed_tokens
    preserves_output_token_signature = current_tokens == confirmed_tokens
    locked_override = allow_locked_token_override and row["review_state"] == "human_locked" and int(row["locked"] or 0) == 1
    token_policy_decision = row.get("token_policy_decision_id") is not None
    token_policy_allowed = require_token_policy_decision or allow_token_policy_decision
    if token_mismatch and require_token_policy_decision and not token_policy_decision:
        return "missing_token_policy_decision", None, None
    if token_mismatch and token_policy_allowed and token_policy_decision:
        if not token_policy_decision:
            return "missing_token_policy_decision", None, None
        if row.get("token_policy_confirmed_text_hash") != sha256_text(confirmed_text):
            return "stale_token_policy_confirmed_hash", None, None
        if row.get("token_policy_output_text_hash") != sha256_text(current_text):
            return "stale_token_policy_output_hash", None, None
    elif token_mismatch and not locked_override and not preserves_output_token_signature:
        return "token_mismatch", None, None

    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        return "line_out_of_range", None, None

    current_line = lines[line_index]
    current_text = row["current_output_text"] or ""
    if current_text == confirmed_text:
        return "already_matches", current_line, current_line

    try:
        new_line = replace_quoted_text(current_line, confirmed_text)
    except ValueError:
        return "line_without_quoted_value", current_line, None

    if new_line == current_line:
        return "already_matches_line", current_line, new_line
    if token_mismatch and preserves_output_token_signature and not require_token_policy_decision:
        return "ready_preserved_output_token_signature", current_line, new_line
    if token_mismatch and token_policy_decision:
        return "ready_token_policy_decision", current_line, new_line
    if token_mismatch and locked_override:
        return "ready_token_override", current_line, new_line
    return "ready", current_line, new_line


def validation_priority(status: str) -> int:
    if status in {
        "ready",
        "ready_preserved_output_token_signature",
        "ready_token_override",
        "ready_token_policy_decision",
    }:
        return 0
    if status in {"already_matches", "already_matches_line"}:
        return 1
    if status == "stale_token_policy_confirmed_hash":
        return 3
    if status == "stale_token_policy_output_hash":
        return 4
    if status == "missing_token_policy_decision":
        return 5
    return 2


def evaluate_best_candidates(
    candidates: list[dict[str, Any]],
    *,
    output_root: Path,
    allow_locked_token_override: bool,
    require_token_policy_decision: bool,
    allow_token_policy_decision: bool,
    include_intentional_blank: bool,
) -> tuple[Counter, list[tuple[dict[str, Any], str, str | None, str | None]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    ordered_segment_ids: list[int] = []
    for row in candidates:
        segment_id = int(row["segment_id"])
        if segment_id not in grouped:
            ordered_segment_ids.append(segment_id)
        grouped[segment_id].append(row)

    result_counts: Counter = Counter()
    previews: list[tuple[dict[str, Any], str, str | None, str | None]] = []
    for segment_id in ordered_segment_ids:
        rows = grouped[segment_id]
        if len(rows) > 1:
            result_counts["duplicate_segment"] += len(rows) - 1
        evaluated = [
            (
                row,
                *validate_candidate(
                    row,
                    output_root,
                    allow_locked_token_override,
                    require_token_policy_decision,
                    allow_token_policy_decision,
                    include_intentional_blank,
                ),
            )
            for row in rows
        ]
        row, status, current_line, new_line = min(
            evaluated,
            key=lambda item: (
                validation_priority(item[1]),
                int(item[0].get("token_policy_decision_id") or 0) * -1,
            ),
        )
        result_counts[status] += 1
        previews.append((row, status, current_line, new_line))
    return result_counts, previews


def insert_apply_run(
    conn,
    *,
    state_run_id: int,
    started_at: str,
    apply: bool,
    limit: int | None,
    path_like: str | None,
    review_states: list[str],
    include_auto_confirmed: bool,
    allow_locked_token_override: bool,
    require_token_policy_decision: bool,
    token_policy_run_id: int | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_output_apply_runs (
            rule_version,
            state_run_id,
            apply,
            limit_count,
            path_filter,
            review_states,
            include_auto_confirmed,
            allow_locked_token_override,
            require_token_policy_decision,
            token_policy_run_id,
            backup_root,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            state_run_id,
            1 if apply else 0,
            limit,
            path_like,
            ",".join(review_states),
            1 if include_auto_confirmed else 0,
            1 if allow_locked_token_override else 0,
            1 if require_token_policy_decision else 0,
            token_policy_run_id,
            None,
            started_at,
            started_at,
        ),
    )
    return int(cur.lastrowid)


def insert_apply_items(
    conn,
    *,
    apply_run_id: int,
    state_run_id: int,
    previews: list[tuple[dict[str, Any], str, str | None, str | None]],
    applied_segment_ids: set[int],
    created_at: str,
    apply: bool,
) -> None:
    rows = []
    for row, status, _current_line, _new_line in previews:
        segment_id = int(row["segment_id"])
        result_status = status
        applied = 0
        if status in {
            "ready",
            "ready_preserved_output_token_signature",
            "ready_token_override",
            "ready_token_policy_decision",
        } and apply:
            if segment_id in applied_segment_ids:
                result_status = "applied"
                applied = 1
            else:
                result_status = "ready_not_applied"
        rows.append(
            (
                apply_run_id,
                state_run_id,
                row["state_item_id"],
                segment_id,
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["output_line_number"],
                row["final_state"],
                row["review_state"],
                result_status,
                applied,
                1 if status == "token_mismatch" else 0,
                sha256_text(row.get("current_output_text")),
                sha256_text(row.get("confirmed_text")),
                json.dumps([status], ensure_ascii=False),
                created_at,
            )
        )
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO segment_output_apply_items (
            run_id,
            state_run_id,
            state_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            output_line_number,
            final_state,
            review_state,
            result_status,
            applied,
            token_mismatch,
            previous_text_hash,
            confirmed_text_hash,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def update_apply_run(
    conn,
    *,
    apply_run_id: int,
    candidates: int,
    ready_count: int,
    applied_count: int,
    skipped_count: int,
    token_mismatch_count: int,
    files_touched_count: int,
    backup_root: Path | None,
    report_path: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE segment_output_apply_runs
        SET
            candidates_inspected = ?,
            ready_count = ?,
            applied_count = ?,
            skipped_count = ?,
            token_mismatch_count = ?,
            files_touched_count = ?,
            backup_root = ?,
            report_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            candidates,
            ready_count,
            applied_count,
            skipped_count,
            token_mismatch_count,
            files_touched_count,
            str(backup_root) if backup_root is not None else None,
            str(report_path),
            finished_at,
            finished_at,
            apply_run_id,
        ),
    )


def apply_by_file(
    *,
    output_root: Path,
    backup_root: Path,
    ready_entries: list[tuple[dict[str, Any], str]],
    create_backup: bool,
) -> tuple[set[int], Counter]:
    applied = 0
    counts: Counter = Counter()
    applied_segment_ids: set[int] = set()
    grouped: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for row, new_line in ready_entries:
        grouped[row["relative_path"]].append((row, new_line))
    for relative_path, entries in sorted(grouped.items()):
        output_path = output_root / Path(relative_path)
        if not output_path.exists():
            counts["missing_output_file_at_apply"] += len(entries)
            continue
        if create_backup:
            make_backup(output_root, backup_root, relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row, new_line in sorted(entries, key=lambda item: int(item[0]["output_line_number"])):
            line_number = int(row["output_line_number"])
            index = line_number - 1
            if index < 0 or index >= len(lines):
                counts["line_out_of_range_at_apply"] += 1
                continue
            lines[index] = new_line
            applied += 1
            applied_segment_ids.add(int(row["segment_id"]))
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        counts["files_touched"] += 1
    counts["applied"] = applied
    return applied_segment_ids, counts


def main(
    *,
    state_run_id: int | None = None,
    limit: int | None = None,
    path_like: str | None = None,
    segment_ids_csv: str | None = None,
    review_states_csv: str | None = None,
    include_auto_confirmed: bool = False,
    include_intentional_blank: bool = False,
    allow_locked_token_override: bool = False,
    require_token_policy_decision: bool = False,
    allow_token_policy_decision: bool = False,
    token_policy_run_id: int | None = None,
    apply: bool = False,
    create_backup: bool = True,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    output_root = db.project_path(settings["output_spanish"])
    backup_root = db.project_path("memory/backups") / started_at.strftime("segment_state_output_%Y%m%d_%H%M%S")
    review_states = parse_review_states(review_states_csv, include_auto_confirmed)
    segment_ids = parse_segment_ids(segment_ids_csv)

    print("[apply_segment_state_updates] Starting segment-state output apply")
    print(f"[apply_segment_state_updates] Rule version: {RULE_VERSION}")
    print(f"[apply_segment_state_updates] Output root: {output_root}")
    print(f"[apply_segment_state_updates] Apply: {apply}")
    print(f"[apply_segment_state_updates] Limit: {limit or 'none'}")
    print(f"[apply_segment_state_updates] Path filter: {path_like or 'none'}")
    print(f"[apply_segment_state_updates] Segment id filter: {len(segment_ids) if segment_ids else 'none'}")
    print(f"[apply_segment_state_updates] Review states: {', '.join(review_states)}")
    print(f"[apply_segment_state_updates] Allow locked token override: {allow_locked_token_override}")
    print(f"[apply_segment_state_updates] Require token policy decision: {require_token_policy_decision}")
    print(f"[apply_segment_state_updates] Allow token policy decision: {allow_token_policy_decision}")
    print(f"[apply_segment_state_updates] Token policy run id: {token_policy_run_id or 'latest/any'}")

    result_counts: Counter = Counter()
    file_updates: dict[str, dict[int, str]] = defaultdict(dict)
    ready_entries: list[tuple[dict[str, Any], str]] = []
    previews: list[tuple[dict[str, Any], str, str | None, str | None]] = []
    selected_run_id: int
    apply_run_id: int

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = state_run_id or latest_state_run_id(conn)
        started_at_db = started_at.isoformat(timespec="seconds")
        apply_run_id = insert_apply_run(
            conn,
            state_run_id=selected_run_id,
            started_at=started_at_db,
            apply=apply,
            limit=limit,
            path_like=path_like,
            review_states=review_states,
            include_auto_confirmed=include_auto_confirmed,
            allow_locked_token_override=allow_locked_token_override,
            require_token_policy_decision=require_token_policy_decision,
            token_policy_run_id=token_policy_run_id,
        )
        candidates = fetch_candidates(
            conn,
            state_run_id=selected_run_id,
            review_states=review_states,
            limit=limit,
            path_like=path_like,
            segment_ids=segment_ids,
            include_intentional_blank=include_intentional_blank,
            require_token_policy_decision=require_token_policy_decision,
            allow_token_policy_decision=allow_token_policy_decision,
            token_policy_run_id=token_policy_run_id,
        )
        conn.commit()

    result_counts, previews = evaluate_best_candidates(
        candidates,
        output_root=output_root,
        allow_locked_token_override=allow_locked_token_override,
        require_token_policy_decision=require_token_policy_decision,
        allow_token_policy_decision=allow_token_policy_decision,
        include_intentional_blank=include_intentional_blank,
    )
    for row, status, _current_line, new_line in previews:
        if status in {
            "ready",
            "ready_preserved_output_token_signature",
            "ready_token_override",
            "ready_token_policy_decision",
        } and new_line is not None:
            file_updates[row["relative_path"]][int(row["output_line_number"])] = new_line
            ready_entries.append((row, new_line))

    applied_segment_ids: set[int] = set()
    apply_counts: Counter = Counter()
    if apply and ready_entries:
        applied_segment_ids, apply_counts = apply_by_file(
            output_root=output_root,
            backup_root=backup_root,
            ready_entries=ready_entries,
            create_backup=create_backup,
        )
    applied = len(applied_segment_ids)

    elapsed = datetime.now() - started_at
    ready_count = (
        result_counts["ready"]
        + result_counts["ready_preserved_output_token_signature"]
        + result_counts["ready_token_override"]
        + result_counts["ready_token_policy_decision"]
    )
    report_lines = [
        "Segment state output apply report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Segment state run id: {selected_run_id}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        f"Path filter: {path_like or 'none'}",
        f"Segment id filter: {len(segment_ids) if segment_ids else 'none'}",
        f"Review states: {', '.join(review_states)}",
        f"Allow locked token override: {allow_locked_token_override}",
        f"Require token policy decision: {require_token_policy_decision}",
        f"Allow token policy decision: {allow_token_policy_decision}",
        f"Token policy run id: {token_policy_run_id or 'latest/any'}",
        f"Backup root: {backup_root if apply and create_backup else 'not created'}",
        "",
        "Summary:",
        f"- Candidates inspected: {len(candidates)}",
        f"- Ready to apply: {ready_count}",
        f"- Applied updates: {applied if apply else 0}",
        f"- Files with ready updates: {len(file_updates)}",
        "",
        "Validation results:",
        *[f"- {key}: {value}" for key, value in result_counts.most_common()],
    ]
    if apply_counts:
        report_lines.extend(["", "Apply results:", *[f"- {key}: {value}" for key, value in apply_counts.most_common()]])

    report_lines.extend(["", "Preview:"])
    for row, status, _current_line, new_line in previews[:80]:
        report_lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | "
                f"line={row['output_line_number']} | {row['source_key']} | {row['review_state']} | {status}",
                f"  CURRENT: {short(row['current_output_text'])}",
                f"  CONFIRMED: {short(row['confirmed_text'])}",
            ]
        )
        if new_line is not None:
            report_lines.append(f"  NEW LINE: {short(new_line)}")
    if not previews:
        report_lines.append("- No candidates selected")

    report_kind = (
        "apply_segment_state_updates_token_policy"
        if require_token_policy_decision
        else "apply_segment_state_updates_mixed_token_policy"
        if allow_token_policy_decision
        else "apply_segment_state_updates"
    )
    report_path = db.write_report(settings, report_kind, report_lines)
    finished_at_db = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        insert_apply_items(
            conn,
            apply_run_id=apply_run_id,
            state_run_id=selected_run_id,
            previews=previews,
            applied_segment_ids=applied_segment_ids,
            created_at=finished_at_db,
            apply=apply,
        )
        update_apply_run(
            conn,
            apply_run_id=apply_run_id,
            candidates=len(candidates),
            ready_count=ready_count,
            applied_count=applied if apply else 0,
            skipped_count=max(0, len(candidates) - ready_count),
            token_mismatch_count=result_counts["token_mismatch"],
            files_touched_count=int(apply_counts.get("files_touched", 0) if apply else len(file_updates)),
            backup_root=backup_root if apply and create_backup else None,
            report_path=report_path,
            finished_at=finished_at_db,
        )
        conn.commit()

    print(f"[apply_segment_state_updates] Apply run id: {apply_run_id}")
    print(f"[apply_segment_state_updates] Segment state run id: {selected_run_id}")
    print(f"[apply_segment_state_updates] Candidates inspected: {len(candidates)}")
    print(f"[apply_segment_state_updates] Ready to apply: {ready_count}")
    print(f"[apply_segment_state_updates] Applied updates: {applied if apply else 0}")
    for key, value in result_counts.most_common():
        print(f"[apply_segment_state_updates] {key}: {value}")
    print(f"[apply_segment_state_updates] Report: {report_path}")
    print("[apply_segment_state_updates] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply segment-state confirmed output updates. Default is dry-run.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--segment-ids", default=None, help="Comma-separated segment ids to apply exactly.")
    parser.add_argument("--review-states", default=None, help="Comma-separated review states. Default: human_locked,human_confirmed.")
    parser.add_argument("--include-auto-confirmed", action="store_true")
    parser.add_argument("--include-intentional-blank", action="store_true")
    parser.add_argument("--allow-locked-token-override", action="store_true")
    parser.add_argument("--require-token-policy-decision", action="store_true")
    parser.add_argument("--allow-token-policy-decision", action="store_true")
    parser.add_argument("--token-policy-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        limit=args.limit,
        path_like=args.path_like,
        segment_ids_csv=args.segment_ids,
        review_states_csv=args.review_states,
        include_auto_confirmed=args.include_auto_confirmed,
        include_intentional_blank=args.include_intentional_blank,
        allow_locked_token_override=args.allow_locked_token_override,
        require_token_policy_decision=args.require_token_policy_decision,
        allow_token_policy_decision=args.allow_token_policy_decision,
        token_policy_run_id=args.token_policy_run_id,
        apply=args.apply,
        create_backup=not args.no_backup,
    )
