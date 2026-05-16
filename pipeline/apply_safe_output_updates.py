from __future__ import annotations

import argparse
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import db


RULE_VERSION = "apply_safe_output_updates_v4"


def protected_tokens(value: str | None) -> Counter:
    import re

    if not value:
        return Counter()
    pattern = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")
    return Counter(normalize_protected_token(token) for token in pattern.findall(value))


def normalize_protected_token(token: str) -> str:
    import re

    if not (token.startswith("[") and token.endswith("]")):
        return token

    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    base_name = command_name.split(".")[-1]
    string_literal_pattern = re.compile(r"'[^']*'|\"[^\"]*\"")

    if base_name == "Concept":
        seen = 0

        def replace_concept_literal(match: re.Match) -> str:
            nonlocal seen
            seen += 1
            if seen == 1:
                return match.group(0)
            return "'<TEXT>'"

        return string_literal_pattern.sub(replace_concept_literal, token)

    if base_name in {
        "Select_CString",
        "SelectLocalization",
        "LocalPlayerString",
        "PlayerString",
        "GetString",
    } or base_name.startswith("SelectLocalization") or base_name.endswith("String"):
        return string_literal_pattern.sub("'<TEXT>'", token)

    return token


def escape_localization_value(new_text: str) -> str:
    escaped = []
    backslashes = 0
    for char in new_text:
        if char == "\n":
            escaped.append("\\n")
            backslashes = 0
            continue
        if char == "\r":
            continue
        if char == "\\":
            escaped.append(char)
            backslashes += 1
            continue
        if char == '"' and backslashes % 2 == 0:
            escaped.append('\\"')
        else:
            escaped.append(char)
        backslashes = 0
    return "".join(escaped)


def replace_quoted_text(raw_line: str, new_text: str) -> str:
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise ValueError("line does not contain a quoted localization value")
    return raw_line[: first_quote + 1] + escape_localization_value(new_text) + raw_line[last_quote:]


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


def load_candidates(conn, include_safe_pending: bool):
    confirmed_rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_line_number,
            s.source_key,
            s.spanish_text,
            s.old_text,
            o.output_line_number,
            sc.confirmed_text AS suggested_text,
            sc.confirmation_level AS status,
            sc.confirmation_level AS decision,
            sc.locked,
            NULL AS corrected_text,
            sc.confirmation_label AS reason
        FROM segment_confirmations sc
        JOIN source_segments s ON s.id = sc.segment_id
        JOIN output_segments o ON o.segment_id = s.id
        WHERE s.is_active = 1
        ORDER BY sc.locked DESC, sc.updated_at DESC, s.relative_path, o.output_line_number
        """
    ).fetchall()

    if include_safe_pending:
        rows = conn.execute(
            """
            SELECT
                s.id AS segment_id,
                s.relative_path,
                s.source_line_number,
                s.source_key,
                s.spanish_text,
                s.old_text,
                o.output_line_number,
                ts.suggested_text,
                ts.status,
                f.decision,
                0 AS locked,
                f.corrected_text,
                f.reason
            FROM translation_suggestions ts
            JOIN source_segments s ON s.id = ts.segment_id
            JOIN output_segments o ON o.segment_id = s.id
            LEFT JOIN suggestion_feedback f
                ON f.suggestion_id = ts.id
               AND (
                   f.decision IN ('accepted', 'edited', 'accepted_old')
               )
            WHERE (ts.status = 'safe' OR f.decision = 'accepted_old')
              AND s.is_active = 1
              AND (
                  f.decision IN ('accepted', 'edited', 'accepted_old')
                  OR NOT EXISTS (
                      SELECT 1
                      FROM suggestion_feedback fx
                      WHERE fx.suggestion_id = ts.id
                        AND fx.decision IN ('rejected')
                  )
              )
            ORDER BY s.relative_path, o.output_line_number, ts.match_score DESC
            """
        ).fetchall()
        return [*confirmed_rows, *rows]

    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_line_number,
            s.source_key,
            s.spanish_text,
            s.old_text,
            o.output_line_number,
            ts.suggested_text,
            ts.status,
            f.decision,
            0 AS locked,
            f.corrected_text,
            f.reason
        FROM suggestion_feedback f
        LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
        JOIN source_segments s ON s.id = f.segment_id
        JOIN output_segments o ON o.segment_id = s.id
        WHERE (
              f.decision IN ('accepted', 'edited', 'accepted_old')
          )
          AND s.is_active = 1
        ORDER BY s.relative_path, o.output_line_number, ts.match_score DESC
        """
    ).fetchall()
    return [*confirmed_rows, *rows]


def load_bootstrap_candidates(conn, include_safe_pending: bool, only_locked_human: bool = False):
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_line_number,
            s.source_key,
            s.spanish_text,
            s.old_text,
            o.output_line_number
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        WHERE s.is_active = 1
          AND s.old_text IS NOT NULL
        ORDER BY s.relative_path, o.output_line_number
        """
    ).fetchall()

    feedback_rows = conn.execute(
        """
        SELECT
            f.segment_id,
            f.decision,
            f.corrected_text,
            ts.suggested_text,
            ts.status,
            ts.match_score
        FROM suggestion_feedback f
        LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
        WHERE f.decision IN ('accepted', 'edited', 'accepted_old')
        ORDER BY f.updated_at ASC, f.id ASC
        """
    ).fetchall()
    feedback_by_segment = {} if only_locked_human else {row["segment_id"]: row for row in feedback_rows}

    safe_rows = []
    if include_safe_pending and not only_locked_human:
        safe_rows = conn.execute(
            """
            SELECT
                ts.segment_id,
                ts.suggested_text,
                ts.match_score
            FROM translation_suggestions ts
            WHERE ts.status = 'safe'
            ORDER BY ts.match_score ASC, ts.updated_at ASC, ts.id ASC
            """
        ).fetchall()
    safe_by_segment = {row["segment_id"]: row for row in safe_rows}

    confirmed_rows = conn.execute(
        """
        SELECT
            segment_id,
            confirmed_text,
            confirmation_level,
            confirmation_label,
            locked,
            updated_at
        FROM segment_confirmations
        WHERE (? = 0 OR (confirmation_level = 'human_confirmed' AND locked = 1))
        ORDER BY locked DESC, updated_at DESC, id DESC
        """
        ,
        (1 if only_locked_human else 0,),
    ).fetchall()
    confirmed_by_segment = {row["segment_id"]: row for row in confirmed_rows}

    candidates = []
    for row in rows:
        confirmed = confirmed_by_segment.get(row["segment_id"])
        feedback = feedback_by_segment.get(row["segment_id"])
        safe = safe_by_segment.get(row["segment_id"])
        suggested_text = row["old_text"]
        decision = "bootstrap_old"

        if confirmed:
            suggested_text = confirmed["confirmed_text"]
            decision = confirmed["confirmation_level"]
        elif feedback:
            if feedback["decision"] == "edited":
                suggested_text = feedback["corrected_text"]
                decision = "edited"
            elif feedback["decision"] == "accepted":
                suggested_text = feedback["suggested_text"]
                decision = "accepted"
            elif feedback["decision"] == "accepted_old":
                suggested_text = row["old_text"]
                decision = "accepted_old"
        elif safe:
            suggested_text = safe["suggested_text"]
            decision = "safe_pending"

        candidates.append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "spanish_text": row["spanish_text"],
                "old_text": row["old_text"],
                "output_line_number": row["output_line_number"],
                "suggested_text": suggested_text,
                "decision": decision,
                "locked": int(confirmed["locked"] or 0) if confirmed else 0,
                "corrected_text": None,
                "reason": None,
            }
        )

    return candidates


def apply_updates(
    include_safe_pending: bool = False,
    create_backup: bool = True,
    bootstrap_old: bool = False,
    only_locked_human: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    output_root = db.project_path(settings["output_spanish"])
    backup_root = db.project_path("memory/backups") / started_at.strftime("output_%Y%m%d_%H%M%S")

    print("[apply_safe_output_updates] Starting output rewrite")
    print(f"[apply_safe_output_updates] Rule version: {RULE_VERSION}")
    print(f"[apply_safe_output_updates] Output root: {output_root}")
    print(f"[apply_safe_output_updates] Include safe pending: {include_safe_pending}")
    print(f"[apply_safe_output_updates] Create backup: {create_backup}")
    print(f"[apply_safe_output_updates] Bootstrap old: {bootstrap_old}")
    print(f"[apply_safe_output_updates] Only locked human: {only_locked_human}")

    applied = 0
    skipped = 0
    skip_reasons: Counter = Counter()
    updates_by_file: dict[str, dict[int, str]] = defaultdict(dict)
    candidate_count = 0

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if bootstrap_old:
            candidates = load_bootstrap_candidates(conn, include_safe_pending, only_locked_human)
        else:
            candidates = load_candidates(conn, include_safe_pending)
        candidate_count = len(candidates)
        print(f"[apply_safe_output_updates] Candidates: {candidate_count}")

        seen_segments: set[int] = set()
        for row in candidates:
            segment_id = row["segment_id"]
            if segment_id in seen_segments:
                skipped += 1
                skip_reasons["duplicate_segment_candidate"] += 1
                continue
            seen_segments.add(segment_id)

            if row["decision"] == "edited":
                suggested_text = row["corrected_text"]
            elif row["decision"] == "accepted_old":
                suggested_text = row["old_text"]
            else:
                suggested_text = row["suggested_text"]
            if suggested_text is None or suggested_text.strip() == "":
                skipped += 1
                skip_reasons["empty_suggestion"] += 1
                continue
            if row["output_line_number"] is None:
                skipped += 1
                skip_reasons["missing_output_line"] += 1
                continue
            token_mismatch = protected_tokens(row["spanish_text"]) != protected_tokens(suggested_text)
            human_locked_override = row["decision"] == "human_confirmed" and int(row["locked"] or 0) == 1
            if token_mismatch and not human_locked_override:
                skipped += 1
                skip_reasons["token_mismatch"] += 1
                continue
            if token_mismatch and human_locked_override:
                skip_reasons["token_mismatch_human_locked_override"] += 1
            updates_by_file[row["relative_path"]][row["output_line_number"]] = suggested_text

    for relative_path, line_updates in updates_by_file.items():
        output_path = output_root / Path(relative_path)
        if not output_path.exists():
            skipped += len(line_updates)
            skip_reasons["missing_output_file"] += len(line_updates)
            continue

        if create_backup:
            make_backup(output_root, backup_root, relative_path)

        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for line_number, suggested_text in sorted(line_updates.items()):
            index = line_number - 1
            if index < 0 or index >= len(lines):
                skipped += 1
                skip_reasons["line_out_of_range"] += 1
                continue
            try:
                lines[index] = replace_quoted_text(lines[index], suggested_text)
                applied += 1
            except ValueError:
                skipped += 1
                skip_reasons["line_without_quoted_value"] += 1

        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        print(f"[apply_safe_output_updates] Updated {relative_path}: {len(line_updates)} candidate line(s)")

    elapsed = datetime.now() - started_at
    report_lines = [
        "Apply safe output updates report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Include safe pending: {include_safe_pending}",
        f"Create backup: {create_backup}",
        f"Bootstrap old: {bootstrap_old}",
        f"Only locked human: {only_locked_human}",
        f"Backup root: {backup_root if create_backup else 'disabled'}",
        "",
        "Summary:",
        f"- Candidates: {candidate_count}",
        f"- Applied updates: {applied}",
        f"- Skipped updates: {skipped}",
        f"- Files touched: {len(updates_by_file)}",
        "",
        "Skip reasons:",
    ]
    for reason, count in skip_reasons.most_common():
        report_lines.append(f"- {reason}: {count}")

    report_path = db.write_report(settings, "apply_safe_output_updates", report_lines)
    print(f"[apply_safe_output_updates] Applied updates: {applied}")
    print(f"[apply_safe_output_updates] Skipped updates: {skipped}")
    print(f"[apply_safe_output_updates] Report: {report_path}")
    print("[apply_safe_output_updates] Done")


def main(
    include_safe_pending: bool | None = None,
    create_backup: bool | None = None,
    bootstrap_old: bool | None = None,
    only_locked_human: bool | None = None,
) -> None:
    if include_safe_pending is None or create_backup is None or bootstrap_old is None or only_locked_human is None:
        parser = argparse.ArgumentParser(description="Apply safe approved suggestions to output/spanish.")
        parser.add_argument(
            "--include-safe-pending",
            action="store_true",
            help="Also apply safe suggestions that are still pending review.",
        )
        parser.add_argument(
            "--bootstrap-old",
            action="store_true",
            help="Initial output bootstrap: write spanish_old to output, with reviewed/safe suggestions as overrides.",
        )
        parser.add_argument(
            "--only-locked-human",
            action="store_true",
            help="During bootstrap, use spanish_old as base and only apply locked human confirmations as overrides.",
        )
        parser.add_argument("--no-backup", action="store_true", help="Do not create memory/backups copy.")
        args = parser.parse_args()
        include_safe_pending = args.include_safe_pending
        create_backup = not args.no_backup
        bootstrap_old = args.bootstrap_old
        only_locked_human = args.only_locked_human

    apply_updates(
        include_safe_pending=include_safe_pending,
        create_backup=create_backup,
        bootstrap_old=bootstrap_old,
        only_locked_human=only_locked_human,
    )


if __name__ == "__main__":
    main()
