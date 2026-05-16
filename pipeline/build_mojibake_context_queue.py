from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "mojibake_context_queue_v1"

FRAGMENT_RE = re.compile(r"\b[\wÀ-ÿ]*\?[\wÀ-ÿ]+\b|\?[\wÀ-ÿ]+|\?{2,}")
NAME_HINT_RE = re.compile(r"^[A-ZÀ-Ý][\wÀ-ÿ?'\-]+$")
TOKEN_HINT_RE = re.compile(r"\[[^\]]*\?|\?[^\[]*\]|[$][^$]*\?|#[^#]*\?")
BROKEN_HINT_RE = re.compile(r"\?{2,}|^\?[a-zA-ZÀ-ÿ]{0,2}$|^[a-zA-ZÀ-ÿ]{0,2}\?$")


def extract_fragments(text: str) -> Counter[str]:
    fragments: Counter[str] = Counter()
    for match in FRAGMENT_RE.findall(text or ""):
        fragments[match] += 1
    return fragments


def filter_intentional_fragments(fragments: Counter[str], english_text: str | None) -> Counter[str]:
    english = english_text or ""
    filtered: Counter[str] = Counter()
    for fragment, count in fragments.items():
        if set(fragment) == {"?"} and english.count(fragment) >= count:
            continue
        filtered[fragment] = count
    return filtered


def classify_fragment(fragment: str) -> str:
    if BROKEN_HINT_RE.search(fragment):
        return "broken_fragment"
    if TOKEN_HINT_RE.search(fragment):
        return "token_or_markup"
    if NAME_HINT_RE.search(fragment):
        return "name_or_transliteration"
    if len(fragment) <= 4:
        return "short_ambiguous"
    return "word_context"


def classify_row(row: dict, fragments: Counter[str]) -> tuple[str, float, list[str]]:
    kinds = Counter(classify_fragment(fragment) for fragment in fragments)
    reasons: list[str] = []
    text = row["confirmed_text"] or ""
    path = row["relative_path"] or ""
    source = row["confirmation_source"] or ""

    if kinds["broken_fragment"]:
        kind = "broken_fragment"
    elif kinds["token_or_markup"]:
        kind = "token_or_markup"
    elif kinds["name_or_transliteration"] and not kinds["word_context"]:
        kind = "name_or_transliteration"
    elif kinds["short_ambiguous"] and not kinds["word_context"]:
        kind = "short_ambiguous"
    else:
        kind = "word_context"

    priority = 0.0
    priority += min(sum(fragments.values()) * 30, 180)
    priority += max(0, 120 - min(len(text), 1200) / 10)

    if kind == "word_context":
        priority += 250
    elif kind == "broken_fragment":
        priority += 220
    elif kind == "token_or_markup":
        priority += 200
    elif kind == "short_ambiguous":
        priority += 120
    elif kind == "name_or_transliteration":
        priority += 60

    if path.startswith("gui/") or "window" in path or "interface" in path:
        priority += 100
        reasons.append("ui_path")
    if "manual" in source or row["confirmation_level"] == "human_confirmed":
        priority += 30
        reasons.append("human_confirmed")
    if row["locked"]:
        reasons.append("locked")

    reasons.append(f"kind:{kind}")
    reasons.extend(f"fragment:{fragment}:{count}" for fragment, count in fragments.most_common(8))
    return kind, priority, reasons


def build_queue(conn) -> Counter[str]:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM mojibake_context_queue")
    rows = conn.execute(
        """
        SELECT
            sc.segment_id,
            ss.relative_path,
            ss.source_key,
            ss.source_line_number,
            ss.english_text,
            ss.spanish_text,
            ss.old_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.locked
        FROM segment_confirmations sc
        JOIN source_segments ss ON ss.id = sc.segment_id
        WHERE ss.is_active = 1
          AND sc.confirmed_text LIKE '%?%'
        ORDER BY ss.relative_path, ss.source_line_number, ss.id
        """
    ).fetchall()

    counts: Counter[str] = Counter()
    for row in rows:
        data = dict(row)
        fragments = extract_fragments(data["confirmed_text"] or "")
        fragments = filter_intentional_fragments(fragments, data["english_text"])
        if not fragments:
            continue
        kind, priority, reasons = classify_row(data, fragments)
        counts[kind] += 1
        conn.execute(
            """
            INSERT INTO mojibake_context_queue (
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                fragment_summary,
                fragment_count,
                residue_kind,
                priority_score,
                text_length,
                english_text,
                spanish_text,
                old_text,
                confirmed_text,
                confirmation_level,
                confirmation_source,
                locked,
                status,
                notes,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                data["segment_id"],
                data["relative_path"],
                data["source_key"],
                data["source_line_number"],
                json.dumps(fragments.most_common(), ensure_ascii=False),
                sum(fragments.values()),
                kind,
                priority,
                len(data["confirmed_text"] or ""),
                data["english_text"],
                data["spanish_text"],
                data["old_text"],
                data["confirmed_text"],
                data["confirmation_level"],
                data["confirmation_source"],
                data["locked"],
                json.dumps(reasons, ensure_ascii=False),
                now,
                now,
            ),
        )
    conn.commit()
    return counts


def fetch_preview(conn, limit: int, kind: str | None) -> list[dict]:
    params: list[object] = []
    where = "status = 'open'"
    if kind:
        where += " AND residue_kind = ?"
        params.append(kind)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM mojibake_context_queue
        WHERE {where}
        ORDER BY priority_score DESC, fragment_count DESC, text_length ASC, segment_id
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_top_fragments(conn, limit: int) -> Counter[str]:
    rows = conn.execute(
        """
        SELECT fragment_summary
        FROM mojibake_context_queue
        WHERE status = 'open'
        """
    ).fetchall()
    counter: Counter[str] = Counter()
    for row in rows:
        try:
            fragments = json.loads(row["fragment_summary"] or "[]")
        except json.JSONDecodeError:
            fragments = []
        for fragment, count in fragments:
            counter[str(fragment)] += int(count)
    return Counter(dict(counter.most_common(limit)))


def clip(value: str | None, limit: int = 360) -> str:
    text = (value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def main(limit: int | None = None, kind: str | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build contextual review queue for confirmed text '?' residues.")
    parser.add_argument("--limit", type=int, default=limit or 80, help="Preview limit.")
    parser.add_argument("--kind", default=kind, help="Filter preview by residue kind.")
    args = parser.parse_args() if limit is None and kind is None else parser.parse_args([])

    settings = db.load_settings()
    started_at = datetime.now()
    print("[build_mojibake_context_queue] Starting contextual mojibake queue")
    print(f"[build_mojibake_context_queue] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        counts = build_queue(conn)
        preview = fetch_preview(conn, args.limit, args.kind)
        top_fragments = fetch_top_fragments(conn, 80)
        total = int(conn.execute("SELECT COUNT(*) FROM mojibake_context_queue WHERE status = 'open'").fetchone()[0])

    elapsed = datetime.now() - started_at
    lines = [
        "Mojibake contextual queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Open rows: {total}",
        *[f"- {kind_name}: {total_kind}" for kind_name, total_kind in counts.most_common()],
        "",
        "Top fragments:",
        *[f"- {fragment}: {count}" for fragment, count in top_fragments.most_common()],
        "",
        "Preview:",
    ]
    for row in preview:
        lines.append(
            f"- segment {row['segment_id']} | {row['residue_kind']} | score={row['priority_score']:.1f} | "
            f"{row['relative_path']}::{row['source_key']}:{row['source_line_number']}"
        )
        lines.append(f"  fragments: {row['fragment_summary']}")
        lines.append(f"  english:   {clip(row['english_text'])}")
        lines.append(f"  spanish:   {clip(row['spanish_text'])}")
        lines.append(f"  confirmed: {clip(row['confirmed_text'])}")

    report_path = db.write_report(settings, "build_mojibake_context_queue", lines)
    log_path = db.write_log(settings, "build_mojibake_context_queue", lines)
    print(f"[build_mojibake_context_queue] Open rows: {total}")
    for kind_name, total_kind in counts.most_common():
        print(f"[build_mojibake_context_queue] {kind_name}: {total_kind}")
    print(f"[build_mojibake_context_queue] Report: {report_path}")
    print(f"[build_mojibake_context_queue] Log: {log_path}")
    print("[build_mojibake_context_queue] Done")


if __name__ == "__main__":
    main()
