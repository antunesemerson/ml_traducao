from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import db


TOKEN_PATTERNS = [
    ("dollar_variable", re.compile(r"\$[^$\s]+\$")),
    ("bracket_command", re.compile(r"\[[^\]]+\]")),
    ("format_tag", re.compile(r"#[A-Za-z0-9_]+|#!")),
    ("icon", re.compile(r"@[A-Za-z0-9_]+!")),
    ("escaped_newline", re.compile(r"\\n")),
]


@dataclass
class Segment:
    relative_path: str
    line_number: int
    key: str
    version_index: str | None
    text: str
    raw_line: str


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines()


def parse_localization_line(line: str, line_number: int, relative_path: str) -> Segment | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("l_"):
        return None

    first_quote = line.find('"')
    if first_quote < 0:
        return None

    colon = line.find(":")
    if colon < 0 or colon > first_quote:
        return None

    key = line[:colon].strip()
    if not key:
        return None

    meta = line[colon + 1:first_quote].strip()
    version_index = meta if meta else None

    last_quote = line.rfind('"')
    if last_quote <= first_quote:
        text = ""
    else:
        text = line[first_quote + 1:last_quote]

    return Segment(
        relative_path=relative_path,
        line_number=line_number,
        key=key,
        version_index=version_index,
        text=text,
        raw_line=line,
    )


def parse_file(path: Path, root: Path) -> tuple[list[Segment], list[str], int, str]:
    lines = read_text_lines(path)
    relative_path = path.relative_to(root).as_posix()
    segments: list[Segment] = []
    parse_warnings: list[str] = []

    for index, line in enumerate(lines, start=1):
        segment = parse_localization_line(line, index, relative_path)
        if segment:
            segments.append(segment)
        elif line.strip() and not line.strip().startswith("#") and not line.strip().startswith("l_"):
            if '"' in line or ":" in line:
                parse_warnings.append(f"{relative_path}:{index}: skipped non-standard line")

    return segments, parse_warnings, len(lines), file_hash(path)


def to_english_relative_path(spanish_relative_path: str) -> str:
    return spanish_relative_path.replace("_l_spanish.yml", "_l_english.yml")


def segments_by_key(segments: Iterable[Segment]) -> dict[str, Segment]:
    mapped: dict[str, Segment] = {}
    for segment in segments:
        mapped.setdefault(segment.key, segment)
    return mapped


def upsert_file(conn, package_name: str, relative_path: str, absolute_path: Path, line_count: int, digest: str) -> None:
    conn.execute(
        """
        INSERT INTO files (
            package_name, relative_path, absolute_path, line_count, file_hash, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(package_name, relative_path) DO UPDATE SET
            absolute_path = excluded.absolute_path,
            line_count = excluded.line_count,
            file_hash = excluded.file_hash,
            indexed_at = excluded.indexed_at
        """,
        (package_name, relative_path, str(absolute_path), line_count, digest, db.utc_now()),
    )


def upsert_source_segment(conn, source: Segment, english: Segment | None, old: Segment | None) -> int:
    now = db.utc_now()
    exact = conn.execute(
        """
        SELECT id
        FROM source_segments
        WHERE relative_path = ?
          AND source_line_number = ?
          AND source_key = ?
        """,
        (source.relative_path, source.line_number, source.key),
    ).fetchone()
    if exact is None:
        # A game update can insert lines without changing a localization key. In
        # that case the segment is still the same historical entity: move its
        # line anchor before the regular upsert instead of allocating a new id.
        # Duplicate keys are deliberately left untouched because their identity
        # is ambiguous and the exact composite key remains the safer contract.
        key_matches = conn.execute(
            """
            SELECT id
            FROM source_segments
            WHERE relative_path = ? AND source_key = ?
            ORDER BY is_active DESC, last_indexed_at DESC, id DESC
            LIMIT 2
            """,
            (source.relative_path, source.key),
        ).fetchall()
        if len(key_matches) == 1:
            conn.execute(
                "UPDATE source_segments SET source_line_number = ? WHERE id = ?",
                (source.line_number, int(key_matches[0]["id"])),
            )
    conn.execute(
        """
        INSERT INTO source_segments (
            relative_path,
            source_line_number,
            source_key,
            version_index,
            spanish_text,
            english_text,
            old_text,
            spanish_raw_line,
            english_raw_line,
            old_raw_line,
            spanish_hash,
            english_hash,
            old_hash,
            has_english,
            has_old,
            is_active,
            first_indexed_at,
            last_indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(relative_path, source_line_number, source_key) DO UPDATE SET
            version_index = excluded.version_index,
            spanish_text = excluded.spanish_text,
            english_text = excluded.english_text,
            old_text = excluded.old_text,
            spanish_raw_line = excluded.spanish_raw_line,
            english_raw_line = excluded.english_raw_line,
            old_raw_line = excluded.old_raw_line,
            spanish_hash = excluded.spanish_hash,
            english_hash = excluded.english_hash,
            old_hash = excluded.old_hash,
            has_english = excluded.has_english,
            has_old = excluded.has_old,
            is_active = 1,
            last_indexed_at = excluded.last_indexed_at
        """,
        (
            source.relative_path,
            source.line_number,
            source.key,
            source.version_index,
            source.text,
            english.text if english else None,
            old.text if old else None,
            source.raw_line,
            english.raw_line if english else None,
            old.raw_line if old else None,
            sha256_text(source.text),
            sha256_text(english.text if english else None),
            sha256_text(old.text if old else None),
            1 if english else 0,
            1 if old else 0,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT id
        FROM source_segments
        WHERE relative_path = ?
          AND source_line_number = ?
          AND source_key = ?
        """,
        (source.relative_path, source.line_number, source.key),
    ).fetchone()
    return int(row["id"])


def upsert_output_segment(conn, segment_id: int, source_relative_path: str, output: Segment | None) -> None:
    conn.execute(
        """
        INSERT INTO output_segments (
            segment_id,
            relative_path,
            output_line_number,
            portuguese_text,
            output_raw_line,
            portuguese_hash,
            last_indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            relative_path = excluded.relative_path,
            output_line_number = excluded.output_line_number,
            portuguese_text = excluded.portuguese_text,
            output_raw_line = excluded.output_raw_line,
            portuguese_hash = excluded.portuguese_hash,
            last_indexed_at = excluded.last_indexed_at
        """,
        (
            segment_id,
            source_relative_path,
            output.line_number if output else None,
            output.text if output else None,
            output.raw_line if output else None,
            sha256_text(output.text if output else None),
            db.utc_now(),
        ),
    )


def replace_tokens(conn, segment_id: int, package_name: str, text: str | None) -> int:
    conn.execute(
        "DELETE FROM protected_tokens WHERE segment_id = ? AND package_name = ?",
        (segment_id, package_name),
    )
    if not text:
        return 0

    tokens: list[tuple[int, str, str]] = []
    for token_type, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            tokens.append((match.start(), token_type, match.group(0)))
    tokens.sort(key=lambda item: item[0])

    for order, (_, token_type, token) in enumerate(tokens, start=1):
        conn.execute(
            """
            INSERT INTO protected_tokens (segment_id, package_name, token, token_type, token_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (segment_id, package_name, token, token_type, order),
        )
    return len(tokens)


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    spanish_root = db.project_path(settings["spanish_source"])
    english_root = db.project_path(settings["english_source"])
    old_root = db.project_path(settings["spanish_traduzido_old"])
    output_root = db.project_path(settings["output_spanish"])

    print("[index_source] Starting source indexing")
    print(f"[index_source] Spanish source: {spanish_root}")
    print(f"[index_source] English source: {english_root}")
    print(f"[index_source] Old translation: {old_root}")
    print(f"[index_source] Output: {output_root}")

    spanish_files = sorted(spanish_root.rglob("*.yml"))
    total_files = len(spanish_files)
    processed_files = 0
    inserted_segments = 0
    missing_english_files = 0
    missing_old_files = 0
    missing_output_files = 0
    missing_english_keys = 0
    missing_old_keys = 0
    parse_warnings: list[str] = []
    token_count = 0
    indexed_manifest_paths: dict[str, set[str]] = {
        "spanish_source": set(),
        "english_source": set(),
        "spanish_old": set(),
        "output_spanish": set(),
    }

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        conn.execute("UPDATE source_segments SET is_active = 0")
        # Rebuild the manifest atomically with the source index. Keeping rows for
        # files that were removed made every later preflight look stale forever,
        # even after a successful reindex.
        conn.execute(
            """
            DELETE FROM files
            WHERE package_name IN (
                'spanish_source', 'english_source', 'spanish_old', 'output_spanish'
            )
            """
        )

        for file_index, spanish_path in enumerate(spanish_files, start=1):
            relative_path = spanish_path.relative_to(spanish_root).as_posix()
            english_relative = to_english_relative_path(relative_path)
            english_path = english_root / Path(english_relative)
            old_path = old_root / Path(relative_path)
            output_path = output_root / Path(relative_path)

            source_segments, warnings, line_count, digest = parse_file(spanish_path, spanish_root)
            parse_warnings.extend(warnings)
            upsert_file(conn, "spanish_source", relative_path, spanish_path, line_count, digest)
            indexed_manifest_paths["spanish_source"].add(relative_path)

            english_segments: list[Segment] = []
            if english_path.exists():
                english_segments, warnings, line_count, digest = parse_file(english_path, english_root)
                parse_warnings.extend(warnings)
                upsert_file(conn, "english_source", english_relative, english_path, line_count, digest)
                indexed_manifest_paths["english_source"].add(english_relative)
            else:
                missing_english_files += 1

            old_segments: list[Segment] = []
            if old_path.exists():
                old_segments, warnings, line_count, digest = parse_file(old_path, old_root)
                parse_warnings.extend(warnings)
                upsert_file(conn, "spanish_old", relative_path, old_path, line_count, digest)
                indexed_manifest_paths["spanish_old"].add(relative_path)
            else:
                missing_old_files += 1

            output_segments: list[Segment] = []
            if output_path.exists():
                output_segments, warnings, line_count, digest = parse_file(output_path, output_root)
                parse_warnings.extend(warnings)
                upsert_file(conn, "output_spanish", relative_path, output_path, line_count, digest)
                indexed_manifest_paths["output_spanish"].add(relative_path)
            else:
                missing_output_files += 1

            english_by_key = segments_by_key(english_segments)
            old_by_key = segments_by_key(old_segments)
            output_by_key = segments_by_key(output_segments)

            for source in source_segments:
                english = english_by_key.get(source.key)
                old = old_by_key.get(source.key)
                output = output_by_key.get(source.key)

                if not english:
                    missing_english_keys += 1
                if not old:
                    missing_old_keys += 1

                segment_id = upsert_source_segment(conn, source, english, old)
                upsert_output_segment(conn, segment_id, relative_path, output)
                token_count += replace_tokens(conn, segment_id, "spanish_source", source.text)
                token_count += replace_tokens(conn, segment_id, "english_source", english.text if english else None)
                token_count += replace_tokens(conn, segment_id, "spanish_old", old.text if old else None)
                token_count += replace_tokens(conn, segment_id, "output_spanish", output.text if output else None)
                inserted_segments += 1

            processed_files += 1
            if file_index == 1 or file_index % 25 == 0 or file_index == total_files:
                print(
                    "[index_source] "
                    f"{file_index}/{total_files} files, "
                    f"{inserted_segments} source segments indexed"
                )
            conn.commit()

        # Keep the file manifest exact even when a package contains an orphan
        # file with no Spanish counterpart. These rows are relevant to the
        # change detector although they do not create active source segments.
        for package_name, package_root in (
            ("spanish_source", spanish_root),
            ("english_source", english_root),
            ("spanish_old", old_root),
            ("output_spanish", output_root),
        ):
            for package_path in sorted(package_root.rglob("*.yml")):
                if not package_path.is_file():
                    continue
                package_relative = package_path.relative_to(package_root).as_posix()
                if package_relative in indexed_manifest_paths[package_name]:
                    continue
                line_count = len(read_text_lines(package_path))
                upsert_file(
                    conn,
                    package_name,
                    package_relative,
                    package_path,
                    line_count,
                    file_hash(package_path),
                )
                indexed_manifest_paths[package_name].add(package_relative)
        conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Source indexing report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        "",
        "Inputs:",
        f"- Spanish source: {spanish_root}",
        f"- English source: {english_root}",
        f"- Old translation: {old_root}",
        f"- Output: {output_root}",
        "",
        "Summary:",
        f"- Spanish files processed: {processed_files}",
        f"- Source segments indexed: {inserted_segments}",
        f"- Protected tokens indexed: {token_count}",
        f"- Missing English files: {missing_english_files}",
        f"- Missing old translation files: {missing_old_files}",
        f"- Missing output files: {missing_output_files}",
        f"- Missing English keys: {missing_english_keys}",
        f"- Missing old translation keys: {missing_old_keys}",
        f"- Parse warnings: {len(parse_warnings)}",
    ]
    if parse_warnings:
        report_lines.extend(["", "Parse warnings sample:"])
        report_lines.extend(f"- {warning}" for warning in parse_warnings[:200])

    report_path = db.write_report(settings, "index_source", report_lines)
    print(f"[index_source] Files processed: {processed_files}")
    print(f"[index_source] Source segments indexed: {inserted_segments}")
    print(f"[index_source] Protected tokens indexed: {token_count}")
    print(f"[index_source] Parse warnings: {len(parse_warnings)}")
    print(f"[index_source] Report: {report_path}")
    print("[index_source] Done")


if __name__ == "__main__":
    main()
