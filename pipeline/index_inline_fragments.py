from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "inline_fragments_v1"
BATCH_SIZE = 5000

COMMAND_PATTERN = re.compile(r"\[([A-Za-z_][A-Za-z0-9_.]*)\((.*?)\)(?:\|[A-Za-z0-9_]+)?\]")
QUOTED_ARG_PATTERN = re.compile(r"'((?:\\'|[^'])*)'|\"((?:\\\"|[^\"])*)\"")
WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff#]+", re.UNICODE)

TEXTUAL_COMMANDS = {
    "Concept",
    "Select_CString",
    "SelectLocalization",
}

TEXTUAL_SUFFIXES = (
    "String",
    "PlayerString",
    "LocalPlayerString",
    "GetString",
)

SPANISH_HINTS = re.compile(
    r"[\u00f1\u00bf\u00a1]|\b("
    r"tu|su|robaste|rob[o\u00f3]|descartaste|descart[o\u00f3]|escapaste|escap[o\u00f3]|"
    r"dejaste|dej[o\u00f3]|salvaste|salv[o\u00f3]|vendiste|vendi[o\u00f3]|asististe|asisti[o\u00f3]|"
    r"cabeza|fe|persona|hogar|comida|mascota|favorita|fiesta|progenitor|"
    r"las|los|el|la|del|por|para|con|sin"
    r")\b",
    re.IGNORECASE,
)

RESERVED_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.$|:() -]+$")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def command_is_textual(command_name: str) -> bool:
    base_name = command_name.rsplit(".", 1)[-1]
    return base_name in TEXTUAL_COMMANDS or base_name.endswith(TEXTUAL_SUFFIXES)


def unescape_arg(value: str) -> str:
    return value.replace("\\'", "'").replace('\\"', '"')


def quoted_args(command_body: str) -> list[str]:
    args: list[str] = []
    for match in QUOTED_ARG_PATTERN.finditer(command_body):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        args.append(unescape_arg(value))
    return args


def classify_fragment(command_name: str, argument_index: int, fragment: str) -> tuple[str, bool, list[dict]]:
    reasons: list[dict] = []
    base_name = command_name.rsplit(".", 1)[-1]
    text = fragment.strip()
    if not text:
        return "empty", False, [{"rule": "empty_fragment"}]

    if base_name == "Concept" and argument_index == 1:
        return "reserved_key", False, [{"rule": "concept_key"}]

    if base_name == "Concept" and argument_index >= 2:
        return "display_text", True, [{"rule": "concept_display_text"}]

    if command_is_textual(command_name):
        if SPANISH_HINTS.search(text):
            return "inline_text", True, [{"rule": "textual_command_spanish_hint"}]
        if "#" in text and len(WORD_PATTERN.findall(text)) <= 3:
            return "inline_text", True, [{"rule": "hash_suffix_display_text"}]
        if not RESERVED_KEY_PATTERN.match(text):
            return "inline_text", True, [{"rule": "textual_command_non_key_shape"}]
        if len(WORD_PATTERN.findall(text)) <= 3 and text.casefold() in {"your", "tu", "su", "your#", "tu#", "su#"}:
            return "inline_text", True, [{"rule": "possessive_short_text"}]
        return "maybe_text", True, [{"rule": "textual_command_default"}]

    if SPANISH_HINTS.search(text) and " " in text:
        return "maybe_text", True, [{"rule": "spanish_phrase_in_unknown_command"}]

    return "reserved_or_unknown", False, [{"rule": "not_textual_command"}]


def iter_fragments(row, package_name: str, text: str | None):
    if not text:
        return
    global_index = 0
    for command_match in COMMAND_PATTERN.finditer(text):
        command_name = command_match.group(1)
        body = command_match.group(2)
        for local_index, fragment in enumerate(quoted_args(body), start=1):
            global_index += 1
            role, should_translate, reasons = classify_fragment(command_name, local_index, fragment)
            yield {
                "segment_id": row["id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "package_name": package_name,
                "command_name": command_name,
                "argument_index": global_index,
                "fragment_text": fragment,
                "fragment_hash": sha256_text(fragment),
                "fragment_role": role,
                "should_translate": 1 if should_translate else 0,
                "reasons_json": json.dumps(reasons, ensure_ascii=False),
            }


def replace_fragments(conn, row) -> Counter:
    counts: Counter = Counter()
    conn.execute("DELETE FROM inline_fragments WHERE segment_id = ?", (row["id"],))
    packages = [
        ("spanish_source", row["spanish_text"]),
        ("english_source", row["english_text"]),
        ("spanish_old", row["old_text"]),
        ("output_spanish", row["portuguese_text"]),
    ]
    for package_name, text in packages:
        for fragment in iter_fragments(row, package_name, text):
            conn.execute(
                """
                INSERT OR REPLACE INTO inline_fragments (
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    package_name,
                    command_name,
                    argument_index,
                    fragment_text,
                    fragment_hash,
                    fragment_role,
                    should_translate,
                    status,
                    reasons_json,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexed', ?, ?)
                """,
                (
                    fragment["segment_id"],
                    fragment["relative_path"],
                    fragment["source_key"],
                    fragment["source_line_number"],
                    fragment["package_name"],
                    fragment["command_name"],
                    fragment["argument_index"],
                    fragment["fragment_text"],
                    fragment["fragment_hash"],
                    fragment["fragment_role"],
                    fragment["should_translate"],
                    fragment["reasons_json"],
                    db.utc_now(),
                ),
            )
            counts[f"{package_name}_fragments"] += 1
            if fragment["should_translate"]:
                counts[f"{package_name}_translatable"] += 1
    return counts


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[index_inline_fragments] Starting inline fragment indexing")
    print(f"[index_inline_fragments] Rule version: {RULE_VERSION}")
    print(f"[index_inline_fragments] Database: {db.get_database_path(settings)}")

    total_counts: Counter = Counter()
    processed = 0

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        total = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM source_segments s
            LEFT JOIN output_segments o ON o.segment_id = s.id
            WHERE s.is_active = 1
              AND (
                  s.spanish_text LIKE '%(''%'
                  OR s.english_text LIKE '%(''%'
                  OR s.old_text LIKE '%(''%'
                  OR o.portuguese_text LIKE '%(''%'
              )
            """
        ).fetchone()["total"]
        print(f"[index_inline_fragments] Candidate segments: {total}")

        offset = 0
        while True:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.relative_path,
                    s.source_line_number,
                    s.source_key,
                    s.spanish_text,
                    s.english_text,
                    s.old_text,
                    o.portuguese_text
                FROM source_segments s
                LEFT JOIN output_segments o ON o.segment_id = s.id
                WHERE s.is_active = 1
                  AND (
                      s.spanish_text LIKE '%(''%'
                      OR s.english_text LIKE '%(''%'
                      OR s.old_text LIKE '%(''%'
                      OR o.portuguese_text LIKE '%(''%'
                  )
                ORDER BY s.id
                LIMIT ? OFFSET ?
                """,
                (BATCH_SIZE, offset),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                total_counts.update(replace_fragments(conn, row))
                processed += 1

            conn.commit()
            offset += len(rows)
            print(
                "[index_inline_fragments] "
                f"{processed}/{total} candidate segments processed "
                f"({processed / total:.1%})"
            )

    elapsed = datetime.now() - started_at
    report_lines = [
        "Inline fragments indexing report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Candidate segments processed: {processed}",
        "",
        "Counts:",
    ]
    for key, count in total_counts.most_common():
        report_lines.append(f"- {key}: {count}")

    report_path = db.write_report(settings, "index_inline_fragments", report_lines)
    print(f"[index_inline_fragments] Candidate segments processed: {processed}")
    for key, count in total_counts.most_common():
        print(f"[index_inline_fragments] {key}: {count}")
    print(f"[index_inline_fragments] Report: {report_path}")
    print("[index_inline_fragments] Done")


if __name__ == "__main__":
    main()
